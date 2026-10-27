package promote

import (
	"context"
	"testing"
)

// UC-043 A17 step 3 (#2096): until the deferred real write lands, an autonomous
// promotion of a hold key writes nothing, so no real write and no watch revert of
// it can land mid-game. A non-hold key is unchanged.
// UC: UC-043 A17
func TestPromoteAutonomousHoldKeyWritesNothing(t *testing.T) {
	for _, key := range []string{"shield.activation", "shield.energy_budget_seconds"} {
		t.Run("UC-043/A17 "+key+" is discarded without a real write", func(t *testing.T) {
			realDef := &recordingRealDefault{}
			p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, realDef, fakeWatcher{degraded: true}, &recordingReverter{})
			prop := promoteProposal
			prop.FlagKey = key
			res := p.Promote(context.Background(), prop, promoteVerdict,
				Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}, testEvidence())
			if res.Outcome != OutcomeDiscarded || res.Reason != HoldPromotionDeferredReason {
				t.Fatalf("outcome = %q (%s), want discarded with the hold reason", res.Outcome, res.Reason)
			}
			if len(realDef.calls) != 0 {
				t.Fatalf("SetRealDefault calls = %d, want 0", len(realDef.calls))
			}
		})
	}
	t.Run("UC-043/A17 a non-hold key is still applied", func(t *testing.T) {
		realDef := &recordingRealDefault{}
		p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, realDef, fakeWatcher{degraded: false}, &recordingReverter{})
		res := p.Promote(context.Background(), promoteProposal, promoteVerdict,
			Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}, testEvidence())
		if res.Outcome != OutcomeApplied || len(realDef.calls) != 1 {
			t.Fatalf("outcome = %q calls %d, want applied once", res.Outcome, len(realDef.calls))
		}
	})
}
