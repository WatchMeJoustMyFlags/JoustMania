package flags

import (
	"context"
	"errors"
	"testing"

	"github.com/open-feature/go-sdk/openfeature"
)

// armEvaluator resolves "sensitivity" like the flag service's rendered experiment
// rung: the experimental arm of exp-1 in a shadow game gets 4, everything else the
// baseline 2. It records the last evaluation context.
type armEvaluator struct {
	stubEvaluator
	seen openfeature.EvaluationContext
	err  error
}

func (a *armEvaluator) IntValue(_ context.Context, flag string, def int64, ec openfeature.EvaluationContext, _ ...openfeature.Option) (int64, error) {
	a.seen = ec
	if a.err != nil {
		return def, a.err
	}
	if flag != "sensitivity" {
		return def, errors.New("unknown flag")
	}
	attrs := ec.Attributes()
	if attrs["game_kind"] == "shadow" && attrs["experiment_id"] == "exp-1" && attrs["arm"] == "experimental" {
		return 4, nil
	}
	return 2, nil
}

// UC: UC-046 BR-019
func TestShadowGameInt_ResolvesPerArm_2392(t *testing.T) {
	ctx := context.Background()
	t.Run("UC-046/BR-019 each arm resolves with its own context", func(t *testing.T) {
		ev := &armEvaluator{}
		f := gameFlags(ev, nil)
		control, err := f.ShadowGameInt(ctx, "sensitivity", "JoustFFA", "exp-1", "control")
		if err != nil {
			t.Fatal(err)
		}
		experimental, err := f.ShadowGameInt(ctx, "sensitivity", "JoustFFA", "exp-1", "experimental")
		if err != nil {
			t.Fatal(err)
		}
		if control != 2 || experimental != 4 {
			t.Fatalf("control=%d experimental=%d, want 2 and 4", control, experimental)
		}
		want := map[string]any{"game_kind": "shadow", "game_mode": "JoustFFA", "experiment_id": "exp-1", "arm": "experimental"}
		for k, v := range want {
			if got := ev.seen.Attributes()[k]; got != v {
				t.Errorf("eval context %s = %v, want %v", k, got, v)
			}
		}
	})
	t.Run("UC-046/BR-019 an unreadable flag is an error, not a default", func(t *testing.T) {
		f := gameFlags(&armEvaluator{err: errors.New("not ready")}, nil)
		if _, err := f.ShadowGameInt(ctx, "sensitivity", "JoustFFA", "exp-1", "control"); err == nil {
			t.Fatal("want an error for an unreadable flag")
		}
		var nilFlags *Flags
		if _, err := nilFlags.ShadowGameInt(ctx, "sensitivity", "JoustFFA", "exp-1", "control"); err == nil {
			t.Fatal("want an error without a game client")
		}
	})
}
