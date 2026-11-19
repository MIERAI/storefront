package main

import (
	"net/http/httptest"
	"strings"
	"testing"
)

func TestStats(t *testing.T) {
	w := &worker{version: "v1"}
	w.tick()
	w.tick()
	rec := httptest.NewRecorder()
	w.mux().ServeHTTP(rec, httptest.NewRequest("GET", "/stats", nil))
	if b := rec.Body.String(); !strings.Contains(b, `"processed":2`) || !strings.Contains(b, `"version":"v1"`) {
		t.Fatal(b)
	}
}
