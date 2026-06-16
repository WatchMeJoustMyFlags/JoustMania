package experiment

import (
	"context"
	"testing"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/decision"
)

// #1188 (epic #1181 PR2): the registry owns a long-lived agent.experiment ROOT span
// per experiment — started when the experiment becomes RUNNING, ended on EVERY terminal
// transition (no leak on any path), and exposed via ExperimentSpanContext for the
// per-experiment analysis spans to re-parent under. These tests pin that lifecycle.

// rootSpanRegistry builds a registry whose root-span tracer is a recording tracer, so a
// test can assert the agent.experiment span is started + ended. It returns the registry
// and the span recorder.
func rootSpanRegistry(t *testing.T, cfg RegistryConfig) (*Registry, *tracetest.SpanRecorder) {
	t.Helper()
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	cfg.Tracer = tp.Tracer("test")
	return newTestRegistry(t, cfg), sr
}

// rootSpans returns the recorded ENDED spans named agent.experiment.
func rootSpans(sr *tracetest.SpanRecorder) []sdktrace.ReadOnlySpan {
	var out []sdktrace.ReadOnlySpan
	for _, s := range sr.Ended() {
		if s.Name() == decision.SpanExperiment {
			out = append(out, s)
		}
	}
	return out
}

func rootSpanAttr(s sdktrace.ReadOnlySpan, key string) (string, bool) {
	for _, kv := range s.Attributes() {
		if string(kv.Key) == key {
			return kv.Value.Emit(), true
		}
	}
	return "", false
}

// TestRootSpan_StartedOnRunning_NotBeforeAndExposed: the root span is NOT started at
// Declare (PROPOSED) and IS started + exposed at Start (RUNNING); it is not yet ended.
func TestRootSpan_StartedOnRunning_NotBeforeAndExposed(t *testing.T) {
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner: &fakeSpawner{}, Verdict: fakeVerdict{concludeAtN: 1000}, Targeting: &fakeTargeting{},
		MaxShadowGames: 4,
	})
	id, err := r.Declare(sampleIntent())
	if err != nil {
		t.Fatalf("declare: %v", err)
	}
	// PROPOSED: no root span exposed yet, none recorded.
	if _, ok := r.ExperimentSpanContext(id); ok {
		t.Error("ExperimentSpanContext must be unavailable before Start (PROPOSED)")
	}

	if err := r.Start(context.Background(), id); err != nil {
		t.Fatalf("start: %v", err)
	}
	sc, ok := r.ExperimentSpanContext(id)
	if !ok || !sc.IsValid() {
		t.Fatal("ExperimentSpanContext must be a valid SpanContext once RUNNING")
	}
	// Started but NOT yet ended (still live for analysis re-parenting).
	if n := len(rootSpans(sr)); n != 0 {
		t.Fatalf("agent.experiment ended spans = %d, want 0 (still live)", n)
	}
}

// TestRootSpan_EndedOnDiscard / _OnPromote / _OnAbort: the root span is ended exactly
// once on EVERY terminal transition, and the SpanContext stops being exposed.
func TestRootSpan_EndedOnDiscard(t *testing.T) {
	spawner := &fakeSpawner{}
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner:   spawner,
		Verdict:   fakeVerdict{outcome: CohortOutcomeDiscard, concludeAtN: 1, alwaysReport: true},
		Targeting: &fakeTargeting{}, MaxShadowGames: 2, EffectiveConcurrency: 1,
	})
	id := startedExperiment(t, r)
	r.AllocateAndSpawn(context.Background())
	g := spawner.calls[0].gameID
	if _, err := r.ConcludeGame(context.Background(), g, 0.3, 60); err != nil {
		t.Fatalf("conclude: %v", err)
	}
	assertRootSpanEndedOnce(t, r, sr, id)
}

func TestRootSpan_EndedOnPromote(t *testing.T) {
	spawner := &fakeSpawner{}
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner:   spawner,
		Verdict:   fakeVerdict{outcome: CohortOutcomePromote, concludeAtN: 1, alwaysReport: true},
		Promoter:  &fakePromoter{},
		Targeting: &fakeTargeting{}, MaxShadowGames: 2, EffectiveConcurrency: 1,
	})
	id := startedExperiment(t, r)
	r.AllocateAndSpawn(context.Background())
	g := spawner.calls[0].gameID
	if _, err := r.ConcludeGame(context.Background(), g, 0.9, 60); err != nil {
		t.Fatalf("conclude: %v", err)
	}
	// The promote→DONE path routes through teardown with an already-terminal status —
	// the span MUST still be ended exactly once (the no-leak invariant on the path the
	// idempotency guard would otherwise early-return on).
	assertRootSpanEndedOnce(t, r, sr, id)
}

func TestRootSpan_EndedOnAbort(t *testing.T) {
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner: &fakeSpawner{}, Verdict: fakeVerdict{concludeAtN: 1000}, Targeting: &fakeTargeting{},
		MaxShadowGames: 2,
	})
	id := startedExperiment(t, r)
	if err := r.Abort(context.Background(), id, "kill-switch"); err != nil {
		t.Fatalf("abort: %v", err)
	}
	assertRootSpanEndedOnce(t, r, sr, id)
}

