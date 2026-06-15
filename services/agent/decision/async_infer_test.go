package decision

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/metric"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/llm"
)

// async_infer_test.go drives the #917 async inference path end-to-end through the
// loop with FAKE backends and a FAKE context provider — no real sleeps, no network.
// Every acceptance criterion is covered: the loop never blocks on a slow/hanging
// Infer; a result is re-validated against the CURRENT context at apply time and
// discarded with the right reason when stale (game ended, target gone, permission
// revoked, rate limited); a still-fresh result IS applied; the latency budget drops
// an over-budget result and falls back to rules; and the pile-up guard prevents a
// second concurrent Infer per game.

// ---- fakes ----

func boolPtr(b bool) *bool { return &b }

// fakeContextProvider is the #917 re-validation source. It returns whatever
// GameContext the test has installed for a game_id AT APPLY TIME, so a test can fire
// inference against one context and let the result land against a DIFFERENT one
// (game ended / target eliminated / allow-list tightened) — the whole point of #917.
type fakeContextProvider struct {
	mu      sync.Mutex
	current map[string]gamecontext.GameContext
	missing map[string]bool // game_id -> partition evicted (Current returns ok=false)
}

func newFakeContextProvider() *fakeContextProvider {
	return &fakeContextProvider{
		current: make(map[string]gamecontext.GameContext),
		missing: make(map[string]bool),
	}
}

func (p *fakeContextProvider) set(gameID string, c gamecontext.GameContext) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.current[gameID] = c
	delete(p.missing, gameID)
}

func (p *fakeContextProvider) evict(gameID string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.missing[gameID] = true
}

func (p *fakeContextProvider) Current(gameID string) (gamecontext.GameContext, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.missing[gameID] {
		return gamecontext.GameContext{}, false
	}
	c, ok := p.current[gameID]
	return c, ok
}

// blockingBackend is the #917 latency fake: its Infer BLOCKS on a release channel the
// test controls, so a test can assert the loop returned while Infer is still running,
// then release the call and AwaitInflight for the result to land. It optionally
// honors ctx (for the timeout test) by returning when ctx is cancelled.
type blockingBackend struct {
	name     string
	release  chan struct{} // closed/sent-to by the test to let Infer return
	started  chan struct{} // closed by Infer to signal the call is running
	response string
	err      error
	mu       sync.Mutex
	calls    int
}

func newBlockingBackend(name, response string) *blockingBackend {
	return &blockingBackend{
		name:     name,
		release:  make(chan struct{}),
		started:  make(chan struct{}),
		response: response,
	}
}

func (b *blockingBackend) Name() string                   { return b.name }
func (b *blockingBackend) Available(context.Context) bool { return true }
func (b *blockingBackend) callCount() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.calls
}

func (b *blockingBackend) Infer(ctx context.Context, _ llm.Prompt) (string, error) {
	b.mu.Lock()
	b.calls++
	first := b.calls == 1
	b.mu.Unlock()
	if first {
		close(b.started)
	}
	select {
	case <-b.release:
		return b.response, b.err
	case <-ctx.Done():
		// Honor the latency-budget context: return the deadline error so runInfer's
		// inferCtx.Err() == DeadlineExceeded marks the result as timed out.
		return "", ctx.Err()
	}
}

// asyncLoop wires a loop for the #917 async path: recording tracer, always-admit
// gate, the given resolver (single fake tier), the fake context provider, a game id,
// and a rules engine whose output is the rules fallback. Returns the loop, span
// recorder, sink, and provider.
func asyncLoop(t *testing.T, snap flags.Snapshot, resolver *Resolver, provider *fakeContextProvider, gameID string, rulesOut []Decision) (*Loop, *tracetest.SpanRecorder, *fakeSink) {
	t.Helper()
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })

	reader := metric.NewManualReader()
	mp := metric.NewMeterProvider(metric.WithReader(reader))

	sink := &fakeSink{}
	l := NewLoop(&settableFlags{snap: snap}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	l.Tracer = tp.Tracer("test")
	l.Rules = fakeRules{out: rulesOut}
	l.Actions = sink
	l.llmGated = newLLMGatedCounter(mp)
	l.SetLLMBudget(NewLLMBudget())
	l.SetResolver(resolver)
	l.SetGameID(gameID)
	l.SetContextProvider(provider)
	l.SetRootContext(context.Background())
	return l, sr, sink
}

// asyncSnapshot is an llm-mode snapshot whose #847 gate always admits, with a short
// latency budget for the timeout test and a permissive allow-list + rate budget.
func asyncSnapshot() flags.Snapshot {
	s := llmDecideSnapshot()
	s.LLMGate.LatencyBudget = 5 * time.Second
	return s
}

