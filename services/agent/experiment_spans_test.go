package main

import (
	"context"
	"testing"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/promote"
)

// #1140 Slice C: the experiment / fitness / verdict learning loop was log-only —
// invisible in traces. onGameEnd now wraps the EXISTING fitness + conclusion
// computations in experiment.fitness / experiment.evaluate / experiment.verdict
// spans (observability only — no change to the values, their order, or the loop's
// control flow), each Linked to the originating game trace when one is in scope.

// expSpan_findByName returns the recorded spans with the given name.
func expSpan_findByName(spans []sdktrace.ReadOnlySpan, name string) []sdktrace.ReadOnlySpan {
	var out []sdktrace.ReadOnlySpan
	for _, s := range spans {
		if s.Name() == name {
			out = append(out, s)
		}
	}
	return out
}

func expSpan_attr(s sdktrace.ReadOnlySpan, key string) (string, bool) {
	for _, kv := range s.Attributes() {
		if string(kv.Key) == key {
			return kv.Value.Emit(), true
		}
	}
	return "", false
}

// gcWithTrace mirrors gcFor but stamps a game trace_id so the experiment spans can be
// asserted to Link to the originating game trace.
func gcWithTrace(gameID, experimentID, arm, traceID string) gamecontext.GameContext {
	gc := gcFor(gameID, experimentID, arm)
	gc.GameTraceID = traceID
	return gc
}

// recordingExperimentLoop builds a loop wired with a recording tracer plus a real
// registry, declares + starts one experiment, and returns the loop, span recorder,
// the recording spawner, and the experiment id.
func recordingExperimentLoop(t *testing.T) (*experimentLoop, *tracetest.SpanRecorder, *recordingSpawner, string) {
	t.Helper()
	spawner := &recordingSpawner{}
	realDef := &recordingRealDefault{}
	github := &recordingGitHub{}
	// Safe-default resolver (mode=issue, local, kill-switch off) — the verdict path is
	// exercised without any real-player write, exactly like the safe-default E2E test.
	resolve := func(context.Context) promote.Config {
		return promote.Config{Mode: promote.ModeIssue, Target: promote.TargetLocal, Enabled: false}
	}
	loop := buildLoopForTest(t, spawner, resolve, realDef, github, armFitness())

	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	loop.tracer = tp.Tracer("test")

	id, err := loop.registry.Declare(experiment.Intent{
		FlagKey: "invincibility_seconds", ExperimentalValue: 6.0, Objective: "balanced", TargetNPerArm: 3,
	})
	if err != nil {
		t.Fatalf("declare: %v", err)
	}
	if err := loop.registry.Start(context.Background(), id); err != nil {
		t.Fatalf("start: %v", err)
	}
	return loop, sr, spawner, id
}

