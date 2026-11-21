// Package obs: the storefront services' metrics (Prometheus text format, no dependency), shaped for SLOs, and their
// traces (OpenTelemetry, off unless an OTLP endpoint is configured: Tracing in trace.go).
//
//	http_requests_total{route, code, result}       result: ok | degraded | error — the availability SLI counts
//	                                               result!="ok" as bad (a 200 page without its products is bad)
//	http_request_duration_seconds{route}           histogram; the buckets include every SLO threshold (0.1, 0.5)
//	upstream_requests_total{upstream, code}        calls to a dependency; code "error" = no response (timeout,
//	                                               refused): where a symptom on this service comes from
//	upstream_request_duration_seconds{upstream}    histogram
//
// Services add their own: Add (counter), Set / Unset (gauge), ObserveIn (histogram with its own buckets).
//
// Routes are the mux patterns (bounded cardinality), never raw paths. Served on a port of its own (Serve), apart
// from the traffic port.
package obs

import (
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
)

// Buckets: an SLO threshold must be a bucket edge (interpolating between buckets is a guess).
var Buckets = []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5}

type Metrics struct {
	mu     sync.Mutex
	reqs   map[string]uint64 // name{label set} → count
	gauges map[string]float64
	hist   map[string]*histogram
}

type histogram struct {
	buckets []float64
	counts  []uint64 // per bucket, +Inf last
	sum     float64
}

func New() *Metrics {
	return &Metrics{reqs: map[string]uint64{}, gauges: map[string]float64{}, hist: map[string]*histogram{}}
}

func labels(kv ...string) string {
	var b strings.Builder
	for i := 0; i < len(kv); i += 2 {
		if i > 0 {
			b.WriteByte(',')
		}
		fmt.Fprintf(&b, "%s=%q", kv[i], kv[i+1])
	}
	return b.String()
}

func (m *Metrics) count(name, ls string) { m.add(name, ls, 1) }

func (m *Metrics) add(name, ls string, n uint64) {
	m.mu.Lock()
	m.reqs[name+"{"+ls+"}"] += n
	m.mu.Unlock()
}

// Add adds n to the counter name{labels} (kv: label, value, …). Add(name, 0, …) exports it at 0: a counter that
// appears only with its first event hides that event from rate(), and an error counter absent until the first
// error reads as "SLI absent".
func (m *Metrics) Add(name string, n uint64, kv ...string) { m.add(name, labels(kv...), n) }

// Inc adds 1 to the counter name{labels}.
func (m *Metrics) Inc(name string, kv ...string) { m.Add(name, 1, kv...) }

// Set sets the gauge name{labels}.
func (m *Metrics) Set(name string, v float64, kv ...string) {
	m.mu.Lock()
	m.gauges[name+"{"+labels(kv...)+"}"] = v
	m.mu.Unlock()
}

// Unset removes the gauge name{labels}: when its value is unknown, no series is better than a stale one.
func (m *Metrics) Unset(name string, kv ...string) {
	m.mu.Lock()
	delete(m.gauges, name+"{"+labels(kv...)+"}")
	m.mu.Unlock()
}

func (m *Metrics) observe(name, ls string, d time.Duration) {
	m.observeIn(name, ls, Buckets, d.Seconds())
}

// ObserveIn records v (seconds) in the histogram name{labels} with its own bucket edges (ascending). The edges of a
// series are fixed by its first observation.
func (m *Metrics) ObserveIn(name string, buckets []float64, v float64, kv ...string) {
	m.observeIn(name, labels(kv...), buckets, v)
}

func (m *Metrics) observeIn(name, ls string, buckets []float64, v float64) {
	m.mu.Lock()
	defer m.mu.Unlock()
	k := name + "\x00" + ls
	h := m.hist[k]
	if h == nil {
		h = &histogram{buckets: buckets, counts: make([]uint64, len(buckets)+1)}
		m.hist[k] = h
	}
	h.counts[sort.SearchFloat64s(h.buckets, v)]++
	h.sum += v
}

