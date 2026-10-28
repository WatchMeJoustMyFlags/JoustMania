package experiment

import (
	"context"
	"errors"
	"net/url"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/experiment/journal"
	"github.com/joustmania/agent/flagservice"
	"github.com/joustmania/agent/flagservice/fake"
	"github.com/joustmania/agent/llm"
)

// proposer_test.go drives the M7-4 (#931) Proposer end to end with in-process
// fakes only — no network, no flagd, no clock (per the testing-strategy doc:
// inferBackend-style fake + tracetest recorder + fake flag service). It asserts the
// four contracts from the brief:
//
//  1. valid reply  -> a Proposal that PASSES Gate.Review and WRITES the shadow
//     experiment to the fake flag service (ProposeProposed).
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

// gameVocab returns the experiment catalog keys of the fake flag service, the
// same surface the Gate validates against.
func gameVocab(svc *fake.Service) func() map[string]flagservice.FlagView {
	return func() map[string]flagservice.FlagView {
		set, err := svc.GetSet(context.Background(), GameFlagSet)
		if err != nil {
			return nil
		}
		out := map[string]flagservice.FlagView{}
		for _, f := range set.Flags {
			if f.Policy.AllowsScope("experiment+arm") {
				out[f.Flag] = f
			}
		}
		return out
	}
}

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

// proposerWithRecorder wires a Proposer over a fake flag service + the given
// fake backend, with an in-memory span recorder and a frozen clock.
func proposerWithRecorder(t *testing.T, backend Backend) (*Proposer, *fake.Service, *tracetest.SpanRecorder, *fakeClock) {
	t.Helper()
	p, svc, rec, clk, _ := proposerWithJournal(t, backend, 0)
	return p, svc, rec, clk
}

// proposerWithJournal is proposerWithRecorder with the registry's verdict min-N
// (0 = the env default) and the journal root the declared intents land in.
func proposerWithJournal(t *testing.T, backend Backend, verdictMinN int) (*Proposer, *fake.Service, *tracetest.SpanRecorder, *fakeClock, string) {
	t.Helper()
	svc := newGameService(t)
	rec := tracetest.NewSpanRecorder()
	tp := trace.NewTracerProvider(trace.WithSpanProcessor(rec))
	w := NewWriter(svc, discardLogger())
	gate := NewGate(w, tp.Tracer("test"), discardLogger())
	root := t.TempDir()
	reg := NewRegistry(RegistryConfig{
		Root:        root,
		Targeting:   NewGateTargetingWriter(gate, w),
		Verdict:     fakeVerdict{concludeAtN: 1000},
		Clock:       fixedClock(),
		VerdictMinN: verdictMinN,
	})
	p := NewProposer(backend, &RegistrySink{Registry: reg, Objective: "balanced"}, gameVocab(svc), tp.Tracer("test"), discardLogger())
	clk := &fakeClock{now: time.Unix(1_700_000_000, 0)}
	p.SetClock(clk.Now)
	return p, svc, rec, clk, root
}

// fakeClock is a deterministic, advanceable clock for cadence tests.
type fakeClock struct{ now time.Time }

func (c *fakeClock) Now() time.Time          { return c.now }
func (c *fakeClock) advance(d time.Duration) { c.now = c.now.Add(d) }

// experimentVariantValue returns the experimental-arm value written for flagKey
// (the registry allocates the experiment id), or ("", false) if none.
func experimentVariantValue(t *testing.T, svc *fake.Service, flagKey string) (string, bool) {
	t.Helper()
	for k, v := range svc.ArmValues(GameFlagSet, flagKey) {
		if strings.HasPrefix(k, "exp_") && strings.HasSuffix(k, "|"+ArmExperimental) {
			return string(v), true
		}
	}
	return "", false
}

