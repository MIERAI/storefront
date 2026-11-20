package main

import (
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
	newMux("w1", api.URL, api.Client()).ServeHTTP(rec, httptest.NewRequest("GET", "/", nil))
	if b := rec.Body.String(); !strings.Contains(b, "web w1 · api a1") || !strings.Contains(b, "mug") {
		t.Fatal(b)
	}
}

func TestPageWithoutProductsIsDegraded(t *testing.T) {
	m := obs.New()
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { http.Error(w, "down", 503) }))
	defer api.Close()
	rec := httptest.NewRecorder()
	m.Instrument(newMux("w1", api.URL, api.Client())).ServeHTTP(rec, httptest.NewRequest("GET", "/", nil))
	var b strings.Builder
	m.Write(&b)
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), "api unreachable") || !strings.Contains(b.String(), `result="degraded"`) {
		t.Fatalf("%d %s\n%s", rec.Code, rec.Body, b.String())
	}
}
