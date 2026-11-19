// api: the storefront's product API (one of two services of this monorepo, released and promoted on its own).
//
//	GET /products → [{"id": 1, "name": "…", "price": 120}] (+ "version")
//	GET /healthz  → ok (header X-App-Version)
package main

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"os"
	"time"
)

type product struct {
	ID    int    `json:"id"`
	Name  string `json:"name"`
	Price int    `json:"price"`
}

var catalog = []product{{1, "coffee beans", 1200}, {2, "drip bag", 180}, {3, "mug", 1600}}

func newMux(version string) *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("X-App-Version", version)
		w.Write([]byte("ok"))
	})
	mux.HandleFunc("GET /products", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"version": version, "products": catalog})
	})
	return mux
}

func main() {
	version := os.Getenv("APP_VERSION")
	srv := &http.Server{Addr: ":8080", Handler: newMux(version), ReadHeaderTimeout: 5 * time.Second}
	slog.Info("api listening", "version", version)
	if err := srv.ListenAndServe(); err != nil {
		slog.Error("serve", "err", err)
		os.Exit(1)
	}
}
