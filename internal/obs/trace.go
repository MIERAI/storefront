package obs

// Tracing (OpenTelemetry), configured only by the standard environment variables:
//
//	OTEL_EXPORTER_OTLP_ENDPOINT          e.g. http://tempo:4318 (OTLP over HTTP/protobuf; gRPC is not built in).
//	                                     Unset (and OTEL_EXPORTER_OTLP_TRACES_ENDPOINT unset): tracing is a no-op —
//	                                     no exporter, no connection attempts, no logs
//	OTEL_SERVICE_NAME                    default: the service's name (web, api, worker)
//	OTEL_RESOURCE_ATTRIBUTES             e.g. deployment.environment.name=dev
//	OTEL_TRACES_SAMPLER(_ARG)            default parentbased_always_on
//	OTEL_SDK_DISABLED=true               off, whatever else is set
//
// The W3C trace context (traceparent) is propagated in both cases, so a no-op service in the middle does not cut a
// trace in two.

import (
	"context"
	"log/slog"
	"net/http"
	"os"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.40.0"
	"go.opentelemetry.io/otel/trace"
)

// TracingEnabled: an OTLP endpoint is configured and the SDK is not disabled.
func TracingEnabled() bool {
	if os.Getenv("OTEL_SDK_DISABLED") == "true" {
		return false
	}
	return os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") != "" || os.Getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") != ""
}

// Tracing sets up the global tracer provider for service (version: service.version). Call it first in main and
// defer the returned shutdown (it flushes the spans not exported yet). Without an endpoint (TracingEnabled) the
// global provider stays the no-op one and shutdown does nothing. A bad configuration is logged once and leaves
// tracing off: the service runs without traces rather than not at all.
func Tracing(ctx context.Context, service, version string) (shutdown func(context.Context) error) {
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
	noop := func(context.Context) error { return nil }
	if !TracingEnabled() {
		return noop
	}
	exp, err := otlptracehttp.New(ctx) // endpoint, headers, timeout, … from OTEL_EXPORTER_OTLP_*
	if err != nil {
		slog.Error("tracing off", "err", err)
		return noop
	}
	attrs := []attribute.KeyValue{semconv.ServiceName(service)}
	if version != "" {
		attrs = append(attrs, semconv.ServiceVersion(version))
	}
	// Later options win: OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES override the defaults.
	res, err := resource.New(ctx, resource.WithAttributes(attrs...), resource.WithTelemetrySDK(), resource.WithFromEnv())
	if err != nil {
		slog.Warn("tracing resource", "err", err) // partial resource: still usable
	}
	tp := sdktrace.NewTracerProvider(sdktrace.WithBatcher(exp), sdktrace.WithResource(res)) // sampler: OTEL_TRACES_SAMPLER
	otel.SetTracerProvider(tp)
	otel.SetErrorHandler(otel.ErrorHandlerFunc(func(err error) { slog.Warn("tracing", "err", err) }))
	slog.Info("tracing on", "service", service)
	return tp.Shutdown
}

// traced: inbound server spans (otelhttp, outermost so that obs.Degraded still finds its recorder). Named after the
// mux pattern once routed, like the metrics. Health checks are not traced.
func traced(h http.Handler) http.Handler {
	return otelhttp.NewHandler(h, "http", otelhttp.WithFilter(func(r *http.Request) bool { return r.URL.Path != "/healthz" }))
}

// tracedTransport: client spans for the calls to upstream name, with the trace context injected (traceparent).
func tracedTransport(name string, next http.RoundTripper) http.RoundTripper {
	return otelhttp.NewTransport(next,
		otelhttp.WithSpanNameFormatter(func(_ string, r *http.Request) string { return r.Method + " " + r.URL.Path }),
		otelhttp.WithSpanOptions(trace.WithAttributes(attribute.String("peer.service", name))))
}
