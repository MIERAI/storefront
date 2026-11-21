// api: the storefront's product API (one of two services of this monorepo, released and promoted on its own).
//
//	GET /products → [{"id": 1, "name": "…", "price": 120}] (+ "version")
//	GET /healthz  → ok (header X-App-Version)
//	:9090 /metrics (internal/obs); traces (internal/obs Tracing, OTEL_* environment): server spans
//
// FAULTS_DIR (internal/fault): products-errors = the share of /products answered 503, for the fault experiments.
package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/MIERAI/storefront/internal/fault"
	"github.com/MIERAI/storefront/internal/obs"
)

type product struct {
	ID    int    `json:"id"`
	Name  string `json:"name"`
	Price int    `json:"price"`
}

var catalog = []product{{1, "coffee beans", 1200}, {2, "drip bag", 180}, {3, "mug", 1600}}

func newMux(version string, faults *fault.Switches) *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("X-App-Version", version)
		w.Write([]byte("ok"))
	})
	mux.HandleFunc("GET /products", func(w http.ResponseWriter, _ *http.Request) {
		if faults.Fail("products") {
			http.Error(w, "injected fault (products-errors)", http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"version": version, "products": catalog})
	})
	return mux
}

func main() {
	version := os.Getenv("APP_VERSION")
	obs.Tracing(context.Background(), "api", version) // no graceful stop here: the last batch (≤5s of spans) is lost on SIGTERM
	m := obs.New()
	m.Serve(":9090")
	srv := &http.Server{Addr: ":8080", Handler: m.Instrument(newMux(version, fault.New(os.Getenv("FAULTS_DIR"), 2*time.Second))), ReadHeaderTimeout: 5 * time.Second}
	slog.Info("api listening", "version", version)
	if err := srv.ListenAndServe(); err != nil {
		slog.Error("serve", "err", err)
		os.Exit(1)
	}
}
