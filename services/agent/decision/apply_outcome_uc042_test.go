package decision

import (
	"context"
	"errors"
	"sync"
	"testing"

	"github.com/joustmania/agent/gamecontext"
)

// applyOutcomeRecorder captures every apply-outcome recorder call (#1771).
type applyOutcomeRecorder struct {
	mu   sync.Mutex
	hits []gamecontext.ApplyOutcome
}

func (r *applyOutcomeRecorder) record(_ string, outcome gamecontext.ApplyOutcome) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.hits = append(r.hits, outcome)
}

func (r *applyOutcomeRecorder) snapshot() []gamecontext.ApplyOutcome {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]gamecontext.ApplyOutcome(nil), r.hits...)
}

// TestRunDecision_RecordsApplyOutcome: a dispatched decision reports how its write
// ended, so the game summary can count applied vs blocked (entity model
// GAME_SUMMARY, #1771): written = applied, actuation-gate no-op = gated_off, write
// error = write_failed. A decision the permission chain blocked never reaches the
// sink and reports no apply outcome.
// UC: UC-042 main
func TestRunDecision_RecordsApplyOutcome(t *testing.T) {
	for _, tc := range []struct {
		name    string
		allowed []string
		sink    ActionSink
		want    []gamecontext.ApplyOutcome
	}{
		{"UC-042/main written -> applied", []string{"grant_shield"}, &gatedSink{gateOn: true}, []gamecontext.ApplyOutcome{gamecontext.ApplyOutcomeApplied}},
		{"UC-042/main actuation gate off -> gated_off", []string{"grant_shield"}, &gatedSink{gateOn: false}, []gamecontext.ApplyOutcome{gamecontext.ApplyOutcomeGatedOff}},
		{"UC-042/main write error -> write_failed", []string{"grant_shield"}, &fakeSink{err: errors.New("boom")}, []gamecontext.ApplyOutcome{gamecontext.ApplyOutcomeWriteFailed}},
		{"UC-042/main permission chain blocked -> no apply outcome", nil, &fakeSink{}, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			l, _, fl := newTestLoop(t)
			fl.snap.InterventionsAllowed = tc.allowed
			rec := &applyOutcomeRecorder{}
			l.SetApplyOutcomeRecorder(rec.record)
			l.Actions = tc.sink
			l.Rules = fakeRules{out: []Decision{{Intervention: "grant_shield", TargetSerial: "AA:11", Reason: "test"}}}

			l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "game-1"}, testTrigger())

			got := rec.snapshot()
			if len(got) != len(tc.want) {
				t.Fatalf("apply outcomes = %v, want %v", got, tc.want)
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Errorf("apply outcome[%d] = %q, want %q", i, got[i], tc.want[i])
				}
			}
		})
	}
}
