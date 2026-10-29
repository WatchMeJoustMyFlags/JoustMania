package experiment

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/experiment/journal"
	"github.com/joustmania/agent/flagservice"
	"github.com/joustmania/agent/flagservice/fake"
	"github.com/joustmania/agent/llm"
)

// proposer_flows_uc042_test.go covers the proposer's failure flows of UC-042:
// A4 (the gate rejects the started experiment: it is aborted with start_failed),
// A10 (a failed or unusable model reply keeps the cadence floor and the budget
// slot used) and A11 (a full concurrent-experiment budget refuses with
// no_capacity).

// slotAdmission is an LLMAdmission with a fixed number of budget slots. A slot is
// spent on every admitted call and never returned, like the shared per-minute
// budget within its window.
type slotAdmission struct {
	mu    sync.Mutex
	slots int
	calls int
}

func (a *slotAdmission) Admit(context.Context, string) (bool, string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.calls++
	if a.slots <= 0 {
		return false, "llm_budget_exhausted"
	}
	a.slots--
	return true, ""
}

func (a *slotAdmission) left() int {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.slots
}

// sequenceBackend answers each call with the next reply.
type sequenceBackend struct {
	mu      sync.Mutex
	replies []string
	calls   int
}

func (b *sequenceBackend) Infer(context.Context, llm.Prompt) (string, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	r := b.replies[b.calls%len(b.replies)]
	b.calls++
	return r, nil
}

// registryProposer wires a Proposer over a real registry and the given sink
// budget, with a span recorder. It returns the registry and its journal root.
func registryProposer(t *testing.T, backend Backend, budget int) (*Proposer, *Registry, string, *fake.Service, *tracetest.SpanRecorder) {
	t.Helper()
	svc := newGameService(t)
	rec := tracetest.NewSpanRecorder()
	tp := trace.NewTracerProvider(trace.WithSpanProcessor(rec))
	w := NewWriter(svc, discardLogger())
	root := t.TempDir()
	reg := NewRegistry(RegistryConfig{
		Root:      root,
		Targeting: NewGateTargetingWriter(NewGate(w, tp.Tracer("test"), discardLogger()), w),
		Verdict:   fakeVerdict{concludeAtN: 1000},
		Clock:     fixedClock(),
	})
	sink := &RegistrySink{Registry: reg, Objective: "balanced"}
	if budget > 0 {
		sink.Budget = func(context.Context) int { return budget }
	}
	p := NewProposer(backend, sink, gameVocab(svc), tp.Tracer("test"), discardLogger())
	return p, reg, root, svc, rec
}

// UC: UC-042 A4
// UC: UC-043 A8
func TestProposeOnce_GateRejectionAbortsWithStartFailed(t *testing.T) {
	cases := []struct {
		name   string
		reply  string
		setup  func(svc *fake.Service)
		reason Reason
	}{
		{
			name:   "a mistyped value",
			reply:  `{"flag":"death_grace_period_seconds","value":"way too long","rationale":"a string"}`,
			reason: ReasonTypeMismatch,
		},
		{
			name:  "a flag service that refuses the dry run",
			reply: `{"flag":"death_grace_period_seconds","value":3.0,"rationale":"longer grace"}`,
			setup: func(svc *fake.Service) {
				svc.FailPut = &flagservice.Error{Status: 503, Reason: flagservice.ReasonNoSyncStream}
			},
			reason: ReasonWriteSimulationError,
		},
	}
	for _, c := range cases {
		t.Run("UC-042/A4 "+c.name+" aborts the declared experiment with start_failed", func(t *testing.T) {
			p, reg, root, svc, rec := registryProposer(t, &fakeProposerBackend{response: c.reply}, 0)
			if c.setup != nil {
				c.setup(svc)
			}

			res := p.ProposeOnce(context.Background(), Narrative{}, ProposerConfig{Permit: allowPropose})

			rej, ok := AsReject(res.Err)
			if res.Outcome != ProposeBlocked || !ok || rej.Reason != c.reason {
				t.Fatalf("result = %q %v, want blocked %s", res.Outcome, res.Err, c.reason)
			}
			// Step 1: the experiment was declared, then aborted with start_failed.
			ids, err := journal.List(root)
			if err != nil || len(ids) != 1 {
				t.Fatalf("journal experiments = %v (err %v), want the one declaration", ids, err)
			}
			if st, _ := reg.Status(ids[0]); st != StatusAborted {
				t.Fatalf("status = %s, want aborted", st)
			}
			outcomes := eventsOfKind(journalEvents(t, reg, ids[0]), journal.KindOutcome)
			if len(outcomes) != 1 || outcomes[0].Status != string(StatusAborted) || outcomes[0].Note != "start_failed" {
				t.Fatalf("journal outcome events = %+v, want one aborted start_failed", outcomes)
			}
			if live := reg.Live(); len(live) != 0 {
				t.Fatalf("live experiments = %v, want none", live)
			}
			if _, busy := reg.ActiveFlags()["death_grace_period_seconds"]; busy {
				t.Fatal("the aborted experiment must not keep its flag under test")
			}
			if arms := svc.ArmValues(GameFlagSet, "death_grace_period_seconds"); len(arms) != 0 {
				t.Fatalf("arm values written for a rejected start: %v", arms)
			}
			// Step 2: the reason is on the gate span (blocked) and the attempt span.
			gateSpan := findSpan(t, rec, SpanProposed)
			assertSpanBoolAttr(t, gateSpan, AttrBlocked, true)
			assertSpanAttr(t, gateSpan, AttrReason, string(c.reason))
			attempt := findSpan(t, rec, SpanProposeAttempt)
			assertSpanAttr(t, attempt, AttrOutcome, string(ProposeBlocked))
			assertSpanAttr(t, attempt, AttrReason, string(c.reason))
		})
	}
}

