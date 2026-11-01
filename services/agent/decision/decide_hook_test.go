package decision

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/open-feature/go-sdk/openfeature"
	"github.com/open-feature/go-sdk/openfeature/memprovider"
	"go.opentelemetry.io/otel/sdk/metric"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// decide_hook_test.go covers the #1226 wiring: the default rules path stays
// byte-identical while gaining an agent.decide span, and decision.backend != rules
// hands the decision to a (fake) remote decide.Backend on the async path.

// ---- characterization: one ranking, unchanged decisions ----

// evaluateOnly hides EvaluateDecide so the loop takes the pre-#1226 plain
// Evaluate path: the reference the hooked path must match.
type evaluateOnly struct{ r *ObjectiveRules }

func (e evaluateOnly) Evaluate(ctx context.Context, c gamecontext.GameContext) []Decision {
	return e.r.Evaluate(ctx, c)
}

// TestEvaluateDecide_MatchesEvaluate pins that EvaluateDecide returns exactly
// Evaluate's decisions (including the seeded R9 chaos picks and the evalInterval
// throttle) and leaves the RNG in the same state: one draw per evaluation.
func TestEvaluateDecide_MatchesEvaluate(t *testing.T) {
	for _, fx := range decideFixtures() {
		t.Run(fx.name, func(t *testing.T) {
			clockA, clockB := time.Unix(10_000, 0), time.Unix(10_000, 0)
			a := newTestEngine(fx.cfg, &clockA)
			b := newTestEngine(fx.cfg, &clockB)
			for step := 0; step < 6; step++ {
				want := a.Evaluate(context.Background(), fx.ctx)
				got, req, resp, ranked := b.EvaluateDecide(context.Background(), fx.ctx, func(_, _ map[string]bool) decide.Request {
					return ffaRequest(fx.ctx, allInterventions())
				})
				if !reflect.DeepEqual(got, want) {
					t.Fatalf("step %d: EvaluateDecide decisions = %+v, Evaluate = %+v", step, got, want)
				}
				if ranked {
					if err := decide.Validate(req, resp); err != nil {
						t.Fatalf("step %d: answers do not validate: %v", step, err)
					}
					iv, _ := resp.Find(decide.QuestionFFAIntervention)
					if len(want) > 0 && iv.Choice != want[0].Intervention {
						t.Errorf("step %d: answer %q, first decision %q: not from the same ranking", step, iv.Choice, want[0].Intervention)
					}
				}
				// Half the steps land inside evalInterval (throttled, no ranking).
				d := 600 * time.Millisecond
				advance(&clockA, d)
				advance(&clockB, d)
			}
			if a.rng.Float64() != b.rng.Float64() {
				t.Fatal("RNG diverged: EvaluateDecide drew a different number of chaos rolls than Evaluate")
			}
		})
	}
}

// capturingSink records the decisions the loop applied.
type capturingSink struct {
	mu  sync.Mutex
	got []Decision
}

func (s *capturingSink) Apply(_ context.Context, d Decision) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.got = append(s.got, d)
	return nil
}

func (s *capturingSink) decisions() []Decision {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]Decision(nil), s.got...)
}

func decideRecordingLoop(t *testing.T, snap flags.Snapshot) (*Loop, *tracetest.SpanRecorder) {
	t.Helper()
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	l := NewLoop(&settableFlags{snap: snap}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	l.Tracer = tp.Tracer("test")
	return l, sr
}

func rulesSnapshot() flags.Snapshot {
	return flags.Snapshot{
		Enabled:              true,
		InterventionsAllowed: allInterventions(),
		Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
		Decision: flags.DecisionBackend{
			Backend:                flags.DecisionBackendRules,
			ActConfidenceThreshold: flags.DefaultDecisionActConfidenceThreshold,
		},
	}
}

// TestDefaultRulesPath_ByteIdentical drives two loops with identically seeded
// engines through the same contexts: one via the plain Evaluate path (the pre-#1226
// behavior), one via the decide hook. Applied decisions and outcomes must match.
func TestDefaultRulesPath_ByteIdentical(t *testing.T) {
	totalDecided := 0
	t.Cleanup(func() {
		if totalDecided == 0 {
			t.Error("no fixture produced a decision: the comparison is vacuous")
		}
	})
	for _, fx := range decideFixtures() {
		t.Run(fx.name, func(t *testing.T) {
			clockA, clockB := time.Unix(10_000, 0), time.Unix(10_000, 0)
			ref, _ := decideRecordingLoop(t, rulesSnapshot())
			hooked, sr := decideRecordingLoop(t, rulesSnapshot())
			ref.Rules = evaluateOnly{newTestEngine(fx.cfg, &clockA)}
			hooked.Rules = newTestEngine(fx.cfg, &clockB)
			refSink, hookedSink := &capturingSink{}, &capturingSink{}
			ref.Actions, hooked.Actions = refSink, hookedSink

			decided := 0
			for step := 0; step < 6; step++ {
				sa := ref.OnEvaluate(context.Background(), fx.ctx, testTrigger())
				sb := hooked.OnEvaluate(context.Background(), fx.ctx, testTrigger())
				if !reflect.DeepEqual(sa.Outcomes, sb.Outcomes) {
					t.Fatalf("step %d: outcomes differ: %+v vs %+v", step, sa.Outcomes, sb.Outcomes)
				}
				if len(sb.Outcomes) > 0 {
					decided++
				}
				advance(&clockA, 600*time.Millisecond)
				advance(&clockB, 600*time.Millisecond)
			}
			if !reflect.DeepEqual(refSink.decisions(), hookedSink.decisions()) {
				t.Fatalf("applied decisions differ:\nref    %+v\nhooked %+v", refSink.decisions(), hookedSink.decisions())
			}
			totalDecided += decided
			if n := len(spansByName(sr.Ended(), SpanDecide)); n != decided {
				t.Fatalf("agent.decide spans = %d, want one per decided cycle (%d)", n, decided)
			}
		})
	}
}

func strAttr(t *testing.T, span sdktrace.ReadOnlySpan, key string) string {
	t.Helper()
	v, ok := attrValue(span, key)
	if !ok {
		t.Fatalf("%s: attribute %s missing", span.Name(), key)
	}
	return v.AsString()
}

// UC: UC-040 A1
// UC: UC-070 main
func TestDefaultRulesPath_DecideSpan(t *testing.T) {
	fx := decideFixtures()[0] // endurance: rules decide on the first evaluation
	fx.ctx.GameKind = "real"
	clock := time.Unix(10_000, 0)
	l, sr := decideRecordingLoop(t, rulesSnapshot())
	l.Rules = newTestEngine(fx.cfg, &clock)
	l.OnEvaluate(context.Background(), fx.ctx, testTrigger())

	spans := sr.Ended()
	ds := spansByName(spans, SpanDecide)
	if len(ds) != 1 {
		t.Fatalf("agent.decide spans = %d, want 1", len(ds))
	}
	span := ds[0]
	roots := spansByName(spans, SignalReceived)
	if len(roots) != 1 || span.Parent().SpanID() != roots[0].SpanContext().SpanID() {
		t.Fatal("agent.decide must be a child of agent.signal_received on the rules path")
	}
	for key, want := range map[string]string{
		AttrDecideBackend:          "rules",
		AttrDecideBackendRequested: "rules",
		AttrDecideFallbackReason:   "",
		AttrDecideOutcome:          DecideOutcomeAct,
		"gen_ai.agent.name":        AgentName,
		"gen_ai.conversation.id":   "s-1",
		AttrGameID:                 "s-1",
		AttrGameKind:               "real",
	} {
		if got := strAttr(t, span, key); got != want {
			t.Errorf("%s = %q, want %q", key, got, want)
		}
	}
	if d := strAttr(t, span, AttrDecideStateDigest); len(d) != 64 {
		t.Errorf("digest = %q, want sha256 hex", d)
	}
	var qs []map[string]any
	if err := json.Unmarshal([]byte(strAttr(t, span, AttrDecideQuestions)), &qs); err != nil || len(qs) != 3 {
		t.Fatalf("decide.questions not the 3 FFA questions as JSON: %v %v", qs, err)
	}
	var as []decide.Answer
	if err := json.Unmarshal([]byte(strAttr(t, span, AttrDecideAnswers)), &as); err != nil || len(as) != 3 {
		t.Fatalf("decide.answers not 3 answers as JSON: %v", err)
	}
	if as[0].Choice != InterventionAdjustMusicTempo {
		t.Errorf("rules answer = %q, want the top-ranked adjust_music_tempo", as[0].Choice)
	}
	for _, key := range []string{AttrDecideConfidence, AttrDecideConfidenceMin, AttrDecideLatencyMs} {
		if _, ok := attrValue(span, key); !ok {
			t.Errorf("attribute %s missing", key)
		}
	}
	if _, ok := attrValue(span, AttrDecideActThreshold); ok {
		t.Error("rules do not route by confidence: no decide.act_confidence_threshold")
	}
	if len(span.Events()) != 0 {
		t.Error("rules path does not route by confidence: no gen_ai.evaluation.result event")
	}
	dec := spansByName(spans, SpanDecision)
	if len(dec) == 0 || strAttr(t, dec[0], AttrDecideBackend) != "rules" {
		t.Error("agent.decision must carry decide.backend=rules")
	}
}

// ---- remote path with a fake decide.Backend ----

type fakeDecideBackend struct {
	name      string
	available bool
	answer    func(req decide.Request) (decide.Response, error)
	block     bool // wait for ctx cancellation (timeout) or release

	mu      sync.Mutex
	calls   int
	reqs    []decide.Request
	started chan struct{}
	release chan struct{}
}

func newFakeDecideBackend(answer func(decide.Request) (decide.Response, error)) *fakeDecideBackend {
	return &fakeDecideBackend{
		name: "claude", available: true, answer: answer,
		started: make(chan struct{}), release: make(chan struct{}),
	}
}

func (b *fakeDecideBackend) Name() string    { return b.name }
func (b *fakeDecideBackend) Available() bool { return b.available }

func (b *fakeDecideBackend) Decide(ctx context.Context, req decide.Request) (decide.Response, error) {
	b.mu.Lock()
	b.calls++
	b.reqs = append(b.reqs, req)
	first := b.calls == 1
	b.mu.Unlock()
	if first {
		close(b.started)
	}
	if b.block {
		select {
		case <-ctx.Done():
			return decide.Response{}, ctx.Err()
		case <-b.release:
		}
	}
	return b.answer(req)
}

func (b *fakeDecideBackend) callCount() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.calls
}

