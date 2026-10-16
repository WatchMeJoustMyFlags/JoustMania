package decision

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// fakePatternObserver records what the loop reports for a pattern game.
type fakePatternObserver struct {
	mu        sync.Mutex
	active    bool
	reasons   []string
	decisions []string
	slot      int
}

func (f *fakePatternObserver) Active() bool { return f.active }
func (f *fakePatternObserver) MarkNonExact(reason string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.reasons = append(f.reasons, reason)
}
func (f *fakePatternObserver) RecordDecision(intervention, _ string, backend string) (int, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.decisions = append(f.decisions, intervention+"@"+backend)
	return f.slot, true
}

func activeContext(serials ...string) gamecontext.GameContext {
	c := gamecontext.GameContext{Players: map[string]*gamecontext.PlayerSignals{}}
	on := true
	for _, s := range serials {
		c.Players[s] = &gamecontext.PlayerSignals{Serial: s, Active: &on}
	}
	return c
}

// UC: UC-046 main BR-007
func TestChaosSeedSource(t *testing.T) {
	c := activeContext("MOCK0000", "MOCK0001", "MOCK0002")
	draws := func(r *ObjectiveRules) []float64 {
		r.mu.Lock()
		defer r.mu.Unlock()
		out := make([]float64, 0, 10)
		for i := 0; i < 5; i++ {
			roll, target := r.chaosRoll(c)
			out = append(out, roll, float64(len(target)))
		}
		return out
	}
	t.Run("UC-046/BR-007 two engines with the same game seed draw the same chaos sequence", func(t *testing.T) {
		a, b := NewObjectiveRulesLive(nil), NewObjectiveRulesLive(nil)
		src := func() (uint64, bool) { return 12345, true }
		a.SetChaosSeedSource(src)
		b.SetChaosSeedSource(src)
		da, db := draws(a), draws(b)
		for i := range da {
			if da[i] != db[i] {
				t.Fatalf("draw %d differs: %v vs %v", i, da, db)
			}
		}
		if a.ChaosDraws() != 5 {
			t.Errorf("ChaosDraws = %d, want 5", a.ChaosDraws())
		}
	})
	t.Run("UC-046/BR-007 the engine seeds once the game is registered, not before", func(t *testing.T) {
		registered := false
		r := NewObjectiveRulesLive(nil)
		r.SetChaosSeedSource(func() (uint64, bool) { return 99, registered })
		draws(r)
		r.mu.Lock()
		seededBefore := r.seeded
		r.mu.Unlock()
		registered = true
		draws(r)
		r.mu.Lock()
		seededAfter := r.seeded
		r.mu.Unlock()
		if seededBefore || !seededAfter {
			t.Errorf("seeded before=%v after=%v, want false then true", seededBefore, seededAfter)
		}
	})
	t.Run("UC-046/BR-007 without a source the engine never seeds (non-pattern games unchanged)", func(t *testing.T) {
		r := NewObjectiveRulesLive(nil)
		draws(r)
		if r.seeded {
			t.Error("engine seeded without a source")
		}
	})
}

// UC: UC-046 A7 BR-008
func TestPatternObserver_RemoteBackendMarksNonExact(t *testing.T) {
	t.Run("UC-046/A7 a remote backend decision in a pattern game marks remote_backend", func(t *testing.T) {
		snap := rulesSnapshot()
		snap.Decision.Backend = "claude"
		l, _ := decideRecordingLoop(t, snap)
		obs := &fakePatternObserver{active: true}
		l.SetPatternObserver(obs)
		l.decide(context.Background(), snap, activeContext("MOCK0000"), EvalTrigger{})
		if len(obs.reasons) != 1 || obs.reasons[0] != "remote_backend" {
			t.Fatalf("reasons = %v, want [remote_backend]", obs.reasons)
		}
	})
	t.Run("UC-046/A7 the rules backend or a non-pattern game marks nothing", func(t *testing.T) {
		l, _ := decideRecordingLoop(t, rulesSnapshot())
		obs := &fakePatternObserver{active: true}
		l.SetPatternObserver(obs)
		l.decide(context.Background(), rulesSnapshot(), activeContext("MOCK0000"), EvalTrigger{})
		snap := rulesSnapshot()
		snap.Decision.Backend = "claude"
		inactive := &fakePatternObserver{active: false}
		l.SetPatternObserver(inactive)
		l.decide(context.Background(), snap, activeContext("MOCK0000"), EvalTrigger{})
		if len(obs.reasons)+len(inactive.reasons) != 0 {
			t.Fatalf("marked %v / %v", obs.reasons, inactive.reasons)
		}
	})
}

