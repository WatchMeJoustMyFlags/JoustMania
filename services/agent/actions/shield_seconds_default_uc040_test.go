package actions

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flagservice"
)

// shield_seconds_default_uc040_test.go covers the grant_shield length (UC-040 BR-029,
// #2395): the ShieldSecondsReader reads the interventions/shield_seconds "default"
// variant at use time and fails closed to 5 s.

type fakeFlagViewer struct {
	mu    sync.Mutex
	view  flagservice.FlagView
	err   error
	calls int
	set   string
	flag  string
}

func (f *fakeFlagViewer) GetFlag(_ context.Context, set, flag string) (flagservice.FlagView, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	f.set, f.flag = set, flag
	return f.view, f.err
}

func shieldView(variants map[string]string) flagservice.FlagView {
	v := flagservice.FlagView{Flag: flagShieldSeconds, BaselineDefault: "none", EffectiveDefault: "none",
		VariantValues: map[string]json.RawMessage{}}
	for name, raw := range variants {
		v.VariantValues[name] = json.RawMessage(raw)
	}
	return v
}

func readerAt(svc FlagViewer, clock *time.Time) *ShieldSecondsReader {
	r := NewShieldSecondsReader(svc, nil)
	r.now = func() time.Time { return *clock }
	return r
}

// UC: UC-040 BR-029
func TestShieldSecondsReader(t *testing.T) {
	ctx := context.Background()
	t.Run("UC-040/BR-029 the length is the value of the variant named default, not the defaultVariant", func(t *testing.T) {
		svc := &fakeFlagViewer{view: shieldView(map[string]string{"none": "0", "short": "3", "default": "3", "long": "10"})}
		clock := time.Unix(1, 0)
		if got := readerAt(svc, &clock).Seconds(ctx); got != 3 {
			t.Fatalf("Seconds = %v, want 3", got)
		}
		if svc.set != FlagSetInterventions || svc.flag != "shield_seconds" {
			t.Fatalf("read %s/%s, want interventions/shield_seconds", svc.set, svc.flag)
		}
	})
	for _, tc := range []struct {
		name string
		svc  *fakeFlagViewer
	}{
		{"the flag service cannot be read", &fakeFlagViewer{err: errors.New("down")}},
		{"the default variant is missing", &fakeFlagViewer{view: shieldView(map[string]string{"none": "0", "short": "3"})}},
		{"the variant is not a number", &fakeFlagViewer{view: shieldView(map[string]string{"default": `"five"`})}},
		{"the variant is 0 (no shield)", &fakeFlagViewer{view: shieldView(map[string]string{"default": "0"})}},
		{"the variant is above the 10 s bound", &fakeFlagViewer{view: shieldView(map[string]string{"default": "11"})}},
	} {
		t.Run("UC-040/BR-029 fail closed to 5 s when "+tc.name, func(t *testing.T) {
			clock := time.Unix(1, 0)
			if got := readerAt(tc.svc, &clock).Seconds(ctx); got != decision.DefaultShieldSeconds {
				t.Fatalf("Seconds = %v, want %v", got, decision.DefaultShieldSeconds)
			}
		})
	}
	t.Run("UC-040/BR-029 read at use time: a changed variant applies to the next grant after 1 s", func(t *testing.T) {
		svc := &fakeFlagViewer{view: shieldView(map[string]string{"default": "5"})}
		clock := time.Unix(1, 0)
		r := readerAt(svc, &clock)
		if got := r.Seconds(ctx); got != 5 {
			t.Fatalf("first Seconds = %v, want 5", got)
		}
		svc.mu.Lock()
		svc.view = shieldView(map[string]string{"default": "3"})
		svc.mu.Unlock()
		clock = clock.Add(500 * time.Millisecond)
		if got := r.Seconds(ctx); got != 5 || svc.calls != 1 {
			t.Fatalf("within 1 s: Seconds = %v after %d reads, want the cached 5 after 1", got, svc.calls)
		}
		clock = clock.Add(600 * time.Millisecond)
		if got := r.Seconds(ctx); got != 3 || svc.calls != 2 {
			t.Fatalf("after 1 s: Seconds = %v after %d reads, want 3 after 2", got, svc.calls)
		}
	})
	t.Run("UC-040/BR-029 the reader is safe for concurrent loops", func(t *testing.T) {
		svc := &fakeFlagViewer{view: shieldView(map[string]string{"default": "4"})}
		r := NewShieldSecondsReader(svc, nil)
		var wg sync.WaitGroup
		for i := 0; i < 8; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				if got := r.Seconds(ctx); got != 4 {
					t.Errorf("Seconds = %v, want 4", got)
				}
			}()
		}
		wg.Wait()
	})
}