// ffaAnswer builds a Validate-clean response: intervention with confidence conf,
// target, normalized magnitude.
func ffaAnswer(intervention string, conf float64, target string, magnitude float64, rationale string) func(decide.Request) (decide.Response, error) {
	return func(decide.Request) (decide.Response, error) {
		return decide.Response{
			Backend:   "claude",
			Rationale: rationale,
			Answers: []decide.Answer{
				{QuestionID: decide.QuestionFFAIntervention, Choice: intervention,
					Probabilities: map[string]float64{intervention: 1}, Confidence: conf},
				{QuestionID: decide.QuestionFFATarget, Choice: target,
					Probabilities: map[string]float64{target: 1}, Confidence: conf},
				{QuestionID: decide.QuestionFFAMagnitude, Score: magnitude, Confidence: conf},
			},
		}, nil
	}
}

const remoteGameID = "s-1"

// remoteContext is an active real game in which the rules engine decides
// (endurance: adjust_music_tempo), so a fallback is observable.
func remoteContext() gamecontext.GameContext {
	c := gameCtx(20, []string{"GONE"}, map[string]testPlayer{"A": {active: true}, "B": {active: true}})
	c.GameKind = "real"
	return c
}

func remoteSnapshot(timeout time.Duration) flags.Snapshot {
	s := rulesSnapshot()
	s.InterventionsAllowed = []string{"noop", "grant_shield", "adjust_music_tempo", "play_audio_cue", "adjust_global_sensitivity"}
	s.LLMGate = flags.LLMGate{EligibleGameKinds: []string{"real"}, MaxRequestsPerMinute: 100}
	s.Decision = flags.DecisionBackend{
		Backend:                flags.DecisionBackendClaude,
		ActConfidenceThreshold: 0.8,
		Claude:                 flags.RemoteBackendConfig{Model: "claude-haiku-4-5-20251001", Timeout: timeout},
	}
	return s
}

// lockedFlags is a mutex-guarded flag source, so a test can flip flags while a
// remote call is in flight (the landing goroutine re-reads them).
type lockedFlags struct {
	mu   sync.Mutex
	snap flags.Snapshot
}

func (f *lockedFlags) Evaluate(context.Context) flags.Snapshot {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.snap
}

func (f *lockedFlags) mutate(fn func(*flags.Snapshot)) {
	f.mu.Lock()
	defer f.mu.Unlock()
	fn(&f.snap)
}

type remoteHarness struct {
	flags    *lockedFlags
	cancel   context.CancelFunc
	loop     *Loop
	sr       *tracetest.SpanRecorder
	sink     *capturingSink
	engine   *ObjectiveRules
	provider *fakeContextProvider
	clock    *time.Time
}

func newRemoteHarness(t *testing.T, snap flags.Snapshot, be decide.Backend) remoteHarness {
	t.Helper()
	l, sr := decideRecordingLoop(t, snap)
	fl := &lockedFlags{snap: snap}
	l.Flags = fl
	clock := time.Unix(10_000, 0)
	engine := newTestEngine(DefaultStaticConfig(), &clock)
	l.Rules = engine
	sink := &capturingSink{}
	l.Actions = sink
	l.SetLLMBudget(NewLLMBudget())
	provider := newFakeContextProvider()
	provider.set(remoteGameID, remoteContext())
	l.SetGameID(remoteGameID)
	l.SetContextProvider(provider)
	// Cancel the root at cleanup and join every background goroutine (the #918
	// effect samplers scheduled for dispatched decisions), so nothing leaks into
	// other tests' goleak checks.
	root, cancel := context.WithCancel(context.Background())
	l.SetRootContext(root)
	// A dispatched remote decision schedules an effect sample (#1699) that
	// AwaitInflight joins: a short window keeps h.run from waiting the default 20 s.
	l.SetEffectWindow(time.Millisecond)
	t.Cleanup(func() {
		cancel()
		l.AwaitInflight()
	})
	reg := NewDecideRegistry()
	if be != nil {
		reg.Register(be.Name(), func(flags.DecisionBackend) decide.Backend { return be })
	}
	l.SetDecideRegistry(reg)
	return remoteHarness{flags: fl, cancel: cancel, loop: l, sr: sr, sink: sink, engine: engine, provider: provider, clock: &clock}
}

