package decision

import (
	"testing"
	"time"
)

// ruleset_r5_tempo_uc040_test.go covers R5's tempo value (UC-040 A52, BR-041,
// #2487): past the accelerate target R5 slows the music to 1.0, the lower end of
// adjust_music_tempo's bounds, which lowers the death threshold so eliminations
// come sooner. R1 writes the upper end (1.3); when both fire in overlapping
// windows the later value wins and the already-applied ledger (A35) still
// suppresses a same-value repeat.

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
