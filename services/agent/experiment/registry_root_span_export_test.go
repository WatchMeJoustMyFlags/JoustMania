package experiment

import (
	"context"
	"testing"

	"go.opentelemetry.io/otel"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/decision"
)

// #1202: the existing root-span tests inject a recording tracer into RegistryConfig.
// That is the right shape for asserting LIFECYCLE (start/end/idempotency), but it
// never exercises the PRODUCTION wiring — a nil cfg.Tracer that makes NewRegistry pull
// the tracer from the GLOBAL provider via otel.Tracer(...). The agent's #1140 analysis
// spans (experiment.evaluate/fitness/verdict) DO export from that global provider, so
// the root span — created by the SAME registry from the SAME instrumentation scope —
// must too. These tests pin that: with a real BatchSpanProcessor-backed global provider
// and NO injected tracer, the agent.experiment ROOT span is recording, sampled, and
// actually EXPORTED on End, carrying its identifying attrs (incl. #1184 experiment.thesis).
//
// They guard against the whole class of "the root span silently never reaches the
// exporter" regression (a no-op/default tracer, an unsampled span context, or a tracer
// captured from the wrong provider) that an injected-tracer test cannot catch.

// globalProviderRegistry installs a real WithBatcher global provider and builds a
// registry with NO injected tracer, so the registry resolves otel.Tracer(...) exactly
// as production does. It returns the registry and the in-memory exporter the provider
// flushes into.
func globalProviderRegistry(t *testing.T, cfg RegistryConfig) (*Registry, *tracetest.InMemoryExporter, *sdktrace.TracerProvider) {
	t.Helper()
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithBatcher(exp))
	prev := otel.GetTracerProvider()
	otel.SetTracerProvider(tp)
	t.Cleanup(func() {
		_ = tp.Shutdown(context.Background())
		otel.SetTracerProvider(prev)
	})
	// cfg.Tracer intentionally LEFT nil => NewRegistry defaults to otel.Tracer(...) —
	// the production default the live agent uses.
	cfg.Tracer = nil
	return newTestRegistry(t, cfg), exp, tp
}

// exportedRootSpans returns the EXPORTED (flushed) spans named agent.experiment.
func exportedRootSpans(exp *tracetest.InMemoryExporter) []tracetest.SpanStub {
	var out []tracetest.SpanStub
	for _, s := range exp.GetSpans() {
		if s.Name == decision.SpanExperiment {
			out = append(out, s)
		}
	}
	return out
}

// TestRootSpan_ExportsFromGlobalProvider (#1202): a registry on the production tracer
// path (nil cfg.Tracer => otel.Tracer(global)) creates a RECORDING, SAMPLED root span
// whose SpanContext is exposed for re-parenting, and the span is EXPORTED on End.
func TestRootSpan_ExportsFromGlobalProvider(t *testing.T) {
	r, exp, tp := globalProviderRegistry(t, RegistryConfig{
		Spawner: &fakeSpawner{}, Verdict: fakeVerdict{concludeAtN: 1000}, Targeting: &fakeTargeting{},
		MaxShadowGames: 2,
	})
	id := startedExperiment(t, r)

	// While RUNNING the root span must be a VALID, SAMPLED SpanContext — this is what the
	// analysis spans re-parent under. An invalid/unsampled context (the live-stack failure
	// mode) would make every re-parent silently fall back to own-root.
	sc, ok := r.ExperimentSpanContext(id)
	if !ok {
		t.Fatal("global-provider registry must expose a live experiment SpanContext while RUNNING")
	}
	if !sc.IsValid() {
		t.Error("root span SpanContext must be valid (non-zero trace/span id) from the global provider")
	}
	if !sc.IsSampled() {
		t.Error("root span SpanContext must be SAMPLED (default ParentBased/AlwaysSample) so it and its children export")
	}

	// Nothing is flushed until the span ends.
	if n := len(exportedRootSpans(exp)); n != 0 {
		t.Fatalf("no agent.experiment span should export before End, got %d", n)
	}

	if err := r.Abort(context.Background(), id, "done"); err != nil {
		t.Fatalf("abort: %v", err)
	}
	if err := tp.ForceFlush(context.Background()); err != nil {
		t.Fatalf("force flush: %v", err)
	}

	spans := exportedRootSpans(exp)
	if len(spans) != 1 {
		t.Fatalf("agent.experiment EXPORTED spans = %d, want exactly 1 (the live-stack bug exported 0)", len(spans))
	}
	got := spans[0]
	if !got.SpanContext.IsSampled() {
		t.Error("exported agent.experiment span must be sampled")
	}
	if got.Parent.IsValid() {
		t.Error("agent.experiment must be a trace ROOT (no valid parent)")
	}
	// The SpanContext exposed while live must match the one that exported, proving the
	// re-parent target is the very span that reaches the collector.
	if got.SpanContext.TraceID() != sc.TraceID() || got.SpanContext.SpanID() != sc.SpanID() {
		t.Errorf("exported span %s/%s != exposed SpanContext %s/%s",
			got.SpanContext.TraceID(), got.SpanContext.SpanID(), sc.TraceID(), sc.SpanID())
	}
}

// TestRootSpan_ExportsThesisAttrFromGlobalProvider (#1202/#1184): the EXPORTED root span
// (production tracer path) carries experiment.id and experiment.thesis, so the trace —
// not just the dossier endpoint — records the experiment's WHY.
func TestRootSpan_ExportsThesisAttrFromGlobalProvider(t *testing.T) {
	r, exp, tp := globalProviderRegistry(t, RegistryConfig{
		Spawner: &fakeSpawner{}, Verdict: fakeVerdict{concludeAtN: 1000}, Targeting: &fakeTargeting{},
		MaxShadowGames: 2,
	})
	id := startedExperiment(t, r)
	if err := r.Abort(context.Background(), id, "done"); err != nil {
		t.Fatalf("abort: %v", err)
	}
	if err := tp.ForceFlush(context.Background()); err != nil {
		t.Fatalf("force flush: %v", err)
	}

	spans := exportedRootSpans(exp)
	if len(spans) != 1 {
		t.Fatalf("agent.experiment EXPORTED spans = %d, want 1", len(spans))
	}
	attrs := map[string]string{}
	for _, kv := range spans[0].Attributes {
		attrs[string(kv.Key)] = kv.Value.Emit()
	}
	if attrs[decision.AttrExperimentID] != id {
		t.Errorf("%s = %q, want %q", decision.AttrExperimentID, attrs[decision.AttrExperimentID], id)
	}
	if want := sampleIntent().Hypothesis; attrs[decision.AttrExperimentThesis] != want {
		t.Errorf("%s = %q, want %q", decision.AttrExperimentThesis, attrs[decision.AttrExperimentThesis], want)
	}
}
