// web: the storefront's page (one of two services of this monorepo, released and promoted on its own). It renders
// the products of the api service (API_URL) and shows both versions: which web and which api an environment runs.
//
//	GET /        → HTML page; without the api's products it is still served (200) but counted degraded: a bad
//	               event for the availability SLO (obs.Degraded)
//	POST /orders → {"amount": 1200} → 201 {"id": n}: a pending order in Postgres (DATABASE_URL), processed later
//	               by the worker; 400 for a bad amount, 503 when the order cannot be stored
//	GET /healthz → ok (header X-App-Version)
//	:9090 /metrics (internal/obs); calls to the api are counted as upstream "api"
//	traces (internal/obs Tracing, OTEL_* environment): server spans, the call to the api, the order INSERT
//
// web also watches the order pipeline from the outside (internal/orders Check, every CHECK_INTERVAL): a gauge
// exported by the worker would vanish with the last worker, exactly when it matters.
//
//	orders_created_total                     orders accepted
//	orders_processed_through_timestamp_seconds   every order created before it is processed (the oldest
//	                                         unprocessed order's creation time, or now): freshness SLI = time() - it.
//	                                         Absent while the database cannot be asked (unknown is not fresh)
//	orders_pending, orders_oldest_pending_age_seconds   backlog (dashboards)
//	orders_overdue_total                     orders not processed ORDER_DEADLINE after creation (completeness SLI:
//	                                         bad events among orders_created_total), each counted once
package main

import (
	"context"
	"encoding/json"
	"html/template"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/MIERAI/storefront/internal/obs"
	"github.com/MIERAI/storefront/internal/orders"
	"github.com/jackc/pgx/v5/pgxpool"
)

var page = template.Must(template.New("").Parse(`<!doctype html><meta charset="utf-8"><title>storefront</title>
<h1>storefront</h1><p>today: free shipping over ¥3000</p><p>web {{.Web}} · api {{or .API "unreachable"}}</p>
<ul>{{range .Products}}<li>{{.Name}} — ¥{{.Price}}</li>{{end}}</ul>`))

type products struct {
	Version  string `json:"version"`
	Products []struct {
		Name  string `json:"name"`
		Price int    `json:"price"`
	} `json:"products"`
}

// createOrder stores an order (orders.Create; nil: no database configured).
type createOrder func(ctx context.Context, amount int) (int64, error)

func newMux(version, apiURL string, client *http.Client, create createOrder, m *obs.Metrics) *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("X-App-Version", version)
		w.Write([]byte("ok"))
	})
	mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, r *http.Request) {
		var p products
		req, err := http.NewRequestWithContext(r.Context(), "GET", apiURL+"/products", nil) // r's trace continues in the api
		var resp *http.Response
		if err == nil {
			resp, err = client.Do(req)
		}
		if err == nil {
			if resp.StatusCode != http.StatusOK || json.NewDecoder(resp.Body).Decode(&p) != nil {
				p = products{}
			}
			resp.Body.Close()
		}
		if p.Products == nil {
			obs.Degraded(w)
		}
		page.Execute(w, map[string]any{"Web": version, "API": p.Version, "Products": p.Products})
	})
	mux.HandleFunc("POST /orders", func(w http.ResponseWriter, r *http.Request) {
		var o struct {
			Amount int `json:"amount"`
		}
		if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<10)).Decode(&o) != nil || o.Amount <= 0 || o.Amount > 1_000_000 {
			http.Error(w, `{"error":"amount: a positive number of yen"}`, http.StatusBadRequest)
			return
		}
		if create == nil {
			http.Error(w, `{"error":"orders are not available"}`, http.StatusServiceUnavailable)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		id, err := create(ctx, o.Amount)
		if err != nil {
			slog.Error("create order", "err", err)
			http.Error(w, `{"error":"the order could not be stored, try again"}`, http.StatusServiceUnavailable)
			return
		}
		m.Inc("orders_created_total")
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		json.NewEncoder(w).Encode(map[string]int64{"id": id})
	})
	return mux
}

// watch runs orders.Check every interval and exports what it sees.
func watch(ctx context.Context, db *pgxpool.Pool, m *obs.Metrics, interval, deadline time.Duration) {
	for {
		c, cancel := context.WithTimeout(ctx, interval)
		s, err := orders.Check(c, db, deadline)
		cancel()
		if err != nil {
			slog.Warn("check orders", "err", err)
			m.Unset("orders_pending")
			m.Unset("orders_oldest_pending_age_seconds")
			m.Unset("orders_processed_through_timestamp_seconds")
		} else {
			m.Set("orders_processed_through_timestamp_seconds", float64(s.Through.UnixMilli())/1000)
			m.Set("orders_pending", float64(s.Pending))
			m.Set("orders_oldest_pending_age_seconds", s.OldestAge.Seconds())
		}
		m.Add("orders_overdue_total", uint64(s.Overdue))
		select {
		case <-ctx.Done():
			return
		case <-time.After(interval):
		}
	}
}

func duration(env string, def time.Duration) time.Duration {
	if d, err := time.ParseDuration(os.Getenv(env)); err == nil {
		return d
	}
	return def
}

func main() {
	version, apiURL := os.Getenv("APP_VERSION"), os.Getenv("API_URL")
	obs.Tracing(context.Background(), "web", version) // no graceful stop here: the last batch (≤5s of spans) is lost on SIGTERM
	m := obs.New()
	m.Add("orders_created_total", 0)
	m.Add("orders_overdue_total", 0)
	m.Serve(":9090")
	client := m.Client("api", &http.Client{Timeout: 2 * time.Second})
	var create createOrder
	if url := os.Getenv("DATABASE_URL"); url != "" {
		db, err := orders.Open(context.Background(), url) // connects lazily: the page is served without a database
		if err != nil {
			slog.Error("DATABASE_URL", "err", err)
			os.Exit(1)
		}
		create = func(ctx context.Context, amount int) (int64, error) { return orders.Create(ctx, db, amount) }
		go func() {
			for err := orders.Migrate(context.Background(), db); err != nil; err = orders.Migrate(context.Background(), db) {
				slog.Warn("migrate (retrying)", "err", err)
				time.Sleep(5 * time.Second)
			}
			watch(context.Background(), db, m, duration("CHECK_INTERVAL", 10*time.Second), duration("ORDER_DEADLINE", 5*time.Minute))
		}()
	}
	srv := &http.Server{Addr: ":8080", Handler: m.Instrument(newMux(version, apiURL, client, create, m)), ReadHeaderTimeout: 5 * time.Second}
	slog.Info("web listening", "version", version, "api", apiURL)
	if err := srv.ListenAndServe(); err != nil {
		slog.Error("serve", "err", err)
		os.Exit(1)
	}
}