// Write: every series, sorted (stable output for tests and diffs).
func (m *Metrics) Write(w io.Writer) {
	m.mu.Lock()
	defer m.mu.Unlock()
	keys := make([]string, 0, len(m.reqs))
	for k := range m.reqs {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	typed := map[string]bool{}
	for _, k := range keys {
		name := k[:strings.IndexByte(k, '{')]
		if !typed[name] {
			fmt.Fprintf(w, "# TYPE %s counter\n", name)
			typed[name] = true
		}
		fmt.Fprintf(w, "%s %d\n", strings.TrimSuffix(k, "{}"), m.reqs[k])
	}
	keys = keys[:0]
	for k := range m.gauges {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		name := k[:strings.IndexByte(k, '{')]
		if !typed[name] {
			fmt.Fprintf(w, "# TYPE %s gauge\n", name)
			typed[name] = true
		}
		fmt.Fprintf(w, "%s %s\n", strings.TrimSuffix(k, "{}"), strconv.FormatFloat(m.gauges[k], 'g', -1, 64))
	}
	keys = keys[:0]
	for k := range m.hist {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		name, ls, _ := strings.Cut(k, "\x00")
		if !typed[name] {
			fmt.Fprintf(w, "# TYPE %s histogram\n", name)
			typed[name] = true
		}
		h := m.hist[k]
		le := "le="
		if ls != "" {
			le = ls + ",le="
		}
		var cum uint64
		for i, b := range h.buckets {
			cum += h.counts[i]
			fmt.Fprintf(w, "%s_bucket{%s%q} %d\n", name, le, strconv.FormatFloat(b, 'g', -1, 64), cum)
		}
		cum += h.counts[len(h.buckets)]
		fmt.Fprintf(w, "%s_bucket{%s\"+Inf\"} %d\n", name, le, cum)
		fmt.Fprintf(w, "%s %g\n", strings.TrimSuffix(name+"_sum{"+ls+"}", "{}"), h.sum)
		fmt.Fprintf(w, "%s %d\n", strings.TrimSuffix(name+"_count{"+ls+"}", "{}"), cum)
	}
}

// Serve: GET /metrics on addr (e.g. ":9090"), in the background.
func (m *Metrics) Serve(addr string) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /metrics", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		m.Write(w)
	})
	srv := &http.Server{Addr: addr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		if err := srv.ListenAndServe(); err != nil {
			slog.Error("metrics", "err", err)
		}
	}()
}

type recorder struct {
	http.ResponseWriter
	code     int
	degraded bool
}

func (r *recorder) WriteHeader(c int) { r.code = c; r.ResponseWriter.WriteHeader(c) }

// Degraded marks the response as degraded: served, but not what the user came for (e.g. a page without its
// products). Counted result="degraded", a bad event for the availability SLI.
func Degraded(w http.ResponseWriter) {
	if r, ok := w.(*recorder); ok {
		r.degraded = true
	}
}

// Instrument records every request of mux under its route pattern, and traces it (a server span, a no-op unless
// Tracing is on; result="degraded" is set on the span too). Health checks are not user traffic: skipped.
func (m *Metrics) Instrument(mux *http.ServeMux) http.Handler {
	return traced(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &recorder{ResponseWriter: w, code: 200}
		mux.ServeHTTP(rec, r)
		_, route := mux.Handler(r)
		if route == "GET /healthz" {
			return
		}
		if route == "" {
			route = "unmatched"
		}
		result := "ok"
		switch {
		case rec.code >= 500:
			result = "error"
		case rec.degraded:
			result = "degraded"
		}
		m.count("http_requests_total", labels("route", route, "code", strconv.Itoa(rec.code), "result", result))
		m.observe("http_request_duration_seconds", labels("route", route), time.Since(start))
		span := trace.SpanFromContext(r.Context()) // named after the route by otelhttp ("GET /{$}")
		if _, path, ok := strings.Cut(route, " "); ok {
			span.SetAttributes(attribute.String("http.route", path))
		}
		if result == "degraded" {
			span.SetAttributes(attribute.String("result", result))
		}
	}))
}

type transport struct {
	m    *Metrics
	name string
	next http.RoundTripper
}

func (t transport) RoundTrip(r *http.Request) (*http.Response, error) {
	start := time.Now()
	resp, err := t.next.RoundTrip(r)
	code := "error"
	if err == nil {
		code = strconv.Itoa(resp.StatusCode)
	}
	t.m.count("upstream_requests_total", labels("upstream", t.name, "code", code))
	t.m.observe("upstream_request_duration_seconds", labels("upstream", t.name), time.Since(start))
	return resp, err
}

// Client: c, with its calls counted as upstream <name> and traced (client spans, trace context propagated). The
// requests must carry the inbound request's context (http.NewRequestWithContext) to join its trace.
func (m *Metrics) Client(name string, c *http.Client) *http.Client {
	next := c.Transport
	if next == nil {
		next = http.DefaultTransport
	}
	cc := *c
	cc.Transport = transport{m, name, tracedTransport(name, next)}
	return &cc
}
