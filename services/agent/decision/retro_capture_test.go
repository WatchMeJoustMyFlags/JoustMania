package decision

import (
	"context"
	"io"
	"log/slog"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/llm"
)

// retroSnapshot is a representative llm-mode snapshot for the retro capture tests.
func retroFlagSnapshot() flags.Snapshot {
	return flags.Snapshot{
		Enabled:              true,
		Mode:                 "llm",
		Objectives:           map[string]float64{"endurance": 0.7, "chaos": 0.3},
		Capability:           flags.Capability{Model: "phi4-mini"},
		InterventionsAllowed: []string{"noop", "grant_shield"},
	}
}

// recordingRetro builds a RetroCoordinator with a recording tracer and the given
// flag snapshot.
func recordingRetro(t *testing.T, snap flags.Snapshot) (*RetroCoordinator, *tracetest.SpanRecorder) {
	t.Helper()
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	rc := NewRetroCoordinator(&settableFlags{snap: snap}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	rc.Tracer = tp.Tracer("test")
	return rc, sr
}

// endedSession is a finished-game GameContext: GameActive false, a session id, an
// elimination sequence, and a survivor.
func endedSession() gamecontext.GameContext {
	active := false
	dur := 90.0
	return gamecontext.GameContext{
		SessionID: "session-7",
		GameKind:  "real",
		Session: gamecontext.SessionSignals{
			GameActive:          &active,
			DurationSeconds:     &dur,
			EliminationSequence: []string{"BB:22"},
		},
		Players: map[string]*gamecontext.PlayerSignals{
			"AA:11": {Serial: "AA:11"},
			"BB:22": {Serial: "BB:22"},
		},
	}
}

// TestRetroCapture_EmitsSpan: a normal game end emits exactly one agent.llm.retro
// span with non-empty prompt text.
func TestRetroCapture_EmitsSpan(t *testing.T) {
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	rc.OnGameEnd(endedSession())

	spans := spansByName(sr.Ended(), SpanLLMRetro)
	if len(spans) != 1 {
		t.Fatalf("agent.llm.retro spans = %d, want 1", len(spans))
	}
	if v, ok := attrValue(spans[0], AttrLLMRetroSystem); !ok || v.AsString() == "" {
		t.Error("llm.retro.system must be present and non-empty")
	}
	if v, ok := attrValue(spans[0], AttrLLMRetroUser); !ok || v.AsString() == "" {
		t.Error("llm.retro.user must be present and non-empty")
	}
}

// TestRetroCapture_DrawsFromGlobalBudget: a retro is one LLM request and draws
// from the SAME shared budget the in-game gate uses (#847 acceptance #7). When the
// budget is exhausted, the retro is GATED: no agent.llm.retro span is emitted.
func TestRetroCapture_DrawsFromGlobalBudget(t *testing.T) {
	snap := retroFlagSnapshot()
	snap.LLMGate = flags.LLMGate{MaxRequestsPerMinute: 1}

	rc, sr := recordingRetro(t, snap)
	budget := NewLLMBudget()
	rc.SetLLMBudget(budget)
	now := time.Unix(1000, 0)
	rc.now = func() time.Time { return now }

	// Pre-spend the single budget slot, mimicking an in-game LLM attempt this minute.
	if !budget.allow(now, 1) {
		t.Fatal("pre-spend of the single budget slot should succeed")
	}

	rc.OnGameEnd(endedSession())
	if n := len(spansByName(sr.Ended(), SpanLLMRetro)); n != 0 {
		t.Errorf("agent.llm.retro spans = %d, want 0 (budget exhausted, retro gated)", n)
	}
}

// TestRetroCapture_CapturesWhenBudgetAvailable: with a budget wired and a free
// slot, the retro captures normally AND consumes one budget slot.
func TestRetroCapture_CapturesWhenBudgetAvailable(t *testing.T) {
	snap := retroFlagSnapshot()
	snap.LLMGate = flags.LLMGate{MaxRequestsPerMinute: 2}

	rc, sr := recordingRetro(t, snap)
	budget := NewLLMBudget()
	rc.SetLLMBudget(budget)
	now := time.Unix(1000, 0)
	rc.now = func() time.Time { return now }

	rc.OnGameEnd(endedSession())
	if n := len(spansByName(sr.Ended(), SpanLLMRetro)); n != 1 {
		t.Errorf("agent.llm.retro spans = %d, want 1 (budget available)", n)
	}
	// The retro consumed one of the two slots; only one remains.
	if !budget.allow(now, 2) {
		t.Error("one budget slot should remain after the retro")
	}
	if budget.allow(now, 2) {
		t.Error("budget should be exhausted after retro + one more (cap 2)")
	}
}

// TestRetroCapture_SchemaComplete: every attribute of the agent.llm.retro schema
// is present, with inference.used == "none" (the documented divergence — no rules
// fallback for a retrospective).
func TestRetroCapture_SchemaComplete(t *testing.T) {
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	rc.OnGameEnd(endedSession())

	span := spansByName(sr.Ended(), SpanLLMRetro)[0]
	wantStr := map[string]string{
		"gen_ai.operation.name":  "chat",
		"gen_ai.request.model":   "phi4-mini",
		"gen_ai.output.type":     "json",
		AttrMode:                 "retro",
		AttrGameKind:             "real",
		AttrObjectives:           "chaos=0.3,endurance=0.7",
		AttrInterventionsAllowed: "noop,grant_shield",
		"session.id":             "session-7",
		// #1088: the retro span carries game.id (= the session id) so a Jaeger query
		// by game.id surfaces the post-game retrospective alongside the in-game spans.
		AttrGameID:              "session-7",
		AttrInferenceConfigured: "phi4-mini",
		AttrInferenceUsed:       DefaultInference, // "none", NOT "rules"
		AttrInferenceFallback:   FallbackNoBackend,
	}
	for key, expected := range wantStr {
		if v, ok := attrValue(span, key); !ok || v.AsString() != expected {
			t.Errorf("attr %s = %q (present=%v), want %q", key, v.AsString(), ok, expected)
		}
	}
	// Explicit divergence guard: never "rules" on a retro span.
	if v, _ := attrValue(span, AttrInferenceUsed); v.AsString() == InferenceRules {
		t.Errorf("inference.used = %q, must NOT be %q for a retrospective", v.AsString(), InferenceRules)
	}
	// llm.retro.bytes equals the combined length of the two prompt texts.
	sys, _ := attrValue(span, AttrLLMRetroSystem)
	usr, _ := attrValue(span, AttrLLMRetroUser)
	wantBytes := int64(len(sys.AsString()) + len(usr.AsString()))
	if v, ok := attrValue(span, AttrLLMRetroBytes); !ok || v.AsInt64() != wantBytes {
		t.Errorf("llm.retro.bytes = %d, want %d", v.AsInt64(), wantBytes)
	}
}

// TestRetroCapture_ExactlyOncePerSession: the same SessionID twice emits ONE
// span; a new SessionID emits a second.
func TestRetroCapture_ExactlyOncePerSession(t *testing.T) {
	rc, sr := recordingRetro(t, retroFlagSnapshot())

	rc.OnGameEnd(endedSession())
	rc.OnGameEnd(endedSession()) // same session-7: deduped
	if n := len(spansByName(sr.Ended(), SpanLLMRetro)); n != 1 {
		t.Fatalf("agent.llm.retro spans = %d, want 1 (same session deduped)", n)
	}

	next := endedSession()
	next.SessionID = "session-8"
	rc.OnGameEnd(next)
	if n := len(spansByName(sr.Ended(), SpanLLMRetro)); n != 2 {
		t.Fatalf("agent.llm.retro spans = %d, want 2 (new session)", n)
	}
}

// TestRetroCapture_InterleavedGameEndDedupe: with concurrent games (#845 PR B),
// game-ends interleave (A ends, B ends, A's id AGAIN). The bounded LRU dedupe must
// suppress the repeated A while still having captured both A and B exactly once —
// the old single last-session string would have re-captured A here.
func TestRetroCapture_InterleavedGameEndDedupe(t *testing.T) {
	rc, sr := recordingRetro(t, retroFlagSnapshot())

	end := func(id string) {
		c := endedSession()
		c.SessionID = id
		rc.OnGameEnd(c)
	}

	end("game-A")
	end("game-B")
	end("game-A") // repeat of an already-captured session: must be suppressed

	if n := len(spansByName(sr.Ended(), SpanLLMRetro)); n != 2 {
		t.Fatalf("agent.llm.retro spans = %d, want 2 (A,B once each; repeated A deduped)", n)
	}
}

// TestRetroCapture_DedupeWindowBounded: once more than retroDedupeWindow distinct
// sessions have been captured, the oldest falls out of the LRU and a repeat of it
// captures again — this bounds dedupe memory and is acceptable (a session id is
// never legitimately re-ended after that many newer games).
func TestRetroCapture_DedupeWindowBounded(t *testing.T) {
	rc, sr := recordingRetro(t, retroFlagSnapshot())

	end := func(id string) {
		c := endedSession()
		c.SessionID = id
		rc.OnGameEnd(c)
	}

	end("game-old")
	// Fill the window with retroDedupeWindow fresh sessions, evicting "game-old".
	for i := 0; i < retroDedupeWindow; i++ {
		end("fill-" + string(rune('a'+i)))
	}
	// "game-old" has aged out of the window, so it captures a second time.
	end("game-old")

	want := retroDedupeWindow + 2 // game-old (x2) + the window-fill sessions
	if n := len(spansByName(sr.Ended(), SpanLLMRetro)); n != want {
		t.Fatalf("agent.llm.retro spans = %d, want %d", n, want)
	}
}

// TestRetroCapture_SkipsActiveGame: a snapshot still reporting GameActive=true
// emits nothing (defensive — only end-of-game captures).
func TestRetroCapture_SkipsActiveGame(t *testing.T) {
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	c := endedSession()
	active := true
	c.Session.GameActive = &active
	rc.OnGameEnd(c)
	if n := len(spansByName(sr.Ended(), SpanLLMRetro)); n != 0 {
		t.Errorf("agent.llm.retro spans = %d, want 0 for an active game", n)
	}
}

// TestRetroCapture_SkipsEmptySession: an empty SessionID emits nothing.
func TestRetroCapture_SkipsEmptySession(t *testing.T) {
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	c := endedSession()
	c.SessionID = ""
	rc.OnGameEnd(c)
	if n := len(spansByName(sr.Ended(), SpanLLMRetro)); n != 0 {
		t.Errorf("agent.llm.retro spans = %d, want 0 for an empty session id", n)
	}
}

// TestRetroCapture_ConfiguredBackendReachable is the #1080 fix: with a real inference
// backend wired via the SHARED resolver (AGENT_INFERENCE_BACKEND=openai → gemma4) and
// reachable, the retro span and log report the CONFIGURED model (gemma4:latest), use
// it as the resolved tier, and carry NO fallback reason — NOT the agent.model flag's
// (phi4-mini) unreachable legacy tier with no_backend_available.
func TestRetroCapture_ConfiguredBackendReachable(t *testing.T) {
	addr, closeFn := listenLocal(t)
	defer closeFn()

	const configured = "gemma4:latest"
	delegate := func(context.Context, llm.Prompt) (string, error) { return `{"intervention":"noop"}`, nil }
	// The flag snapshot still says phi4-mini (the ci default) — the configured backend
	// must win regardless, exactly like the decision loop.
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	r := NewInferenceResolver(InferenceTier{Model: configured, Addr: addr, Infer: delegate}, Endpoints{}, 0)
	r.Refresh(context.Background())
	rc.SetResolver(r)

	rc.OnGameEnd(endedSession())

	span := spansByName(sr.Ended(), SpanLLMRetro)[0]
	want := map[string]string{
		"gen_ai.request.model":  configured,
		AttrInferenceConfigured: configured,
		AttrInferenceUsed:       configured,
		AttrInferenceFallback:   "", // configured backend reachable: no degradation
	}
	for key, expected := range want {
		if v, ok := attrValue(span, key); !ok || v.AsString() != expected {
			t.Errorf("attr %s = %q (present=%v), want %q", key, v.AsString(), ok, expected)
		}
	}
}

// TestRetroCapture_ConfiguredBackendUnreachable: with a configured backend wired but
// unreachable (and the legacy chain down), the retro still ATTRIBUTES the configured
// model (gemma4), but honestly reports used="none" (the retro DIVERGENCE — no rules
// fallback at game end) with no_backend_available. This is the genuine-unreachable
// degradation, distinct from the #1080 bug where it always hit phi4-mini.
func TestRetroCapture_ConfiguredBackendUnreachable(t *testing.T) {
	const configured = "gemma4:latest"
	delegate := func(context.Context, llm.Prompt) (string, error) { return "", nil }
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	// Addr nothing listens on → probe fails; legacy endpoints empty → all unreachable.
	r := NewInferenceResolver(InferenceTier{Model: configured, Addr: "127.0.0.1:1", Infer: delegate}, Endpoints{}, 0)
	r.Refresh(context.Background())
	rc.SetResolver(r)

	rc.OnGameEnd(endedSession())

	span := spansByName(sr.Ended(), SpanLLMRetro)[0]
	want := map[string]string{
		"gen_ai.request.model":  configured, // still attribute the configured model
		AttrInferenceConfigured: configured,
		AttrInferenceUsed:       DefaultInference, // "none" — retro has no rules fallback
		AttrInferenceFallback:   FallbackNoBackend,
	}
	for key, expected := range want {
		if v, ok := attrValue(span, key); !ok || v.AsString() != expected {
			t.Errorf("attr %s = %q (present=%v), want %q", key, v.AsString(), ok, expected)
		}
	}
	if v, _ := attrValue(span, AttrInferenceUsed); v.AsString() == InferenceRules {
		t.Errorf("inference.used = %q, must NOT be %q for a retrospective", v.AsString(), InferenceRules)
	}
}

// attrAbsent fails if the named attribute is present on the span — used to assert
// the #1100 experiment.* correlation attrs are cleanly OMITTED for a non-experiment
// game (no empty/garbage tags).
func attrAbsent(t *testing.T, span sdktrace.ReadOnlySpan, key string) {
	t.Helper()
	if _, ok := attrValue(span, key); ok {
		t.Errorf("attr %s must be ABSENT, but it is present", key)
	}
}

// TestRetroCapture_OutcomeAttributes: every retro span carries the STRUCTURED
// game-outcome attributes (#1100) derived from the GameContext (not the prompt
// text). endedSession() has survivor AA:11, one elimination (BB:22), duration 90,
// and no observed active-player count.
func TestRetroCapture_OutcomeAttributes(t *testing.T) {
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	rc.OnGameEnd(endedSession())

	span := spansByName(sr.Ended(), SpanLLMRetro)[0]
	if v, ok := attrValue(span, AttrGameWinner); !ok || v.AsString() != "AA:11" {
		t.Errorf("game.winner = %q (present=%v), want %q", v.AsString(), ok, "AA:11")
	}
	if v, ok := attrValue(span, AttrGameEliminationCount); !ok || v.AsInt64() != 1 {
		t.Errorf("game.elimination_count = %d (present=%v), want 1", v.AsInt64(), ok)
	}
	if v, ok := attrValue(span, AttrGameDurationSeconds); !ok || v.AsFloat64() != 90.0 {
		t.Errorf("game.duration_seconds = %v (present=%v), want 90", v.AsFloat64(), ok)
	}
	// No active-player count was observed, so the attribute is cleanly omitted (not 0).
	attrAbsent(t, span, AttrGameFinalActivePlayers)
}

// TestRetroCapture_ExperimentAttributesPresent: when the finished game was a
// shadow-experiment arm (ExperimentID/Arm on the GameContext) AND a recorded
// fitness sample is available via the lookup seam, the retro span stamps
// experiment.id / experiment.arm / experiment.fitness (#1100).
func TestRetroCapture_ExperimentAttributesPresent(t *testing.T) {
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	rc.SetFitnessLookup(func(gameID string) (float64, bool) {
		if gameID == "session-7" {
			return 0.42, true
		}
		return 0, false
	})

	c := endedSession()
	c.GameKind = "shadow"
	c.ExperimentID = "exp_abc123def456"
	c.Arm = "experimental"
	rc.OnGameEnd(c)

	span := spansByName(sr.Ended(), SpanLLMRetro)[0]
	if v, ok := attrValue(span, AttrExperimentID); !ok || v.AsString() != "exp_abc123def456" {
		t.Errorf("experiment.id = %q (present=%v), want %q", v.AsString(), ok, "exp_abc123def456")
	}
	if v, ok := attrValue(span, AttrExperimentArm); !ok || v.AsString() != "experimental" {
		t.Errorf("experiment.arm = %q (present=%v), want %q", v.AsString(), ok, "experimental")
	}
	if v, ok := attrValue(span, AttrExperimentFitness); !ok || v.AsFloat64() != 0.42 {
		t.Errorf("experiment.fitness = %v (present=%v), want 0.42", v.AsFloat64(), ok)
	}
}

// TestRetroCapture_ExperimentAttributesAbsentForRealGame: a non-experiment (real /
// standalone) game carries NO experiment.* correlation attrs — they are cleanly
// omitted, never emitted empty/garbage (#1100). The structured outcome attrs are
// still present (they apply to every game).
func TestRetroCapture_ExperimentAttributesAbsentForRealGame(t *testing.T) {
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	// A fitness lookup IS wired, but the game is not an experiment, so it must never
	// be consulted and experiment.fitness must be absent.
	rc.SetFitnessLookup(func(string) (float64, bool) { return 0.99, true })

	rc.OnGameEnd(endedSession()) // ExperimentID == "" (a real game)

	span := spansByName(sr.Ended(), SpanLLMRetro)[0]
	attrAbsent(t, span, AttrExperimentID)
	attrAbsent(t, span, AttrExperimentArm)
	attrAbsent(t, span, AttrExperimentFitness)
	// Outcome attrs are game-agnostic, so they are still present.
	if _, ok := attrValue(span, AttrGameEliminationCount); !ok {
		t.Error("game.elimination_count must be present for a real game too")
	}
}

// TestRetroCapture_ExperimentFitnessOmittedWhenUnavailable: an experiment game with
// id/arm but NO recorded fitness yet (lookup reports not-found, or no lookup wired)
// stamps experiment.id / experiment.arm but OMITS experiment.fitness — default-safe
// (#1100).
func TestRetroCapture_ExperimentFitnessOmittedWhenUnavailable(t *testing.T) {
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	// No fitness lookup wired at all (the pre-#1100 / nil-seam default).
	c := endedSession()
	c.ExperimentID = "exp_no_fitness"
	c.Arm = "control"
	rc.OnGameEnd(c)

	span := spansByName(sr.Ended(), SpanLLMRetro)[0]
	if v, ok := attrValue(span, AttrExperimentID); !ok || v.AsString() != "exp_no_fitness" {
		t.Errorf("experiment.id = %q (present=%v), want %q", v.AsString(), ok, "exp_no_fitness")
	}
	if v, ok := attrValue(span, AttrExperimentArm); !ok || v.AsString() != "control" {
		t.Errorf("experiment.arm = %q (present=%v), want %q", v.AsString(), ok, "control")
	}
	attrAbsent(t, span, AttrExperimentFitness)
}

// TestRetroCapture_StubDefaultUnchanged: with NO resolver wired (the stub default),
// the retro attribution is byte-identical to pre-#1080 — model = the agent.model flag
// (phi4-mini), used = "none", fallback = no_backend_available. This is the
// default-safety guarantee: the stub path does not change.
func TestRetroCapture_StubDefaultUnchanged(t *testing.T) {
	rc, sr := recordingRetro(t, retroFlagSnapshot()) // no SetResolver
	rc.OnGameEnd(endedSession())

	span := spansByName(sr.Ended(), SpanLLMRetro)[0]
	want := map[string]string{
		"gen_ai.request.model":  "phi4-mini",
		AttrInferenceConfigured: "phi4-mini",
		AttrInferenceUsed:       DefaultInference,
		AttrInferenceFallback:   FallbackNoBackend,
	}
	for key, expected := range want {
		if v, ok := attrValue(span, key); !ok || v.AsString() != expected {
			t.Errorf("attr %s = %q (present=%v), want %q", key, v.AsString(), ok, expected)
		}
	}
}
