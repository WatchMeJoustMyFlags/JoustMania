package flags

import (
	"context"
	"errors"
	"testing"

	"github.com/open-feature/go-sdk/openfeature"
)

// UC: UC-040 BR-036
// prompt_profile_uc040_test.go: decision.prompt_profile is read per game in the
// game's flag context and falls back to v1 (#2467 PR 5, decide-contract.md §3).

// profileEvaluator answers decision.prompt_profile with value and err, recording the
// evaluation context. A failed read still returns value, so only the caller's error
// handling can turn it into v1.
type profileEvaluator struct {
	stubEvaluator
	value string
	err   error
	seen  []openfeature.EvaluationContext
}

func (p *profileEvaluator) StringValue(_ context.Context, flag, def string, ec openfeature.EvaluationContext, _ ...openfeature.Option) (string, error) {
	if flag != keyDecisionPromptProfile {
		return def, nil
	}
	p.seen = append(p.seen, ec)
	return p.value, p.err
}

func TestPromptProfile_Read(t *testing.T) {
	for _, tc := range []struct {
		name, value string
		err         error
		want        string
	}{
		{"UC-040/BR-036 neutral is neutral", "neutral", nil, "neutral"},
		{"UC-040/BR-036 v1 is v1", "v1", nil, "v1"},
		{"UC-040/BR-036 an unknown value is v1", "louder", nil, "v1"},
		{"UC-040/BR-036 an empty value is v1", "", nil, "v1"},
		{"UC-040/BR-036 a read error is v1", "neutral", errors.New("PROVIDER_NOT_READY"), "v1"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ev := &profileEvaluator{value: tc.value, err: tc.err}
			if got := New(ev, nil).PromptProfile(context.Background(), "g-1"); got != tc.want {
				t.Fatalf("PromptProfile = %q, want %q", got, tc.want)
			}
		})
	}
	t.Run("UC-040/BR-036 a nil wrapper or client is v1", func(t *testing.T) {
		var f *Flags
		if got := f.PromptProfile(context.Background(), "g-1"); got != "v1" {
			t.Fatalf("nil Flags = %q", got)
		}
		if got := New(nil, nil).PromptProfile(context.Background(), "g-1"); got != "v1" {
			t.Fatalf("nil client = %q", got)
		}
	})
}

func TestPromptProfile_GameContext(t *testing.T) {
	t.Run("UC-040/BR-036 read with the game's kind, experiment and arm", func(t *testing.T) {
		ev := &profileEvaluator{value: "neutral"}
		f := New(ev, nil)
		f.SetGameAttrs(func(id string) GameAttrs {
			if id != "g-7" {
				t.Errorf("lookup for %q", id)
			}
			return GameAttrs{GameKind: "shadow", ExperimentID: "exp-1", Arm: "b"}
		})
		f.PromptProfile(context.Background(), "g-7")
		if len(ev.seen) != 1 {
			t.Fatalf("reads = %d", len(ev.seen))
		}
		attrs := ev.seen[0].Attributes()
		for k, want := range map[string]any{"gameId": "g-7", "game_kind": "shadow", "experiment_id": "exp-1", "arm": "b"} {
			if attrs[k] != want {
				t.Errorf("%s = %v, want %v", k, attrs[k], want)
			}
		}
	})
	t.Run("UC-040/BR-036 an unknown game reads as real", func(t *testing.T) {
		ev := &profileEvaluator{value: "v1"}
		New(ev, nil).PromptProfile(context.Background(), "g-8")
		if attrs := ev.seen[0].Attributes(); attrs["game_kind"] != "real" || attrs["arm"] != nil {
			t.Errorf("context = %v", attrs)
		}
	})
}
