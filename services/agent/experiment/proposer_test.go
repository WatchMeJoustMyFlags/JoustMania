package experiment

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/llm"
)

// proposer_test.go drives the M7-4 (#931) Proposer end to end with in-process
// fakes only — no network, no flagd, no clock (per the testing-strategy doc:
// inferBackend-style fake + tracetest recorder + temp game.json). It asserts the
// four contracts from the brief:
//
//  1. valid reply  -> a Proposal that PASSES Gate.Review and WRITES the shadow
//     experiment to the temp game.json (ProposeProposed).
//  2. unparseable/invalid reply -> NO proposal, NO write, recorded
//     (ProposeUndecodable).
//  3. out-of-vocab/unknown-flag reply -> no proposal (rejected by the decoder
//     before the Gate; the Gate's ReasonUnknownFlag is the defense-in-depth
//     backstop, exercised separately).
//  4. cadence gating -> two cycles inside MinInterval => exactly one proposal.

// fakeProposerBackend is the inferBackend-style fake (mirrors decision's
// fakeBackend / the validate_test.go fakes): a canned raw response or an error,
// recording the call count so cadence tests can assert Infer was/was not invoked.
type fakeProposerBackend struct {
	response string
	err      error
	calls    int
}

func (b *fakeProposerBackend) Infer(context.Context, llm.Prompt) (string, error) {
	b.calls++
	if b.err != nil {
		return "", b.err
	}
	return b.response, nil
}

// gameVocab reads the live flag keys from the temp game.json at path — the same
// surface the Gate validates against, so the prompt/decoder vocabulary and the
// Gate's known-flag set agree.
func gameVocab(t *testing.T, path string) func() map[string]struct{} {
	t.Helper()
	return func() map[string]struct{} {
		raw, err := os.ReadFile(path)
		if err != nil {
			return nil
		}
		var doc struct {
			Flags map[string]json.RawMessage `json:"flags"`
		}
		if err := json.Unmarshal(raw, &doc); err != nil {
			return nil
		}
		set := make(map[string]struct{}, len(doc.Flags))
		for k := range doc.Flags {
			set[k] = struct{}{}
		}
		return set
	}
}

// proposerWithRecorder wires a Proposer over a temp game.json + the given fake
// backend, with an in-memory span recorder and a frozen clock the test advances.
func proposerWithRecorder(t *testing.T, backend Backend) (*Proposer, string, *tracetest.SpanRecorder, *fakeClock) {
	t.Helper()
	path := newGameFile(t)
	rec := tracetest.NewSpanRecorder()
	tp := trace.NewTracerProvider(trace.WithSpanProcessor(rec))
	w := NewWriter(path, discardLogger())
	gate := NewGate(w, tp.Tracer("test"), discardLogger())
	p := NewProposer(backend, gate, gameVocab(t, path), tp.Tracer("test"), discardLogger())
	clk := &fakeClock{now: time.Unix(1_700_000_000, 0)}
	p.SetClock(clk.Now)
	return p, path, rec, clk
}

// fakeClock is a deterministic, advanceable clock for cadence tests.
type fakeClock struct{ now time.Time }

func (c *fakeClock) Now() time.Time          { return c.now }
func (c *fakeClock) advance(d time.Duration) { c.now = c.now.Add(d) }

// experimentVariantValue returns the experimental variant's value for flagKey in
// the file at path, or ("", false) if no experiment was written.
func experimentVariantValue(t *testing.T, path, flagKey string) (string, bool) {
	t.Helper()
	raw := readFlagRaw(t, path, flagKey)
	var flag struct {
		Variants map[string]json.RawMessage `json:"variants"`
	}
	if err := json.Unmarshal(raw, &flag); err != nil {
		t.Fatalf("decode flag: %v", err)
	}
	v, ok := flag.Variants[ExperimentVariant]
	return string(v), ok
}

func TestProposeOnce_ValidProposalWritesShadowExperiment(t *testing.T) {
	backend := &fakeProposerBackend{
		response: `{"flag":"death_grace_period_seconds","value":0.75,"rationale":"longer grace may extend sessions"}`,
	}
	p, path, rec, _ := proposerWithRecorder(t, backend)

	res := p.ProposeOnce(context.Background(), Narrative{GameMode: "joust"}, ProposerConfig{Engine: "stub"})

	if res.Outcome != ProposeProposed {
		t.Fatalf("outcome = %q, want proposed (err=%v)", res.Outcome, res.Err)
	}
	if res.Proposal == nil || res.Proposal.FlagKey != "death_grace_period_seconds" {
		t.Fatalf("proposal = %+v, want death_grace_period_seconds", res.Proposal)
	}
	// The shadow experiment must be WRITTEN to game.json (the Gate accepted + applied).
	val, ok := experimentVariantValue(t, path, "death_grace_period_seconds")
	if !ok {
		t.Fatalf("expected experimental variant written to game.json, found none")
	}
	if val != "0.75" {
		t.Fatalf("experimental value = %s, want 0.75", val)
	}
	// The attempt span records the engine, the diff summary, and the proposed outcome.
	span := findSpan(t, rec, SpanProposeAttempt)
	assertSpanAttr(t, span, AttrEngine, "stub")
	assertSpanAttr(t, span, AttrOutcome, string(ProposeProposed))
	if !hasNonEmptyAttr(span, AttrProposedDiffSummary) {
		t.Fatalf("expected non-empty %s on attempt span", AttrProposedDiffSummary)
	}
	// The Gate's own SpanProposed (blocked=false) nests under the attempt.
	gateSpan := findSpan(t, rec, SpanProposed)
	assertSpanBoolAttr(t, gateSpan, AttrBlocked, false)
}

