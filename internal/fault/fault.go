// Package fault: application fault switches for the fault experiments (gitops-manifests experiments/). Faults that
// infrastructure tools cannot make — an answer that is wrong or an error the service itself returns — are switched
// on by files in a directory, normally an optional ConfigMap volume: creating the ConfigMap turns a fault on,
// deleting it turns it off, no restart (kubelet updates the volume within ~1 min).
//
//	<dir>/<name>-errors   a ratio 0..1: that share of the requests fail with 503
//	<dir>/expires         unix seconds: after this every fault is off, whether or not the ConfigMap is deleted
//
// No directory, no file, or an unparsable value: no fault. A fault without `expires` never expires — the experiment
// runner always writes one: when the runner dies (a laptop going to sleep did, 2026-11-20), the fault must end anyway.
package fault

import (
	"math/rand/v2"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

type Switches struct {
	dir   string
	every time.Duration
	mu    sync.Mutex
	read  time.Time
	vals  map[string]float64
}

// New reads dir at most every `every` (0: on every call — tests).
func New(dir string, every time.Duration) *Switches {
	return &Switches{dir: dir, every: every, vals: map[string]float64{}}
}

func (s *Switches) value(key string) float64 {
	if s == nil || s.dir == "" {
		return 0
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.every == 0 || time.Since(s.read) >= s.every {
		s.read = time.Now()
		s.vals = map[string]float64{}
		entries, _ := os.ReadDir(s.dir)
		for _, e := range entries {
			if e.IsDir() || strings.HasPrefix(e.Name(), ".") { // ConfigMap volumes keep their data in ..data dirs
				continue
			}
			b, err := os.ReadFile(filepath.Join(s.dir, e.Name()))
			if err != nil {
				continue
			}
			v, err := strconv.ParseFloat(strings.TrimSpace(string(b)), 64)
			if err == nil && (e.Name() == "expires" || v >= 0 && v <= 1) {
				s.vals[e.Name()] = v
			}
		}
	}
	if exp, ok := s.vals["expires"]; ok && float64(time.Now().Unix()) > exp {
		return 0
	}
	return s.vals[key]
}

// Fail: whether this request of <name> must fail (its "<name>-errors" ratio).
func (s *Switches) Fail(name string) bool {
	r := s.value(name + "-errors")
	return r > 0 && rand.Float64() < r
}
