package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
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