// run fires one cycle and waits for the async decision to land.
func (h remoteHarness) run(t *testing.T) sdktrace.ReadOnlySpan {
	t.Helper()
	state := h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
	if len(state.Outcomes) != 0 {
		t.Fatalf("remote path decided synchronously: %+v", state.Outcomes)
	}
	h.loop.AwaitInflight()
	ds := spansByName(h.sr.Ended(), SpanDecide)
	if len(ds) != 1 {
		t.Fatalf("agent.decide spans = %d, want 1", len(ds))
	}
	return ds[0]
}

// UC: UC-040 main
func TestRemote_ActAboveThreshold(t *testing.T) {
	be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.9, "A", 0.5, "  shield the\nweak player "))
	h := newRemoteHarness(t, remoteSnapshot(time.Second), be)
	span := h.run(t)

	got := h.sink.decisions()
	// GameID: the sink call is addressed to the deciding game (#1310); GameKind labels its write counter (#2294).
	want := []Decision{{Intervention: "grant_shield", TargetSerial: "A", Value: "6.5", Reason: "shield the weak player", GameID: "s-1", GameKind: "real"}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("applied = %+v, want %+v", got, want)
	}
	for key, w := range map[string]string{
		AttrDecideBackend:          "claude",
		AttrDecideBackendRequested: "claude",
		AttrDecideOutcome:          DecideOutcomeAct,
		AttrDecideRationale:        "shield the weak player",
		"gen_ai.request.model":     "claude-haiku-4-5-20251001",
	} {
		if g := strAttr(t, span, key); g != w {
			t.Errorf("%s = %q, want %q", key, g, w)
		}
	}
	if v, _ := attrValue(span, AttrDecideConfidence); v.AsFloat64() != 0.9 {
		t.Errorf("decide.confidence = %v, want 0.9", v.AsFloat64())
	}
	assertEvaluation(t, span, evaluationLabelPass, 0.9)

	// agent.decision -> agent.action nest under agent.decide.
	dec := spansByName(h.sr.Ended(), SpanDecision)
	if len(dec) != 1 || dec[0].Parent().SpanID() != span.SpanContext().SpanID() {
		t.Fatal("agent.decision must be a child of agent.decide on the remote path")
	}
	if strAttr(t, dec[0], AttrDecideBackend) != "claude" {
		t.Error("agent.decision must carry decide.backend=claude")
	}
	// Remote backends never see the raw GameContext.
	if be.reqs[0].Context != nil {
		t.Fatal("remote request carried Request.Context")
	}
}

func assertEvaluation(t *testing.T, span sdktrace.ReadOnlySpan, label string, score float64) {
	t.Helper()
	for _, ev := range span.Events() {
		if ev.Name != evaluationEventName {
			continue
		}
		attrs := map[string]any{}
		for _, kv := range ev.Attributes {
			attrs[string(kv.Key)] = kv.Value.AsInterface()
		}
		if attrs["gen_ai.evaluation.name"] != evaluationActThreshold ||
			attrs["gen_ai.evaluation.score.label"] != label ||
			attrs["gen_ai.evaluation.score.value"] != score {
			t.Fatalf("evaluation event = %v, want label %s score %v", attrs, label, score)
		}
		return
	}
	t.Fatal("no gen_ai.evaluation.result event")
}

// UC: UC-040 A5
func TestRemote_HeldBelowThreshold(t *testing.T) {
	be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.5, "A", 0.5, "maybe"))
	h := newRemoteHarness(t, remoteSnapshot(time.Second), be)
	span := h.run(t)
	if n := len(h.sink.decisions()); n != 0 {
		t.Fatalf("held answer applied %d decisions", n)
	}
	if g := strAttr(t, span, AttrDecideOutcome); g != DecideOutcomeHeld {
		t.Fatalf("outcome = %q, want held", g)
	}
	assertEvaluation(t, span, evaluationLabelFail, 0.5)
	if len(spansByName(h.sr.Ended(), SpanDecision)) != 0 {
		t.Fatal("a held answer must not reach the permission chain")
	}
}

// UC: UC-040 A8 A9
func TestRemote_NoopAndInvalid(t *testing.T) {
	cases := []struct {
		name, intervention, target, outcome, reason string
	}{
		{"UC-040/A9 noop", "noop", "none", DecideOutcomeNoop, ""},
		{"UC-040/A8 noop with target", "noop", "A", DecideOutcomeInvalid, invalidNoopWithTarget},
		{"UC-040/A8 player intervention without target", "grant_shield", "none", DecideOutcomeInvalid, invalidPlayerTargetMissing},
		{"UC-040/A8 session intervention with target", "adjust_music_tempo", "B", DecideOutcomeInvalid, invalidSessionTargetedAtPlayer},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			be := newFakeDecideBackend(ffaAnswer(tc.intervention, 0.95, tc.target, 0, "x"))
			h := newRemoteHarness(t, remoteSnapshot(time.Second), be)
			span := h.run(t)
			if n := len(h.sink.decisions()); n != 0 {
				t.Fatalf("applied %d decisions, want none", n)
			}
			if g := strAttr(t, span, AttrDecideOutcome); g != tc.outcome {
				t.Fatalf("outcome = %q, want %q", g, tc.outcome)
			}
			if tc.reason != "" && strAttr(t, span, AttrDecideInvalidReason) != tc.reason {
				t.Fatalf("invalid_reason = %q, want %q", strAttr(t, span, AttrDecideInvalidReason), tc.reason)
			}
		})
	}
}

// assertRulesFallback checks a remote failure fell back to the rules engine for
// the current context, which applied its endurance decision.
func assertRulesFallback(t *testing.T, h remoteHarness, span sdktrace.ReadOnlySpan, reason, errType string) {
	t.Helper()
	assertRulesFallbackFrom(t, h, span, flags.DecisionBackendClaude, reason, errType)
}

// assertRulesFallbackFrom is assertRulesFallback for a call to the requested backend.
func assertRulesFallbackFrom(t *testing.T, h remoteHarness, span sdktrace.ReadOnlySpan, requested, reason, errType string) {
	t.Helper()
	for key, w := range map[string]string{
		AttrDecideBackend:          "rules",
		AttrDecideBackendRequested: requested,
		AttrDecideFallbackReason:   reason,
		AttrDecideOutcome:          DecideOutcomeFallback,
		"error.type":               errType,
	} {
		if g := strAttr(t, span, key); g != w {
			t.Errorf("%s = %q, want %q", key, g, w)
		}
	}
	got := h.sink.decisions()
	if len(got) == 0 || got[0].Intervention != InterventionAdjustMusicTempo {
		t.Fatalf("fallback applied %+v, want the rules decision adjust_music_tempo", got)
	}
	if strAttr(t, span, AttrDecideAnswers) == "" {
		t.Error("fallback span must carry the rules answers")
	}
}

// NFR: NFR-010 NFR-013
// UC: UC-040 A2
// UC: UC-070 main A2 A3
func TestRemote_TimeoutFallsBackToRules(t *testing.T) {
	be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.9, "A", 0.5, ""))
	be.block = true
	h := newRemoteHarness(t, remoteSnapshot(20*time.Millisecond), be)
	span := h.run(t)
	assertRulesFallback(t, h, span, DecideFallbackTimeout, DecideFallbackTimeout)
}

