package orders

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// testDB: a pool on a schema of its own (tests run in parallel), in TEST_DATABASE_URL (CI: a postgres service).
func testDB(t *testing.T) *pgxpool.Pool {
	t.Helper()
	url := os.Getenv("TEST_DATABASE_URL")
	if url == "" {
		t.Skip("TEST_DATABASE_URL not set (e.g. postgres://postgres:test@localhost:5432/postgres?sslmode=disable)")
	}
	ctx := context.Background()
	name := "t_" + strings.ToLower(strings.NewReplacer("/", "_", "-", "_").Replace(t.Name()))
	admin, err := pgxpool.New(ctx, url)
	if err != nil {
		t.Fatal(err)
	}
	defer admin.Close()
	if _, err := admin.Exec(ctx, fmt.Sprintf("DROP SCHEMA IF EXISTS %s CASCADE; CREATE SCHEMA %s", name, name)); err != nil {
		t.Fatal(err)
	}
	cfg, err := pgxpool.ParseConfig(url)
	if err != nil {
		t.Fatal(err)
	}
	cfg.ConnConfig.RuntimeParams["search_path"] = name
	db, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	if err := Migrate(ctx, db); err != nil {
		t.Fatal(err)
	}
	return db
}

func TestCreateProcess(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	db := testDB(t)
	if err := Migrate(ctx, db); err != nil { // idempotent
		t.Fatal(err)
	}
	id, err := Create(ctx, db, 1200)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Create(ctx, db, 0); err == nil {
		t.Error("amount 0 accepted")
	}
	o, ok, err := Process(ctx, db, func(context.Context, Order) error { return nil })
	if err != nil || !ok || o.ID != id || o.Amount != 1200 || o.Delay < 0 {
		t.Fatalf("%+v %v %v", o, ok, err)
	}
	if _, ok, err := Process(ctx, db, func(context.Context, Order) error { return nil }); ok || err != nil {
		t.Fatalf("processed twice: %v %v", ok, err)
	}
}

func TestFailedWorkRollsBack(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	db := testDB(t)
	Create(ctx, db, 500)
	boom := errors.New("boom")
	if _, ok, err := Process(ctx, db, func(context.Context, Order) error { return boom }); ok || !errors.Is(err, boom) {
		t.Fatalf("%v %v", ok, err)
	}
	if s, err := Check(ctx, db, time.Hour); err != nil || s.Pending != 1 {
		t.Fatalf("order lost: %+v %v", s, err)
	}
}

// Workers in parallel: every order processed exactly once (SKIP LOCKED).
func TestConcurrentWorkersProcessEachOrderOnce(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	db := testDB(t)
	const n = 40
	for i := 0; i < n; i++ {
		if _, err := Create(ctx, db, 100+i); err != nil {
			t.Fatal(err)
		}
	}
	var mu sync.Mutex
	seen := map[int64]int{}
	var wg sync.WaitGroup
	for w := 0; w < 4; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				o, ok, err := Process(ctx, db, func(context.Context, Order) error { time.Sleep(time.Millisecond); return nil })
				if err != nil {
					t.Error(err)
					return
				}
				if !ok {
					return
				}
				mu.Lock()
				seen[o.ID]++
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	if len(seen) != n {
		t.Fatalf("processed %d of %d", len(seen), n)
	}
	for id, c := range seen {
		if c != 1 {
			t.Errorf("order %d processed %d times", id, c)
		}
	}
}

func TestCheckFreshnessAndOverdueOnce(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	db := testDB(t)
	if s, err := Check(ctx, db, time.Minute); err != nil || s != (State{}) {
		t.Fatalf("empty: %+v %v", s, err)
	}
	Create(ctx, db, 100)
	Create(ctx, db, 200)
	if _, err := db.Exec(ctx, `UPDATE orders SET created_at = now() - interval '10 minutes' WHERE amount = 100`); err != nil {
		t.Fatal(err)
	}
	s, err := Check(ctx, db, 5*time.Minute)
	if err != nil || s.Pending != 2 || s.OldestAge < 10*time.Minute || s.OldestAge > 11*time.Minute || s.Overdue != 1 {
		t.Fatalf("%+v %v", s, err)
	}
	if s, err := Check(ctx, db, 5*time.Minute); err != nil || s.Overdue != 0 { // counted once
		t.Fatalf("%+v %v", s, err)
	}
	// an order a worker holds right now is not counted (yet); the free one is
	if _, err := db.Exec(ctx, `UPDATE orders SET created_at = now() - interval '10 minutes' WHERE amount = 200`); err != nil {
		t.Fatal(err)
	}
	Create(ctx, db, 300)
	if _, err := db.Exec(ctx, `UPDATE orders SET created_at = now() - interval '20 minutes' WHERE amount = 300`); err != nil {
		t.Fatal(err)
	}
	held, release := make(chan struct{}), make(chan struct{})
	done := make(chan error)
	go func() {
		_, _, err := Process(ctx, db, func(_ context.Context, o Order) error {
			if o.Amount != 300 { // the oldest goes first
				t.Errorf("claimed %+v", o)
			}
			close(held)
			<-release
			return errors.New("fail: back to pending")
		})
		done <- err
	}()
	<-held
	if s, err := Check(ctx, db, 5*time.Minute); err != nil || s.Overdue != 1 || s.Pending != 3 {
		t.Fatalf("while held: %+v %v", s, err)
	}
	close(release)
	<-done
	if s, err := Check(ctx, db, 5*time.Minute); err != nil || s.Overdue != 1 {
		t.Fatalf("after the failure: %+v %v", s, err)
	}
}
