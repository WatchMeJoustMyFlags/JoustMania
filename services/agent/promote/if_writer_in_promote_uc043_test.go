package promote

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"testing"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flagservice"
	"github.com/joustmania/agent/flagservice/fake"
)

// if_writer_in_promote_uc043_test.go covers the promotion write precondition
// (#2260, UC-043 A19): the agent's real-overlay PUT carries
// ?if_writer_in=promotion, so it never overwrites another writer's real
// overlay; a 412 writer_mismatch means "do not promote".

func putOverlay(t *testing.T, svc *fake.Service, writer string, value float64) {
	t.Helper()
	if _, err := svc.Put(context.Background(), "game", promotedFlag, flagservice.Write{
		Value: value, Scope: flagservice.Scope{GameKind: "real"}, Writer: writer,
	}); err != nil {
		t.Fatal(err)
	}
}

// UC: UC-043 A19
func TestUC043_A19_PromotionWritePrecondition(t *testing.T) {
	ctx := context.Background()

	t.Run("UC-043/A19 SetRealDefault writes where no real overlay is present", func(t *testing.T) {
		svc := newPromotionService(t)
		w := writerForTest(t, svc, t.TempDir())
		if err := w.SetRealDefault(ctx, promotedFlag, 3.0); err != nil {
			t.Fatalf("SetRealDefault: %v", err)
		}
		if got := realOverlayWriterOf(t, svc); got != promotionWriter {
			t.Fatalf("real overlay writer = %q, want promotion", got)
		}
	})

	t.Run("UC-043/A19 SetRealDefault replaces a previous promotion's overlay", func(t *testing.T) {
		svc := newPromotionService(t)
		putOverlay(t, svc, promotionWriter, 2.0)
		w := writerForTest(t, svc, t.TempDir())
		if err := w.SetRealDefault(ctx, promotedFlag, 3.0); err != nil {
			t.Fatalf("SetRealDefault: %v", err)
		}
	})

	t.Run("UC-043/A19 SetRealDefault over another writer's overlay is refused and leaves it in place", func(t *testing.T) {
		svc := newPromotionService(t)
		putOverlay(t, svc, "operator", 2.0)
		w := writerForTest(t, svc, t.TempDir())
		var logBuf bytes.Buffer
		w.log = slog.New(slog.NewTextHandler(&logBuf, nil))
		err := w.SetRealDefault(ctx, promotedFlag, 3.0)
		if !strings.Contains(logBuf.String(), "owner=operator") {
			t.Fatalf("WARN log = %q, want the owning writer", logBuf.String())
		}
		if !errors.Is(err, ErrRealOverlayOwnedByAnotherWriter) {
			t.Fatalf("SetRealDefault = %v, want ErrRealOverlayOwnedByAnotherWriter", err)
		}
		if got := realOverlayWriterOf(t, svc); got != "operator" {
			t.Fatalf("real overlay writer = %q after a refused promotion, want operator", got)
		}
		if _, cooling := w.PersistedRevertTimes()[promotedFlag]; cooling {
			t.Fatal("a refused promotion stamped the post-revert cooldown")
		}
	})

	t.Run("UC-043/A19 an autonomous promotion over another writer's overlay is discarded, not retried", func(t *testing.T) {
		svc := newPromotionService(t)
		putOverlay(t, svc, "operator", 2.0)
		rev := &recordingReverter{}
		p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, writerForTest(t, svc, t.TempDir()), fakeWatcher{}, rev)
		ev := testEvidence()
		ev.RealDefaultValue = 3.0
		prop := experiment.Proposal{FlagKey: promotedFlag, ExperimentID: "exp-a19", ExperimentalValue: 3.0}
		res := p.Promote(ctx, prop, promoteVerdict, autoOn, ev)
		if res.Outcome != OutcomeDiscarded || res.Reason != ReasonRealOverlayForeign+" owner=operator" {
			t.Fatalf("outcome = %q (%s), want discarded (%s owner=operator)", res.Outcome, res.Reason, ReasonRealOverlayForeign)
		}
		if got := realOverlayWriterOf(t, svc); got != "operator" {
			t.Fatalf("real overlay writer = %q, want operator", got)
		}
		if _, cooling := p.cooldownRemaining(promotedFlag); cooling {
			t.Fatal("a refused promotion started the post-revert cooldown")
		}
		if len(p.PendingFlags()) != 0 {
			t.Fatalf("pending = %v, want nothing queued", p.PendingFlags())
		}
	})
}

// UC: UC-043 A19
// UC: UC-043 A17
func TestUC043_A19_DeferredWriteMeetsAnotherWriter(t *testing.T) {
	t.Run("UC-043/A19 a waiting write that meets writer_mismatch is dropped, journaled discarded", func(t *testing.T) {
		rig := newDeferredRig(t, nil)
		if res := rig.promote(t, holdProposal("exp-w")); res.Outcome != OutcomeDeferred {
			t.Fatalf("outcome = %q, want deferred", res.Outcome)
		}
		rig.realDef.err = fmt.Errorf("promote game/%s: %w", holdKey, &foreignOverlayError{Owner: "operator"})
		rig.probe.set(false)
		rig.p.DrainDeferred(context.Background(), fixed(autoOn))
		if got := rig.journal.all(); len(got) != 1 || got[0] != "exp-w|discarded|"+ReasonRealOverlayForeign+" owner=operator" {
			t.Fatalf("journal = %v, want one discarded %s", got, ReasonRealOverlayForeign)
		}
		if len(rig.p.PendingFlags()) != 0 {
			t.Fatalf("pending = %v, want the entry dropped", rig.p.PendingFlags())
		}
		rig.p.DrainDeferred(context.Background(), fixed(autoOn))
		if len(rig.realDef.calls) != 1 {
			t.Fatalf("SetRealDefault calls = %d, want 1 (no retry)", len(rig.realDef.calls))
		}
	})
}