// NFR: NFR-013
// UC: UC-040 A2
// UC: UC-070 main A2
func TestRemote_ErrorFallsBackToRules(t *testing.T) {
	be := newFakeDecideBackend(func(decide.Request) (decide.Response, error) {
		return decide.Response{}, errors.New("boom")
	})
	h := newRemoteHarness(t, remoteSnapshot(time.Second), be)
	span := h.run(t)
	assertRulesFallback(t, h, span, DecideFallbackError, "*errors.errorString")
}

// TestRemote_ErrorTextNeverOnSpans (#1244): a backend error string can echo a
// vendor body (and a masked key in it), so no span the decide path records may
// carry it (not as an attribute, a status description or an event), and neither
// may the fallback log line. Only the low-cardinality error.type is recorded.
func TestRemote_ErrorTextNeverOnSpans(t *testing.T) {
	const secret = "sk-live-SECRET-echoed-by-vendor"
	be := newFakeDecideBackend(func(decide.Request) (decide.Response, error) {
		return decide.Response{}, fmt.Errorf("openai: status 401: invalid key %s", secret)
	})
	h := newRemoteHarness(t, remoteSnapshot(time.Second), be)
	var logs bytes.Buffer
	h.loop.Log = slog.New(slog.NewTextHandler(&logs, nil))
	h.run(t)
	h.loop.AwaitInflight()
	if out := logs.String(); strings.Contains(out, secret) || !strings.Contains(out, "agent.decide.fallback") {
		t.Errorf("fallback log must carry error.type, never the backend error text:\n%s", out)
	}
	for _, sp := range h.sr.Ended() {
		if strings.Contains(sp.Status().Description, secret) {
			t.Errorf("span %s status description carries the backend error: %q", sp.Name(), sp.Status().Description)
		}
		for _, kv := range sp.Attributes() {
			if strings.Contains(kv.Value.Emit(), secret) {
				t.Errorf("span %s attribute %s carries the backend error", sp.Name(), kv.Key)
			}
		}
		for _, ev := range sp.Events() {
			for _, kv := range ev.Attributes {
				if strings.Contains(kv.Value.Emit(), secret) {
					t.Errorf("span %s event %s carries the backend error", sp.Name(), ev.Name)
				}
			}
		}
	}
}

// UC: UC-040 A2
func TestRemote_InvalidAnswerFallsBackToRules(t *testing.T) {
	be := newFakeDecideBackend(func(decide.Request) (decide.Response, error) {
		// Probabilities do not sum to 1: decide.Validate rejects it.
		resp, _ := ffaAnswer("grant_shield", 0.9, "A", 0.5, "")(decide.Request{})
		resp.Answers[0].Probabilities = map[string]float64{"grant_shield": 0.4}
		return resp, nil
	})
	h := newRemoteHarness(t, remoteSnapshot(time.Second), be)
	span := h.run(t)
	assertRulesFallback(t, h, span, DecideFallbackInvalidAnswer, DecideFallbackInvalidAnswer)
}

// TestRemote_BackendInvalidErrorIsInvalidAnswer (#1230): the real remote
// backends validate their own output (llmio.Parse) and return an ERROR wrapping
// decide.ErrInvalid for garbage model output. That is an invalid answer, not a
// transport error, so fallback_reason must be invalid_answer, as it is when a
// backend returns an invalid Response without an error.
// UC: UC-040 A2
func TestRemote_BackendInvalidErrorIsInvalidAnswer(t *testing.T) {
	be := newFakeDecideBackend(func(decide.Request) (decide.Response, error) {
		return decide.Response{}, fmt.Errorf("openai: %w", fmt.Errorf("%w: malformed model output", decide.ErrInvalid))
	})
	h := newRemoteHarness(t, remoteSnapshot(time.Second), be)
	span := h.run(t)
	assertRulesFallback(t, h, span, DecideFallbackInvalidAnswer, DecideFallbackInvalidAnswer)
}

// UC: UC-040 A3
// UC: UC-044 A1 Post-F-1
func TestRemote_UnknownOrUnavailableFallsBackSynchronously(t *testing.T) {
	unavailable := newFakeDecideBackend(nil)
	unavailable.available = false
	cases := []struct {
		name   string
		be     decide.Backend
		reason string
	}{
		{"unknown", nil, DecideFallbackUnknownBackend},
		{"unavailable", unavailable, DecideFallbackUnavailable},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			h := newRemoteHarness(t, remoteSnapshot(time.Second), tc.be)
			state := h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
			if len(state.Outcomes) == 0 {
				t.Fatal("rules must decide synchronously when the backend cannot be called")
			}
			ds := spansByName(h.sr.Ended(), SpanDecide)
			if len(ds) != 1 {
				t.Fatalf("agent.decide spans = %d, want 1", len(ds))
			}
			if g := strAttr(t, ds[0], AttrDecideFallbackReason); g != tc.reason {
				t.Fatalf("fallback_reason = %q, want %q", g, tc.reason)
			}
			if g := strAttr(t, ds[0], AttrDecideOutcome); g != DecideOutcomeFallback {
				t.Fatalf("outcome = %q, want fallback", g)
			}
		})
	}
}

// TestRemote_InflightRulesDoNotAct: while a call is in flight for the game, a
// second cycle neither fires another call nor lets rules act.
// NFR: NFR-012 NFR-013
// UC: UC-040 A4
func TestRemote_InflightRulesDoNotAct(t *testing.T) {
	be := newFakeDecideBackend(ffaAnswer("noop", 0.9, "none", 0, ""))
	be.block = true
	h := newRemoteHarness(t, remoteSnapshot(5*time.Second), be)
	reader := metric.NewManualReader()
	h.loop.llmGated = newLLMGatedCounter(metric.NewMeterProvider(metric.WithReader(reader)))

	h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
	<-be.started
	advance(h.clock, 2*time.Second) // rules would be due again
	state := h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
	if len(state.Outcomes) != 0 || len(h.sink.decisions()) != 0 {
		t.Fatal("rules acted while a remote call was in flight")
	}
	// UC-040 A4 step 1: a cycle that loses the in-flight claim is not counted.
	if got := gatedByReason(t, reader); len(got) != 0 {
		t.Fatalf("agent_llm_gated_total = %v, want nothing counted while a call is in flight", got)
	}
	close(be.release)
	h.loop.AwaitInflight()
	if be.callCount() != 1 {
		t.Fatalf("backend calls = %d, want 1", be.callCount())
	}
}

// TestRemote_ActArmsVarianceCooldown: a dispatched remote difficulty intervention
// arms the rules engine's variance cooldown like a rules one.
func TestRemote_ActArmsVarianceCooldown(t *testing.T) {
	be := newFakeDecideBackend(ffaAnswer("adjust_music_tempo", 0.9, "none", 1, ""))
	h := newRemoteHarness(t, remoteSnapshot(time.Second), be)
	h.run(t)
	got := h.sink.decisions()
	if len(got) != 1 || got[0].Value != "1.3" {
		t.Fatalf("applied %+v, want adjust_music_tempo at the top of its bounds", got)
	}
	h.engine.mu.Lock()
	armed := h.engine.lastDifficultyAt.Equal(*h.clock)
	h.engine.mu.Unlock()
	if !armed {
		t.Fatal("variance cooldown not armed by the remote difficulty intervention")
	}
}