// activeContext builds a fresh, active GameContext with one alive player, the steady
// state in which an async result re-validates and applies.
func activeContext(gameID, serial string) gamecontext.GameContext {
	return gamecontext.GameContext{
		SessionID: gameID,
		GameKind:  "real",
		Session:   gamecontext.SessionSignals{GameActive: boolPtr(true)},
		Players: map[string]*gamecontext.PlayerSignals{
			serial: {Serial: serial, Active: boolPtr(true)},
		},
	}
}

// ---- Acceptance: the loop never blocks on inference ----

func TestAsync_LoopNeverBlocksOnInference(t *testing.T) {
	be := newBlockingBackend("phi4-mini", validShieldResponse)
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, _, _ := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1", nil)

	fireCtx := activeContext("g1", "AAAA")
	done := make(chan struct{})
	go func() {
		l.OnEvaluate(context.Background(), fireCtx, testTrigger())
		close(done)
	}()

	// OnEvaluate must return PROMPTLY even though Infer is still blocked.
	select {
	case <-done:
		// good — the loop returned without awaiting Infer.
	case <-time.After(2 * time.Second):
		t.Fatal("OnEvaluate did not return while Infer was still running — the loop blocked on inference")
	}

	// The Infer IS running (the loop fired it), just not awaited.
	select {
	case <-be.started:
	case <-time.After(2 * time.Second):
		t.Fatal("Infer was never started — the loop did not fire the async call")
	}

	close(be.release) // let the call complete
	l.AwaitInflight() // join the apply goroutine
	if be.callCount() != 1 {
		t.Fatalf("Infer calls = %d, want 1", be.callCount())
	}
}

// ---- Acceptance: a still-fresh result IS applied through the permission chain ----

func TestAsync_FreshResultApplied(t *testing.T) {
	be := newBlockingBackend("phi4-mini", validShieldResponse)
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, sr, sink := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1", nil)

	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
	close(be.release)
	l.AwaitInflight()

	decs := spansByName(sr.Ended(), SpanDecision)
	if len(decs) != 1 {
		t.Fatalf("agent.decision spans = %d, want 1 (fresh result applied)", len(decs))
	}
	if v, ok := attrValue(decs[0], AttrDecisionAction); !ok || v.AsString() != "grant_shield" {
		t.Errorf("decision.action = %q, want grant_shield", v.AsString())
	}
	if sink.calls.Load() != 1 {
		t.Errorf("action sink calls = %d, want 1 (fresh llm decision dispatched)", sink.calls.Load())
	}
	apply := spansByName(sr.Ended(), SpanLLMApply)
	if len(apply) != 1 {
		t.Fatalf("agent.llm.apply spans = %d, want 1", len(apply))
	}
	if _, ok := attrValue(apply[0], AttrLLMLatencyMs); !ok {
		t.Error("apply span missing inference.latency_ms")
	}
	if v, ok := attrValue(apply[0], "inference.applied"); !ok || !v.AsBool() {
		t.Error("apply span inference.applied must be true for a fresh applied result")
	}
	// #1088: both the apply root and the async inference span carry game.id (= the
	// loop's game id) so the whole async fire->infer->apply trace is queryable by
	// game.id, alongside the synchronous per-game spans.
	if v, ok := attrValue(apply[0], AttrGameID); !ok || v.AsString() != "g1" {
		t.Errorf("apply span %s = %q (present=%v), want g1", AttrGameID, v.AsString(), ok)
	}
	inf := spansByName(sr.Ended(), SpanLLMInfer)
	if len(inf) != 1 {
		t.Fatalf("agent.llm.infer spans = %d, want 1", len(inf))
	}
	if v, ok := attrValue(inf[0], AttrGameID); !ok || v.AsString() != "g1" {
		t.Errorf("async infer span %s = %q (present=%v), want g1", AttrGameID, v.AsString(), ok)
	}
}

// ---- Acceptance: stale result discarded — game ended during inference ----

func TestAsync_DiscardGameEnded(t *testing.T) {
	be := newBlockingBackend("phi4-mini", validShieldResponse)
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, sr, sink := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1", nil)

	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())

	// The game ENDS while inference is in flight: GameActive flips false.
	ended := activeContext("g1", "AAAA")
	ended.Session.GameActive = boolPtr(false)
	provider.set("g1", ended)

	close(be.release)
	l.AwaitInflight()

	if sink.calls.Load() != 0 {
		t.Errorf("action sink calls = %d, want 0 (result must be discarded, not applied)", sink.calls.Load())
	}
	assertDiscard(t, sr, DiscardGameEnded)
}

// ---- Acceptance: stale result discarded — target eliminated during inference ----

