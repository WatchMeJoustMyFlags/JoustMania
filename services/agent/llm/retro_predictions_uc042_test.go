package llm

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/joustmania/agent/gamesummary"
)

// retroPredictionOutcomes is a mixed game: a rules and a model prediction (one hit,
// one miss, one counterfactual, one unscorable) and controls for two templates.
func retroPredictionOutcomes() []gamesummary.PredictionOutcome {
	return []gamesummary.PredictionOutcome{
		{ID: "p1", OffsetSeconds: 12, Backend: "rules", Source: "rules", Template: "survives", Scope: "target", Target: "AA:11", HorizonS: 15, P: 0.7, Rule: "R4", Acted: true, Outcome: "hit"},
		{ID: "p2", OffsetSeconds: 40, Backend: "claude", Source: "model", Template: "no_elimination", HorizonS: 15, P: 0.9, Acted: true, Outcome: "miss"},
		{ID: "p3", OffsetSeconds: 55, Backend: "claude", Source: "lever_default", Template: "activity_rises", Scope: "target", Target: "BB:22", HorizonS: 10, P: 0.6, Acted: false, Outcome: "unscorable", UnscorableReason: "game_ended"},
		{ID: "c1", OffsetSeconds: 15, Backend: "rules", Source: "control", Template: "survives", Scope: "target", Target: "AA:11", HorizonS: 15, P: 0.5, Outcome: "hit"},
		{ID: "c2", OffsetSeconds: 30, Backend: "rules", Source: "control", Template: "survives", Scope: "target", Target: "BB:22", HorizonS: 15, P: 0.5, Outcome: "miss"},
		{ID: "c3", OffsetSeconds: 45, Backend: "rules", Source: "control", Template: "survives", Scope: "target", Target: "AA:11", HorizonS: 15, P: 0.5, Outcome: "hit"},
		{ID: "c4", OffsetSeconds: 15, Backend: "rules", Source: "control", Template: "game_ends", HorizonS: 60, P: 0.5, Outcome: "unscorable", UnscorableReason: "game_ended"},
	}
}

func retroWithPredictions(outcomes []gamesummary.PredictionOutcome) RetroPrompt {
	ctx := retroThreePlayers()
	summary := gamesummary.BuildSummary(ctx, gamesummary.BuildOptions{Now: fixedNow})
	summary.Predictions = outcomes
	summary.PredictionTally = gamesummary.TallyPredictions(outcomes)
	return BuildRetro(RetroInput{Snapshot: retroSnapshot(), Context: ctx, Summary: summary, Now: fixedNow})
}

// UC: UC-042 A12 BR-009
// The retro prompt of a game with predictions pins the "Predictions vs outcomes"
// block and the calibration sentence (golden).
func TestRetroPredictionsGolden(t *testing.T) {
	got := renderRetroGolden(retroWithPredictions(retroPredictionOutcomes()))
	path := filepath.Join("testdata", "retro_predictions.golden")
	if *update {
		if err := os.WriteFile(path, []byte(got), 0o644); err != nil {
			t.Fatalf("write golden: %v", err)
		}
		return
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read golden (run with -update to create): %v", err)
	}
	if got != string(want) {
		t.Errorf("UC-042/A12 retro predictions mismatch\n--- got ---\n%s\n--- want ---\n%s", got, want)
	}
}

// UC: UC-042 A12 BR-009
// A game without predictions gets neither the block nor the sentence, so its retro
// prompt is the one it always had (the existing goldens pin the bytes).
func TestRetroPredictionsAbsentWhenEmpty(t *testing.T) {
	p := retroWithPredictions(nil)
	for _, s := range []string{"Predictions vs outcomes", "calibrated"} {
		if strings.Contains(p.System, s) || strings.Contains(p.User, s) {
			t.Errorf("UC-042/A12 empty: prompt mentions %q", s)
		}
	}
	with := retroWithPredictions(retroPredictionOutcomes())
	if !strings.Contains(with.System, "calibrated") || !strings.Contains(with.User, "Predictions vs outcomes") {
		t.Errorf("UC-042/A12: calibration sentence or block missing")
	}
	if !strings.HasPrefix(with.System, p.System) {
		t.Errorf("UC-042/A12: the calibration sentence must only append to the System prompt")
	}
}
