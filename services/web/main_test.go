package main

import (
	"context"
	"encoding/json"
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
	newMux("w1", fromAPI(api.URL, api.Client()), nil, obs.New()).ServeHTTP(rec, httptest.NewRequest("GET", "/", nil))
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
	m.Instrument(newMux("w1", fromAPI(api.URL, m.Client("api", api.Client())), nil, m)).ServeHTTP(httptest.NewRecorder(), req)
	if !strings.HasPrefix(got, "00-4bf92f3577b34da6a3ce929d0e0e4736-") {
		t.Fatalf("traceparent at the api: %q", got)
	}
}

// A fake catalog: the Connect JSON of ListProducts (one category per call, paged) and /healthz.
func fakeCatalog(t *testing.T, fail string) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" {
			w.Header().Set("X-App-Version", "c1")
			w.Write([]byte("ok"))
			return
		}
		if r.Method != "POST" || r.URL.Path != listProductsPath || r.Header.Get("Content-Type") != "application/json" {
			t.Errorf("%s %s %s", r.Method, r.URL.Path, r.Header.Get("Content-Type"))
		}
		var in struct{ Category, PageToken string }
		json.NewDecoder(r.Body).Decode(&in)
		switch {
		case in.Category == fail:
			w.WriteHeader(503)
			w.Write([]byte(`{"code":"unavailable"}`))
		case in.Category == "coffee" && in.PageToken == "":
			w.Write([]byte(`{"products":[{"sku":"BAG-002","name":"drip bag","price":"180","version":1}],"nextPageToken":"p2"}`))
		case in.Category == "coffee":
			w.Write([]byte(`{"products":[{"sku":"BEAN-001","name":"coffee beans","price":"1200","version":1}]}`))
		case in.Category == "goods":
			w.Write([]byte(`{"products":[{"sku":"MUG-003","name":"mug","price":"1600.50","version":1}]}`))
		default:
			w.Write([]byte(`{}`)) // an empty category: protojson leaves the empty list out
		}
	}))
}

func TestPageFromCatalog(t *testing.T) {
	c := fakeCatalog(t, "")
	defer c.Close()
	m := obs.New()
	rec := httptest.NewRecorder()
	m.Instrument(newMux("w1", fromCatalog(c.URL, []string{"coffee", "goods", "tea"}, c.Client()), nil, m)).
		ServeHTTP(rec, httptest.NewRequest("GET", "/", nil))
	b := rec.Body.String()
	for _, want := range []string{"web w1 · catalog c1", "drip bag — ¥180", "coffee beans — ¥1200", "mug — ¥1600.50"} {
		if !strings.Contains(b, want) {
			t.Errorf("missing %q in %s", want, b)
		}
	}
	var mb strings.Builder
	m.Write(&mb)
	if !strings.Contains(mb.String(), `result="ok"`) || strings.Contains(mb.String(), `result="degraded"`) {
		t.Error(mb.String())
	}
}

func TestPageWithoutCatalogIsDegraded(t *testing.T) {
	c := fakeCatalog(t, "goods") // one category failing is enough
	defer c.Close()
	m := obs.New()
	rec := httptest.NewRecorder()
	m.Instrument(newMux("w1", fromCatalog(c.URL, []string{"coffee", "goods"}, c.Client()), nil, m)).
		ServeHTTP(rec, httptest.NewRequest("GET", "/", nil))
	var mb strings.Builder
	m.Write(&mb)
	if rec.Code != 200 || strings.Contains(rec.Body.String(), "drip bag") || !strings.Contains(mb.String(), `result="degraded"`) {
		t.Fatalf("%d %s\n%s", rec.Code, rec.Body, mb.String())
	}
}

func TestPageWithoutProductsIsDegraded(t *testing.T) {
	m := obs.New()
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { http.Error(w, "down", 503) }))
	defer api.Close()
	rec := httptest.NewRecorder()
	m.Instrument(newMux("w1", fromAPI(api.URL, api.Client()), nil, obs.New())).ServeHTTP(rec, httptest.NewRequest("GET", "/", nil))
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
	h := m.Instrument(newMux("w1", fromAPI("", nil), create, m))
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
	newMux("w1", fromAPI("", nil), nil, m).ServeHTTP(rec, httptest.NewRequest("POST", "/orders", strings.NewReader(`{"amount": 5}`)))
	if rec.Code != 503 {
		t.Error(rec.Code)
	}
}
