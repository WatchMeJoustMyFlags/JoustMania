package experiment

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/joustmania/agent/llm"
)

type failingProposerBackend struct{ err error }

func (b failingProposerBackend) Infer(context.Context, llm.Prompt) (string, error) { return "", b.err }

// UC: UC-042 Post-F-1
func TestProposeOnce_FailedProposalLeavesNoFlagChange(t *testing.T) {
	cases := []struct {
		name    string
		backend Backend
	}{
		{"undecodable reply", &fakeProposerBackend{response: "raise the grace period"}},
		{"name outside the catalog", &fakeProposerBackend{response: `{"flag":"made_up_flag","value":1,"rationale":"x"}`}},
		{"model call fails", failingProposerBackend{err: errors.New("boom")}},
	}
	for _, tc := range cases {
		t.Run("UC-042/Post-F-1 "+tc.name+": no real value changed, no arm value remains on any flag", func(t *testing.T) {
			p, svc, _, _ := proposerWithRecorder(t, tc.backend)
			set, err := svc.GetSet(context.Background(), GameFlagSet)
			if err != nil || len(set.Flags) == 0 {
				t.Fatalf("game flag set unavailable: %v", err)
			}
			rendered := map[string]string{}
			for _, f := range set.Flags {
				rendered[f.Flag] = string(svc.Rendered(GameFlagSet, f.Flag))
			}

			res := p.ProposeOnce(context.Background(), Narrative{}, ProposerConfig{Permit: allowPropose, Engine: "claude"})
			if res.Outcome == ProposeProposed {
				t.Fatalf("outcome = %q, want a failed proposal", res.Outcome)
			}

			for _, f := range set.Flags {
				if arms := svc.ArmValues(GameFlagSet, f.Flag); len(arms) != 0 {
					t.Fatalf("flag %s still carries arm values %v", f.Flag, arms)
				}
				if got := string(svc.Rendered(GameFlagSet, f.Flag)); got != rendered[f.Flag] {
					t.Fatalf("flag %s changed: %s -> %s", f.Flag, rendered[f.Flag], got)
				}
			}
		})
	}
}

// UC: UC-042 A7
func TestProposeOnce_NotEligibleIsGatedAndKeepsTheCadenceFloor(t *testing.T) {
	t.Run("UC-042/A7 game kind not eligible: llm_gated llm_not_eligible, no call, next game end may try again", func(t *testing.T) {
		backend := &fakeProposerBackend{response: validReply}
		p, svc, _, _ := proposerWithRecorder(t, backend)
		adm := &fakeAdmission{ok: false, reason: "llm_not_eligible"}
		p.SetLLMAdmission(adm)
		cfg := ProposerConfig{Engine: "claude", MinInterval: time.Hour, Permit: allowPropose, GameKind: "shadow"}

		res := p.ProposeOnce(context.Background(), Narrative{}, cfg)
		if res.Outcome != ProposeLLMGated || res.Reason != "llm_not_eligible" {
			t.Fatalf("result = (%q, %q), want (llm_gated, llm_not_eligible)", res.Outcome, res.Reason)
		}
		if backend.calls != 0 {
			t.Fatalf("Infer calls = %d, want 0", backend.calls)
		}
		if _, ok := experimentVariantValue(t, svc, "death_grace_period_seconds"); ok {
			t.Fatal("a gated cycle must not write an experiment")
		}
		adm.ok, adm.reason = true, ""
		if got := p.ProposeOnce(context.Background(), Narrative{}, cfg).Outcome; got != ProposeProposed {
			t.Fatalf("next cycle = %q, want proposed (the cadence floor was not used up)", got)
		}
	})
}
