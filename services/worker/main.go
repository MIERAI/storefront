// worker: the storefront's background worker (the third service of this monorepo). Deployed with a Helm chart
// (values per environment) instead of kustomize: the platform takes both.
//
//	GET /healthz → ok (header X-App-Version)
//	GET /stats   → {"version": "…", "processed": n}
package main

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"os"
	"sync/atomic"
	"time"
)

type worker struct {
	version   string
	processed atomic.Int64
}

func (w *worker) tick() { w.processed.Add(1) }

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

func main() {
	w := &worker{version: os.Getenv("APP_VERSION")}
	go func() {
		for range time.Tick(5 * time.Second) {
			w.tick()
			slog.Info("processed pending orders", "total", w.processed.Load())
		}
	}()
	srv := &http.Server{Addr: ":8080", Handler: w.mux(), ReadHeaderTimeout: 5 * time.Second}
	slog.Info("worker listening", "version", w.version)
	if err := srv.ListenAndServe(); err != nil {
		slog.Error("serve", "err", err)
		os.Exit(1)
	}
}
