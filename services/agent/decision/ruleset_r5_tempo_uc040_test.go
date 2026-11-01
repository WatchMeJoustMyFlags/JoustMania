package decision

import (
	"context"
	"math/rand/v2"
	"testing"
	"time"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// ruleset_r5_tempo_uc040_test.go covers R5's tempo value (UC-040 A52, BR-041,
// #2487): past the accelerate target R5 slows the music to 1.0, the lower end of
// adjust_music_tempo's bounds, which lowers the death threshold so eliminations
// come sooner. R1 writes the upper end (1.3). When both fire in overlapping
// windows the higher-ranked one holds the tempo: an already-applied winner still
// blocks the lower-ranked rival, so the tempo changes once, when the ranking
// changes, not on every cycle (#2487 review). The already-applied ledger (A35)
// still suppresses a same-value repeat.

func tempoDecision(value string) Decision {
	return Decision{Intervention: InterventionAdjustMusicTempo, Value: value,
		ObjectiveServed: ObjectiveAccelerate, Objectives: balancedFocused()}
}

// UC: UC-040 A52 BR-041
func TestR5SlowsTheTempo(t *testing.T) {
	t.Run("UC-040/A52 BR-041 R5 proposes tempo 1.0, the lower bound", func(t *testing.T) {
		players := map[string]testPlayer{"A": {active: true}, "B": {active: true}}
		cands := accelerateCandidates(gameCtx(80, nil, players), defaultFit(), map[string]float64{ObjectiveAccelerate: 1})
		var got *candidate
		for i := range cands {
			if cands[i].rule == "R5" {
				got = &cands[i]
			}
		}
		if got == nil {
			t.Fatalf("candidates = %v, want R5", interventionsOf(cands))
		}
		if got.decision.Value != "1" {
			t.Fatalf("R5 value = %q, want the literal \"1\" (1.0)", got.decision.Value)
		}
		if normalisedValue(got.decision.Value) != normalisedValue("1.0") {
			t.Fatalf("R5 value %q is not 1.0", got.decision.Value)
		}
	})

	t.Run("UC-040/A52 BR-041 R1 then R5 both write; a same-value R5 repeat is already_applied", func(t *testing.T) {
		l, sink, clock, _ := appliedLoop(t, tempoDecision("1.3"))
		expectOutcome(t, runCycle(l, budgetCtx()), "adjust_music_tempo/:dispatched")
		*clock = clock.Add(time.Second)
		l.Rules = fakeRules{out: []Decision{tempoDecision(accelerateTempo)}}
		expectOutcome(t, runCycle(l, budgetCtx()), "adjust_music_tempo/:dispatched")
		*clock = clock.Add(time.Second)
		l.Rules = fakeRules{out: []Decision{tempoDecision("1.0")}}
		expectOutcome(t, runCycle(l, budgetCtx()), "adjust_music_tempo/:already_applied")
		if n := writesOf(sink, InterventionAdjustMusicTempo, ""); n != 2 {
			t.Fatalf("tempo writes = %d, want 2 (1.3 then 1.0)", n)
		}
	})
}

// tempoMixWeights are weight sets under which R1 (endurance, falling urgency) and
// R5 (accelerate, rising urgency) both rank in the 61..119 s window of a
// 120 s / 60 s session: endurance_focused is a shipped objectives variant.
var tempoMixWeights = map[string]map[string]float64{
	"endurance_focused": {ObjectiveEndurance: 0.7, ObjectiveBalanced: 0.1, ObjectiveAccelerate: 0.1, ObjectiveChaos: 0.1},
	"half_half":         {ObjectiveEndurance: 0.5, ObjectiveAccelerate: 0.5},
}

// tempoMixCtx is the overlap window: 2 active + 2 eliminated players at dur seconds.
func tempoMixCtx(dur float64) gamecontext.GameContext {
	return gameCtx(dur, []string{"D", "E"}, map[string]testPlayer{"A": {active: true}, "B": {active: true}})
}

// tempoChanges counts the value changes in a sequence of written tempo values.
func tempoChanges(values []string) int {
	n := 0
	for i := 1; i < len(values); i++ {
		if normalisedValue(values[i]) != normalisedValue(values[i-1]) {
			n++
		}
	}
	return n
}

// UC: UC-040 A52 BR-041 BR-042
func TestR1R5TempoDoesNotFlap(t *testing.T) {
	for name, w := range tempoMixWeights {
		t.Run("UC-040/A52 BR-042 rules engine: the tempo changes once in the overlap, "+name, func(t *testing.T) {
			cfg := DefaultStaticConfig()
			cfg.ObjectiveWeights = w
			clock := time.Unix(10_000, 0)
			r := newObjectiveRules(cfg, cfg, cfg, func() time.Time { return clock }, rand.New(rand.NewPCG(1, 2)))
			applied := map[string]string{}
			r.SetAppliedSource(func() map[string]string { return applied })
			var written []string
			for dur := 61.0; dur < 120; dur++ {
				perEval := 0
				for _, d := range r.Evaluate(context.Background(), tempoMixCtx(dur)) {
					if d.Intervention != InterventionAdjustMusicTempo {
						continue
					}
					perEval++
					applied[appliedKey(d)] = normalisedValue(d.Value)
					written = append(written, d.Value)
				}
				if perEval > 1 {
					t.Fatalf("at %.0fs one ranking emitted %d tempo decisions, want at most 1", dur, perEval)
				}
				clock = clock.Add(time.Second)
			}
			if len(written) == 0 || normalisedValue(written[0]) != "1.3" {
				t.Fatalf("tempo writes = %v, want R1's 1.3 first", written)
			}
			if n := tempoChanges(written); n != 1 {
				t.Fatalf("tempo writes = %v: %d changes, want exactly 1 (R1 1.3 until R5 outscores it, then R5 1.0)", written, n)
			}
			if normalisedValue(written[len(written)-1]) != "1" {
				t.Fatalf("tempo writes = %v, want R5's 1.0 last", written)
			}
		})
	}

	for _, budget := range []int{8, 20} {
		for name, w := range tempoMixWeights {
			t.Run("UC-040/A52 BR-042 loop: the tempo changes once, not every admitted write, "+name, func(t *testing.T) {
				snap := rulesSnapshot()
				snap.Objectives = w
				snap.Policy = flags.Policy{MaxInterventionsPerMinute: budget}
				snap.InterventionsAllowed = []string{InterventionAdjustMusicTempo, InterventionGrantShield, InterventionSendControllerEffect}
				l, _ := decideRecordingLoop(t, snap)
				clock := time.Unix(50_000, 0)
				l.now = func() time.Time { return clock }
				sink := &capturingSink{}
				l.Actions = sink
				l.SetSuppressReapplied(true)
				cfg := DefaultStaticConfig()
				cfg.ObjectiveWeights = w
				rules := newObjectiveRules(cfg, cfg, cfg, func() time.Time { return clock }, rand.New(rand.NewPCG(1, 2)))
				rules.SetAppliedSource(l.AppliedValues)
				l.Rules = rules
				for dur := 61.0; dur < 120; dur++ {
					runCycle(l, tempoMixCtx(dur))
					clock = clock.Add(time.Second)
				}
				var written []string
				for _, d := range sink.decisions() {
					if d.Intervention == InterventionAdjustMusicTempo {
						written = append(written, d.Value)
					}
				}
				if n := tempoChanges(written); n > 1 {
					t.Fatalf("budget %d: tempo writes = %v: %d changes, want at most 1", budget, written, n)
				}
			})
		}
	}

	t.Run("UC-040/A52 BR-042 an applied winner holds its key against a lower-ranked rival", func(t *testing.T) {
		c := budgetCtx()
		sc := func(v string) scoredCandidate {
			return scoredCandidate{candidate: candidate{decision: tempoDecision(v)}}
		}
		key := appliedKey(tempoDecision(""))
		values := func(in []scoredCandidate) []string {
			var out []string
			for _, s := range in {
				out = append(out, s.decision.Value)
			}
			return out
		}
		cases := []struct {
			name    string
			ranked  []string
			applied map[string]string
			want    []string
		}{
			{"applied winner blocks the rival", []string{"1.3", "1"}, map[string]string{key: "1.3"}, nil},
			{"rival outranks the applied value", []string{"1", "1.3"}, map[string]string{key: "1.3"}, []string{"1"}},
			{"nothing applied: one value per key", []string{"1.3", "1"}, nil, []string{"1.3"}},
		}
		for _, tc := range cases {
			var ranked []scoredCandidate
			for _, v := range tc.ranked {
				ranked = append(ranked, sc(v))
			}
			if got := values(applyAppliedPolicy(ranked, tc.applied, c)); !equalStrings(got, tc.want) {
				t.Fatalf("%s: kept = %v, want %v", tc.name, got, tc.want)
			}
		}
	})
}
