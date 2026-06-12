package gamewindow

import (
	"strings"
	"testing"

	"github.com/joustmania/agent/gamesummary"
)

func f64(v float64) *float64 { return &v }
func intp(v int) *int        { return &v }

func TestRenderEmptyWindow(t *testing.T) {
	got := Render(nil)
	if !strings.Contains(got, blockHeader) {
		t.Errorf("empty render missing header %q:\n%s", blockHeader, got)
	}
	if !strings.Contains(got, "(no prior games)") {
		t.Errorf("empty render missing empty marker:\n%s", got)
	}
	// nil and empty slice render identically.
	if Render([]gamesummary.Summary{}) != got {
		t.Errorf("nil and empty-slice renders differ")
	}
}

func TestRenderDeterministic(t *testing.T) {
	in := []gamesummary.Summary{
		{
			GameKind:          "real",
			GameMode:          "joust",
			DurationSeconds:   f64(92),
			PlayerCount:       4,
			EliminationCount:  3,
			InterventionCount: 2,
			Dominance:         gamesummary.Dominance{Dominated: true, Serial: "AA11", Heuristic: "survivor_outlasted_mean_2x", Margin: 2.5},
			Interventions: []gamesummary.InterventionOutcome{
				{Intervention: "grant_shield", TargetSerial: "BB22", Dispatched: true,
					Effect: &gamesummary.InterventionEffect{MeanIntensityDelta: f64(0.12), ActivePlayersDelta: intp(1)}},
			},
		},
	}
	first := Render(in)
	for i := 0; i < 5; i++ {
		if got := Render(in); got != first {
			t.Fatalf("Render not deterministic on call %d:\n--- first ---\n%s\n--- got ---\n%s", i, first, got)
		}
	}
}

func TestRenderPopulatedBlock(t *testing.T) {
	in := []gamesummary.Summary{
		{ // oldest
			GameKind: "shadow", GameMode: "ffa", DurationSeconds: f64(40),
			PlayerCount: 6, EliminationCount: 5, InterventionCount: 0,
		},
		{ // newest
			GameKind: "real", GameMode: "joust", DurationSeconds: f64(92),
			PlayerCount: 4, EliminationCount: 3, InterventionCount: 2,
			Dominance: gamesummary.Dominance{Dominated: true, Serial: "AA11", Heuristic: "survivor_outlasted_mean_2x", Margin: 2.5},
			Interventions: []gamesummary.InterventionOutcome{
				{Intervention: "grant_shield", TargetSerial: "BB22", Dispatched: true,
					Effect: &gamesummary.InterventionEffect{MeanIntensityDelta: f64(0.12)}},
				{Intervention: "end_game", Dispatched: false},
			},
		},
	}
	got := Render(in)

	if !strings.Contains(got, blockHeader) {
		t.Errorf("missing header:\n%s", got)
	}
	// Header counts of both games.
	if !strings.Contains(got, "shadow/ffa duration=40.0s players=6 eliminations=5 interventions=0") {
		t.Errorf("missing oldest game header line:\n%s", got)
	}
	if !strings.Contains(got, "real/joust duration=92.0s players=4 eliminations=3 interventions=2") {
		t.Errorf("missing newest game header line:\n%s", got)
	}
	// Dominance line (only the dominated game).
	if !strings.Contains(got, "dominated_by=AA11 (survivor_outlasted_mean_2x, margin=2.5)") {
		t.Errorf("missing dominance line:\n%s", got)
	}
	// Interventions: dispatched with effect, and blocked.
	if !strings.Contains(got, "intervention=grant_shield->BB22 dispatched [intensity_delta=0.12]") {
		t.Errorf("missing dispatched intervention line:\n%s", got)
	}
	if !strings.Contains(got, "intervention=end_game blocked") {
		t.Errorf("missing blocked session-scoped intervention line:\n%s", got)
	}

	// Order: oldest (ffa) appears BEFORE newest (joust).
	oldestIdx := strings.Index(got, "shadow/ffa")
	newestIdx := strings.Index(got, "real/joust")
	if oldestIdx < 0 || newestIdx < 0 || oldestIdx > newestIdx {
		t.Errorf("order wrong: oldest must render before newest (oldest=%d newest=%d):\n%s", oldestIdx, newestIdx, got)
	}
	// Ordinals are 1-based in window position.
	if !strings.Contains(got, "1. shadow/ffa") || !strings.Contains(got, "2. real/joust") {
		t.Errorf("ordinals wrong (want 1. oldest, 2. newest):\n%s", got)
	}
}

func TestRenderUnknownFields(t *testing.T) {
	// A summary with unobserved kind/mode/duration renders the "unknown" sentinels.
	got := Render([]gamesummary.Summary{{PlayerCount: 2}})
	if !strings.Contains(got, "unknown/unknown duration=unknown players=2") {
		t.Errorf("unknown sentinels not rendered:\n%s", got)
	}
}
