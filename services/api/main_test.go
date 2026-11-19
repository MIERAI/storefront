package main

import (
	"encoding/json"
	"net/http/httptest"
	"testing"
)

func TestProducts(t *testing.T) {
	rec := httptest.NewRecorder()
	newMux("v1").ServeHTTP(rec, httptest.NewRequest("GET", "/products", nil))
	var body struct {
		Version  string    `json:"version"`
		Products []product `json:"products"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || rec.Code != 200 || body.Version != "v1" || len(body.Products) != 3 {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
}
