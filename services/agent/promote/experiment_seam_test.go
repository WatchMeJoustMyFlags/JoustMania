package promote

// experiment_seam_test.go covers the #980 registry-seam adapter
// (ExperimentPromoter): it builds the #961 Promoter's inputs from a
// journal.CompactView and reuses the existing env-gated dispatch + safety rail
// UNCHANGED. The fakes/helpers (recordingGitHub, recordingRealDefault,
// promoterWithRecorder, discardLogger) are the in-package recorders from
// promote_test.go.

import (
	"context"
	"testing"

	"go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/experiment/journal"
)

// ExperimentPromoter must satisfy the registry's promotion seam (experiment.Promoter).
var _ experiment.Promoter = (*ExperimentPromoter)(nil)

// promoteView builds a CONCLUDED+promote CompactView: control mean 0.40 over 6
// games, experimental mean 0.70 over 8 games (total N = 14), promote verdict.
func promoteView() journal.CompactView {
	return journal.CompactView{
		Intent: journal.Intent{
			ExperimentID:      "exp_abc123",
			FlagKey:           "death_grace_period_ms",
			ExperimentalValue: 500,
			Hypothesis:        "longer grace reduces early eliminations",
			Objective:         "engagement_balanced",
			Arms:              []string{experiment.ArmExperimental, experiment.ArmControl},
		},
		Status: "concluded",
		Arms: map[string]journal.ArmView{
			experiment.ArmControl:      {Count: 6, Mean: 0.40},
			experiment.ArmExperimental: {Count: 8, Mean: 0.70},
		},
		Verdict: &journal.Verdict{Outcome: experiment.CohortOutcomePromote, Delta: 0.30, Significant: true},
	}
}

// fixedConfig returns a ConfigResolver that always yields cfg (no flagd).
func fixedConfig(cfg Config) ConfigResolver {
	return func(context.Context) Config { return cfg }
}

// adapterWithRecorder wires an ExperimentPromoter over a Promoter that uses the
// given seams + a span recorder, with the supplied live Config.
func adapterWithRecorder(t *testing.T, gh GitHubClient, git GitClient, realDef RealDefaultWriter, cfg Config) (*ExperimentPromoter, *tracetest.SpanRecorder) {
	t.Helper()
	rec := tracetest.NewSpanRecorder()
	tp := trace.NewTracerProvider(trace.WithSpanProcessor(rec))
	p := NewPromoter(gh, git, realDef, nil, nil, testRepo(), tp.Tracer("test"), discardLogger())
	return NewExperimentPromoter(p, fixedConfig(cfg)), rec
}

// TestExperimentSeamEvidenceMapping: a promote view builds Evidence with control
// mean→Before, experimental mean→After, delta, and N=total across arms, and the
// underlying #961 Promoter is invoked with the right flag/value. mode=pr local
// exercises a clean APPLIED outcome (a local commit) so the adapter returns nil.
func TestExperimentSeamEvidenceMapping(t *testing.T) {
	gh := &recordingGitHub{}
	git := &recordingGit{sha: "deadbeef"}
	a, rec := adapterWithRecorder(t, gh, git, nil, Config{Mode: ModePR, Target: TargetLocal, Enabled: false})

	if err := a.Promote(context.Background(), promoteView()); err != nil {
		t.Fatalf("Promote returned error on clean local PR: %v", err)
	}

	// The #961 Promoter ran and stamped the fitness Evidence on its span.
	span := spanByName(t, rec, SpanPromote)
	if got := float64Attr(span, AttrFitnessBefore); got != 0.40 {
		t.Errorf("FitnessBefore: want control mean 0.40, got %v", got)
	}
	if got := float64Attr(span, AttrFitnessAfter); got != 0.70 {
		t.Errorf("FitnessAfter: want experimental mean 0.70, got %v", got)
	}
	if got := float64Attr(span, AttrFitnessDelta); got < 0.2999 || got > 0.3001 {
		t.Errorf("FitnessDelta: want 0.30, got %v", got)
	}
	if got := stringAttr(span, AttrFlagKey); got != "death_grace_period_ms" {
		t.Errorf("flag key: want death_grace_period_ms, got %q", got)
	}

	// A local PR commits the change; the recorder proves the right flag/value flowed
	// through into the #961 Promoter's body (N=14 sample size).
	if len(git.commits) == 0 {
		t.Fatalf("expected a local commit (mode=pr, target=local)")
	}
}

