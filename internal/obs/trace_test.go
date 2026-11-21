package obs

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func TestTracingOffWithoutEndpoint(t *testing.T) {
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
	t.Setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")
	if TracingEnabled() {
		t.Fatal("enabled without an endpoint")
	}
	if err := Tracing(context.Background(), "test", "v1")(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, sdk := otel.GetTracerProvider().(*sdktrace.TracerProvider); sdk {
		t.Fatal("an SDK tracer provider without an endpoint")
	}
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
	t.Setenv("OTEL_SDK_DISABLED", "true")
	if TracingEnabled() {
		t.Fatal("enabled although OTEL_SDK_DISABLED=true")
	}
}

// Two services in one test: front (GET /) calls back (GET /products) through Client. One trace, three spans:
// front's server span → its client span → back's server span; health checks are not traced.
func TestTraceAcrossServices(t *testing.T) {
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
	prev := otel.GetTracerProvider()
	otel.SetTracerProvider(tp)
	t.Cleanup(func() { otel.SetTracerProvider(prev) })
	Tracing(context.Background(), "test", "") // the propagator (no endpoint: no exporter)

	backMux := http.NewServeMux()
	backMux.HandleFunc("GET /products", func(w http.ResponseWriter, _ *http.Request) { w.Write([]byte("[]")) })
	back := httptest.NewServer(New().Instrument(backMux))
	defer back.Close()

	m := New()
	client := m.Client("back", back.Client())
	frontMux := http.NewServeMux()
	frontMux.HandleFunc("GET /{$}", func(w http.ResponseWriter, r *http.Request) {
		req, _ := http.NewRequestWithContext(r.Context(), "GET", back.URL+"/products", nil)
		resp, err := client.Do(req)
		if err != nil {
			t.Error(err)
			return
		}
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		Degraded(w)
	})
	frontMux.HandleFunc("GET /healthz", func(http.ResponseWriter, *http.Request) {})
	front := m.Instrument(frontMux)
	front.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", "/healthz", nil))
	front.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", "/", nil))

	var srv, cli, up tracetest.SpanStub
	for _, s := range exp.GetSpans() {
		switch {
		case s.Name == "GET /{$}":
			srv = s
		case s.Name == "GET /products" && s.SpanKind == trace.SpanKindClient:
			cli = s
		case s.Name == "GET /products" && s.SpanKind == trace.SpanKindServer:
			up = s
		}
	}
	if n := len(exp.GetSpans()); n != 3 || !srv.SpanContext.IsValid() || !cli.SpanContext.IsValid() || !up.SpanContext.IsValid() {
		t.Fatalf("want GET /{$} (server), GET /products (client, server); got %d: %v", n, exp.GetSpans())
	}
	tid := srv.SpanContext.TraceID()
	if cli.SpanContext.TraceID() != tid || up.SpanContext.TraceID() != tid {
		t.Fatal("not one trace")
	}
	if cli.Parent.SpanID() != srv.SpanContext.SpanID() || up.Parent.SpanID() != cli.SpanContext.SpanID() {
		t.Fatal("wrong parents")
	}
	if !has(srv.Attributes, attribute.String("result", "degraded")) || !has(srv.Attributes, attribute.String("http.route", "/{$}")) || !has(cli.Attributes, attribute.String("peer.service", "back")) {
		t.Fatalf("attributes: %v / %v", srv.Attributes, cli.Attributes)
	}
}

func has(attrs []attribute.KeyValue, want attribute.KeyValue) bool {
	for _, a := range attrs {
		if a == want {
			return true
		}
	}
	return false
}
