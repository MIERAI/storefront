// worker: the storefront's background worker (the third service of this monorepo). Deployed with a Helm chart
// (values per environment) instead of kustomize: the platform takes both.
//
// It processes the orders web accepted (internal/orders): claims the oldest pending one no other worker holds,
// works on it (WORK_TIME, simulated), marks it done. Several replicas share the queue (FOR UPDATE SKIP LOCKED).
//
//	GET /healthz → ok (header X-App-Version)
//	GET /stats   → {"version": "…", "processed": n}
//	:9090 /metrics (internal/obs):
//	  orders_processed_total                 orders done by this pod
//	  order_processing_delay_seconds         histogram: created → done (time in the queue + the work)
//	  worker_errors_total                    failed attempts (database unreachable, …): the order stays pending
//	traces (internal/obs Tracing, OTEL_* environment): "process order" per order (internal/orders Process) with its
//	queries; flushed on SIGTERM
package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/MIERAI/storefront/internal/obs"
	"github.com/MIERAI/storefront/internal/orders"
)

// DelayBuckets: created → processed, up to the completeness deadline (5m) and beyond.
var DelayBuckets = []float64{0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600}

type worker struct {
	version   string
	m         *obs.Metrics
	process   func(ctx context.Context) (orders.Order, bool, error)
	processed atomic.Int64
}

// step processes at most one order; false: nothing was done (idle or failed), wait before the next.
func (w *worker) step(ctx context.Context) bool {
	o, ok, err := w.process(ctx)
	if err != nil {
		if ctx.Err() == nil {
			w.m.Inc("worker_errors_total")
			slog.Warn("process order", "err", err)
		}
		return false
	}
	if !ok {
		return false
	}
	w.processed.Add(1)
	w.m.Inc("orders_processed_total")
	w.m.ObserveIn("order_processing_delay_seconds", DelayBuckets, o.Delay.Seconds())
	return true
}

func (w *worker) run(ctx context.Context, poll time.Duration) {
	for ctx.Err() == nil {
		if !w.step(ctx) {
			select {
			case <-ctx.Done():
			case <-time.After(poll):
			}
		}
	}
}

func (w *worker) mux() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(rw http.ResponseWriter, _ *http.Request) {
		rw.Header().Set("X-App-Version", w.version)
		rw.Write([]byte("ok"))
	})
	mux.HandleFunc("GET /stats", func(rw http.ResponseWriter, _ *http.Request) {
		rw.Header().Set("Content-Type", "application/json")
		json.NewEncoder(rw).Encode(map[string]any{"version": w.version, "processed": w.processed.Load()})
	})
	return mux
}

func duration(env string, def time.Duration) time.Duration {
	if d, err := time.ParseDuration(os.Getenv(env)); err == nil {
		return d
	}
	return def
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer stop()
	defer obs.Tracing(context.Background(), "worker", os.Getenv("APP_VERSION"))(context.Background()) // flush on stop
	m := obs.New()
	m.Add("orders_processed_total", 0)
	m.Add("worker_errors_total", 0)
	m.Serve(":9090")
	w := &worker{version: os.Getenv("APP_VERSION"), m: m}
	db, err := orders.Open(ctx, os.Getenv("DATABASE_URL"))
	if err != nil {
		slog.Error("DATABASE_URL", "err", err)
		os.Exit(1)
	}
	work, txTimeout := duration("WORK_TIME", 100*time.Millisecond), duration("TX_TIMEOUT", time.Minute)
	w.process = func(ctx context.Context) (orders.Order, bool, error) {
		ctx, cancel := context.WithTimeout(ctx, txTimeout)
		defer cancel()
		return orders.Process(ctx, db, func(ctx context.Context, _ orders.Order) error {
			select { // the work (simulated); a stop rolls the order back to pending
			case <-time.After(work):
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		})
	}
	go func() {
		for err := orders.Migrate(ctx, db); err != nil && ctx.Err() == nil; err = orders.Migrate(ctx, db) {
			slog.Warn("migrate (retrying)", "err", err)
			time.Sleep(5 * time.Second)
		}
		w.run(ctx, duration("POLL_INTERVAL", time.Second))
	}()
	srv := &http.Server{Addr: ":8080", Handler: w.mux(), ReadHeaderTimeout: 5 * time.Second}
	go func() {
		<-ctx.Done()
		srv.Shutdown(context.Background())
	}()
	slog.Info("worker listening", "version", w.version, "work", work)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		slog.Error("serve", "err", err)
		os.Exit(1)
	}
}