// TestExperimentSeamGitHubGatedNoOp: with the real github path NOT enabled (the
// default — main.go passes inert no-op clients), a github-target promotion degrades
// to a RECORDED no-op. The adapter does NOT bypass the #961 gates: it surfaces the
// recorded reason as a (non-fatal) error and touches nothing real.
func TestExperimentSeamGitHubGatedNoOp(t *testing.T) {
	// gh/git nil ⇒ NewPromoter installs the inert recording no-ops (the default,
	// fail-closed floor). realDef nil ⇒ no real-default writer wired.
	a, rec := adapterWithRecorder(t, nil, nil, nil, Config{Mode: ModeIssue, Target: TargetGitHub, Enabled: false})

	err := a.Promote(context.Background(), promoteView())
	if err == nil {
		t.Fatalf("expected a recorded no-op error when github target is not enabled")
	}

	span := spanByName(t, rec, SpanPromote)
	if got := stringAttr(span, AttrOutcome); got != string(OutcomeDiscarded) {
		t.Errorf("outcome: want discarded (no-op), got %q", got)
	}
	if stringAttr(span, AttrReason) == "" {
		t.Errorf("expected a non-empty reason proving the safety rail degraded the github path")
	}
}

// TestExperimentSeamAutonomousDisabledNoOp: autonomous mode while the agent is
// DISABLED must never mutate the real default — the kill-switch (Config.Enabled) is
// the #961 Promoter's, reused. The recordingRealDefault must record ZERO writes.
func TestExperimentSeamAutonomousDisabledNoOp(t *testing.T) {
	realDef := &recordingRealDefault{}
	a, rec := adapterWithRecorder(t, nil, nil, realDef, Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: false})

	err := a.Promote(context.Background(), promoteView())
	if err == nil {
		t.Fatalf("expected a recorded no-op error when autonomous is requested while disabled")
	}
	if len(realDef.calls) != 0 {
		t.Errorf("kill-switch breached: real default written %d time(s) while disabled", len(realDef.calls))
	}
	span := spanByName(t, rec, SpanPromote)
	if got := stringAttr(span, AttrOutcome); got != string(OutcomeDiscarded) {
		t.Errorf("outcome: want discarded (kill-switch no-op), got %q", got)
	}
}

// TestExperimentSeamNilResolverSafeDefault: with NO resolver, every promotion runs
// under the SAFE default Config (issue/local, disabled) — the adapter never
// fabricates a privileged mode. A github real path is unreachable (default
// target=local, mode=issue), so a local issue mode no-ops cleanly without touching
// the real default.
func TestExperimentSeamNilResolverSafeDefault(t *testing.T) {
	realDef := &recordingRealDefault{}
	rec := tracetest.NewSpanRecorder()
	tp := trace.NewTracerProvider(trace.WithSpanProcessor(rec))
	// nil git/github ⇒ inert no-ops; nil resolver ⇒ safe default issue/local/disabled.
	p := NewPromoter(nil, nil, realDef, nil, nil, testRepo(), tp.Tracer("test"), discardLogger())
	a := NewExperimentPromoter(p, nil)

	_ = a.Promote(context.Background(), promoteView())

	span := spanByName(t, rec, SpanPromote)
	if got := stringAttr(span, AttrGate); got != string(ModeIssue) {
		t.Errorf("nil resolver should default to issue mode, got %q", got)
	}
	if got := stringAttr(span, AttrTarget); got != string(TargetLocal) {
		t.Errorf("nil resolver should default to local target, got %q", got)
	}
	if len(realDef.calls) != 0 {
		t.Errorf("safe default must not write the real default; got %d write(s)", len(realDef.calls))
	}
}

// TestExperimentSeamNonPromoteVerdictNoOp: the adapter only ACTS on a promote
// verdict. A view whose verdict is NOT promote (defensive — the registry should
// never route it here) synthesizes an OutcomeDiscard Decision, so the #961
// Promoter's "only PROMOTE acts" guard no-ops and nothing real is touched.
func TestExperimentSeamNonPromoteVerdictNoOp(t *testing.T) {
	realDef := &recordingRealDefault{}
	a, rec := adapterWithRecorder(t, nil, nil, realDef, Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true})

	view := promoteView()
	view.Verdict = &journal.Verdict{Outcome: experiment.CohortOutcomeDiscard}

	_ = a.Promote(context.Background(), view)

	if len(realDef.calls) != 0 {
		t.Errorf("non-promote verdict must not promote: real default written %d time(s)", len(realDef.calls))
	}
	span := spanByName(t, rec, SpanPromote)
	if got := stringAttr(span, AttrOutcome); got != string(OutcomeDiscarded) {
		t.Errorf("non-promote verdict: want discarded, got %q", got)
	}
	if got := stringAttr(span, AttrReason); got == "" {
		t.Errorf("expected a reason explaining the non-promote no-op")
	}
}

// float64Attr reads a float64 span attribute by key (helper local to this file;
// stringAttr lives in promote_test.go).
func float64Attr(s trace.ReadOnlySpan, key string) float64 {
	for _, a := range s.Attributes() {
		if string(a.Key) == key {
			return a.Value.AsFloat64()
		}
	}
	return 0
}