// UC: UC-046 main
func TestPatternObserver_RecordsDecisionsWithTargetSlot(t *testing.T) {
	t.Run("UC-046/main a decision in a pattern game is recorded with pattern.target_slot", func(t *testing.T) {
		snap := rulesSnapshot()
		l, sr := decideRecordingLoop(t, snap)
		l.Actions = &capturingSink{}
		rules := NewObjectiveRulesLive(nil)
		l.Rules = rules
		obs := &fakePatternObserver{active: true, slot: 2}
		l.SetPatternObserver(obs)
		d := Decision{Intervention: InterventionSendControllerEffect, TargetSerial: "MOCK0002"}
		l.runDecision(context.Background(), snap, activeContext("MOCK0002"), d, time.Now(), &LayerState{})
		if len(obs.decisions) != 1 || obs.decisions[0] != InterventionSendControllerEffect+"@rules" {
			t.Fatalf("decisions = %v", obs.decisions)
		}
		var found bool
		for _, s := range sr.Ended() {
			if s.Name() != SpanDecision {
				continue
			}
			for _, kv := range s.Attributes() {
				if string(kv.Key) == AttrPatternTargetSlot && kv.Value.AsInt64() == 2 {
					found = true
				}
			}
		}
		if !found {
			t.Error("agent.decision span lacks pattern.target_slot=2")
		}
	})
	t.Run("UC-046/main nothing is recorded outside pattern games", func(t *testing.T) {
		l, _ := decideRecordingLoop(t, rulesSnapshot())
		l.Actions = &capturingSink{}
		obs := &fakePatternObserver{active: false}
		l.SetPatternObserver(obs)
		l.runDecision(context.Background(), rulesSnapshot(), activeContext("A"), Decision{Intervention: InterventionSendControllerEffect, TargetSerial: "A"}, time.Now(), &LayerState{})
		if len(obs.decisions) != 0 {
			t.Fatalf("recorded %v", obs.decisions)
		}
	})
}

// UC: UC-046 BR-007 BR-008
func TestChaosSeedSource_LateSeedIsNonExact(t *testing.T) {
	t.Run("UC-046/BR-007 chaos draws before the seed arrived mark the game unseeded_draws", func(t *testing.T) {
		c := activeContext("A", "B")
		registered := false
		late := 0
		r := NewObjectiveRulesLive(nil)
		r.SetChaosSeedSource(func() (uint64, bool) { return 1, registered })
		r.SetLateSeedHook(func() { late++ })
		r.mu.Lock()
		r.chaosRoll(c) // drawn from the time-seeded RNG
		r.mu.Unlock()
		registered = true
		r.mu.Lock()
		r.chaosRoll(c)
		r.chaosRoll(c)
		r.mu.Unlock()
		if late != 1 {
			t.Fatalf("late-seed hook called %d times, want 1", late)
		}
		fresh := NewObjectiveRulesLive(nil)
		fresh.SetChaosSeedSource(func() (uint64, bool) { return 1, true })
		fresh.SetLateSeedHook(func() { late++ })
		fresh.mu.Lock()
		fresh.chaosRoll(c)
		fresh.mu.Unlock()
		if late != 1 {
			t.Error("a game seeded before its first draw was marked late")
		}
	})
}