// UC: UC-040 A7
func TestRemote_DiscardedWhenGameEnded(t *testing.T) {
	be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.9, "A", 0.5, ""))
	be.block = true
	h := newRemoteHarness(t, remoteSnapshot(5*time.Second), be)
	h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
	<-be.started
	ended := remoteContext()
	ended.Session.GameActive = boolPtr(false)
	h.provider.set(remoteGameID, ended)
	close(be.release)
	h.loop.AwaitInflight()
	span := spansByName(h.sr.Ended(), SpanDecide)[0]
	if strAttr(t, span, AttrDecideOutcome) != DecideOutcomeDiscarded || strAttr(t, span, AttrDecideDiscardedReason) != DiscardGameEnded {
		t.Fatal("an answer landing after the game ended must be discarded")
	}
	if len(h.sink.decisions()) != 0 {
		t.Fatal("discarded answer was applied")
	}
}

// ---- units ----

// UC: UC-040 main
func TestMapAnswers_MagnitudeAndRationale(t *testing.T) {
	resp, _ := ffaAnswer("adjust_global_sensitivity", 0.9, "none", 0.6, strings.Repeat("x", 400))(decide.Request{})
	d, outcome, _ := mapAnswers(decide.Request{}, resp)
	if outcome != DecideOutcomeAct || d.Value != "2" || d.TargetSerial != "" {
		t.Fatalf("got %+v %s, want adjust_global_sensitivity value 2 (rounded), session-scoped", d, outcome)
	}
	if n := len([]rune(d.Reason)); n != maxRationaleRunes+3 {
		t.Fatalf("reason runes = %d, want truncated to %d+...", n, maxRationaleRunes)
	}
	resp, _ = ffaAnswer("play_audio_cue", 0.9, "none", 0.6, "")(decide.Request{})
	d, _, _ = mapAnswers(decide.Request{}, resp)
	if d.Value != "" || d.Reason != "decided by claude" {
		t.Fatalf("bounds-less intervention: %+v, want empty Value and a default reason", d)
	}
}

func TestCapJSON(t *testing.T) {
	long := strings.Repeat("é", maxDecideJSONBytes)
	got := capJSON(long)
	if len(got) > maxDecideJSONBytes || !strings.HasSuffix(got, truncatedJSONMarker) {
		t.Fatalf("capJSON len %d, want <= %d with marker", len(got), maxDecideJSONBytes)
	}
}

func TestDecideRegistry(t *testing.T) {
	cfg := flags.DecisionBackend{Backend: "claude"}
	var nilReg *DecideRegistry
	if b, r := nilReg.Resolve(cfg); b != nil || r != DecideFallbackUnknownBackend {
		t.Fatalf("nil registry: %v %q", b, r)
	}
	reg := NewDecideRegistry()
	if b, r := reg.Resolve(cfg); b != nil || r != DecideFallbackUnknownBackend {
		t.Fatalf("unregistered: %v %q", b, r)
	}
	reg.Register("claude", func(flags.DecisionBackend) decide.Backend { return nil })
	if b, r := reg.Resolve(cfg); b != nil || r != DecideFallbackUnavailable {
		t.Fatalf("nil factory result: %v %q", b, r)
	}
	fake := newFakeDecideBackend(nil)
	fake.available = false
	var seen flags.DecisionBackend
	reg.Register("claude", func(d flags.DecisionBackend) decide.Backend { seen = d; return fake })
	cfg.Claude.Model = "m1"
	if b, r := reg.Resolve(cfg); b != nil || r != DecideFallbackUnavailable {
		t.Fatalf("unavailable: %v %q", b, r)
	}
	if seen.Claude.Model != "m1" {
		t.Fatal("factory must receive the current flag snapshot")
	}
	fake.available = true
	if b, r := reg.Resolve(cfg); b != fake || r != "" {
		t.Fatalf("available: %v %q", b, r)
	}
	reg.Register("rules", func(flags.DecisionBackend) decide.Backend { return fake })
	if b, _ := reg.Resolve(flags.DecisionBackend{Backend: "rules"}); b != nil {
		t.Fatal("rules must not be registrable: it is the built-in floor")
	}
}

// TestRemote_CadenceRulesDoNotAct: between remote calls (the #847 per-game
// cadence) the remote backend still owns the decision, so rules stay quiet.
// NFR: NFR-012
// UC: UC-040 A4
func TestRemote_CadenceRulesDoNotAct(t *testing.T) {
	be := newFakeDecideBackend(ffaAnswer("noop", 0.9, "none", 0, ""))
	snap := remoteSnapshot(time.Second)
	snap.Decision.MinInterval = time.Minute
	h := newRemoteHarness(t, snap, be)
	reader := metric.NewManualReader()
	h.loop.llmGated = newLLMGatedCounter(metric.NewMeterProvider(metric.WithReader(reader)))
	logs := &syncBuffer{}
	h.loop.Log = slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug}))
	h.run(t)
	advance(h.clock, 2*time.Second) // rules would be due again
	state := h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
	h.loop.AwaitInflight()
	if len(state.Outcomes) != 0 || len(h.sink.decisions()) != 0 {
		t.Fatal("rules acted between remote calls")
	}
	if be.callCount() != 1 || len(spansByName(h.sr.Ended(), SpanDecide)) != 1 {
		t.Fatal("cadence-gated cycle must not call the backend or emit a decide span")
	}
	// UC-040 A4 step 2: the gated counter and a debug log say why.
	if got := gatedByReason(t, reader); got[DecideGateInterval] != 1 || len(got) != 1 {
		t.Fatalf("agent_llm_gated_total = %v, want exactly {decide_interval: 1}", got)
	}
	if n := logs.count("agent.decide.cadence_wait"); n != 1 {
		t.Fatalf("agent.decide.cadence_wait logs = %d, want 1", n)
	}
}

// TestRemote_ShadowGameFallsBackToRules: ineligible game kinds stay rules-only.
// UC: UC-040 A3
func TestRemote_ShadowGameFallsBackToRules(t *testing.T) {
	be := newFakeDecideBackend(ffaAnswer("noop", 0.9, "none", 0, ""))
	h := newRemoteHarness(t, remoteSnapshot(time.Second), be)
	c := remoteContext()
	c.GameKind = "shadow"
	state := h.loop.OnEvaluate(context.Background(), c, testTrigger())
	if be.callCount() != 0 || len(state.Outcomes) == 0 {
		t.Fatal("a shadow game must be decided by rules without a remote call")
	}
	ds := spansByName(h.sr.Ended(), SpanDecide)
	if len(ds) != 1 || strAttr(t, ds[0], AttrDecideFallbackReason) != FallbackNotEligible {
		t.Fatal("shadow fallback must record fallback_reason=llm_not_eligible")
	}
}

// TestRemote_PrimaryLabelledGameIsEligible (#1249): a menu game the coordinator
// labels game_kind="primary" is normalised to "real" at ingest, so it is admitted
// under the default llm.eligible_game_kinds=["real"] and calls the remote backend.
// UC: UC-040 main
func TestRemote_PrimaryLabelledGameIsEligible(t *testing.T) {
	be := newFakeDecideBackend(ffaAnswer("noop", 0.9, "none", 0, ""))
	h := newRemoteHarness(t, remoteSnapshot(time.Second), be)
	ingest := gamecontext.NewStore(time.Hour, time.Hour, nil)
	ingest.SetGameKind("primary") // the coordinator's telemetry label
	c := remoteContext()
	c.GameKind = ingest.Snapshot().GameKind
	if reason := h.loop.gateRemoteDecide(context.Background(), remoteSnapshot(time.Second), c); reason != "" {
		t.Fatalf("primary-labelled game gated: %q, want admitted", reason)
	}
	h.loop.lastDecideAttempt = time.Time{} // reset the cadence charged by the direct gate call
	h.loop.OnEvaluate(context.Background(), c, testTrigger())
	h.loop.AwaitInflight()
	if be.callCount() != 1 {
		t.Fatalf("primary-labelled game must reach the remote backend, calls=%d", be.callCount())
	}
}

