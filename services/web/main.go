// web: the storefront's page (one of two services of this monorepo, released and promoted on its own). It renders
// the products of the api service (API_URL) and shows both versions: which web and which api an environment runs.
//
//	GET /        → HTML page; without the api's products it is still served (200) but counted degraded: a bad
//	               event for the availability SLO (obs.Degraded)
//	GET /healthz → ok (header X-App-Version)
//	:9090 /metrics (internal/obs); calls to the api are counted as upstream "api"
package main

import (
	"encoding/json"
	"html/template"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/MIERAI/storefront/internal/obs"
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

func newMux(version, apiURL string, client *http.Client) *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("X-App-Version", version)
		w.Write([]byte("ok"))
	})
	mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, _ *http.Request) {
		var p products
		resp, err := client.Get(apiURL + "/products")
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
	return mux
}

func main() {
	version, apiURL := os.Getenv("APP_VERSION"), os.Getenv("API_URL")
	m := obs.New()
	m.Serve(":9090")
	client := m.Client("api", &http.Client{Timeout: 2 * time.Second})
	srv := &http.Server{Addr: ":8080", Handler: m.Instrument(newMux(version, apiURL, client)), ReadHeaderTimeout: 5 * time.Second}
	slog.Info("web listening", "version", version, "api", apiURL)
	if err := srv.ListenAndServe(); err != nil {
		slog.Error("serve", "err", err)
		os.Exit(1)
	}
}
