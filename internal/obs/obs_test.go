package obs

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestInstrumentResults(t *testing.T) {
	m := New()
	mux := http.NewServeMux()
	mux.HandleFunc("GET /ok", func(w http.ResponseWriter, _ *http.Request) { w.Write([]byte("x")) })
	mux.HandleFunc("GET /deg", func(w http.ResponseWriter, _ *http.Request) { Degraded(w); w.Write([]byte("x")) })
	mux.HandleFunc("GET /fail", func(w http.ResponseWriter, _ *http.Request) { http.Error(w, "x", 503) })
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {})
	h := m.Instrument(mux)
	for _, p := range []string{"/ok", "/ok", "/deg", "/fail", "/healthz", "/nope"} {
		h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", p, nil))
	}
	var b bytes.Buffer
	m.Write(&b)
	out := b.String()
	for _, want := range []string{
		`http_requests_total{route="GET /ok",code="200",result="ok"} 2`,
		`http_requests_total{route="GET /deg",code="200",result="degraded"} 1`,
		`http_requests_total{route="GET /fail",code="503",result="error"} 1`,
		`http_requests_total{route="unmatched",code="404",result="ok"} 1`,
		`http_request_duration_seconds_bucket{route="GET /ok",le="0.5"} 2`,
		`http_request_duration_seconds_count{route="GET /ok"} 2`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %s in\n%s", want, out)
		}
	}
	if strings.Contains(out, "healthz") {
		t.Error("health checks counted")
	}
}

func TestClientCountsUpstream(t *testing.T) {
	m := New()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(500) }))
	c := m.Client("api", srv.Client())
	resp, _ := c.Get(srv.URL)
	resp.Body.Close()
	srv.Close()
	c.Get(srv.URL) // refused
	var b bytes.Buffer
	m.Write(&b)
	for _, want := range []string{`upstream_requests_total{upstream="api",code="500"} 1`, `upstream_requests_total{upstream="api",code="error"} 1`} {
		if !strings.Contains(b.String(), want) {
			t.Errorf("missing %s in\n%s", want, b.String())
		}
	}
}

func TestCountersGaugesHistograms(t *testing.T) {
	m := New()
	m.Add("orders_created_total", 0)
	m.Add("orders_overdue_total", 0)
	m.Inc("orders_created_total")
	m.Set("orders_pending", 3)
	m.Set("orders_oldest_pending_age_seconds", 12.5)
	m.Set("gone", 1)
	m.Unset("gone")
	b := []float64{1, 10, 60}
	m.ObserveIn("order_processing_delay_seconds", b, 0.5)
	m.ObserveIn("order_processing_delay_seconds", b, 30)
	var out bytes.Buffer
	m.Write(&out)
	for _, want := range []string{
		"# TYPE orders_created_total counter\norders_created_total 1\n",
		"orders_overdue_total 0\n",
		"# TYPE orders_oldest_pending_age_seconds gauge\norders_oldest_pending_age_seconds 12.5\n",
		"orders_pending 3\n",
		`order_processing_delay_seconds_bucket{le="1"} 1`,
		`order_processing_delay_seconds_bucket{le="10"} 1`,
		`order_processing_delay_seconds_bucket{le="60"} 2`,
		"order_processing_delay_seconds_count 2\n",
	} {
		if !strings.Contains(out.String(), want) {
			t.Errorf("missing %q in\n%s", want, out.String())
		}
	}
	if strings.Contains(out.String(), "gone") {
		t.Error("unset gauge still exported")
	}
}