// ---- live flags at landing (PR #1240 review blocker) ----

// fireAndMutate fires one remote call, lets the test change state while it is in
// flight, then releases it (unless it is meant to time out) and waits.
func fireAndMutate(t *testing.T, be *fakeDecideBackend, timeout time.Duration, mutate func(h remoteHarness)) (remoteHarness, sdktrace.ReadOnlySpan) {
	t.Helper()
	return fireAndMutateSnapshot(t, be, remoteSnapshot(timeout), timeout, mutate)
}

// fireAndMutateSnapshot is fireAndMutate with a caller-built flag snapshot.
func fireAndMutateSnapshot(t *testing.T, be *fakeDecideBackend, snap flags.Snapshot, timeout time.Duration, mutate func(h remoteHarness)) (remoteHarness, sdktrace.ReadOnlySpan) {
	t.Helper()
	be.block = true
	h := newRemoteHarness(t, snap, be)
	h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
	<-be.started
	mutate(h)
	if timeout >= time.Minute {
		close(be.release)
	}
	h.loop.AwaitInflight()
	ds := spansByName(h.sr.Ended(), SpanDecide)
	if len(ds) != 1 {
		t.Fatalf("agent.decide spans = %d, want 1", len(ds))
	}
	return h, ds[0]
}

func assertDiscarded(t *testing.T, h remoteHarness, span sdktrace.ReadOnlySpan, reason string) {
	t.Helper()
	if g := strAttr(t, span, AttrDecideOutcome); g != DecideOutcomeDiscarded {
		t.Fatalf("outcome = %q, want discarded", g)
	}
	if g := strAttr(t, span, AttrDecideDiscardedReason); g != reason {
		t.Fatalf("discarded_reason = %q, want %q", g, reason)
	}
	if got := h.sink.decisions(); len(got) != 0 {
		t.Fatalf("applied %+v, want nothing", got)
	}
	if n := len(spansByName(h.sr.Ended(), SpanDecision)); n != 0 {
		t.Fatalf("agent.decision spans = %d, want 0", n)
	}
}

// UC: UC-040 A6 Post-F-1
// UC: UC-045 main
func TestRemote_KillSwitchAtLanding(t *testing.T) {
	be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x"))
	h, span := fireAndMutate(t, be, time.Hour, func(h remoteHarness) {
		h.flags.mutate(func(s *flags.Snapshot) { s.Enabled = false })
	})
	assertDiscarded(t, h, span, DecideDiscardKillSwitch)
	// The discarded answer's confidence is still reported (#1230), not 0.
	if v, _ := attrValue(span, AttrDecideConfidence); v.AsFloat64() != 0.95 {
		t.Fatalf("decide.confidence = %v, want the discarded answer's 0.95", v.AsFloat64())
	}
}

// UC: UC-040 A6
// UC: UC-045 main
func TestRemote_KillSwitchSuppressesTimeoutFallback(t *testing.T) {
	be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x"))
	h, span := fireAndMutate(t, be, 30*time.Millisecond, func(h remoteHarness) {
		h.flags.mutate(func(s *flags.Snapshot) { s.Enabled = false })
	})
	assertDiscarded(t, h, span, DecideDiscardKillSwitch)
	if g := strAttr(t, span, AttrDecideFallbackReason); g != DecideFallbackTimeout {
		t.Fatalf("fallback_reason = %q, want timeout still recorded", g)
	}
}

// UC: UC-040 A10 BR-005
// UC: UC-045 A2
func TestRemote_AllowListAtLanding(t *testing.T) {
	be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x"))
	h, span := fireAndMutate(t, be, time.Hour, func(h remoteHarness) {
		h.flags.mutate(func(s *flags.Snapshot) { s.InterventionsAllowed = []string{"noop"} })
	})
	if got := h.sink.decisions(); len(got) != 0 {
		t.Fatalf("applied %+v although grant_shield was removed from the allow-list mid-flight", got)
	}
	if g := strAttr(t, span, AttrDecideOutcome); g != DecideOutcomeAct {
		t.Fatalf("outcome = %q, want act (blocked by the permission chain)", g)
	}
	dec := spansByName(h.sr.Ended(), SpanDecision)
	if len(dec) != 1 || strAttr(t, dec[0], AttrDecisionBlockReason) != string(ReasonNotAllowed) {
		t.Fatal("agent.decision must be blocked with not_allowed by the LIVE allow-list")
	}
}

// UC: UC-040 A5
// UC: UC-044 A3
func TestRemote_ThresholdRaisedMidFlight(t *testing.T) {
	be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.9, "A", 0.5, "x"))
	h, span := fireAndMutate(t, be, time.Hour, func(h remoteHarness) {
		h.flags.mutate(func(s *flags.Snapshot) { s.Decision.ActConfidenceThreshold = 0.95 })
	})
	if g := strAttr(t, span, AttrDecideOutcome); g != DecideOutcomeHeld {
		t.Fatalf("outcome = %q, want held under the live strict threshold", g)
	}
	if v, _ := attrValue(span, AttrDecideActThreshold); v.AsFloat64() != 0.95 {
		t.Fatalf("threshold attr = %v, want 0.95", v.AsFloat64())
	}
	if len(h.sink.decisions()) != 0 {
		t.Fatal("held answer applied")
	}
}

// TestRemote_BackendChangedMidFlight: the operator switches claude -> openai
// while a claude call is in flight. The late claude answer is discarded with
// backend_changed (UC-040 A7), and the next cycle calls openai and its
// agent.decide span carries the new backend (UC-044 A2, then main step 5).
// UC: UC-040 A7
// UC: UC-044 A2
func TestRemote_BackendChangedMidFlight(t *testing.T) {
	be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x"))
	next := newFakeDecideBackend(ffaAnswer("noop", 0.9, "none", 0, ""))
	next.name = flags.DecisionBackendOpenAI
	h, span := fireAndMutate(t, be, time.Hour, func(h remoteHarness) {
		reg := NewDecideRegistry()
		for _, b := range []*fakeDecideBackend{be, next} {
			reg.Register(b.Name(), func(flags.DecisionBackend) decide.Backend { return b })
		}
		h.loop.SetDecideRegistry(reg)
		h.flags.mutate(func(s *flags.Snapshot) { s.Decision.Backend = flags.DecisionBackendOpenAI })
	})
	assertDiscarded(t, h, span, DecideDiscardBackendChanged)

	t.Run("UC-044/A2 the next cycle uses the new backend", func(t *testing.T) {
		advance(h.clock, time.Minute) // past decision.min_interval_ms
		h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
		h.loop.AwaitInflight()
		ds := spansByName(h.sr.Ended(), SpanDecide)
		if len(ds) != 2 {
			t.Fatalf("agent.decide spans = %d, want 2 (the discarded call and the next cycle)", len(ds))
		}
		for key, want := range map[string]string{
			AttrDecideBackend:          flags.DecisionBackendOpenAI,
			AttrDecideBackendRequested: flags.DecisionBackendOpenAI,
		} {
			if got := strAttr(t, ds[1], key); got != want {
				t.Errorf("next cycle %s = %q, want %q", key, got, want)
			}
		}
		if be.callCount() != 1 || next.callCount() != 1 {
			t.Fatalf("calls claude=%d openai=%d, want 1 and 1", be.callCount(), next.callCount())
		}
	})
}

