// Package orders: the storefront's order pipeline in Postgres. web accepts an order (Create, status pending), the
// worker processes it (Process: claim with FOR UPDATE SKIP LOCKED, work, mark done), and Check measures the
// pipeline from the outside — so a pipeline without any worker left still reports its backlog.
//
// A claimed order stays locked inside the worker's transaction until it is done: a worker that dies mid-way rolls
// back and the order is pending again (never lost in a "processing" state).
package orders

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

const schema = `
SELECT pg_advisory_xact_lock(7461);  -- web and worker migrate at start: one at a time
CREATE TABLE IF NOT EXISTS orders (
  id           bigserial   PRIMARY KEY,
  amount       integer     NOT NULL CHECK (amount > 0),
  status       text        NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done')),
  created_at   timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz,
  overdue      boolean     NOT NULL DEFAULT false  -- counted once as not processed within the deadline (Check)
);
CREATE INDEX IF NOT EXISTS orders_pending ON orders (created_at) WHERE status = 'pending';
`

// Open: a pool on url (e.g. DATABASE_URL), at most 4 connections per pod. It connects lazily: no database, no error
// until it is used. Queries made inside a trace are spans (queryTracer).
func Open(ctx context.Context, url string) (*pgxpool.Pool, error) {
	cfg, err := pgxpool.ParseConfig(url)
	if err != nil {
		return nil, err
	}
	cfg.MaxConns = 4
	cfg.ConnConfig.Tracer = queryTracer{}
	return pgxpool.NewWithConfig(ctx, cfg)
}

// Migrate creates the table if needed (idempotent, safe to run from several pods at once).
func Migrate(ctx context.Context, db *pgxpool.Pool) error {
	return pgx.BeginFunc(ctx, db, func(tx pgx.Tx) error {
		_, err := tx.Exec(ctx, schema)
		return err
	})
}

// Create stores a pending order and returns its id. Times are the database's (one clock for the whole pipeline).
func Create(ctx context.Context, db *pgxpool.Pool, amount int) (int64, error) {
	var id int64
	err := db.QueryRow(ctx, `INSERT INTO orders (amount) VALUES ($1) RETURNING id`, amount).Scan(&id)
	return id, err
}

// Order as processed: Delay is created → processed (time in the queue + the work).
type Order struct {
	ID     int64
	Amount int
	Delay  time.Duration
}

// Process claims the oldest pending order no other worker holds, runs work on it inside the transaction and marks
// it done. ok = false: nothing to do. An error from work (or a crash) rolls back: the order is pending again.
//
// A claimed order is traced: the span "process order" (order.id) covers the work, the UPDATE and the commit — the
// claim itself is not part of it (a worker polling an empty queue makes no trace).
func Process(ctx context.Context, db *pgxpool.Pool, work func(context.Context, Order) error) (o Order, ok bool, err error) {
	tx, err := db.Begin(ctx)
	if err != nil {
		return o, false, err
	}
	defer tx.Rollback(ctx) // after Commit: a no-op
	err = tx.QueryRow(ctx, `SELECT id, amount FROM orders WHERE status = 'pending'
		ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED`).Scan(&o.ID, &o.Amount)
	if errors.Is(err, pgx.ErrNoRows) {
		return o, false, nil
	}
	if err != nil {
		return o, false, err
	}
	ctx, span := tracer.Start(ctx, "process order", trace.WithAttributes(attribute.Int64("order.id", o.ID)))
	defer func() {
		if err != nil {
			span.RecordError(err)
			span.SetStatus(codes.Error, err.Error())
		} else {
			span.SetAttributes(attribute.Float64("order.delay_seconds", o.Delay.Seconds()))
		}
		span.End()
	}()
	if err = work(ctx, o); err != nil {
		return o, false, err
	}
	var secs float64
	if err = tx.QueryRow(ctx, `UPDATE orders SET status = 'done', processed_at = now() WHERE id = $1
		RETURNING extract(epoch FROM processed_at - created_at)::float8`, o.ID).Scan(&secs); err != nil {
		return o, false, err
	}
	if err = tx.Commit(ctx); err != nil {
		return o, false, err
	}
	o.Delay = time.Duration(secs * float64(time.Second))
	return o, true, nil
}

// State of the pipeline, as Check sees it. Through is the freshness watermark: every order created before it is
// processed (the oldest unprocessed order's creation time, or the database's now when there is none); the
// freshness SLI is now - Through.
type State struct {
	Pending   int64         // orders not processed yet (including those a worker is working on)
	OldestAge time.Duration // age of the oldest of them (0: none)
	Through   time.Time
	Overdue   int64 // orders that crossed the deadline unprocessed since the last Check — completeness
}

// Check measures the backlog and marks every order still unprocessed deadline after its creation as overdue,
// exactly once (several pods may run Check: the UPDATE counts each order for one of them). Orders a worker holds
// right now are skipped: they are counted on a later Check if their processing fails.
func Check(ctx context.Context, db *pgxpool.Pool, deadline time.Duration) (s State, err error) {
	var secs float64
	if err = db.QueryRow(ctx, `SELECT count(*), coalesce(extract(epoch FROM now() - min(created_at)), 0)::float8,
		coalesce(min(created_at), now()) FROM orders WHERE status = 'pending'`).Scan(&s.Pending, &secs, &s.Through); err != nil {
		return s, err
	}
	s.OldestAge = time.Duration(secs * float64(time.Second))
	tag, err := db.Exec(ctx, `UPDATE orders SET overdue = true WHERE id IN (
		SELECT id FROM orders WHERE status = 'pending' AND NOT overdue AND created_at < now() - make_interval(secs => $1)
		FOR UPDATE SKIP LOCKED)`, deadline.Seconds())
	s.Overdue = tag.RowsAffected()
	return s, err
}
