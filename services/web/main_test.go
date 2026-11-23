package main

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/MIERAI/storefront/internal/obs"
)

func TestPageShowsBothVersions(t *testing.T) {
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Write([]byte(`{"version":"a1","products":[{"name":"mug","price":1600}]}`))
	}))
	defer api.Close()
	rec := httptest.NewRecorder()
	newMux("w1", api.URL, api.Client(), nil, obs.New()).ServeHTTP(rec, httptest.NewRequest("GET", "/", nil))
	if b := rec.Body.String(); !strings.Contains(b, "web w1 · api a1") || !strings.Contains(b, "mug") {
		t.Fatal(b)
	}
}

// The call to the api carries the page request's trace (traceparent), with or without a tracing backend.
func TestPagePropagatesTrace(t *testing.T) {
	obs.Tracing(context.Background(), "web", "") // no endpoint: no-op provider, the propagator only
	const parent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
	var got string
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.Header.Get("traceparent")
		w.Write([]byte(`{"version":"a1","products":[]}`))
	}))
	defer api.Close()
	m := obs.New()
	req := httptest.NewRequest("GET", "/", nil)
	req.Header.Set("traceparent", parent)
	m.Instrument(newMux("w1", api.URL, m.Client("api", api.Client()), nil, m)).ServeHTTP(httptest.NewRecorder(), req)
	if !strings.HasPrefix(got, "00-4bf92f3577b34da6a3ce929d0e0e4736-") {
		t.Fatalf("traceparent at the api: %q", got)
	}
}

func TestPageWithoutProductsIsDegraded(t *testing.T) {
	m := obs.New()
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { http.Error(w, "down", 503) }))
	defer api.Close()
	rec := httptest.NewRecorder()
	m.Instrument(newMux("w1", api.URL, api.Client(), nil, obs.New())).ServeHTTP(rec, httptest.NewRequest("GET", "/", nil))
	var b strings.Builder
	m.Write(&b)
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), "api unreachable") || !strings.Contains(b.String(), `result="degraded"`) {
		t.Fatalf("%d %s\n%s", rec.Code, rec.Body, b.String())
	}
}

func TestOrders(t *testing.T) {
	m := obs.New()
	var got []int
	create := func(_ context.Context, amount int) (int64, error) {
		if amount == 13 {
			return 0, errors.New("database down")
		}
		got = append(got, amount)
		return int64(len(got)), nil
	}
	h := m.Instrument(newMux("w1", "", nil, create, m))
	for _, c := range []struct {
		body string
		code int
	}{
		{`{"amount": 1200}`, 201},
		{`{"amount": 0}`, 400},
		{`nope`, 400},
		{`{"amount": 13}`, 503},
	} {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest("POST", "/orders", strings.NewReader(c.body)))
		if rec.Code != c.code {
			t.Errorf("%s: %d %s", c.body, rec.Code, rec.Body)
		}
	}
	var b strings.Builder
	m.Write(&b)
	for _, want := range []string{
		"orders_created_total 1\n",
		`http_requests_total{route="POST /orders",code="201",result="ok"} 1`,
		`http_requests_total{route="POST /orders",code="400",result="ok"} 2`,
		`http_requests_total{route="POST /orders",code="503",result="error"} 1`,
	} {
		if !strings.Contains(b.String(), want) {
			t.Errorf("missing %s in\n%s", want, b.String())
		}
	}
	if len(got) != 1 || got[0] != 1200 {
		t.Error(got)
	}
	rec := httptest.NewRecorder() // no database configured
	newMux("w1", "", nil, nil, m).ServeHTTP(rec, httptest.NewRequest("POST", "/orders", strings.NewReader(`{"amount": 5}`)))
	if rec.Code != 503 {
		t.Error(rec.Code)
	}
}