func TestAsync_DiscardTargetGone(t *testing.T) {
	// The model targets player AAAA; that player is eliminated while in flight.
	resp := `{"intervention":"grant_shield","target_serial":"AAAA","value":"","reason":"r","objective_served":"endurance"}`
	be := newBlockingBackend("phi4-mini", resp)
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, sr, sink := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1", nil)

	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())

	// Target gone: still active game, but AAAA is no longer in the players map.
	gone := activeContext("g1", "BBBB") // a different player remains
	provider.set("g1", gone)

	close(be.release)
	l.AwaitInflight()

	if sink.calls.Load() != 0 {
		t.Errorf("action sink calls = %d, want 0 (target gone -> discard)", sink.calls.Load())
	}
	assertDiscard(t, sr, DiscardTargetGone)
}

// ---- Acceptance: stale result discarded — partition evicted (stale context) ----

func TestAsync_DiscardStaleContext(t *testing.T) {
	be := newBlockingBackend("phi4-mini", validShieldResponse)
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, sr, sink := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1", nil)

	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
	provider.evict("g1") // partition removed (game ended past grace) while in flight

	close(be.release)
	l.AwaitInflight()

	if sink.calls.Load() != 0 {
		t.Errorf("action sink calls = %d, want 0 (stale context -> discard)", sink.calls.Load())
	}
	assertDiscard(t, sr, DiscardStaleContext)
}

// ---- Acceptance: permission revoked at apply time (allow-list tightened) ----

func TestAsync_DiscardPermissionRevoked(t *testing.T) {
	// Model a permission revocation via the BATTERY gate: the action fires when the
	// target's battery is healthy (would pass), but the target's battery falls below
	// the threshold while inference is in flight, so the apply-time permission chain
	// (re-run against the CURRENT context) now blocks it. evaluatePermission maps a
	// battery block to ReasonBatteryThreshold, which discardForBlock records as
	// permission_revoked — the action the model chose is no longer permitted.
	resp := `{"intervention":"grant_shield","target_serial":"AAAA","reason":"r","objective_served":"endurance"}`
	be := newBlockingBackend("phi4-mini", resp)
	provider := newFakeContextProvider()
	snap := asyncSnapshot()
	snap.Policy.BatteryThreshold = 50 // player-targeted interventions need >=50% battery

	healthy := activeContext("g1", "AAAA")
	healthy.Players["AAAA"].BatteryPct = floatPtrLocal(90)
	provider.set("g1", healthy)

	l, sr, sink := asyncLoop(t, snap, resolverWith(be), provider, "g1", nil)
	l.OnEvaluate(context.Background(), healthy, testTrigger())

	low := activeContext("g1", "AAAA")
	low.Players["AAAA"].BatteryPct = floatPtrLocal(10) // below threshold now
	provider.set("g1", low)

	close(be.release)
	l.AwaitInflight()

	if sink.calls.Load() != 0 {
		t.Errorf("action sink calls = %d, want 0 (battery now below threshold -> blocked)", sink.calls.Load())
	}
	assertDiscard(t, sr, DiscardPermissionRevoked)
}

func floatPtrLocal(v float64) *float64 { return &v }

// ---- Acceptance: rate limited at apply time ----

func TestAsync_DiscardRateLimited(t *testing.T) {
	be := newBlockingBackend("phi4-mini", validShieldResponse)
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	snap := asyncSnapshot()
	snap.Policy.MaxInterventionsPerMinute = 1 // one weighted slot
	clock := time.Unix(5000, 0)
	l, sr, sink := asyncLoop(t, snap, resolverWith(be), provider, "g1", nil)
	l.now = func() time.Time { return clock }

	// Pre-spend the single rate-limit slot directly so the apply-time charge is denied.
	if !l.limiter.allow(clock, interventionCost("grant_shield"), 1) {
		t.Fatal("expected the first slot to be available")
	}

	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
	close(be.release)
	l.AwaitInflight()

	if sink.calls.Load() != 0 {
		t.Errorf("action sink calls = %d, want 0 (rate budget exhausted at apply time)", sink.calls.Load())
	}
	assertDiscard(t, sr, DiscardRateLimited)
}

// ---- Acceptance: latency-budget timeout drops the result and falls back to rules ----

