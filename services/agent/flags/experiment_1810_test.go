package flags

import (
	"context"
	"errors"
	"testing"
	"time"
)

// #1810 (FR-150, C-035, flags.md §15): experiment_completed_grace_seconds is
// tuning: the flag wins, 0 is the `off` variant (never floored to the default),
// and only an unresolved flag falls back to the caller's compiled default.
func TestExperiment_CompletedGrace(t *testing.T) {
	def := ExperimentDefaults{CompletedGrace: 45 * time.Second}
	cases := []struct {
		name string
		ev   stubEvaluator
		def  ExperimentDefaults
		want time.Duration
	}{
		{"flag wins over the default", stubEvaluator{ints: map[string]int64{keyExperimentCompletedGrace: 30}}, def, 30 * time.Second},
		{"flag off variant 0 stays off", stubEvaluator{ints: map[string]int64{keyExperimentCompletedGrace: 0}}, def, 0},
		{"unresolved flag falls back to the default", stubEvaluator{errs: map[string]error{keyExperimentCompletedGrace: errors.New("flagd down")}}, def, 45 * time.Second},
		{"unresolved flag with default 0 stays off", stubEvaluator{errs: map[string]error{keyExperimentCompletedGrace: errors.New("flagd down")}}, ExperimentDefaults{}, 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := New(tc.ev, nil).Experiment(context.Background(), tc.def).CompletedGrace; got != tc.want {
				t.Fatalf("CompletedGrace = %v, want %v", got, tc.want)
			}
		})
	}
}

// #1810 (C-034, FR-152 d): the flag half of the proposer gate never falls back to
// a default: an unresolved flag reads false (closed), in both getters.
// UC: UC-042 A3 BR-002
func TestExperimentProposerEnabled_FailsClosed(t *testing.T) {
	cases := []struct {
		name string
		ev   stubEvaluator
		want bool
	}{
		{"UC-042/BR-002 flag on", stubEvaluator{booleans: map[string]bool{keyExperimentProposerEnabled: true}}, true},
		{"UC-042/BR-002 flag off", stubEvaluator{booleans: map[string]bool{keyExperimentProposerEnabled: false}}, false},
		{"UC-042/BR-002 unresolved is closed", stubEvaluator{errs: map[string]error{keyExperimentProposerEnabled: errors.New("flagd down")}}, false},
		{"UC-042/BR-002 missing is closed", stubEvaluator{}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := New(tc.ev, nil)
			if got := f.Experiment(context.Background(), ExperimentDefaults{}).ProposerEnabled; got != tc.want {
				t.Fatalf("Experiment().ProposerEnabled = %v, want %v", got, tc.want)
			}
			if got := f.ExperimentProposerEnabled(context.Background()); got != tc.want {
				t.Fatalf("ExperimentProposerEnabled = %v, want %v", got, tc.want)
			}
		})
	}
}
