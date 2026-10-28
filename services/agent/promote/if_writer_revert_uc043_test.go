package promote

import (
	"context"
	"net/url"
	"strings"
	"testing"

	"github.com/joustmania/agent/flagservice"
	"github.com/joustmania/agent/flagservice/fake"
)

// if_writer_revert_uc043_test.go covers the compare-and-delete revert (#1877,
// UC-043 A18): the agent's DELETE carries ?if_writer=promotion, so a real
// overlay another writer set between the agent's read and its DELETE is left in
// place (412 writer_mismatch) and the revert is dropped, not retried.

// racingService is the flag service with another writer's real overlay landing
// right before the agent's DELETE reaches it (the #1753 window).
type racingService struct {
	*fake.Service
	writer string
}

func (r *racingService) Delete(ctx context.Context, set, flag string, q url.Values) error {
	if _, err := r.Service.Put(ctx, set, flag, flagservice.Write{
		Value: 3.0, Scope: flagservice.Scope{GameKind: "real"}, Writer: r.writer,
	}); err != nil {
		return err
	}
	return r.Service.Delete(ctx, set, flag, q)
}

func realOverlayWriterOf(t *testing.T, svc *fake.Service) string {
	t.Helper()
	v, err := svc.GetFlag(context.Background(), "game", promotedFlag)
	if err != nil {
		t.Fatal(err)
	}
	if v.RealOverlay == nil {
		return ""
	}
	return v.RealOverlay.Writer
}

func racingWriter(t *testing.T, svc *fake.Service, other string) *serviceRealDefaultWriter {
	t.Helper()
	snaps, err := newSnapStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	return newServiceRealDefaultWriter(&racingService{Service: svc, writer: other}, snaps, discardLogger())
}

// UC: UC-043 A18
func TestUC043_A18_CompareAndDeleteRevert(t *testing.T) {
	ctx := context.Background()

	t.Run("UC-043/A18 the revert DELETE carries if_writer=promotion", func(t *testing.T) {
		svc := newPromotionService(t)
		w := writerForTest(t, svc, t.TempDir())
		if err := w.SetRealDefault(ctx, promotedFlag, 3.0); err != nil {
			t.Fatal(err)
		}
		if err := w.RevertRealDefault(ctx, promotedFlag); err != nil {
			t.Fatalf("RevertRealDefault: %v", err)
		}
		last := svc.Calls[len(svc.Calls)-1]
		if !strings.HasPrefix(last, "DELETE game/"+promotedFlag) || !strings.Contains(last, "if_writer=promotion") {
			t.Fatalf("revert call = %s, want a DELETE with if_writer=promotion", last)
		}
		if got := realOverlayWriterOf(t, svc); got != "" {
			t.Fatalf("the promotion overlay survived (writer %q)", got)
		}
	})

	for _, guarded := range []bool{false, true} {
		name := "plain"
		if guarded {
			name = "guarded (unless_real_game)"
		}
		t.Run("UC-043/A18 the "+name+" revert leaves an overlay another writer set after the read and is dropped", func(t *testing.T) {
			svc := newPromotionService(t)
			if err := writerForTest(t, svc, t.TempDir()).SetRealDefault(ctx, promotedFlag, 3.0); err != nil {
				t.Fatal(err)
			}
			w := racingWriter(t, svc, "operator")
			var err error
			if guarded {
				err = w.RevertRealDefaultUnlessRealGame(ctx, promotedFlag)
			} else {
				err = w.RevertRealDefault(ctx, promotedFlag)
			}
			if err != nil {
				t.Fatalf("a writer_mismatch must drop the revert, not fail it: %v", err)
			}
			if got := realOverlayWriterOf(t, svc); got != "operator" {
				t.Fatalf("real overlay writer = %q after the revert, want operator (left in place)", got)
			}
			if _, ok := w.PersistedRevertTimes()[promotedFlag]; !ok {
				t.Error("the cooldown was not stamped")
			}
		})
	}

	t.Run("UC-043/A18 the kill switch never removes an overlay another writer set after its read", func(t *testing.T) {
		svc := newPromotionService(t)
		if err := writerForTest(t, svc, t.TempDir()).SetRealDefault(ctx, promotedFlag, 3.0); err != nil {
			t.Fatal(err)
		}
		w := racingWriter(t, svc, "admin")
		p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, w, fakeWatcher{}, w)

		got := p.RevertOnKillSwitch(ctx)

		if len(got) != 1 || got[0].Outcome != OutcomeReverted || got[0].Reason != KillSwitchReason {
			t.Fatalf("kill switch = %+v, want one reverted / kill_switch (the promotion's overlay is gone)", got)
		}
		if w := realOverlayWriterOf(t, svc); w != "admin" {
			t.Fatalf("real overlay writer = %q after the kill switch, want admin", w)
		}
		if again := p.RevertOnKillSwitch(ctx); len(again) != 0 {
			t.Fatalf("second tick reverted %+v, want nothing (no promotion overlay left)", again)
		}
	})
}
