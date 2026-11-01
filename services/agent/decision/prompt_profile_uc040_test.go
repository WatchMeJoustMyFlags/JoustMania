package decision

import (
	"context"
	"reflect"
	"testing"
	"time"

	"github.com/joustmania/agent/decide"
)

// UC: UC-040 BR-036
// prompt_profile_uc040_test.go: a remote decide request carries the game's
// decision.prompt_profile, which changes neither the state nor the questions, and
// agent.decide records it (#2467 PR 5, decide-contract.md §3, §12).

func TestRemote_PromptProfileOnTheRequest(t *testing.T) {
	run := func(t *testing.T, src PromptProfileSource) (decide.Request, string, bool) {
		t.Helper()
		be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.9, "A", 0.5, "shield"))
		h := newRemoteHarness(t, remoteSnapshot(time.Second), be)
		var asked []string
		if src != nil {
			h.loop.SetPromptProfileSource(func(ctx context.Context, gameID string) string {
				asked = append(asked, gameID)
				return src(ctx, gameID)
			})
		}
		span := h.run(t)
		if src != nil && (len(asked) != 1 || asked[0] != remoteGameID) {
			t.Fatalf("profile read for %v, want once for %s", asked, remoteGameID)
		}
		v, ok := attrValue(span, AttrDecidePromptProfile)
		return be.reqs[0], v.AsString(), ok
	}
	t.Run("UC-040/BR-036 neutral rides on the request and the span", func(t *testing.T) {
		req, attr, ok := run(t, func(context.Context, string) string { return "neutral" })
		if req.PromptProfile != decide.PromptProfileNeutral || !ok || attr != "neutral" {
			t.Fatalf("request profile %q, span %q (%v)", req.PromptProfile, attr, ok)
		}
	})
	t.Run("UC-040/BR-036 an unknown value is normalized to v1", func(t *testing.T) {
		req, attr, _ := run(t, func(context.Context, string) string { return "louder" })
		if req.PromptProfile != decide.PromptProfileV1 || attr != "v1" {
			t.Fatalf("request profile %q, span %q", req.PromptProfile, attr)
		}
	})
	t.Run("UC-040/BR-036 no source leaves the request v1 and the span without the attribute", func(t *testing.T) {
		req, _, ok := run(t, nil)
		if req.PromptProfile != "" || ok {
			t.Fatalf("request profile %q, span attribute present %v", req.PromptProfile, ok)
		}
	})
	t.Run("UC-040/BR-036 the profile changes neither the state nor the questions", func(t *testing.T) {
		neutral, _, _ := run(t, func(context.Context, string) string { return "neutral" })
		v1, _, _ := run(t, nil)
		if neutral.State.Digest != v1.State.Digest || !reflect.DeepEqual(neutral.Questions, v1.Questions) {
			t.Fatal("the prompt profile must only change the wording a backend renders")
		}
	})
}