// UC: UC-042 main Post-S-2
func TestProposeOnce_ValidProposalWritesShadowExperiment(t *testing.T) {
	backend := &fakeProposerBackend{
		response: `{"flag":"death_grace_period_seconds","value":3.0,"rationale":"longer grace may extend sessions"}`,
	}
	const verdictMinN = 7
	p, svc, rec, _, root := proposerWithJournal(t, backend, verdictMinN)

	res := p.ProposeOnce(context.Background(), Narrative{GameMode: "joust"}, ProposerConfig{Permit: allowPropose})

	if res.Outcome != ProposeProposed {
		t.Fatalf("outcome = %q, want proposed (err=%v)", res.Outcome, res.Err)
	}
	if res.Proposal == nil || res.Proposal.FlagKey != "death_grace_period_seconds" {
		t.Fatalf("proposal = %+v, want death_grace_period_seconds", res.Proposal)
	}
	// The shadow experiment must be WRITTEN as an experiment-arm value (the Gate accepted + applied).
	val, ok := experimentVariantValue(t, svc, "death_grace_period_seconds")
	if !ok {
		t.Fatalf("expected experimental variant written, found none")
	}
	if val != "3" {
		t.Fatalf("experimental value = %s, want 3", val)
	}
	// The attempt span records the diff summary and the proposed outcome.
	span := findSpan(t, rec, SpanProposeAttempt)
	assertSpanAttr(t, span, AttrOutcome, string(ProposeProposed))
	if !hasNonEmptyAttr(span, AttrProposedDiffSummary) {
		t.Fatalf("expected non-empty %s on attempt span", AttrProposedDiffSummary)
	}
	// The Gate's own SpanProposed (blocked=false) nests under the attempt.
	gateSpan := findSpan(t, rec, SpanProposed)
	assertSpanBoolAttr(t, gateSpan, AttrBlocked, false)

	// UC-042 step 5: the declared intent (its own exp_ id, arms experimental and
	// control, hypothesis = the rationale, objective balanced, target N =
	// verdict_min_n games per arm).
	ids, err := journal.List(root)
	if err != nil || len(ids) != 1 {
		t.Fatalf("journal ids = %v (err %v), want exactly one declared experiment", ids, err)
	}
	j, err := journal.Load(root, ids[0])
	if err != nil {
		t.Fatalf("load journal: %v", err)
	}
	intent := j.Intent()
	if !strings.HasPrefix(intent.ExperimentID, "exp_") {
		t.Errorf("experiment id = %q, want an exp_<hex> id", intent.ExperimentID)
	}
	if intent.Hypothesis != "longer grace may extend sessions" {
		t.Errorf("hypothesis = %q, want the proposal's rationale", intent.Hypothesis)
	}
	if intent.Objective != "balanced" {
		t.Errorf("objective = %q, want balanced", intent.Objective)
	}
	if intent.TargetNPerArm != verdictMinN {
		t.Errorf("target N per arm = %d, want verdict_min_n %d", intent.TargetNPerArm, verdictMinN)
	}
	if len(intent.Arms) != 2 || intent.Arms[0] != ArmExperimental || intent.Arms[1] != ArmControl {
		t.Errorf("arms = %v, want [%s %s]", intent.Arms, ArmExperimental, ArmControl)
	}
	if intent.FlagKey != "death_grace_period_seconds" {
		t.Errorf("flag key = %q, want death_grace_period_seconds", intent.FlagKey)
	}
}

