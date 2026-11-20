package main

import (
	"context"
	"errors"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/MIERAI/storefront/internal/obs"
	"github.com/MIERAI/storefront/internal/orders"
)

func TestStepCountsOrders(t *testing.T) {
	results := []error{nil, nil, errors.New("database down")}
	w := &worker{version: "v1", m: obs.New()}
	w.process = func(context.Context) (orders.Order, bool, error) {
		if len(results) == 0 {
			return orders.Order{}, false, nil
		}
		err := results[0]
		results = results[1:]
		return orders.Order{ID: 1, Delay: 3 * time.Second}, err == nil, err
	}
	ctx := context.Background()
	if !w.step(ctx) || !w.step(ctx) || w.step(ctx) || w.step(ctx) {
		t.Fatal("step results")
	}
	var b strings.Builder
	w.m.Write(&b)
	for _, want := range []string{"orders_processed_total 2\n", "worker_errors_total 1\n",
		`order_processing_delay_seconds_bucket{le="2.5"} 0`, `order_processing_delay_seconds_bucket{le="5"} 2`} {
		if !strings.Contains(b.String(), want) {
			t.Errorf("missing %s in\n%s", want, b.String())
		}
	}
	rec := httptest.NewRecorder()
	w.mux().ServeHTTP(rec, httptest.NewRequest("GET", "/stats", nil))
	if b := rec.Body.String(); !strings.Contains(b, `"processed":2`) || !strings.Contains(b, `"version":"v1"`) {
		t.Fatal(b)
	}
}

func TestRunStops(t *testing.T) {
	w := &worker{m: obs.New(), process: func(context.Context) (orders.Order, bool, error) { return orders.Order{}, false, nil }}
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	w.run(ctx, 10*time.Millisecond) // returns when ctx ends
}