// TestRootSpan_DoubleTeardownNoDoubleEnd: a repeated terminal call (Abort then Abort,
// or AbortAll over an already-aborted experiment) must NOT end the span twice.
func TestRootSpan_DoubleTeardownNoDoubleEnd(t *testing.T) {
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner: &fakeSpawner{}, Verdict: fakeVerdict{concludeAtN: 1000}, Targeting: &fakeTargeting{},
		MaxShadowGames: 2,
	})
	id := startedExperiment(t, r)
	_ = r.Abort(context.Background(), id, "first")
	_ = r.Abort(context.Background(), id, "second (no-op)")
	if n := len(rootSpans(sr)); n != 1 {
		t.Fatalf("agent.experiment ended spans = %d, want exactly 1 (idempotent end)", n)
	}
}

// TestRootSpan_CarriesIdentifyingAttrs: the root span carries experiment.id / flag_key /
// objective / target_n / arms from the intent.
func TestRootSpan_CarriesIdentifyingAttrs(t *testing.T) {
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner: &fakeSpawner{}, Verdict: fakeVerdict{concludeAtN: 1000}, Targeting: &fakeTargeting{},
		MaxShadowGames: 2,
	})
	id := startedExperiment(t, r)
	_ = r.Abort(context.Background(), id, "done")

	spans := rootSpans(sr)
	if len(spans) != 1 {
		t.Fatalf("agent.experiment spans = %d, want 1", len(spans))
	}
	s := spans[0]
	if v, ok := rootSpanAttr(s, decision.AttrExperimentID); !ok || v != id {
		t.Errorf("%s = %q, want %q", decision.AttrExperimentID, v, id)
	}
	for _, key := range []string{
		decision.AttrExperimentFlagKey, decision.AttrExperimentObjective,
		decision.AttrExperimentTargetN, decision.AttrExperimentArms,
	} {
		if _, ok := rootSpanAttr(s, key); !ok {
			t.Errorf("agent.experiment must carry %s", key)
		}
	}
}

// TestRootSpan_NilTracerSafe: a registry without a recording tracer (the global no-op
// provider) drives the full lifecycle with no panic and exposes no SpanContext.
func TestRootSpan_NilTracerSafe(t *testing.T) {
	// No Tracer injected => NewRegistry defaults to the global no-op tracer.
	r := newTestRegistry(t, RegistryConfig{
		Spawner: &fakeSpawner{}, Verdict: fakeVerdict{concludeAtN: 1000}, Targeting: &fakeTargeting{},
		MaxShadowGames: 2,
	})
	id := startedExperiment(t, r)
	// The no-op tracer yields an invalid (non-recording) SpanContext, so the accessor
	// reports unavailable — graceful for the no-telemetry path.
	if _, ok := r.ExperimentSpanContext(id); ok {
		t.Error("no-op tracer: ExperimentSpanContext should report unavailable")
	}
	if err := r.Abort(context.Background(), id, "done"); err != nil {
		t.Fatalf("abort with no-op tracer must not error: %v", err)
	}
}

// TestRootSpan_RehydrateGetsFreshSpan: a rehydrated (post-restart) experiment gets a
// BRAND-NEW root span (the #1188 restart discontinuity) — its SpanContext is valid and
// it ends on the next teardown.
func TestRootSpan_RehydrateGetsFreshSpan(t *testing.T) {
	root := t.TempDir()
	// Declare + start an experiment in one registry, then rehydrate it in a fresh one
	// (simulating a restart) and assert it gets a new live root span.
	r1, _ := rootSpanRegistry(t, RegistryConfig{
		Root: root, Spawner: &fakeSpawner{}, Verdict: fakeVerdict{concludeAtN: 1000},
		Targeting: &fakeTargeting{}, MaxShadowGames: 2,
	})
	id := startedExperiment(t, r1)

	r2, sr2 := rootSpanRegistry(t, RegistryConfig{
		Root: root, Spawner: &fakeSpawner{}, Verdict: fakeVerdict{concludeAtN: 1000},
		Targeting: &fakeTargeting{}, MaxShadowGames: 2,
	})
	if err := r2.Rehydrate([]string{id}); err != nil {
		t.Fatalf("rehydrate: %v", err)
	}
	sc, ok := r2.ExperimentSpanContext(id)
	if !ok || !sc.IsValid() {
		t.Fatal("rehydrated experiment must get a fresh, valid root span")
	}
	_ = r2.Abort(context.Background(), id, "done")
	if n := len(rootSpans(sr2)); n != 1 {
		t.Fatalf("rehydrated agent.experiment ended spans = %d, want 1", n)
	}
}

// startedExperiment declares + starts one experiment and returns its id.
func startedExperiment(t *testing.T, r *Registry) string {
	t.Helper()
	id, err := r.Declare(sampleIntent())
	if err != nil {
		t.Fatalf("declare: %v", err)
	}
	if err := r.Start(context.Background(), id); err != nil {
		t.Fatalf("start: %v", err)
	}
	return id
}

// assertRootSpanEndedOnce asserts exactly one agent.experiment span ended and the
// SpanContext is no longer exposed after teardown.
func assertRootSpanEndedOnce(t *testing.T, r *Registry, sr *tracetest.SpanRecorder, id string) {
	t.Helper()
	if n := len(rootSpans(sr)); n != 1 {
		t.Fatalf("agent.experiment ended spans = %d, want exactly 1 (no leak, no double-end)", n)
	}
	if _, ok := r.ExperimentSpanContext(id); ok {
		t.Error("ExperimentSpanContext must be unavailable after teardown (span ended)")
	}
}
