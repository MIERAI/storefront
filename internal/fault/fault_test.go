package fault

import (
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"
)

func TestSwitches(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	s := New(dir, 0)
	if s.Fail("products") {
		t.Fatal("no file: no fault")
	}
	write := func(v string) { os.WriteFile(filepath.Join(dir, "products-errors"), []byte(v), 0o644) }
	write("1.0\n")
	if !s.Fail("products") || s.Fail("orders") {
		t.Fatal("ratio 1: every products request fails, nothing else")
	}
	write("0")
	if s.Fail("products") {
		t.Fatal("ratio 0: none")
	}
	write("garbage")
	if s.Fail("products") {
		t.Fatal("unparsable: no fault")
	}
	write("0.5")
	n := 0
	for i := 0; i < 2000; i++ {
		if s.Fail("products") {
			n++
		}
	}
	if n < 800 || n > 1200 {
		t.Fatalf("ratio 0.5: %d of 2000", n)
	}
	if New("", 0).Fail("products") || (*Switches)(nil).Fail("products") {
		t.Fatal("no directory: no fault")
	}
}

func TestExpires(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	s := New(dir, 0)
	os.WriteFile(filepath.Join(dir, "products-errors"), []byte("1"), 0o644)
	os.WriteFile(filepath.Join(dir, "expires"), []byte(strconv.FormatInt(time.Now().Add(time.Hour).Unix(), 10)), 0o644)
	if !s.Fail("products") {
		t.Fatal("before expiry: fault on")
	}
	os.WriteFile(filepath.Join(dir, "expires"), []byte(strconv.FormatInt(time.Now().Add(-time.Second).Unix(), 10)), 0o644)
	if s.Fail("products") {
		t.Fatal("after expiry: fault off")
	}
}