// UC: UC-042 A10
func TestProposeOnce_FailedModelCallKeepsCadenceAndBudgetUsed(t *testing.T) {
	cases := []struct {
		name    string
		backend *fakeProposerBackend
		outcome ProposeOutcome
	}{
		{"a model call error", &fakeProposerBackend{err: errors.New("claude: 529 overloaded")}, ProposeNoBackend},
		{"a timeout", &fakeProposerBackend{err: context.DeadlineExceeded}, ProposeNoBackend},
		{"an undecodable reply", &fakeProposerBackend{response: "raise the grace period, I think"}, ProposeUndecodable},
		{"a flag outside the catalog", &fakeProposerBackend{response: `{"flag":"num_teams","value":4,"rationale":"x"}`}, ProposeUndecodable},
	}
	for _, c := range cases {
		t.Run("UC-042/A10 "+c.name+" declares nothing and keeps the cadence floor and budget slot used", func(t *testing.T) {
			p, reg, root, svc, rec := registryProposer(t, c.backend, 0)
			clk := &fakeClock{now: time.Unix(1_700_000_000, 0)}
			p.SetClock(clk.Now)
			adm := &slotAdmission{slots: 2}
			p.SetLLMAdmission(adm)
			cfg := ProposerConfig{Permit: allowPropose, MinInterval: time.Minute, GameKind: "real"}

			res := p.ProposeOnce(context.Background(), Narrative{}, cfg)
			if res.Outcome != c.outcome || res.Proposal != nil {
				t.Fatalf("result = %q %+v, want %s and no proposal", res.Outcome, res.Proposal, c.outcome)
			}
			assertSpanAttr(t, findSpan(t, rec, SpanProposeAttempt), AttrOutcome, string(c.outcome))
			if ids, _ := journal.List(root); len(ids) != 0 || len(reg.Live()) != 0 {
				t.Fatalf("declared %v (live %v), want nothing", ids, reg.Live())
			}
			if arms := svc.ArmValues(GameFlagSet, "death_grace_period_seconds"); len(arms) != 0 {
				t.Fatalf("arm values written: %v", arms)
			}
			// The budget slot stays used: one of two is gone.
			if left := adm.left(); left != 1 {
				t.Fatalf("budget slots left = %d, want 1 (the failed call keeps its slot)", left)
			}
			// The cadence floor stays used: a cycle inside the window is gated,
			// makes no model call and takes no slot.
			clk.advance(30 * time.Second)
			if got := p.ProposeOnce(context.Background(), Narrative{}, cfg).Outcome; got != ProposeGated {
				t.Fatalf("cycle inside the floor = %q, want gated", got)
			}
			if c.backend.calls != 1 || adm.calls != 1 {
				t.Fatalf("Infer calls = %d, admissions = %d, want 1 and 1", c.backend.calls, adm.calls)
			}
		})
	}
}

// UC: UC-042 A11
func TestProposeOnce_FullExperimentBudgetRefusesNoCapacity(t *testing.T) {
	t.Run("UC-042/A11 a full concurrent-experiment budget refuses with no_capacity and declares nothing", func(t *testing.T) {
		backend := &sequenceBackend{replies: []string{
			`{"flag":"nonstop.respawn_seconds","value":6.0,"rationale":"longer spawn shield"}`,
			`{"flag":"death_grace_period_seconds","value":3.0,"rationale":"longer grace"}`,
		}}
		p, reg, root, svc, rec := registryProposer(t, backend, 1)
		cfg := ProposerConfig{Permit: allowPropose}

		if got := p.ProposeOnce(context.Background(), Narrative{}, cfg).Outcome; got != ProposeProposed {
			t.Fatalf("first proposal = %q, want proposed", got)
		}
		res := p.ProposeOnce(context.Background(), Narrative{}, cfg)
		rej, ok := AsReject(res.Err)
		if res.Outcome != ProposeBlocked || !ok || rej.Reason != ReasonNoCapacity {
			t.Fatalf("second proposal = %q %v, want blocked no_capacity", res.Outcome, res.Err)
		}
		var attempts []trace.ReadOnlySpan
		for _, s := range rec.Ended() {
			if s.Name() == SpanProposeAttempt {
				attempts = append(attempts, s)
			}
		}
		if len(attempts) != 2 {
			t.Fatalf("attempt spans = %d, want 2", len(attempts))
		}
		assertSpanAttr(t, attempts[1], AttrOutcome, string(ProposeBlocked))
		assertSpanAttr(t, attempts[1], AttrReason, string(ReasonNoCapacity))
		if ids, _ := journal.List(root); len(ids) != 1 || len(reg.Live()) != 1 {
			t.Fatalf("declared %v (live %v), want only the first experiment", ids, reg.Live())
		}
		if arms := svc.ArmValues(GameFlagSet, "death_grace_period_seconds"); len(arms) != 0 {
			t.Fatalf("arm values written for the refused proposal: %v", arms)
		}
	})
}
