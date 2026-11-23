// Package migrations: order's versioned schema migrations (goose), embedded in the server binary and run by
// `/server migrate` before the API starts (the Deployment's init container: the same image, so the schema always
// matches the code it ships with).
//
// Hand-written (storefront), not generated: the factory's packaging template has no migrations yet (api-factory
// docs/memo/schema-migrations.md, option B). The files live here and not in db/migrations because the generated
// Dockerfile copies cmd/, gen/ and internal/ only. Rules (scripts/migrations-check.sh, CI):
//   - NNNNN_snake_case.sql, versions increasing, "-- +goose Up" and "-- +goose Down" in each;
//   - append-only: a migration on main is never edited or deleted (it may have run in prod: fix forward);
//   - all migrations on an empty database == db/schema/*.sql (the factory's input).
package migrations

import (
	"context"
	"database/sql"
	"embed"
	"fmt"
	"io"
	"os"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib" // database/sql driver "pgx"
	"github.com/pressly/goose/v3"
	"github.com/pressly/goose/v3/lock"
)

//go:embed *.sql
var files embed.FS

// Main runs `migrate [up|status]` against DATABASE_URL and returns the process exit code. A Postgres advisory lock
// makes pods that start together run each migration once; each migration runs in its own transaction.
func Main(args []string, out io.Writer) int {
	cmd := "up"
	if len(args) > 0 {
		cmd = args[0]
	}
	url := os.Getenv("DATABASE_URL")
	if url == "" {
		fmt.Fprintln(out, "✗ migrate: DATABASE_URL is required")
		return 1
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	if err := Run(ctx, url, cmd, out); err != nil {
		fmt.Fprintln(out, "✗ migrate:", err)
		return 1
	}
	return 0
}

// Run applies (up) or lists (status) the migrations on the database at url.
func Run(ctx context.Context, url, cmd string, out io.Writer) error {
	db, err := sql.Open("pgx", url)
	if err != nil {
		return err
	}
	defer db.Close()
	for i := 0; ; i++ { // the database may still be starting (a new or re-created cluster)
		if err = db.PingContext(ctx); err == nil {
			break
		}
		if i == 60 {
			return fmt.Errorf("connect: %w", err)
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("connect: %w", err)
		case <-time.After(2 * time.Second):
		}
	}
	locker, err := lock.NewPostgresSessionLocker()
	if err != nil {
		return err
	}
	p, err := goose.NewProvider(goose.DialectPostgres, db, files, goose.WithSessionLocker(locker))
	if err != nil {
		return err
	}
	switch cmd {
	case "status":
		st, err := p.Status(ctx)
		if err != nil {
			return err
		}
		for _, s := range st {
			fmt.Fprintf(out, "%05d %-32s %s\n", s.Source.Version, s.Source.Path, s.State)
		}
		return nil
	case "up":
		t0 := time.Now()
		res, err := p.Up(ctx)
		for _, r := range res {
			fmt.Fprintf(out, "applied %05d %-32s %6dms\n", r.Source.Version, r.Source.Path, r.Duration.Milliseconds())
		}
		if err != nil {
			return err
		}
		v, err := p.GetDBVersion(ctx)
		if err != nil {
			return err
		}
		fmt.Fprintf(out, "✓ order schema at version %d (%d applied, %s)\n", v, len(res), time.Since(t0).Round(time.Millisecond))
		return nil
	}
	return fmt.Errorf("unknown command %q (up | status)", cmd)
}
