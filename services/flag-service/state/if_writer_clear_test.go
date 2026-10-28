package state

import (
	"net/http"
	"sync"
	"testing"

	"github.com/joustmania/flag-service/fserr"
)

// ifWriterClear is the promoter's compare-and-delete revert (#1877).
func ifWriterClear(flag, ifWriter string) ClearRequest {
	return ClearRequest{Set: "game", Flag: flag, Scope: realScope, Writer: "promotion", IfWriter: ifWriter}
}

func (f *fixture) realWriter(key string) (string, bool) {
	f.t.Helper()
	v, err := f.state.FlagView("game", key)
	if err != nil {
		f.t.Fatalf("view %s: %v", key, err)
	}
	if v.RealOverlay == nil {
		return "", false
	}
	return v.RealOverlay.Writer, true
}

// UC: UC-043 A18
func TestIfWriterClear(t *testing.T) {
	sens := func(writer string) WriteRequest {
		return WriteRequest{Set: "game", Flag: "sensitivity", Variant: "fast", Scope: realScope, Writer: writer}
	}

	t.Run("UC-043/A18 if_writer equal to the overlay's writer clears it", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		f.put(sens("promotion"))
		res, err := f.state.Clear(ifWriterClear("sensitivity", "promotion"))
		if err != nil || res.Cleared != 1 {
			t.Fatalf("cleared %d, err %v; want 1, nil", res.Cleared, err)
		}
		if _, ok := f.realWriter("sensitivity"); ok {
			t.Fatal("the real overlay is still there")
		}
	})

	t.Run("UC-043/A18 another writer's overlay is refused 412 writer_mismatch and left in place", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		f.put(sens("operator"))
		v0 := f.state.Version()
		res, err := f.state.Clear(ifWriterClear("sensitivity", "promotion"))
		wantReason(t, err, http.StatusPreconditionFailed, fserr.WriterMismatch)
		if res.Cleared != 0 {
			t.Fatalf("the refused DELETE cleared %d values", res.Cleared)
		}
		if w, ok := f.realWriter("sensitivity"); !ok || w != "operator" {
			t.Fatalf("real overlay after a refused DELETE: writer %q present %v, want operator", w, ok)
		}
		if f.state.Version() != v0 {
			t.Fatal("a refused DELETE bumped the snapshot version")
		}
	})

	t.Run("UC-043/A18 an overlay without a writer label does not match", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		f.put(sens(""))
		_, err := f.state.Clear(ifWriterClear("sensitivity", "promotion"))
		wantReason(t, err, http.StatusPreconditionFailed, fserr.WriterMismatch)
	})

	t.Run("UC-043/A18 no overlay: an idempotent clear of nothing, not a mismatch", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		res, err := f.state.Clear(ifWriterClear("sensitivity", "promotion"))
		if err != nil || res.Cleared != 0 {
			t.Fatalf("cleared %d, err %v; want 0, nil", res.Cleared, err)
		}
	})

	t.Run("UC-043/A18 the informational writer label is never compared: a plain DELETE clears another writer's overlay", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		f.put(sens("operator"))
		res, err := f.state.Clear(ifWriterClear("sensitivity", ""))
		if err != nil || res.Cleared != 1 {
			t.Fatalf("cleared %d, err %v; want 1, nil", res.Cleared, err)
		}
	})

	t.Run("UC-043/A18 if_writer outside scope real is 400 malformed", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		f.put(WriteRequest{Set: "game", Flag: "sensitivity", Variant: "fast", Writer: "promotion"})
		_, err := f.state.Clear(ClearRequest{Set: "game", Flag: "sensitivity", IfWriter: "promotion"})
		wantReason(t, err, http.StatusBadRequest, fserr.Malformed)
		_, err = f.state.Clear(ClearRequest{Set: "game", Flag: "sensitivity", IfWriter: "promotion", Persist: true})
		wantReason(t, err, http.StatusBadRequest, fserr.Malformed)
	})

	t.Run("UC-043/A18 with unless_real_game too: 409 real_game_live takes precedence over 412", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		f.put(WriteRequest{Set: "game", Flag: "shield.energy_budget_seconds", Value: raw("20"), Scope: realScope, Writer: "operator"})
		f.registerKind("g-real", "real")
		req := ifWriterClear("shield.energy_budget_seconds", "promotion")
		req.UnlessRealGame = true
		_, err := f.state.Clear(req)
		wantReason(t, err, http.StatusConflict, fserr.RealGameLive)
		f.state.EndGame("g-real", EndReasonEnded)
		_, err = f.state.Clear(req)
		wantReason(t, err, http.StatusPreconditionFailed, fserr.WriterMismatch)
		if w, _ := f.realWriter("shield.energy_budget_seconds"); w != "operator" {
			t.Fatalf("writer %q after two refused DELETEs, want operator", w)
		}
	})

	t.Run("UC-043/A18 with unless_real_game too: both preconditions hold, the overlay is cleared", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		f.put(WriteRequest{Set: "game", Flag: "shield.energy_budget_seconds", Value: raw("20"), Scope: realScope, Writer: "promotion"})
		req := ifWriterClear("shield.energy_budget_seconds", "promotion")
		req.UnlessRealGame = true
		res, err := f.state.Clear(req)
		if err != nil || res.Cleared != 1 {
			t.Fatalf("cleared %d, err %v; want 1, nil", res.Cleared, err)
		}
	})
}

// UC: UC-043 A18
func TestIfWriterClearConcurrency(t *testing.T) {
	t.Run("UC-043/A18 compare and delete are atomic: a concurrent other writer's overlay is never removed (run with -race)", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		const rounds = 300
		var wg sync.WaitGroup
		wg.Add(2)
		go func() {
			defer wg.Done()
			for i := 0; i < rounds; i++ {
				w := "promotion"
				if i%2 == 1 {
					w = "operator"
				}
				if _, err := f.state.Put(WriteRequest{Set: "game", Flag: "sensitivity", Variant: "fast", Scope: realScope, Writer: w}); err != nil {
					t.Errorf("put: %v", err)
					return
				}
			}
		}()
		errs := make(chan string, rounds)
		go func() {
			defer wg.Done()
			for i := 0; i < rounds; i++ {
				_, err := f.state.Clear(ifWriterClear("sensitivity", "promotion"))
				if err != nil && err.Reason != fserr.WriterMismatch {
					errs <- err.Error()
				}
			}
		}()
		wg.Wait()
		close(errs)
		for e := range errs {
			t.Errorf("unexpected rejection: %s", e)
		}
		// The last write was the operator's: no later promotion-only clear may remove it.
		if _, err := f.state.Clear(ifWriterClear("sensitivity", "promotion")); err == nil || err.Reason != fserr.WriterMismatch {
			t.Fatalf("final clear: %v, want writer_mismatch", err)
		}
	})
}