// UC: UC-040 A29
func TestRemote_ShutdownDiscards(t *testing.T) {
	t.Run("UC-040/A29 shutdown during a remote call discards it with no rules fallback and no action", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x"))
		h, span := fireAndMutate(t, be, 30*time.Second, func(h remoteHarness) { h.cancel() })
		assertDiscarded(t, h, span, decideDiscardShutdown)
		if _, ok := attrValue(span, "error.type"); ok {
			t.Fatal("a shutdown-cancelled call must not record error.type")
		}
		if span.Status().Code != 0 {
			t.Fatalf("span status = %v, want unset", span.Status())
		}
		if g := strAttr(t, span, AttrDecideFallbackReason); g != "" {
			t.Fatalf("fallback_reason = %q, want empty", g)
		}
	})
}

// UC: UC-040 A7
func TestRemote_TargetGone(t *testing.T) {
	be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x"))
	h, span := fireAndMutate(t, be, time.Hour, func(h remoteHarness) {
		c := remoteContext()
		c.Players["A"].Active = boolPtr(false)
		h.provider.set(remoteGameID, c)
	})
	assertDiscarded(t, h, span, DiscardTargetGone)
}

// UC: UC-040 A3
func TestRemote_AsyncNotWiredFallsBack(t *testing.T) {
	be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x"))
	h := newRemoteHarness(t, remoteSnapshot(time.Second), be)
	h.loop.SetContextProvider(nil)
	state := h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
	if be.callCount() != 0 || len(state.Outcomes) == 0 {
		t.Fatal("without async wiring rules must decide synchronously, no remote call")
	}
	ds := spansByName(h.sr.Ended(), SpanDecide)
	if len(ds) != 1 || strAttr(t, ds[0], AttrDecideFallbackReason) != DecideFallbackNotWired {
		t.Fatal("fallback_reason must be async_not_wired")
	}
	if _, ok := attrValue(ds[0], AttrDecideActThreshold); ok {
		t.Fatal("a rules decision does not route by confidence: no threshold attribute")
	}
}

// UC: UC-040 BR-013
func TestRemote_SpanParentAndFireCycleLink(t *testing.T) {
	const (
		traceID = "4bf92f3577b34da6a3ce929d0e0e4736"
		rootID  = "00f067aa0ba902b7"
		phaseID = "b7ad6b7169203331"
	)
	be := newFakeDecideBackend(ffaAnswer("noop", 0.95, "none", 0, ""))
	h := newRemoteHarness(t, remoteSnapshot(time.Second), be)
	c := remoteContext()
	c.GameTraceID, c.GameTraceSpanID, c.GameplayPhaseSpanID = traceID, rootID, phaseID
	h.loop.OnEvaluate(context.Background(), c, testTrigger())
	h.loop.AwaitInflight()

	spans := h.sr.Ended()
	ds := spansByName(spans, SpanDecide)
	anchors := spansByName(spans, SignalReceived)
	if len(ds) != 1 || len(anchors) != 1 {
		t.Fatalf("decide=%d anchors=%d, want 1 each", len(ds), len(anchors))
	}
	span := ds[0]
	if span.Parent().TraceID().String() != traceID || span.Parent().SpanID().String() != phaseID {
		t.Fatalf("parent = %v, want the gameplay_phase span %s", span.Parent(), phaseID)
	}
	links := span.Links()
	if len(links) != 1 || links[0].SpanContext.SpanID() != anchors[0].SpanContext().SpanID() {
		t.Fatalf("links = %+v, want one Link to the fire-cycle agent.signal_received", links)
	}
}

// TestRemote_RealCadenceFlag drives the remote path through the real flags
// wrapper (in-memory OpenFeature provider): decision.min_interval_ms gates the
// second call. llm.min_decision_interval_seconds is a key REMOVED in #1234; it is
// kept in the provider deliberately, set huge, to prove a stale flag left in a
// deployment's agent.json has no effect.
// NFR: NFR-012
// UC: UC-040 A4
func TestRemote_RealCadenceFlag(t *testing.T) {
	str := func(v string) memprovider.InMemoryFlag {
		return memprovider.InMemoryFlag{State: memprovider.Enabled, DefaultVariant: "v", Variants: map[string]any{"v": v}}
	}
	num := func(v any) memprovider.InMemoryFlag {
		return memprovider.InMemoryFlag{State: memprovider.Enabled, DefaultVariant: "v", Variants: map[string]any{"v": v}}
	}
	provider := memprovider.NewInMemoryProvider(map[string]memprovider.InMemoryFlag{
		"enabled":                           num(true),
		"interventions_allowed":             str("noop,grant_shield,adjust_music_tempo"),
		"llm.eligible_game_kinds":           str("real"),
		"llm.max_requests_per_minute":       num(100),
		"llm.min_decision_interval_seconds": num(3600.0), // removed key (#1234): must be ignored
		"decision.backend":                  str("claude"),
		"decision.min_interval_ms":          num(1500),
	})
	const domain = "decide-hook-real-cadence"
	if err := openfeature.SetNamedProviderAndWait(domain, provider); err != nil {
		t.Fatal(err)
	}
	real := flags.New(openfeature.NewClient(domain), nil)
	if got := real.Evaluate(context.Background()).Decision.MinInterval; got != 1500*time.Millisecond {
		t.Fatalf("MinInterval = %v, want 1.5s from the flag", got)
	}

	be := newFakeDecideBackend(ffaAnswer("noop", 0.95, "none", 0, ""))
	h := newRemoteHarness(t, remoteSnapshot(time.Second), be)
	h.loop.Flags = real
	loopNow := time.Unix(20_000, 0)
	h.loop.now = func() time.Time { return loopNow }

	cycle := func() {
		state := h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
		h.loop.AwaitInflight()
		if len(state.Outcomes) != 0 || len(h.sink.decisions()) != 0 {
			t.Fatal("rules acted on the remote path")
		}
	}
	cycle()
	cycle() // inside the 1.5s cadence
	if be.callCount() != 1 {
		t.Fatalf("calls = %d, want 1 (second cycle inside decision.min_interval_ms)", be.callCount())
	}
	loopNow = loopNow.Add(1600 * time.Millisecond)
	cycle()
	if be.callCount() != 2 {
		t.Fatalf("calls = %d, want 2 after the cadence elapsed", be.callCount())
	}
}

// ---- safety flows without a test before #1363 ----

