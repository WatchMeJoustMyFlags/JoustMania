package experiment

import (
	"context"
	"sync"
	"testing"

	"go.opentelemetry.io/otel/trace"
)

// #1182 (epic #1181 PR3): the registry carries the experiment's long-lived ROOT
// span context onto the spawn ctx (as a remote parent), so the real shadowSpawner
// can inject it as the outgoing traceparent and the coordinator's game span
// becomes a CHILD of the experiment span. These tests pin that the SpanContext the
// registry exposes is exactly the one handed to Spawn — and that a no-telemetry
// registry hands a clean (invalid) SpanContext, the graceful own-root path.

// spanCapturingSpawner records the SpanContext present on the ctx of each Spawn
// call, so a test can assert the registry propagated the experiment root span.
type spanCapturingSpawner struct {
	mu       sync.Mutex
	captured []trace.SpanContext
	seq      int
}

func (s *spanCapturingSpawner) Spawn(ctx context.Context, _, _ string, _ uint64) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.captured = append(s.captured, trace.SpanContextFromContext(ctx))
	s.seq++
	return spanCapturingGameID(s.seq), nil
}

func spanCapturingGameID(n int) string {
	return "game_spancap" + string(rune('0'+n%10))
}

func (s *spanCapturingSpawner) first() (trace.SpanContext, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.captured) == 0 {
		return trace.SpanContext{}, false
	}
	return s.captured[0], true
}

// TestSpawnCtxCarriesExperimentSpanContext: with a recording tracer (a valid
// experiment root span), the SpanContext on the spawn ctx equals the experiment's
// exposed root SpanContext — the parent the coordinator will nest the game under.
func TestSpawnCtxCarriesExperimentSpanContext(t *testing.T) {
	spawner := &spanCapturingSpawner{}
	r, _ := rootSpanRegistry(t, RegistryConfig{
		Spawner: spawner, Verdict: fakeVerdict{concludeAtN: 1000}, Targeting: &fakeTargeting{},
		MaxShadowGames: 2, EffectiveConcurrency: 1,
	})
	id := startedExperiment(t, r)

	want, ok := r.ExperimentSpanContext(id)
	if !ok || !want.IsValid() {
		t.Fatal("experiment must expose a valid root SpanContext once RUNNING")
	}

	r.AllocateAndSpawn(context.Background())

	got, ok := spawner.first()
	if !ok {
		t.Fatal("no spawn call recorded")
	}
	if !got.IsValid() {
		t.Fatal("spawn ctx carried no valid SpanContext (experiment root not propagated)")
	}
	if got.TraceID() != want.TraceID() || got.SpanID() != want.SpanID() {
		t.Fatalf("spawn ctx SpanContext = trace=%s span=%s, want trace=%s span=%s",
			got.TraceID(), got.SpanID(), want.TraceID(), want.SpanID())
	}
	if !got.IsRemote() {
		t.Error("propagated experiment SpanContext should be marked remote (a cross-service parent)")
	}
}

// TestSpawnCtxNoSpanContextWhenNoTracer: a registry with the no-op tracer exposes
// no valid root SpanContext, so the spawn ctx carries an invalid SpanContext and
// the spawner injects nothing — the game stays own-rooted (graceful).
func TestSpawnCtxNoSpanContextWhenNoTracer(t *testing.T) {
	spawner := &spanCapturingSpawner{}
	// No Tracer => global no-op provider => invalid (non-recording) root span.
	r := newTestRegistry(t, RegistryConfig{
		Spawner: spawner, Verdict: fakeVerdict{concludeAtN: 1000}, Targeting: &fakeTargeting{},
		MaxShadowGames: 2, EffectiveConcurrency: 1,
	})
	_ = startedExperiment(t, r)

	r.AllocateAndSpawn(context.Background())

	got, ok := spawner.first()
	if !ok {
		t.Fatal("no spawn call recorded")
	}
	if got.IsValid() {
		t.Fatalf("spawn ctx carried a valid SpanContext with no tracer: %v", got)
	}
}
