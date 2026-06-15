package promote

import (
	"context"
	"errors"
	"testing"

	"github.com/joustmania/agent/experiment"
)

// watcher_test.go proves the autonomous safety net's decision logic (#1016): given
// FAKE real-game telemetry (no real games exist in a unit test), the fitnessWatcher
// declares degradation per the WatchPolicy, and the Promoter auto-reverts a
// regressing autonomous promotion (OutcomeRevert) while keeping one that holds.

// fakeRealGameFitness returns canned recent real-game fitness samples (or an error)
// for the watcher to judge — the unit-test stand-in for the #731 evaluator /
// gamewindow source.
type fakeRealGameFitness struct {
	samples []float64
	err     error
}

func (f fakeRealGameFitness) Recent(_ context.Context, _ string, n int) ([]float64, error) {
	if f.err != nil {
		return nil, f.err
	}
	if n < len(f.samples) {
		return f.samples[:n], nil
	}
	return f.samples, nil
}

func TestFitnessWatcherDegradationVerdict(t *testing.T) {
	policy := WatchPolicy{MinGames: 3, DegradationMargin: 0.05, WindowGames: 5}
	const baseline = 0.70

	cases := []struct {
		name    string
		samples []float64
		fitErr  error
		wantDeg bool
		wantErr bool
	}{
		{
			name:    "clear regression below baseline-margin reverts",
			samples: []float64{0.50, 0.52, 0.48, 0.51}, // mean ~0.5025, drop ~0.20 > 0.05
			wantDeg: true,
		},
		{
			name:    "fitness holds at baseline is kept",
			samples: []float64{0.70, 0.71, 0.69, 0.70},
			wantDeg: false,
		},
		{
			name:    "fitness improves is kept",
			samples: []float64{0.80, 0.82, 0.78},
			wantDeg: false,
		},
		{
			name:    "drop within margin (noise) is kept",
			samples: []float64{0.68, 0.67, 0.69}, // mean ~0.68, drop ~0.02 <= 0.05
			wantDeg: false,
		},
		{
			name:    "insufficient games does not revert (no false positive on one unlucky game)",
			samples: []float64{0.10, 0.10}, // terrible but only 2 < MinGames(3)
			wantDeg: false,
		},
		{
			name:    "telemetry error fails closed (error surfaced for fail-safe revert)",
			fitErr:  errors.New("telemetry unreachable"),
			wantErr: true,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			w := NewFitnessWatcher(fakeRealGameFitness{samples: tc.samples, err: tc.fitErr}, policy, discardLogger())
			deg, err := w.Degraded(context.Background(), "death_grace_period_ms", baseline)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("Degraded err = nil, want an error (fail closed)")
				}
				return
			}
			if err != nil {
				t.Fatalf("Degraded err = %v, want nil", err)
			}
			if deg != tc.wantDeg {
				t.Errorf("Degraded = %v, want %v", deg, tc.wantDeg)
			}
		})
	}
}

func TestWatchPolicyNormalizeDefaults(t *testing.T) {
	got := WatchPolicy{}.normalize()
	want := DefaultWatchPolicy()
	if got.MinGames != want.MinGames || got.DegradationMargin != want.DegradationMargin || got.WindowGames != want.WindowGames {
		t.Errorf("zero policy normalized to %+v, want defaults %+v", got, want)
	}

	// WindowGames is never smaller than MinGames.
	p := WatchPolicy{MinGames: 8, DegradationMargin: 0.1, WindowGames: 2}.normalize()
	if p.WindowGames < p.MinGames {
		t.Errorf("WindowGames %d < MinGames %d after normalize", p.WindowGames, p.MinGames)
	}
}

// --- END-TO-END watch→revert: a Promoter wired with the concrete fitnessWatcher
// over FAKE real-game telemetry auto-reverts a regressing autonomous promotion and
// records outcome=reverted (the #1016 / #936 AC). ---
func TestPromoteAutonomousAutoRevertsViaFitnessWatcher(t *testing.T) {
	realDef := &recordingRealDefault{}
	rev := &recordingReverter{}
	// Real-game fitness collapses well below the baseline (0.50 in testEvidence).
	regressing := fakeRealGameFitness{samples: []float64{0.20, 0.18, 0.22, 0.19}}
	watcher := NewFitnessWatcher(regressing, DefaultWatchPolicy(), discardLogger())
	p, rec := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, realDef, watcher, rev)

	res := p.Promote(context.Background(), promoteProposal, promoteVerdict,
		Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}, testEvidence())

	if res.Outcome != OutcomeReverted {
		t.Fatalf("outcome = %q, want reverted", res.Outcome)
	}
	if len(realDef.calls) != 1 {
		t.Errorf("SetRealDefault calls = %d, want 1 (applied then reverted)", len(realDef.calls))
	}
	if rev.calls != 1 {
		t.Errorf("RevertRealDefault calls = %d, want 1", rev.calls)
	}
	// The #936/#1016 AC: outcome=reverted is visible on the promote span.
	span := spanByName(t, rec, SpanPromote)
	if got := stringAttr(span, AttrOutcome); got != string(OutcomeReverted) {
		t.Errorf("span outcome attr = %q, want reverted", got)
	}
}

// --- END-TO-END: a holding/improving real-game fitness keeps the promotion. ---
func TestPromoteAutonomousKeepsWhenFitnessHolds(t *testing.T) {
	realDef := &recordingRealDefault{}
	rev := &recordingReverter{}
	// Baseline in testEvidence is 0.50; real games hold above it.
	holding := fakeRealGameFitness{samples: []float64{0.55, 0.58, 0.60, 0.57}}
	watcher := NewFitnessWatcher(holding, DefaultWatchPolicy(), discardLogger())
	p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, realDef, watcher, rev)

	res := p.Promote(context.Background(), promoteProposal, promoteVerdict,
		Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}, testEvidence())

	if res.Outcome != OutcomeApplied {
		t.Fatalf("outcome = %q, want applied (fitness held)", res.Outcome)
	}
	if rev.calls != 0 {
		t.Errorf("RevertRealDefault calls = %d, want 0 (no degradation)", rev.calls)
	}
}

// keep the experiment import meaningful if a future edit drops the only use.
var _ = experiment.OutcomePromote