// UC: UC-042 Post-F-1
func TestProposeOnce_UndecodableReplyMakesNoProposal(t *testing.T) {
	backend := &fakeProposerBackend{response: "I really think you should raise the grace period."}
	p, svc, rec, _ := proposerWithRecorder(t, backend)

	res := p.ProposeOnce(context.Background(), Narrative{}, ProposerConfig{Permit: allowPropose})

	if res.Outcome != ProposeUndecodable {
		t.Fatalf("outcome = %q, want undecodable", res.Outcome)
	}
	if res.Proposal != nil {
		t.Fatalf("expected nil proposal, got %+v", res.Proposal)
	}
	// NO write: no experiment variant on any flag.
	if _, ok := experimentVariantValue(t, svc, "death_grace_period_seconds"); ok {
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
	p, svc, rec, _ := proposerWithRecorder(t, backend)

	res := p.ProposeOnce(context.Background(), Narrative{}, ProposerConfig{Permit: allowPropose})

	if res.Outcome != ProposeUndecodable {
		t.Fatalf("outcome = %q, want undecodable (decoder rejects out-of-vocab)", res.Outcome)
	}
	if !errors.Is(res.Err, errUnknownProposalFlag) {
		t.Fatalf("err = %v, want errUnknownProposalFlag", res.Err)
	}
	if _, ok := experimentVariantValue(t, svc, "death_grace_period_seconds"); ok {
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
	p, svc, rec, _ := proposerWithRecorder(t, backend)

	res := p.ProposeOnce(context.Background(), Narrative{}, ProposerConfig{Permit: allowPropose})

	if res.Outcome != ProposeBlocked {
		t.Fatalf("outcome = %q, want blocked", res.Outcome)
	}
	rej, ok := AsReject(res.Err)
	if !ok || rej.Reason != ReasonTypeMismatch {
		t.Fatalf("err = %v, want RejectError{TypeMismatch}", res.Err)
	}
	if _, ok := experimentVariantValue(t, svc, "death_grace_period_seconds"); ok {
		t.Fatalf("type-mismatched proposal must not write an experiment")
	}
	// The Gate emitted its blocked span; the attempt span records the blocked outcome.
	gateSpan := findSpan(t, rec, SpanProposed)
	assertSpanBoolAttr(t, gateSpan, AttrBlocked, true)
	assertSpanAttr(t, gateSpan, AttrReason, string(ReasonTypeMismatch))
	attemptSpan := findSpan(t, rec, SpanProposeAttempt)
	assertSpanAttr(t, attemptSpan, AttrOutcome, string(ProposeBlocked))
}

// UC: UC-042 A7 A10
func TestProposeOnce_NoBackendDegradesToNothing(t *testing.T) {
	// The sentinel-stub case: Backend.Infer errors (no text backend configured,
	// inference.backend=stub). The Proposer degrades to NO proposal, NO error,
	// and the cycle uses up the cadence floor (UC-042 A7 step 3, A10).
	t.Run("UC-042/A10 a failed call writes nothing, records no_backend and uses up the cadence floor", func(t *testing.T) {
		backend := &fakeProposerBackend{err: errors.New("inference not implemented")}
		p, svc, rec, clk := proposerWithRecorder(t, backend)
		cfg := ProposerConfig{Permit: allowPropose, MinInterval: time.Minute}

		res := p.ProposeOnce(context.Background(), Narrative{}, cfg)

		if res.Outcome != ProposeNoBackend {
			t.Fatalf("outcome = %q, want no_backend", res.Outcome)
		}
		if res.Proposal != nil {
			t.Fatalf("no proposal expected when backend errors")
		}
		if _, ok := experimentVariantValue(t, svc, "death_grace_period_seconds"); ok {
			t.Fatalf("a failed backend must not write an experiment")
		}
		span := findSpan(t, rec, SpanProposeAttempt)
		assertSpanAttr(t, span, AttrOutcome, string(ProposeNoBackend))

		clk.advance(30 * time.Second)
		if got := p.ProposeOnce(context.Background(), Narrative{}, cfg).Outcome; got != ProposeGated {
			t.Fatalf("cycle inside the floor = %q, want gated (the failed call used it up)", got)
		}
		if backend.calls != 1 {
			t.Fatalf("Infer calls = %d, want 1", backend.calls)
		}
	})
}

// UC: UC-042 A7
func TestProposeOnce_CadenceGatesSecondCycleWithinInterval(t *testing.T) {
	backend := &fakeProposerBackend{
		response: `{"flag":"death_grace_period_seconds","value":3.0,"rationale":"longer grace"}`,
	}
	p, _, _, clk := proposerWithRecorder(t, backend)
	cfg := ProposerConfig{Permit: allowPropose, MinInterval: 60 * time.Second}

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
	// (The registry refuses a second experiment on the same flag, so the outcome
	// is blocked; the cadence floor admitted the cycle, which is what counts here.)
	r3 := p.ProposeOnce(context.Background(), Narrative{}, cfg)
	if r3.Outcome == ProposeGated {
		t.Fatalf("cycle 3 outcome = %q, want admitted", r3.Outcome)
	}
	if backend.calls != 2 {
		t.Fatalf("Infer calls = %d, want 2", backend.calls)
	}
}

func TestProposeOnce_ZeroIntervalDisablesCadence(t *testing.T) {
	backend := &fakeProposerBackend{
		response: `{"flag":"death_grace_period_seconds","value":3.0,"rationale":"longer grace"}`,
	}
	p, _, _, _ := proposerWithRecorder(t, backend)
	cfg := ProposerConfig{Permit: allowPropose} // MinInterval 0 => no floor

	for i := 0; i < 3; i++ {
		if got := p.ProposeOnce(context.Background(), Narrative{}, cfg).Outcome; got == ProposeGated {
			t.Fatalf("cycle %d outcome = %q, want admitted (no cadence floor)", i, got)
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

// UC: UC-042 main A11 Post-S-2
func TestProposeOnce_ProposalBecomesARegistryExperiment(t *testing.T) {
	// PR #1417 review B2: a proposal is declared as an experiment the registry runs
	// in shadow games (its own exp_ id and arms), never a value no game reads; a
	// second proposal on the same flag is refused while the first one runs.
	backend := &fakeProposerBackend{
		response: `{"flag":"death_grace_period_seconds","value":3.0,"rationale":"longer grace"}`,
	}
	svc := newGameService(t)
	w := NewWriter(svc, discardLogger())
	reg := NewRegistry(RegistryConfig{
		Root:      t.TempDir(),
		Targeting: NewGateTargetingWriter(NewGate(w, nil, discardLogger()), w),
		Verdict:   fakeVerdict{concludeAtN: 1000},
		Clock:     fixedClock(),
	})
	p := NewProposer(backend, &RegistrySink{Registry: reg, Objective: "balanced"}, gameVocab(svc), nil, discardLogger())

	if got := p.ProposeOnce(context.Background(), Narrative{}, ProposerConfig{Permit: allowPropose}).Outcome; got != ProposeProposed {
		t.Fatalf("outcome = %q, want proposed", got)
	}
	live := reg.Live()
	if len(live) != 1 {
		t.Fatalf("live experiments = %v, want one", live)
	}
	if st, _ := reg.Status(live[0]); st != StatusRunning {
		t.Fatalf("status = %s, want running", st)
	}
	if _, ok := svc.ArmValues(GameFlagSet, "death_grace_period_seconds")[live[0]+"|"+ArmExperimental]; !ok {
		t.Fatalf("no arm value for %s: %v", live[0], svc.ArmValues(GameFlagSet, "death_grace_period_seconds"))
	}

	res := p.ProposeOnce(context.Background(), Narrative{}, ProposerConfig{Permit: allowPropose})
	rej, ok := AsReject(res.Err)
	if res.Outcome != ProposeBlocked || !ok || rej.Reason != ReasonFlagUnderTest {
		t.Fatalf("second proposal = %q %v, want blocked flag_under_test", res.Outcome, res.Err)
	}
	if len(reg.Live()) != 1 || len(svc.ArmValues(GameFlagSet, "death_grace_period_seconds")) != 1 {
		t.Fatalf("the refused proposal left state behind: live=%v arms=%v", reg.Live(), svc.ArmValues(GameFlagSet, "death_grace_period_seconds"))
	}
}

// UC: UC-042 A11
func TestProposeOnce_NoExperimentLoopWritesNothing(t *testing.T) {
	backend := &fakeProposerBackend{
		response: `{"flag":"death_grace_period_seconds","value":3.0,"rationale":"longer grace"}`,
	}
	svc := newGameService(t)
	p := NewProposer(backend, &LateSink{}, gameVocab(svc), nil, discardLogger())
	res := p.ProposeOnce(context.Background(), Narrative{}, ProposerConfig{Permit: allowPropose})
	if rej, ok := AsReject(res.Err); res.Outcome != ProposeBlocked || !ok || rej.Reason != ReasonNoExperimentLoop {
		t.Fatalf("outcome = %q %v, want blocked no_experiment_loop", res.Outcome, res.Err)
	}
	if arms := svc.ArmValues(GameFlagSet, "death_grace_period_seconds"); len(arms) != 0 {
		t.Fatalf("arm values written without an experiment loop: %v", arms)
	}
}

// UC: UC-043 main A10
func TestRestartedFlagServiceGetsTheArmValueAgain(t *testing.T) {
	// A flag-service restart drops every experiment-arm value. Before the next
	// pair spawns, the registry sees the new boot epoch and writes the value
	// again, so the experiment's shadow games keep reading it (PR #1417 review).
	svc := newGameService(t)
	w := NewWriter(svc, discardLogger())
	spawner := &fakeSpawner{}
	reg := NewRegistry(RegistryConfig{
		Root:           t.TempDir(),
		MaxShadowGames: 8,
		Spawner:        spawner,
		Targeting:      NewGateTargetingWriter(NewGate(w, nil, discardLogger()), w),
		Verdict:        fakeVerdict{concludeAtN: 1000},
		Clock:          fixedClock(),
	})
	ctx := context.Background()
	id, err := reg.Declare(sampleIntent())
	if err != nil {
		t.Fatal(err)
	}
	if err := reg.Start(ctx, id); err != nil {
		t.Fatal(err)
	}
	key := id + "|" + ArmExperimental
	if _, ok := svc.ArmValues(GameFlagSet, "death_grace_period_seconds")[key]; !ok {
		t.Fatal("no arm value after Start")
	}

	reg.AllocateAndSpawn(ctx) // same epoch: nothing is written again
	puts := 0
	for _, c := range svc.Calls {
		if strings.HasPrefix(c, "PUT ") {
			puts++
		}
	}
	if puts != 1 {
		t.Fatalf("PUT calls = %d before the restart, want 1: %v", puts, svc.Calls)
	}

	// Free the slots of the pair just spawned so the next tick spawns again.
	spawner.mu.Lock()
	spawned := append([]spawnCall(nil), spawner.calls...)
	spawner.mu.Unlock()
	if len(spawned) == 0 {
		t.Fatal("no pair spawned")
	}
	for _, c := range spawned {
		reg.ReleaseGame(c.gameID, "test")
	}

	svc.Restart()
	if len(svc.ArmValues(GameFlagSet, "death_grace_period_seconds")) != 0 {
		t.Fatal("the fake restart kept the arm value")
	}
	reg.AllocateAndSpawn(ctx)
	if _, ok := svc.ArmValues(GameFlagSet, "death_grace_period_seconds")[key]; !ok {
		t.Fatalf("the arm value was not written again after the restart: %v", svc.Calls)
	}
	// The idle expiry (same epoch) drops the value: it is written again too.
	spawner.mu.Lock()
	spawned = append([]spawnCall(nil), spawner.calls...)
	spawner.mu.Unlock()
	for _, c := range spawned {
		reg.ReleaseGame(c.gameID, "test")
	}
	if err := svc.Delete(ctx, GameFlagSet, "death_grace_period_seconds", url.Values{"experiment_id": {id}, "arm": {ArmExperimental}}); err != nil {
		t.Fatal(err)
	}
	reg.AllocateAndSpawn(ctx)
	if _, ok := svc.ArmValues(GameFlagSet, "death_grace_period_seconds")[key]; !ok {
		t.Fatalf("the arm value was not written again after the idle expiry: %v", svc.Calls)
	}
}
