package main

import (
	"encoding/json"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/MIERAI/storefront/internal/fault"
)

func TestProducts(t *testing.T) {
	rec := httptest.NewRecorder()
	newMux("v1", nil).ServeHTTP(rec, httptest.NewRequest("GET", "/products", nil))
	var body struct {
		Version  string    `json:"version"`
		Products []product `json:"products"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || rec.Code != 200 || body.Version != "v1" || len(body.Products) != 3 {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
}

func TestProductsFaultSwitch(t *testing.T) {
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, "products-errors"), []byte("1"), 0o644)
	rec := httptest.NewRecorder()
	newMux("v1", fault.New(dir, 0)).ServeHTTP(rec, httptest.NewRequest("GET", "/products", nil))
	if rec.Code != 503 {
		t.Fatalf("%d", rec.Code)
	}
}
