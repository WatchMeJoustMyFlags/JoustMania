package state

import (
	"net/http"
	"strconv"
	"sync"
	"testing"
	"time"

	"github.com/joustmania/flag-service/fserr"
)

// guardRegistry widens two hold keys to scope real (as FR-162 will) and keeps
// sensitivity, a real-scoped key without the attribute, as the control. The
// real registry.yaml keeps the hold keys stack-only, so the guard is inert
// there until the widening PR.
const guardRegistry = `
flags:
  - {set: game, key: shield.activation, kind: overlay, scope: [stack, experiment+arm, real], bounds: {variants: [instant, trigger, hold]}, real_write: no_live_real_game}
  - {set: game, key: shield.energy_budget_seconds, kind: overlay, scope: [stack, experiment+arm, real], bounds: {range: [3, 30]}, real_write: no_live_real_game}
  - {set: game, key: sensitivity, kind: overlay, scope: [stack, experiment+arm, real]}
`

var realScope = Scope{GameKind: "real"}

func budgetReal(value string) WriteRequest {
	return WriteRequest{Set: "game", Flag: "shield.energy_budget_seconds", Value: raw(value), Scope: realScope, Writer: "agent"}
}

func (f *fixture) registerKind(id, kind string) {
	f.t.Helper()
	if _, err := f.state.RegisterGame(Game{ID: id, CoordinatorInstance: "c1", Kind: kind}); err != nil {
		f.t.Fatalf("register %s: %v", id, err)
	}
}

// UC: UC-043 A17
func TestRealWriteGuard(t *testing.T) {
	t.Run("UC-043/A17 a real write of a hold key is refused 409 real_game_live while a real game is registered", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		f.registerKind("g-real", "real")
		_, err := f.state.Put(budgetReal("20"))
		wantReason(t, err, http.StatusConflict, fserr.RealGameLive)
		_, err = f.state.Put(WriteRequest{Set: "game", Flag: "shield.activation", Variant: "hold", Scope: realScope})
		wantReason(t, err, http.StatusConflict, fserr.RealGameLive)
		_, err = f.state.Put(WriteRequest{Set: "game", Flag: "shield.activation", Variant: "hold", Scope: realScope, DryRun: true})
		wantReason(t, err, http.StatusConflict, fserr.RealGameLive)
		if v, _ := f.resolve("shield.energy_budget_seconds", map[string]any{"game_kind": "real"}); v != "default" {
			t.Fatalf("the refused write changed the real default: variant %q", v)
		}
	})

	t.Run("UC-043/A17 the guard covers scope real only, and only keys carrying the attribute", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		f.registerKind("g-real", "real")
		f.put(WriteRequest{Set: "game", Flag: "shield.energy_budget_seconds", Value: raw("12"), Scope: Scope{ExperimentID: "e1", Arm: "treatment"}})
		f.put(WriteRequest{Set: "game", Flag: "shield.energy_budget_seconds", Value: raw("12")}) // stack: operator on stage
		f.put(WriteRequest{Set: "game", Flag: "sensitivity", Variant: "fast", Scope: realScope})
	})

	t.Run("UC-043/A17 a shadow game does not block the real write", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		f.registerKind("g-shadow", "shadow")
		f.put(budgetReal("20"))
		if _, val := f.resolve("shield.energy_budget_seconds", map[string]any{"game_kind": "real"}); val != float64(20) {
			t.Fatalf("real default = %v, want 20", val)
		}
	})

	for _, kind := range []string{"", "primary", "Shadow"} {
		t.Run("UC-043/A17 the guard fails closed: a game of kind "+strconv.Quote(kind)+" blocks the real write", func(t *testing.T) {
			f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
			f.registerKind("g-shadow", "shadow")
			f.registerKind("g-other", kind)
			_, err := f.state.Put(budgetReal("20"))
			wantReason(t, err, http.StatusConflict, fserr.RealGameLive)
		})
	}

	t.Run("UC-043/A17 ttl_seconds on a real write of a guarded key is 400 malformed, with no game registered", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		req := budgetReal("20")
		req.TTLSeconds = ttl(60)
		_, err := f.state.Put(req)
		wantReason(t, err, http.StatusBadRequest, fserr.Malformed)
		// A key without the attribute keeps its expiring real overlay.
		f.put(WriteRequest{Set: "game", Flag: "sensitivity", Variant: "fast", Scope: realScope, TTLSeconds: ttl(60)})
	})

	ends := []struct {
		name string
		end  func(f *fixture)
	}{
		{"ended", func(f *fixture) { f.state.EndGame("g-real", EndReasonEnded) }},
		{"reconciled away", func(f *fixture) {
			if _, err := f.state.Reconcile("c1", nil); err != nil {
				f.t.Fatal(err)
			}
		}},
		{"expired", func(f *fixture) {
			f.clock.Advance(f.state.opts.MaxGameAge + time.Second)
			f.state.ExpireGames()
		}},
	}
	for _, e := range ends {
		t.Run("UC-043/A17 the real write is accepted once the real game is "+e.name, func(t *testing.T) {
			f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
			f.registerKind("g-real", "real")
			_, err := f.state.Put(budgetReal("20"))
			wantReason(t, err, http.StatusConflict, fserr.RealGameLive)
			e.end(f)
			f.put(budgetReal("20"))
		})
	}

	t.Run("UC-043/A17 a re-registration that turns a shadow game real blocks the write", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		f.registerKind("g1", "shadow")
		f.put(budgetReal("20"))
		f.registerKind("g1", "real")
		_, err := f.state.Put(budgetReal("25"))
		wantReason(t, err, http.StatusConflict, fserr.RealGameLive)
	})

	t.Run("UC-043/A17 a DELETE of the real overlay (kill switch or any revert) is never refused", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		f.put(budgetReal("20"))
		f.registerKind("g-real", "real")
		res, err := f.state.Clear(ClearRequest{Set: "game", Flag: "shield.energy_budget_seconds", Scope: realScope, Writer: "agent"})
		if err != nil {
			t.Fatalf("DELETE of the real overlay refused while a real game runs: %v", err)
		}
		if res.Cleared != 1 {
			t.Fatalf("cleared %d values, want 1", res.Cleared)
		}
		if v, _ := f.resolve("shield.energy_budget_seconds", map[string]any{"game_kind": "real"}); v != "default" {
			t.Fatalf("real default still overridden after the DELETE: variant %q", v)
		}
	})
}