func TestProposeOnce_UndecodableReplyMakesNoProposal(t *testing.T) {
	backend := &fakeProposerBackend{response: "I really think you should raise the grace period."}
	p, path, rec, _ := proposerWithRecorder(t, backend)

	res := p.ProposeOnce(context.Background(), Narrative{}, ProposerConfig{Engine: "stub"})

	if res.Outcome != ProposeUndecodable {
		t.Fatalf("outcome = %q, want undecodable", res.Outcome)
	}
	if res.Proposal != nil {
		t.Fatalf("expected nil proposal, got %+v", res.Proposal)
	}
	// NO write: no experiment variant on any flag.
	if _, ok := experimentVariantValue(t, path, "death_grace_period_seconds"); ok {
		t.Fatalf("undecodable reply must not write an experiment")
	}
	// Recorded: attempt span carries the undecodable outcome; the Gate never ran.
	span := findSpan(t, rec, SpanProposeAttempt)
	assertSpanAttr(t, span, AttrOutcome, string(ProposeUndecodable))
	if spanExists(rec, SpanProposed) {
		t.Fatalf("Gate.Review must not run on an undecodable reply")
	}
}

func TestProposeOnce_UnknownFlagRejectedByDecoderNoWrite(t *testing.T) {
	// The model names a flag that does not exist on the game surface. The decoder
	// rejects it (out-of-vocab) BEFORE the Gate, so no proposal, no write.
	backend := &fakeProposerBackend{
		response: `{"flag":"made_up_flag","value":1,"rationale":"hallucinated"}`,
	}
	p, path, rec, _ := proposerWithRecorder(t, backend)

	res := p.ProposeOnce(context.Background(), Narrative{}, ProposerConfig{Engine: "stub"})

	if res.Outcome != ProposeUndecodable {
		t.Fatalf("outcome = %q, want undecodable (decoder rejects out-of-vocab)", res.Outcome)
	}
	if !errors.Is(res.Err, errUnknownProposalFlag) {
		t.Fatalf("err = %v, want errUnknownProposalFlag", res.Err)
	}
	if _, ok := experimentVariantValue(t, path, "death_grace_period_seconds"); ok {
		t.Fatalf("unknown-flag reply must not write an experiment")
	}
	if spanExists(rec, SpanProposed) {
		t.Fatalf("Gate.Review must not run on an out-of-vocab reply")
	}
}

// TestProposeOnce_TypeMismatchBlockedByGate exercises the defense-in-depth path:
// a KNOWN flag but a MISTYPED value passes the decoder (it only checks presence +
// vocab) and reaches the Gate, which blocks it as ReasonTypeMismatch and emits the
// blocked span. No experiment is written.
func TestProposeOnce_TypeMismatchBlockedByGate(t *testing.T) {
	backend := &fakeProposerBackend{
		// death_grace_period_seconds variants are numbers; a string is a type mismatch.
		response: `{"flag":"death_grace_period_seconds","value":"way too long","rationale":"oops a string"}`,
	}
	p, path, rec, _ := proposerWithRecorder(t, backend)

	res := p.ProposeOnce(context.Background(), Narrative{}, ProposerConfig{Engine: "stub"})

	if res.Outcome != ProposeBlocked {
		t.Fatalf("outcome = %q, want blocked", res.Outcome)
	}
	rej, ok := AsReject(res.Err)
	if !ok || rej.Reason != ReasonTypeMismatch {
		t.Fatalf("err = %v, want RejectError{TypeMismatch}", res.Err)
	}
	if _, ok := experimentVariantValue(t, path, "death_grace_period_seconds"); ok {
		t.Fatalf("type-mismatched proposal must not write an experiment")
	}
	// The Gate emitted its blocked span; the attempt span records the blocked outcome.
	gateSpan := findSpan(t, rec, SpanProposed)
	assertSpanBoolAttr(t, gateSpan, AttrBlocked, true)
	assertSpanAttr(t, gateSpan, AttrReason, string(ReasonTypeMismatch))
	attemptSpan := findSpan(t, rec, SpanProposeAttempt)
	assertSpanAttr(t, attemptSpan, AttrOutcome, string(ProposeBlocked))
}

