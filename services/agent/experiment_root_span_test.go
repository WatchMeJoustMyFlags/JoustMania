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

// #1188 (epic #1181 PR2): the per-experiment ANALYSIS spans (experiment.fitness /
// experiment.evaluate) re-parent as CHILDREN of the long-lived agent.experiment ROOT
// span for an EXPERIMENT-bound game; a non-experiment game keeps its own-root behavior.
// These tests assert the re-parenting end-to-end (same trace_id, parent = the root
// span's span_id) by sharing ONE recording tracer between the registry and the loop.

// rootSpanLoop builds a loop + registry sharing ONE recording tracer provider, so the
// experiment ROOT span (registry-emitted) and the analysis spans (loop-emitted) land in
// the same recorder and their parent/child relationship is assertable. It declares +
// starts one experiment and returns the loop, recorder, spawner, and experiment id.
func rootSpanLoop(t *testing.T) (*experimentLoop, *tracetest.SpanRecorder, *recordingSpawner, string) {
	t.Helper()
	spawner := &recordingSpawner{}
	realDef := &recordingRealDefault{}
	github := &recordingGitHub{}
	resolve := func(context.Context) promote.Config {
		return promote.Config{Mode: promote.ModeIssue, Target: promote.TargetLocal, Enabled: false}
	}

	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	tr := tp.Tracer("test")

	// Build the registry with the SHARED recording tracer so its agent.experiment ROOT
	// span is recorded by sr, then build the loop over that registry with the same tracer.
	loop := buildLoopForTestWithTracer(t, spawner, resolve, realDef, github, armFitness(), tr)
	loop.tracer = tr

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

// TestRootSpan_FitnessAndEvaluateReparentedUnderExperiment: for an experiment-bound
// game, experiment.fitness + experiment.evaluate are CHILDREN of the experiment ROOT
// span (same trace_id, parent = the root span's span_id).
func TestRootSpan_FitnessAndEvaluateReparentedUnderExperiment(t *testing.T) {
	loop, sr, spawner, id := rootSpanLoop(t)
	rootSC, _ := loop.registry.ExperimentSpanContext(id)
	rootTrace := rootSC.TraceID()
	rootSpan := rootSC.SpanID()

	loop.registry.AllocateAndSpawn(context.Background())
	rec := spawner.drain()
	if len(rec) == 0 {
		t.Fatal("expected one allocated shadow game")
	}
	c := rec[0]
	// A game trace is in scope too (the Link is retained); re-parenting is independent.
	loop.onGameEnd(gcWithTrace(c.gameID, c.experimentID, c.arm,
		"4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7"))

	for _, name := range []string{decision.SpanExperimentFitness, decision.SpanExperimentEvaluate} {
		spans := expSpan_findByName(sr.Ended(), name)
		if len(spans) != 1 {
			t.Fatalf("%s spans = %d, want 1", name, len(spans))
		}
		s := spans[0]
		if got := s.SpanContext().TraceID(); got != rootTrace {
			t.Errorf("%s trace_id = %s, want experiment root trace %s", name, got, rootTrace)
		}
		if got := s.Parent().SpanID(); got != rootSpan {
			t.Errorf("%s parent span_id = %s, want experiment root span %s", name, got, rootSpan)
		}
		// The game Link is RETAINED (re-parenting is additive to the cross-trace Link).
		if n := len(s.Links()); n != 1 {
			t.Errorf("%s links = %d, want 1 (game Link retained)", name, n)
		}
	}
}

// TestRootSpan_NonExperimentGameKeepsOwnRoot: a NON-experiment (real) game has no
// experiment root span, so its analysis spans keep CURRENT behavior — own-root (no
// parent), graceful.
func TestRootSpan_NonExperimentGameKeepsOwnRoot(t *testing.T) {
	loop, sr, _, _ := rootSpanLoop(t)

	// A real game (empty ExperimentID) flows through onGameEnd's non-experiment branch,
	// which records baseline fitness but emits NO experiment.* spans. To prove the
	// graceful fallback of the re-parent itself, drive scoreGameFitness directly with an
	// empty experiment id: it must produce an OWN-ROOT span (no parent).
	gc := gcFor("game_real_1", "", "")
	_ = loop.scoreGameFitness(gc, "balanced", nil)

	spans := expSpan_findByName(sr.Ended(), decision.SpanExperimentFitness)
	if len(spans) != 1 {
		t.Fatalf("experiment.fitness spans = %d, want 1", len(spans))
	}
	// An empty experiment id yields no remote parent (graceful), so the span is own-root.
	if sc := trace.SpanContextFromContext(loop.experimentParentCtx("")); sc.IsValid() {
		t.Error("experimentParentCtx(\"\") must carry no parent SpanContext")
	}
	if spans[0].Parent().IsValid() {
		t.Error("a non-experiment fitness span must be own-root (no valid parent)")
	}
}

// TestRootSpan_ReparentDoesNotChangeFitness: re-parenting is observability only — the
// fitness scalar recorded under the experiment root span equals the bare fitness func.
func TestRootSpan_ReparentDoesNotChangeFitness(t *testing.T) {
	loop, sr, spawner, _ := rootSpanLoop(t)
	loop.registry.AllocateAndSpawn(context.Background())
	rec := spawner.drain()
	if len(rec) == 0 {
		t.Fatal("expected one allocated shadow game")
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
	if !ok || got != want {
		t.Errorf("recorded fitness = %v (ok=%v), want %v (re-parent must not change it)", got, ok, want)
	}
}

// TestRootSpan_RetroReparentedUnderExperiment proves the retro re-parent SEAM at the
// helper level: the gamecontext remote-parent helper, fed the experiment SpanContext,
// installs the root span as the parent; a non-experiment / torn-down lookup falls back.
func TestRootSpan_RetroReparentedUnderExperiment(t *testing.T) {
	loop, _, _, id := rootSpanLoop(t)
	sc, ok := loop.registry.ExperimentSpanContext(id)
	if !ok {
		t.Fatal("experiment span context must be live")
	}
	ctx, reparented := gamecontext.RemoteParentFromSpanContext(context.Background(), sc)
	if !reparented {
		t.Fatal("RemoteParentFromSpanContext must re-parent under a valid experiment SpanContext")
	}
	if got := trace.SpanContextFromContext(ctx).SpanID(); got != sc.SpanID() {
		t.Errorf("remote parent span_id = %s, want %s", got, sc.SpanID())
	}
	// Invalid (torn-down / non-experiment) SpanContext => no re-parent, ctx unchanged.
	if _, ok := gamecontext.RemoteParentFromSpanContext(context.Background(), trace.SpanContext{}); ok {
		t.Error("RemoteParentFromSpanContext on an invalid SpanContext must report no re-parent")
	}
}