// TestRemote_BudgetExhaustedFallsBackToRules: with the shared per-minute budget
// (llm.max_requests_per_minute) already spent this minute, the cycle makes no
// remote call and charges no cadence; rules decide in the SAME cycle and the
// agent.decide span records outcome=fallback with llm_budget_exhausted, which
// agent_llm_gated_total counts too.
// NFR: NFR-011
// UC: UC-040 A3
// UC: UC-070 A4
func TestRemote_BudgetExhaustedFallsBackToRules(t *testing.T) {
	t.Run("UC-040/A3 exhausted budget: rules decide now, no remote call", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x"))
		snap := remoteSnapshot(time.Second)
		snap.LLMGate.MaxRequestsPerMinute = 1
		h := newRemoteHarness(t, snap, be)
		reader := metric.NewManualReader()
		h.loop.llmGated = newLLMGatedCounter(metric.NewMeterProvider(metric.WithReader(reader)))
		// Another decide call or the retro already took this minute's only slot.
		if !h.loop.budget.allow(h.loop.nowOrDefault()(), 1) {
			t.Fatal("pre-spend of the single budget slot should succeed")
		}

		// Synchronous: no call is launched, so there is nothing to await (cleanup
		// cancels the effect sampler the dispatched rules decision schedules).
		state := h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())

		// No call was launched: the in-flight claim is released (a launched call
		// would hold it until its goroutine ends), and the backend saw nothing.
		if h.loop.inflight.Load() {
			t.Fatal("in-flight slot still claimed: a remote call was launched")
		}
		if n := be.callCount(); n != 0 {
			t.Fatalf("backend calls = %d, want 0 (budget exhausted)", n)
		}
		if len(state.Outcomes) == 0 {
			t.Fatal("rules must decide in the same cycle when the budget is exhausted")
		}
		got := h.sink.decisions()
		if len(got) == 0 || got[0].Intervention != InterventionAdjustMusicTempo {
			t.Fatalf("applied %+v, want the rules decision adjust_music_tempo", got)
		}
		ds := spansByName(h.sr.Ended(), SpanDecide)
		if len(ds) != 1 {
			t.Fatalf("agent.decide spans = %d, want 1", len(ds))
		}
		for key, w := range map[string]string{
			AttrDecideBackend:          RulesBackendName,
			AttrDecideBackendRequested: "claude",
			AttrDecideFallbackReason:   FallbackBudgetExhausted,
			AttrDecideOutcome:          DecideOutcomeFallback,
		} {
			if g := strAttr(t, ds[0], key); g != w {
				t.Errorf("%s = %q, want %q", key, g, w)
			}
		}
		if g := gatedByReason(t, reader)[FallbackBudgetExhausted]; g != 1 {
			t.Fatalf("agent_llm_gated_total{reason=%s} = %d, want 1", FallbackBudgetExhausted, g)
		}
		if !h.loop.lastDecideAttempt.IsZero() {
			t.Fatal("a budget-gated cycle must not charge the decide cadence")
		}
	})
}

// TestRemote_FallbackOnGoneContextDecidesNothing: a failed call falls back to the
// rules engine for the CURRENT context; when that context is gone or the game has
// ended by the time the call returns, nothing is decided and the span carries
// outcome=fallback together with the discarded_reason (and the original
// fallback_reason).
// UC: UC-040 A2
func TestRemote_FallbackOnGoneContextDecidesNothing(t *testing.T) {
	cases := []struct {
		name   string
		reason string
		mutate func(h remoteHarness)
	}{
		{"UC-040/A2 context gone: fallback with stale_context", DiscardStaleContext, func(h remoteHarness) {
			h.provider.evict(remoteGameID)
		}},
		{"UC-040/A2 game ended: fallback with game_ended", DiscardGameEnded, func(h remoteHarness) {
			ended := remoteContext()
			ended.Session.GameActive = boolPtr(false)
			h.provider.set(remoteGameID, ended)
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			be := newFakeDecideBackend(func(decide.Request) (decide.Response, error) {
				return decide.Response{}, errors.New("boom")
			})
			h, span := fireAndMutate(t, be, time.Hour, tc.mutate)
			for key, w := range map[string]string{
				AttrDecideBackend:         RulesBackendName,
				AttrDecideOutcome:         DecideOutcomeFallback,
				AttrDecideFallbackReason:  DecideFallbackError,
				AttrDecideDiscardedReason: tc.reason,
			} {
				if g := strAttr(t, span, key); g != w {
					t.Errorf("%s = %q, want %q", key, g, w)
				}
			}
			if got := h.sink.decisions(); len(got) != 0 {
				t.Fatalf("applied %+v, want nothing (no context to decide on)", got)
			}
			if n := len(spansByName(h.sr.Ended(), SpanDecision)); n != 0 {
				t.Fatalf("agent.decision spans = %d, want 0 (the rules engine must not run)", n)
			}
		})
	}
}

// TestRemote_DiscardedStaleContext: an answer that would act lands after the
// game's context partition is gone: discarded with stale_context, nothing applied.
// UC: UC-040 A7
func TestRemote_DiscardedStaleContext(t *testing.T) {
	t.Run("UC-040/A7 acting answer with no current context is discarded", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x"))
		h, span := fireAndMutate(t, be, time.Hour, func(h remoteHarness) {
			h.provider.evict(remoteGameID)
		})
		assertDiscarded(t, h, span, DiscardStaleContext)
	})
}

// TestRemote_HeldNotDiscardedAfterGameEnded: confidence routing comes before the
// context check, so a low-confidence answer landing after the game ended is
// held, not discarded.
// UC: UC-040 A5
func TestRemote_HeldNotDiscardedAfterGameEnded(t *testing.T) {
	t.Run("UC-040/A5 low confidence after game end is held, not discarded", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.5, "A", 0.5, "maybe"))
		h, span := fireAndMutate(t, be, time.Hour, func(h remoteHarness) {
			ended := remoteContext()
			ended.Session.GameActive = boolPtr(false)
			h.provider.set(remoteGameID, ended)
		})
		if g := strAttr(t, span, AttrDecideOutcome); g != DecideOutcomeHeld {
			t.Fatalf("outcome = %q, want held", g)
		}
		if v, ok := attrValue(span, AttrDecideDiscardedReason); ok {
			t.Fatalf("discarded_reason = %q, want none on a held answer", v.AsString())
		}
		if got := h.sink.decisions(); len(got) != 0 {
			t.Fatalf("applied %+v, want nothing", got)
		}
		if n := len(spansByName(h.sr.Ended(), SpanDecision)); n != 0 {
			t.Fatalf("agent.decision spans = %d, want 0", n)
		}
	})
}

// TestMapAnswers_MissingInterventionIsInvalid: the fourth invalid_reason. A
// response without an ffa.intervention answer maps to outcome=invalid with
// missing_intervention_answer (TestRemote_NoopAndInvalid covers the other three
// through the hook; decide.Validate rejects such a response before mapAnswers on
// the remote path, so this unit is the backstop).
// UC: UC-040 A8
func TestMapAnswers_MissingInterventionIsInvalid(t *testing.T) {
	t.Run("UC-040/A8 missing ffa.intervention answer is invalid", func(t *testing.T) {
		resp := decide.Response{Backend: "claude", Answers: []decide.Answer{
			{QuestionID: decide.QuestionFFATarget, Choice: decide.TargetNone,
				Probabilities: map[string]float64{decide.TargetNone: 1}, Confidence: 1},
		}}
		d, outcome, reason := mapAnswers(decide.Request{}, resp)
		if outcome != DecideOutcomeInvalid || reason != invalidMissingIntervention {
			t.Fatalf("mapAnswers = (%q, %q), want (invalid, %s)", outcome, reason, invalidMissingIntervention)
		}
		if d.Intervention != "" || d.TargetSerial != "" {
			t.Fatalf("decision = %+v, want none", d)
		}
	})
}