// TestExperimentSpans_FitnessAndEvaluateEmitted: concluding a single shadow game
// emits one experiment.fitness span and one experiment.evaluate span, each carrying
// the experiment correlation (id / arm / game.id) and a Link to the game trace, and
// experiment.fitness carries the computed fitness scalar.
func TestExperimentSpans_FitnessAndEvaluateEmitted(t *testing.T) {
	loop, sr, spawner, expID := recordingExperimentLoop(t)
	const gameTrace = "4bf92f3577b34da6a3ce929d0e0e4736"

	// Allocate one game so the registry binds it to the experiment, then conclude the
	// ACTUAL bound game (using the spawner's recorded gameID) so ConcludeGame folds it.
	loop.registry.AllocateAndSpawn(context.Background())
	rec := spawner.drain()
	if len(rec) == 0 {
		t.Fatal("expected the registry to allocate one shadow game")
	}
	c := rec[0]
	loop.onGameEnd(gcWithTrace(c.gameID, c.experimentID, c.arm, gameTrace))

	ended := sr.Ended()

	fit := expSpan_findByName(ended, decision.SpanExperimentFitness)
	if len(fit) != 1 {
		t.Fatalf("experiment.fitness spans = %d, want 1", len(fit))
	}
	if v, ok := expSpan_attr(fit[0], decision.AttrExperimentID); !ok || v != expID {
		t.Errorf("experiment.fitness %s = %q, want %q", decision.AttrExperimentID, v, expID)
	}
	if v, ok := expSpan_attr(fit[0], decision.AttrExperimentArm); !ok || v != c.arm {
		t.Errorf("experiment.fitness %s = %q, want %q", decision.AttrExperimentArm, v, c.arm)
	}
	if _, ok := expSpan_attr(fit[0], decision.AttrExperimentFitness); !ok {
		t.Errorf("experiment.fitness must carry %s", decision.AttrExperimentFitness)
	}
	assertLinksToTrace(t, fit[0], gameTrace, "experiment.fitness")

	eval := expSpan_findByName(ended, decision.SpanExperimentEvaluate)
	if len(eval) != 1 {
		t.Fatalf("experiment.evaluate spans = %d, want 1", len(eval))
	}
	if v, ok := expSpan_attr(eval[0], decision.AttrExperimentStatus); !ok || v == "" {
		t.Errorf("experiment.evaluate must carry a non-empty %s, got %q", decision.AttrExperimentStatus, v)
	}
	assertLinksToTrace(t, eval[0], gameTrace, "experiment.evaluate")
}

// TestExperimentSpans_VerdictEmitted: running enough conclusions to reach a rolling
// verdict emits an experiment.verdict span carrying the outcome / delta / significance
// the registry computed, nested under an experiment.evaluate span.
func TestExperimentSpans_VerdictEmitted(t *testing.T) {
	loop, sr, spawner, expID := recordingExperimentLoop(t)

	// Conclude enough games per arm (TargetNPerArm=3) for the #979 verdict to settle.
	concludeCohort(t, loop, spawner, 4)

	ended := sr.Ended()
	verdicts := expSpan_findByName(ended, decision.SpanExperimentVerdict)
	if len(verdicts) == 0 {
		t.Fatalf("expected at least one experiment.verdict span once a verdict settles, got 0")
	}
	last := verdicts[len(verdicts)-1]
	if v, ok := expSpan_attr(last, decision.AttrVerdictOutcome); !ok || v == "" {
		t.Errorf("experiment.verdict must carry a non-empty %s", decision.AttrVerdictOutcome)
	}
	if _, ok := expSpan_attr(last, decision.AttrVerdictDelta); !ok {
		t.Errorf("experiment.verdict must carry %s", decision.AttrVerdictDelta)
	}
	if v, ok := expSpan_attr(last, decision.AttrExperimentID); !ok || v != expID {
		t.Errorf("experiment.verdict %s = %q, want %q", decision.AttrExperimentID, v, expID)
	}
	// The verdict span nests under an experiment.evaluate span (shares its trace).
	evals := expSpan_findByName(ended, decision.SpanExperimentEvaluate)
	if len(evals) == 0 {
		t.Fatal("expected experiment.evaluate spans to parent the verdict")
	}
	var nested bool
	for _, ev := range evals {
		if ev.SpanContext().TraceID() == last.SpanContext().TraceID() {
			nested = true
		}
	}
	if !nested {
		t.Error("experiment.verdict must nest under an experiment.evaluate span (same trace)")
	}
}

// TestExperimentSpans_NoLinkWhenTraceAbsent: a concluded game with no GameTraceID
// (the common shadow-game case) still emits the spans, but with NO Link — graceful
// fallback, no panic.
func TestExperimentSpans_NoLinkWhenTraceAbsent(t *testing.T) {
	loop, sr, spawner, _ := recordingExperimentLoop(t)

	loop.registry.AllocateAndSpawn(context.Background())
	rec := spawner.drain()
	if len(rec) == 0 {
		t.Fatal("expected the registry to allocate one shadow game")
	}
	c := rec[0]
	loop.onGameEnd(gcFor(c.gameID, c.experimentID, c.arm)) // no GameTraceID

	ended := sr.Ended()
	for _, name := range []string{decision.SpanExperimentFitness, decision.SpanExperimentEvaluate} {
		spans := expSpan_findByName(ended, name)
		if len(spans) != 1 {
			t.Fatalf("%s spans = %d, want 1", name, len(spans))
		}
		if n := len(spans[0].Links()); n != 0 {
			t.Errorf("%s links = %d, want 0 (no game trace_id)", name, n)
		}
	}
}

