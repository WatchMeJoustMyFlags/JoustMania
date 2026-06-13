package experiment

import (
	"context"
	"errors"
	"testing"

	"go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

// ---- Fakes for the three injectable seams (#935). The Validator owns only the
// decision logic; the game run, fitness reads, and rollback are faked here so the
// promote/discard/revert paths are exercised against exact numbers with no clock,
// network, or game stack (the live wiring is the documented follow-up). ----

// fakeBaseline returns a fixed baseline fitness (or an error to exercise the
// fail-closed path).
type fakeBaseline struct {
	fitness float64
	err     error
}

func (b fakeBaseline) Fitness(context.Context, Proposal) (float64, error) {
	return b.fitness, b.err
}

// fakeRunner records the game count it was asked to run and returns a Run handle
// (or an error). realizedGames, when >0, simulates a runner that completed a
// different count than requested.
type fakeRunner struct {
	gotGames      int
	realizedGames int
	err           error
}

func (r *fakeRunner) Run(_ context.Context, _ Proposal, games int) (Run, error) {
	r.gotGames = games
	if r.err != nil {
		return Run{}, r.err
	}
	g := games
	if r.realizedGames > 0 {
		g = r.realizedGames
	}
	return Run{ID: "synthetic-batch", Games: g}, nil
}

// fakeMeasurer returns a fixed post-experiment fitness (or an error).
type fakeMeasurer struct {
	fitness float64
	err     error
}

func (m fakeMeasurer) Measure(context.Context, Run) (float64, error) {
	return m.fitness, m.err
}

// fakeReverter records whether Revert was called (and can return an error).
type fakeReverter struct {
	called bool
	err    error
}

func (r *fakeReverter) Revert(context.Context, Proposal) error {
	r.called = true
	return r.err
}

// validatorWithRecorder wires a Validator over the given seams with an in-memory
// span recorder, using the recording-tracer pattern from experiment_test.go /
// decision/*_test.go.
func validatorWithRecorder(t *testing.T, b Baseline, r SyntheticRunner, m FitnessMeasurer, rev Reverter) (*Validator, *tracetest.SpanRecorder) {
	t.Helper()
	rec := tracetest.NewSpanRecorder()
	tp := trace.NewTracerProvider(trace.WithSpanProcessor(rec))
	v := NewValidator(b, r, m, rev, tp.Tracer("test"), discardLogger())
	return v, rec
}

var testProposal = Proposal{FlagKey: "death_grace_period_ms", ExperimentalValue: 500, Rationale: "longer grace may extend sessions"}

// findSpan returns the first recorded span with the given name, or fails.
func findSpan(t *testing.T, rec *tracetest.SpanRecorder, name string) trace.ReadOnlySpan {
	t.Helper()
	for _, s := range rec.Ended() {
		if s.Name() == name {
			return s
		}
	}
	t.Fatalf("span %q not found", name)
	return nil
}

// floatAttr reads a float64 span attribute by key, failing if absent.
func floatAttr(t *testing.T, s trace.ReadOnlySpan, key string) float64 {
	t.Helper()
	for _, a := range s.Attributes() {
		if string(a.Key) == key {
			return a.Value.AsFloat64()
		}
	}
	t.Fatalf("span %q missing float attr %q", s.Name(), key)
	return 0
}

// stringAttr reads a string span attribute by key (returns "" if absent).
func stringAttr(s trace.ReadOnlySpan, key string) string {
	for _, a := range s.Attributes() {
		if string(a.Key) == key {
			return a.Value.AsString()
		}
	}
	return ""
}

func defaultCfg() Config {
	return Config{ValidationGames: 5, ImprovementThreshold: 0.05, RevertOnDegradation: true}
}

// TestValidatePromote: improvement strictly above the threshold → PROMOTE, and
// the apply leaf (not discard) is emitted. (#935 AC: promote only if improvement
// exceeds threshold.)
func TestValidatePromote(t *testing.T) {
	v, rec := validatorWithRecorder(t,
		fakeBaseline{fitness: 0.50},
		&fakeRunner{},
		fakeMeasurer{fitness: 0.70}, // delta 0.20 > 0.05
		&fakeReverter{})

	dec := v.Validate(context.Background(), testProposal, defaultCfg())

	if dec.Outcome != OutcomePromote {
		t.Fatalf("outcome = %q, want promote", dec.Outcome)
	}
	if dec.Err != nil {
		t.Fatalf("unexpected err: %v", dec.Err)
	}
	// apply leaf present, discard leaf absent.
	findSpan(t, rec, SpanApply)
	for _, s := range rec.Ended() {
		if s.Name() == SpanDiscard {
			t.Fatalf("discard leaf should not be emitted on promote")
		}
	}
}

// TestValidateDiscardBelowThreshold: a positive but insufficient improvement →
// DISCARD (not promote). Covers the strict ">" boundary: delta exactly at the
// threshold must NOT promote.
func TestValidateDiscardBelowThreshold(t *testing.T) {
	// Each case: {baseline, after}. The "at threshold" case uses baseline 0 so the
	// delta is EXACTLY the float literal 0.05 — `delta > 0.05` is then false, proving
	// the strict boundary without floating-point slop (0.55-0.50 is NOT exactly 0.05).
	cases := map[string][2]float64{
		"below threshold": {0.50, 0.53}, // delta 0.03 < 0.05
		"at threshold":    {0.00, 0.05}, // delta exactly 0.05, not strictly greater
	}
	for name, fc := range cases {
		t.Run(name, func(t *testing.T) {
			v, rec := validatorWithRecorder(t,
				fakeBaseline{fitness: fc[0]},
				&fakeRunner{},
				fakeMeasurer{fitness: fc[1]},
				&fakeReverter{})

			dec := v.Validate(context.Background(), testProposal, defaultCfg())
			if dec.Outcome != OutcomeDiscard {
				t.Fatalf("outcome = %q, want discard", dec.Outcome)
			}
			findSpan(t, rec, SpanDiscard)
		})
	}
}

// TestValidateRevertOnDegradation: degradation + revert flag → REVERT, and the
// Reverter is invoked. (#935 AC: revert on degradation when the flag is true.)
func TestValidateRevertOnDegradation(t *testing.T) {
	rev := &fakeReverter{}
	v, rec := validatorWithRecorder(t,
		fakeBaseline{fitness: 0.50},
		&fakeRunner{},
		fakeMeasurer{fitness: 0.40}, // strictly worse than baseline
		rev)

	dec := v.Validate(context.Background(), testProposal, defaultCfg())

	if dec.Outcome != OutcomeRevert {
		t.Fatalf("outcome = %q, want revert", dec.Outcome)
	}
	if !dec.Reverted || !rev.called {
		t.Fatalf("expected revert invoked: dec.Reverted=%v reverter.called=%v", dec.Reverted, rev.called)
	}
	leaf := findSpan(t, rec, SpanDiscard)
	if got := stringAttr(leaf, AttrOutcome); got != string(OutcomeRevert) {
		t.Fatalf("discard leaf outcome = %q, want revert", got)
	}
}

// TestValidateDegradationWithoutRevertFlag: degradation but revert_on_degradation
// false → plain DISCARD, Reverter NOT called.
func TestValidateDegradationWithoutRevertFlag(t *testing.T) {
	rev := &fakeReverter{}
	cfg := defaultCfg()
	cfg.RevertOnDegradation = false
	v, _ := validatorWithRecorder(t,
		fakeBaseline{fitness: 0.50},
		&fakeRunner{},
		fakeMeasurer{fitness: 0.40},
		rev)

	dec := v.Validate(context.Background(), testProposal, cfg)
	if dec.Outcome != OutcomeDiscard {
		t.Fatalf("outcome = %q, want discard", dec.Outcome)
	}
	if dec.Reverted || rev.called {
		t.Fatalf("reverter must not run without the flag: reverted=%v called=%v", dec.Reverted, rev.called)
	}
}

// TestValidateRevertNoReverterWired: degradation + revert flag but a nil Reverter
// → still REVERT verdict (recorded), no panic. Covers the "no rollback wired yet"
// follow-up state.
func TestValidateRevertNoReverterWired(t *testing.T) {
	v, rec := validatorWithRecorder(t,
		fakeBaseline{fitness: 0.50},
		&fakeRunner{},
		fakeMeasurer{fitness: 0.40},
		nil) // no reverter

	dec := v.Validate(context.Background(), testProposal, defaultCfg())
	if dec.Outcome != OutcomeRevert || !dec.Reverted {
		t.Fatalf("outcome = %q reverted=%v, want revert/true", dec.Outcome, dec.Reverted)
	}
	span := findSpan(t, rec, SpanSyntheticValidation)
	if got := stringAttr(span, AttrValidationError); got == "" {
		t.Fatalf("expected validation_error noting missing reverter")
	}
}

// TestValidateRecordsFitnessOnSpan: fitness_before/after/delta and the resolved
// flags ride the validation span. (#935 AC: fitness_before/after on the span.)
func TestValidateRecordsFitnessOnSpan(t *testing.T) {
	v, rec := validatorWithRecorder(t,
		fakeBaseline{fitness: 0.40},
		&fakeRunner{},
		fakeMeasurer{fitness: 0.62},
		&fakeReverter{})

	v.Validate(context.Background(), testProposal, defaultCfg())

	span := findSpan(t, rec, SpanSyntheticValidation)
	if got := floatAttr(t, span, AttrFitnessBefore); got != 0.40 {
		t.Errorf("fitness_before = %v, want 0.40", got)
	}
	if got := floatAttr(t, span, AttrFitnessAfter); got != 0.62 {
		t.Errorf("fitness_after = %v, want 0.62", got)
	}
	if got := floatAttr(t, span, AttrFitnessDelta); got < 0.21 || got > 0.23 {
		t.Errorf("fitness_delta = %v, want ~0.22", got)
	}
	if got := floatAttr(t, span, AttrImprovementThreshold); got != 0.05 {
		t.Errorf("improvement_threshold = %v, want 0.05", got)
	}
	if got := stringAttr(span, AttrOutcome); got != string(OutcomePromote) {
		t.Errorf("outcome attr = %q, want promote", got)
	}
}

// TestValidateSingleTraceTree: the apply|discard leaf is a CHILD of the
// synthetic_validation span (same trace, leaf's parent == validation's span id),
// proving the cycle is one trace tree. (#935 AC: full cycle visible as a single
// trace tree.)
func TestValidateSingleTraceTree(t *testing.T) {
	v, rec := validatorWithRecorder(t,
		fakeBaseline{fitness: 0.50},
		&fakeRunner{},
		fakeMeasurer{fitness: 0.70},
		&fakeReverter{})

	v.Validate(context.Background(), testProposal, defaultCfg())

	parent := findSpan(t, rec, SpanSyntheticValidation)
	leaf := findSpan(t, rec, SpanApply)

	if parent.SpanContext().TraceID() != leaf.SpanContext().TraceID() {
		t.Fatalf("leaf and parent are in different traces")
	}
	if leaf.Parent().SpanID() != parent.SpanContext().SpanID() {
		t.Fatalf("apply leaf is not a child of the synthetic_validation span")
	}
}

// TestValidateGameCountFromConfig: the runner is asked for exactly
// cfg.ValidationGames, and the realized count is recorded on the span. (#935 AC:
// game count controlled by validation_games.)
func TestValidateGameCountFromConfig(t *testing.T) {
	runner := &fakeRunner{realizedGames: 7}
	cfg := defaultCfg()
	cfg.ValidationGames = 7
	v, rec := validatorWithRecorder(t,
		fakeBaseline{fitness: 0.50}, runner, fakeMeasurer{fitness: 0.70}, &fakeReverter{})

	dec := v.Validate(context.Background(), testProposal, cfg)
	if runner.gotGames != 7 {
		t.Errorf("runner asked for %d games, want 7", runner.gotGames)
	}
	if dec.Games != 7 {
		t.Errorf("decision games = %d, want 7", dec.Games)
	}
	span := findSpan(t, rec, SpanSyntheticValidation)
	var got int
	for _, a := range span.Attributes() {
		if string(a.Key) == AttrValidationGames {
			got = int(a.Value.AsInt64())
		}
	}
	if got != 7 {
		t.Errorf("span validation_games = %d, want 7", got)
	}
}

// TestValidateClampsNonPositiveGames: a misconfigured zero/negative game count
// runs at least one game rather than validating on no evidence.
func TestValidateClampsNonPositiveGames(t *testing.T) {
	runner := &fakeRunner{}
	cfg := defaultCfg()
	cfg.ValidationGames = 0
	v, _ := validatorWithRecorder(t,
		fakeBaseline{fitness: 0.50}, runner, fakeMeasurer{fitness: 0.70}, &fakeReverter{})

	v.Validate(context.Background(), testProposal, cfg)
	if runner.gotGames != 1 {
		t.Errorf("runner asked for %d games, want clamp to 1", runner.gotGames)
	}
}

// TestValidateFailsClosed: any seam error → DISCARD with Err set and
// validation_error on the span; NEVER a promote on a broken fitness signal.
func TestValidateFailsClosed(t *testing.T) {
	boom := errors.New("boom")
	cases := map[string]struct {
		b Baseline
		r SyntheticRunner
		m FitnessMeasurer
	}{
		"baseline error": {fakeBaseline{err: boom}, &fakeRunner{}, fakeMeasurer{fitness: 0.9}},
		"runner error":   {fakeBaseline{fitness: 0.1}, &fakeRunner{err: boom}, fakeMeasurer{fitness: 0.9}},
		"measure error":  {fakeBaseline{fitness: 0.1}, &fakeRunner{}, fakeMeasurer{err: boom}},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			v, rec := validatorWithRecorder(t, tc.b, tc.r, tc.m, &fakeReverter{})
			dec := v.Validate(context.Background(), testProposal, defaultCfg())
			if dec.Outcome != OutcomeDiscard {
				t.Fatalf("outcome = %q, want discard (fail closed)", dec.Outcome)
			}
			if dec.Err == nil {
				t.Fatalf("expected Decision.Err on fail-closed path")
			}
			span := findSpan(t, rec, SpanSyntheticValidation)
			if stringAttr(span, AttrValidationError) == "" {
				t.Fatalf("expected validation_error attr on the span")
			}
		})
	}
}