func TestProposeOnce_NoBackendDegradesToNothing(t *testing.T) {
	// The sentinel-stub case: Backend.Infer errors (no real Ollama/cloud transport
	// until #738/#742). The Proposer degrades to NO proposal, NO error to the caller.
	backend := &fakeProposerBackend{err: errors.New("inference not implemented")}
	p, path, rec, _ := proposerWithRecorder(t, backend)

	res := p.ProposeOnce(context.Background(), Narrative{}, ProposerConfig{Engine: "stub"})

	if res.Outcome != ProposeNoBackend {
		t.Fatalf("outcome = %q, want no_backend", res.Outcome)
	}
	if res.Proposal != nil {
		t.Fatalf("no proposal expected when backend errors")
	}
	if _, ok := experimentVariantValue(t, path, "death_grace_period_seconds"); ok {
		t.Fatalf("a failed backend must not write an experiment")
	}
	span := findSpan(t, rec, SpanProposeAttempt)
	assertSpanAttr(t, span, AttrOutcome, string(ProposeNoBackend))
}

func TestProposeOnce_CadenceGatesSecondCycleWithinInterval(t *testing.T) {
	backend := &fakeProposerBackend{
		response: `{"flag":"death_grace_period_seconds","value":0.75,"rationale":"longer grace"}`,
	}
	p, _, _, clk := proposerWithRecorder(t, backend)
	cfg := ProposerConfig{Engine: "stub", MinInterval: 60 * time.Second}

	// First cycle: admitted (no prior attempt) -> proposes, calls Infer once.
	r1 := p.ProposeOnce(context.Background(), Narrative{}, cfg)
	if r1.Outcome != ProposeProposed {
		t.Fatalf("cycle 1 outcome = %q, want proposed", r1.Outcome)
	}

	// Second cycle 30s later (inside the 60s floor): GATED, no Infer call.
	clk.advance(30 * time.Second)
	r2 := p.ProposeOnce(context.Background(), Narrative{}, cfg)
	if r2.Outcome != ProposeGated {
		t.Fatalf("cycle 2 outcome = %q, want gated", r2.Outcome)
	}
	if backend.calls != 1 {
		t.Fatalf("Infer calls = %d, want 1 (gated cycle must not infer)", backend.calls)
	}

	// Third cycle past the floor: admitted again.
	clk.advance(31 * time.Second)
	r3 := p.ProposeOnce(context.Background(), Narrative{}, cfg)
	if r3.Outcome != ProposeProposed {
		t.Fatalf("cycle 3 outcome = %q, want proposed", r3.Outcome)
	}
	if backend.calls != 2 {
		t.Fatalf("Infer calls = %d, want 2", backend.calls)
	}
}

func TestProposeOnce_ZeroIntervalDisablesCadence(t *testing.T) {
	backend := &fakeProposerBackend{
		response: `{"flag":"death_grace_period_seconds","value":0.75,"rationale":"longer grace"}`,
	}
	p, _, _, _ := proposerWithRecorder(t, backend)
	cfg := ProposerConfig{Engine: "stub"} // MinInterval 0 => no floor

	for i := 0; i < 3; i++ {
		if got := p.ProposeOnce(context.Background(), Narrative{}, cfg).Outcome; got != ProposeProposed {
			t.Fatalf("cycle %d outcome = %q, want proposed (no cadence floor)", i, got)
		}
	}
	if backend.calls != 3 {
		t.Fatalf("Infer calls = %d, want 3", backend.calls)
	}
}

// ---- span assertion helpers (mirror the experiment_test.go / validate_test.go
// recorder pattern) ----

func spanExists(rec *tracetest.SpanRecorder, name string) bool {
	for _, s := range rec.Ended() {
		if s.Name() == name {
			return true
		}
	}
	return false
}

func assertSpanAttr(t *testing.T, span trace.ReadOnlySpan, key, want string) {
	t.Helper()
	for _, a := range span.Attributes() {
		if string(a.Key) == key {
			if a.Value.AsString() != want {
				t.Fatalf("span attr %s = %q, want %q", key, a.Value.AsString(), want)
			}
			return
		}
	}
	t.Fatalf("span %q missing attr %s", span.Name(), key)
}

func assertSpanBoolAttr(t *testing.T, span trace.ReadOnlySpan, key string, want bool) {
	t.Helper()
	for _, a := range span.Attributes() {
		if string(a.Key) == key {
			if a.Value.AsBool() != want {
				t.Fatalf("span attr %s = %v, want %v", key, a.Value.AsBool(), want)
			}
			return
		}
	}
	t.Fatalf("span %q missing attr %s", span.Name(), key)
}

func hasNonEmptyAttr(span trace.ReadOnlySpan, key string) bool {
	for _, a := range span.Attributes() {
		if string(a.Key) == key && a.Value.AsString() != "" {
			return true
		}
	}
	return false
}