// TestExperimentSpans_FitnessValueUnchanged: the experiment.fitness span is pure
// observability — the scalar recorded on the span MUST equal the bare gameFitnessFunc
// result, proving the span does not alter the computed fitness.
func TestExperimentSpans_FitnessValueUnchanged(t *testing.T) {
	loop, sr, spawner, _ := recordingExperimentLoop(t)

	loop.registry.AllocateAndSpawn(context.Background())
	rec := spawner.drain()
	if len(rec) == 0 {
		t.Fatal("expected the registry to allocate one shadow game")
	}
	c := rec[0]
	gc := gcFor(c.gameID, c.experimentID, c.arm)

	want := armFitness()(gc, "balanced")
	loop.onGameEnd(gc)

	fit := expSpan_findByName(sr.Ended(), decision.SpanExperimentFitness)
	if len(fit) != 1 {
		t.Fatalf("experiment.fitness spans = %d, want 1", len(fit))
	}
	got, ok := fitnessAttrFloat(fit[0])
	if !ok {
		t.Fatalf("experiment.fitness must carry %s", decision.AttrExperimentFitness)
	}
	if got != want {
		t.Errorf("recorded fitness = %v, want %v (span must not change the computed value)", got, want)
	}
}

// fitnessAttrFloat extracts the experiment.fitness float64 attribute.
func fitnessAttrFloat(s sdktrace.ReadOnlySpan) (float64, bool) {
	for _, kv := range s.Attributes() {
		if string(kv.Key) == decision.AttrExperimentFitness {
			return kv.Value.AsFloat64(), true
		}
	}
	return 0, false
}

// assertLinksToTrace asserts the span carries exactly one Link to the given game
// trace_id with the trace_id attribute.
func assertLinksToTrace(t *testing.T, s sdktrace.ReadOnlySpan, gameTrace, spanName string) {
	t.Helper()
	links := s.Links()
	if len(links) != 1 {
		t.Fatalf("%s links = %d, want 1", spanName, len(links))
	}
	wantTID, _ := trace.TraceIDFromHex(gameTrace)
	if got := links[0].SpanContext.TraceID(); got != wantTID {
		t.Errorf("%s link trace_id = %s, want %s", spanName, got, wantTID)
	}
	var found bool
	for _, kv := range links[0].Attributes {
		if string(kv.Key) == decision.AttrGameTraceID && kv.Value.AsString() == gameTrace {
			found = true
		}
	}
	if !found {
		t.Errorf("%s link must carry %s=%s", spanName, decision.AttrGameTraceID, gameTrace)
	}
}

// TestExperimentGameTraceLink_Direct unit-tests the replicated #1133 link primitive:
// valid -> one option, empty/invalid -> nil (graceful fallback).
func TestExperimentGameTraceLink_Direct(t *testing.T) {
	if got := experimentGameTraceLink(""); got != nil {
		t.Errorf("experimentGameTraceLink(empty) = %v, want nil", got)
	}
	if got := experimentGameTraceLink("zzzz"); got != nil {
		t.Errorf("experimentGameTraceLink(invalid) = %v, want nil", got)
	}
	if got := experimentGameTraceLink("00000000000000000000000000000000"); got != nil {
		t.Errorf("experimentGameTraceLink(all-zero) = %v, want nil", got)
	}
	if got := experimentGameTraceLink("4bf92f3577b34da6a3ce929d0e0e4736"); len(got) != 1 {
		t.Errorf("experimentGameTraceLink(valid) = %d options, want 1", len(got))
	}
}