// UC: UC-043 A17
func TestRealWriteGuardConcurrency(t *testing.T) {
	t.Run("UC-043/A17 the check runs under the registration lock: a game registered while a Put waits for the lock blocks it", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		f.state.mu.Lock()
		done := make(chan *fserr.Error, 1)
		go func() {
			_, err := f.state.Put(budgetReal("20"))
			done <- err
		}()
		// The Put cannot run until the lock is released; the registration
		// made under the same lock must be visible to it.
		time.Sleep(20 * time.Millisecond)
		f.state.registerLocked(Game{ID: "g-real", CoordinatorInstance: "c1", Kind: "real"})
		f.state.mu.Unlock()
		wantReason(t, <-done, http.StatusConflict, fserr.RealGameLive)
	})

	t.Run("UC-043/A17 concurrent RegisterGame, EndGame and Put: every write is accepted or refused real_game_live (run with -race)", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		const rounds = 300
		var wg sync.WaitGroup
		wg.Add(2)
		go func() {
			defer wg.Done()
			for i := 0; i < rounds; i++ {
				if _, err := f.state.RegisterGame(Game{ID: "g-real", CoordinatorInstance: "c1", Kind: "real"}); err != nil {
					t.Errorf("register: %v", err)
					return
				}
				f.state.EndGame("g-real", EndReasonEnded)
			}
		}()
		errs := make(chan string, rounds)
		go func() {
			defer wg.Done()
			for i := 0; i < rounds; i++ {
				_, err := f.state.Put(budgetReal("20"))
				if err != nil && err.Reason != fserr.RealGameLive {
					errs <- err.Error()
				}
			}
		}()
		wg.Wait()
		close(errs)
		for e := range errs {
			t.Errorf("unexpected rejection: %s", e)
		}
	})
}

// UC: UC-043 A17
func TestRealWriteGuard_LongReconciledGame(t *testing.T) {
	t.Run("UC-043/A17 a real game past 1 h that keeps being reconciled keeps blocking the write", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{registryYAML: guardRegistry})
		f.registerKind("g-real", "real")
		for i := 0; i < 15; i++ { // 75 min, reconciled every 5 min
			f.clock.Advance(5 * time.Minute)
			f.state.ExpireGames() // the expiry sweep runs between two reconciles
			_, err := f.state.Put(budgetReal("20"))
			wantReason(t, err, http.StatusConflict, fserr.RealGameLive)
			if _, err := f.state.Reconcile("c1", []Game{{ID: "g-real", Kind: "real"}}); err != nil {
				t.Fatal(err)
			}
		}
	})
}
