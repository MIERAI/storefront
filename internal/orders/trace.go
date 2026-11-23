package orders

import (
	"context"
	"strings"

	"github.com/jackc/pgx/v5"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

var tracer = otel.Tracer("github.com/MIERAI/storefront/internal/orders")

// queryTracer: every query is a span — but only inside a trace (a request, an order being processed). Queries
// without one (the 10s pipeline Check, migrations, a worker polling an empty queue) are not traced: they would be a
// root span each, thousands of one-span traces a day that answer no question.
type queryTracer struct{}

func (queryTracer) TraceQueryStart(ctx context.Context, _ *pgx.Conn, d pgx.TraceQueryStartData) context.Context {
	if !trace.SpanFromContext(ctx).IsRecording() {
		return ctx
	}
	ctx, _ = tracer.Start(ctx, spanName(d.SQL), trace.WithSpanKind(trace.SpanKindClient), trace.WithAttributes(
		attribute.String("db.system.name", "postgresql"),
		attribute.String("db.query.text", d.SQL), // parameters ($1…) are not recorded
	))
	return context.WithValue(ctx, queryTracer{}, true)
}

func (queryTracer) TraceQueryEnd(ctx context.Context, _ *pgx.Conn, d pgx.TraceQueryEndData) {
	if ctx.Value(queryTracer{}) == nil {
		return
	}
	span := trace.SpanFromContext(ctx)
	if d.Err != nil {
		span.RecordError(d.Err)
		span.SetStatus(codes.Error, d.Err.Error())
	} else {
		span.SetAttributes(attribute.Int64("db.response.returned_rows", d.CommandTag.RowsAffected()))
	}
	span.End()
}

// spanName: the statement's first keyword and its first table, e.g. "INSERT orders", "UPDATE orders", "BEGIN".
func spanName(sql string) string {
	f := strings.Fields(sql)
	if len(f) == 0 {
		return "query"
	}
	for i, w := range f[:len(f)-1] {
		if k := strings.ToUpper(w); k == "FROM" || k == "INTO" || k == "UPDATE" {
			return strings.ToUpper(f[0]) + " " + strings.Trim(f[i+1], "(),")
		}
	}
	return strings.ToUpper(f[0])
}