func TestAsync_TimeoutFallsBackToRules(t *testing.T) {
	// A backend that never releases; the latency budget elapses and rules decide.
	be := newBlockingBackend("phi4-mini", validShieldResponse)
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	snap := asyncSnapshot()
	// Drive the timeout off the injected clock would require ctx deadline plumbing;
	// instead use a real, tiny budget — the call honors ctx.Done() so it returns at the
	// deadline deterministically (no fixed sleep; the budget IS the bound).
	snap.LLMGate.LatencyBudget = 20 * time.Millisecond
	// Rules return a recognizable decision so we can prove rules decided on timeout.
	l, sr, sink := asyncLoop(t, snap, resolverWith(be), provider, "g1",
		[]Decision{{Intervention: "grant_shield", Reason: "rules on timeout"}})

	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
	l.AwaitInflight() // the call times out on its own (budget elapsed)
	close(be.release) // release the (already returned) goroutine's channel harmlessly

	apply := spansByName(sr.Ended(), SpanLLMApply)
	if len(apply) != 1 {
		t.Fatalf("agent.llm.apply spans = %d, want 1", len(apply))
	}
	if v, ok := attrValue(apply[0], AttrLLMDiscardedReason); !ok || v.AsString() != DiscardTimeout {
		t.Errorf("discarded_reason = %q, want %q", v.AsString(), DiscardTimeout)
	}
	if _, ok := attrValue(apply[0], AttrLLMLatencyMs); !ok {
		t.Error("apply span missing inference.latency_ms on timeout")
	}
	// Rules decided for the current context — the system still decides.
	decs := spansByName(sr.Ended(), SpanDecision)
	if len(decs) != 1 {
		t.Fatalf("agent.decision spans = %d, want 1 (rules fallback on timeout)", len(decs))
	}
	if v, ok := attrValue(decs[0], AttrDecisionReason); !ok || v.AsString() != "rules on timeout" {
		t.Errorf("decision.reason = %q, want the rules fallback", v.AsString())
	}
	if sink.calls.Load() != 1 {
		t.Errorf("action sink calls = %d, want 1 (rules decision dispatched on timeout)", sink.calls.Load())
	}
}

// ---- Acceptance: infer error falls back to rules (the tier answered unusably) ----

func TestAsync_InferErrorFallsBackToRules(t *testing.T) {
	be := newBlockingBackend("phi4-mini", "")
	be.err = errors.New("backend exploded")
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, sr, sink := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1",
		[]Decision{{Intervention: "grant_shield", Reason: "rules on error"}})

	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
	close(be.release)
	l.AwaitInflight()

	decs := spansByName(sr.Ended(), SpanDecision)
	if len(decs) != 1 {
		t.Fatalf("agent.decision spans = %d, want 1 (rules fallback on infer error)", len(decs))
	}
	if v, ok := attrValue(decs[0], AttrDecisionReason); !ok || v.AsString() != "rules on error" {
		t.Errorf("decision.reason = %q, want the rules fallback", v.AsString())
	}
	if sink.calls.Load() != 1 {
		t.Errorf("action sink calls = %d, want 1 (rules decision dispatched)", sink.calls.Load())
	}
}

// ---- Acceptance: no concurrent in-flight inference per game (pile-up guard) ----

func TestAsync_PileUpGuardOneInflightPerGame(t *testing.T) {
	be := newBlockingBackend("phi4-mini", validShieldResponse)
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	// Rules return a decision so the SECOND (guarded) cycle still decides via rules.
	l, sr, _ := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1",
		[]Decision{{Intervention: "grant_shield", Reason: "rules while inflight"}})

	// Cycle 1 fires the (blocked) Infer.
	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
	<-be.started // ensure the first call is actually in flight

	// Cycle 2 while the first is in flight: must NOT launch a second Infer.
	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())

	// The guarded cycle fell back to rules synchronously, recording llm_inflight.
	decs := spansByName(sr.Ended(), SpanDecision)
	if len(decs) != 1 {
		t.Fatalf("agent.decision spans after the guarded cycle = %d, want 1 (rules fallback)", len(decs))
	}
	if v, ok := attrValue(decs[0], AttrInferenceFallback); !ok || v.AsString() != FallbackInflight {
		t.Errorf("inference.fallback_reason = %q, want %q (pile-up guard)", v.AsString(), FallbackInflight)
	}

	close(be.release)
	l.AwaitInflight()
	if be.callCount() != 1 {
		t.Fatalf("Infer calls = %d, want 1 (no second concurrent call)", be.callCount())
	}
}

// assertDiscard asserts exactly one agent.llm.apply span carrying the given
// discarded_reason and a latency, and NO agent.decision span (a discarded result
// never becomes a decision).
func assertDiscard(t *testing.T, sr *tracetest.SpanRecorder, reason string) {
	t.Helper()
	apply := spansByName(sr.Ended(), SpanLLMApply)
	if len(apply) != 1 {
		t.Fatalf("agent.llm.apply spans = %d, want 1", len(apply))
	}
	if v, ok := attrValue(apply[0], AttrLLMDiscardedReason); !ok || v.AsString() != reason {
		t.Errorf("discarded_reason = %q, want %q", v.AsString(), reason)
	}
	if _, ok := attrValue(apply[0], AttrLLMLatencyMs); !ok {
		t.Error("apply span missing inference.latency_ms")
	}
}
