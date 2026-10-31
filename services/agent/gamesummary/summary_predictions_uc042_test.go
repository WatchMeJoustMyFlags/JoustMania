package gamesummary

import (
	"encoding/json"
	"math"
	"strings"
	"testing"
)

// predictionFixture: a hit and a miss of the rules (acted), a hit of the model
// that was held (counterfactual), an unscorable one, and two controls. The
// registration times are offsets from the game's first timeline event (base).
func predictionFixture() []PredictionOutcome {
	return []PredictionOutcome{
		{ID: "c1", RegisteredAt: at(20), Backend: "rules", Source: PredictionSourceControl, Template: "survives", Scope: "target", Target: "AA:11", HorizonS: 15, P: 0.5, Outcome: PredictionHit},
		{ID: "r2", RegisteredAt: at(30), Backend: "rules", Source: "rules", Template: "survives", Scope: "target", Target: "AA:11", HorizonS: 15, P: 0.8, Rule: "R4", Acted: true, Outcome: PredictionMiss},
		{ID: "r1", RegisteredAt: at(10), Backend: "rules", Source: "rules", Template: "survives", Scope: "target", Target: "BB:22", HorizonS: 15, P: 0.6, Rule: "R4", Acted: true, Outcome: PredictionHit},
		{ID: "m1", RegisteredAt: at(40), Backend: "claude", Source: "model", Template: "no_elimination", HorizonS: 15, P: 0.9, Acted: false, Outcome: PredictionHit},
		{ID: "r3", RegisteredAt: at(50), Backend: "rules", Source: "rules", Template: "survives", Scope: "target", Target: "BB:22", HorizonS: 15, P: 0.7, Rule: "R4", Acted: true, Outcome: PredictionUnscorable, UnscorableReason: "game_ended"},
	}
}

func tallyOf(t *testing.T, tallies []PredictionTally, backend, source, template string, acted bool) PredictionTally {
	t.Helper()
	for _, x := range tallies {
		if x.Backend == backend && x.Source == source && x.Template == template && x.Acted == acted {
			return x
		}
	}
	t.Fatalf("no tally for %s/%s/%s acted=%t in %+v", backend, source, template, acted, tallies)
	return PredictionTally{}
}

// UC: UC-042 A12 BR-009
func TestBuildSummary_Predictions(t *testing.T) {
	snap := representativeGame(t)
	s := BuildSummary(snap, BuildOptions{Now: at(61), Predictions: predictionFixture()})

	t.Run("UC-042/A12 predictions are ordered with offsets from the game start", func(t *testing.T) {
		var ids []string
		for _, p := range s.Predictions {
			ids = append(ids, p.ID)
		}
		if got := strings.Join(ids, ","); got != "r1,c1,r2,m1,r3" {
			t.Fatalf("order = %s, want r1,c1,r2,m1,r3", got)
		}
		// representativeGame starts at base (+0s), so the offset is the registration second.
		if s.Predictions[0].OffsetSeconds != 10 || s.Predictions[4].OffsetSeconds != 50 {
			t.Errorf("offsets = %v / %v, want 10 / 50", s.Predictions[0].OffsetSeconds, s.Predictions[4].OffsetSeconds)
		}
	})

	t.Run("UC-042/A12 BR-009 tally keeps acted and counterfactual apart", func(t *testing.T) {
		rules := tallyOf(t, s.PredictionTally, "rules", "rules", "survives", true)
		if rules.Hits != 1 || rules.Misses != 1 || rules.Unscorable != 1 {
			t.Errorf("rules survives = %+v, want 1 hit, 1 miss, 1 unscorable", rules)
		}
		// Brier over hit+miss only: (0.6-1)^2 + 0.8^2 = 0.16 + 0.64; the unscorable adds nothing.
		if math.Abs(rules.BrierSum-0.8) > 1e-9 {
			t.Errorf("rules brier_sum = %v, want 0.8", rules.BrierSum)
		}
		held := tallyOf(t, s.PredictionTally, "claude", "model", "no_elimination", false)
		if held.Hits != 1 {
			t.Errorf("counterfactual model = %+v, want 1 hit", held)
		}
		for _, x := range s.PredictionTally {
			if x.Backend == "claude" && x.Acted {
				t.Errorf("a held decision leaked into the acted tally: %+v", x)
			}
		}
		control := tallyOf(t, s.PredictionTally, "rules", PredictionSourceControl, "survives", false)
		if control.Hits != 1 || control.Misses != 0 {
			t.Errorf("control = %+v, want 1 hit", control)
		}
	})

	t.Run("UC-042/A12 the span counts split hit, miss and unscorable", func(t *testing.T) {
		n := CountPredictions(s.Predictions)
		if n.Scored != 4 || n.Hit != 3 || n.Miss != 1 || n.Unscorable != 1 {
			t.Errorf("counts = %+v, want scored 4, hit 3, miss 1, unscorable 1", n)
		}
	})
}

// UC: UC-042 A12
// A game without predictions serializes exactly as before (additive, omitempty).
func TestBuildSummary_NoPredictionsOmitted(t *testing.T) {
	s := BuildSummary(representativeGame(t), BuildOptions{Now: at(61)})
	raw, err := json.Marshal(s)
	if err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{`"predictions"`, `"prediction_tally"`} {
		if strings.Contains(string(raw), key) {
			t.Errorf("UC-042/A12: summary without predictions has %s", key)
		}
	}
	if s.SchemaVersion != SchemaVersion {
		t.Errorf("schema version changed")
	}
}

// UC: UC-042 A12 BR-009
// The coordinator reads the lookup by game id and the summary span carries the counts.
func TestCoordinator_PredictionsOnSpan(t *testing.T) {
	c, sr, _ := recordingCoordinator(t)
	snap := representativeGame(t)
	var asked string
	c.SetPredictionLookup(func(gameID string) []PredictionOutcome {
		asked = gameID
		return predictionFixture()
	})
	c.OnGameEnd(snap)
	if asked != snap.SessionID {
		t.Fatalf("lookup asked for %q, want %q", asked, snap.SessionID)
	}
	spans := spansByName(sr.Ended(), SpanGameSummary)
	if len(spans) != 1 {
		t.Fatalf("%d summary spans, want 1", len(spans))
	}
	for key, want := range map[string]int64{
		"summary.predictions.scored": 4, "summary.predictions.hit": 3,
		"summary.predictions.miss": 1, "summary.predictions.unscorable": 1,
	} {
		v, ok := spanAttr(spans[0], key)
		if !ok || v.AsInt64() != want {
			t.Errorf("UC-042/A12 %s = %v (present %t), want %d", key, v.AsInt64(), ok, want)
		}
	}

	// No lookup: no predictions attributes.
	c2, sr2, _ := recordingCoordinator(t)
	c2.OnGameEnd(representativeGame(t))
	if _, ok := spanAttr(spansByName(sr2.Ended(), SpanGameSummary)[0], "summary.predictions.scored"); ok {
		t.Errorf("UC-042/A12: span carries summary.predictions.* without a lookup")
	}
}
