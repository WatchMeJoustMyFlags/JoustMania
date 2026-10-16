package flags

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"testing"

	"github.com/open-feature/go-sdk/openfeature"

	"github.com/joustmania/agent/patterngame"
)

// variantEvaluator mimics flagd's targeting {"var": "archetype"}: the variant is
// named by the evaluation-context attribute; an unknown name is an error.
type variantEvaluator struct {
	stubEvaluator
	variants map[string]any
	seenKey  string
}

func (v *variantEvaluator) ObjectValue(_ context.Context, flag string, def any, ec openfeature.EvaluationContext, _ ...openfeature.Option) (any, error) {
	v.seenKey = flag
	name, _ := ec.Attribute(patterngame.ArchetypeContextKey).(string)
	if obj, ok := v.variants[name]; ok {
		return obj, nil
	}
	return def, errors.New("error processing the flag for evaluation")
}

// UC: UC-046 main A1 A2 BR-013
func TestPatternFlags(t *testing.T) {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	t.Run("UC-046/main the archetype variant is selected by the archetype context attribute", func(t *testing.T) {
		ev := &variantEvaluator{variants: map[string]any{"slow_cascade": map[string]any{"players": 4.0}}}
		f := New(ev, log)
		got, err := f.MockArchetype(context.Background(), "slow_cascade")
		if err != nil || got.(map[string]any)["players"] != 4.0 {
			t.Fatalf("MockArchetype = %v, %v", got, err)
		}
		if ev.seenKey != patterngame.ArchetypeFlagKey {
			t.Errorf("evaluated %q, want %q", ev.seenKey, patterngame.ArchetypeFlagKey)
		}
	})
	t.Run("UC-046/A2 an unknown archetype name is an evaluation error", func(t *testing.T) {
		f := New(&variantEvaluator{variants: map[string]any{}}, log)
		if _, err := f.MockArchetype(context.Background(), "ghost"); err == nil {
			t.Fatal("unknown archetype resolved without error")
		}
	})
	t.Run("UC-046/A1 selection and cap fall back to none and 300 on an unreachable flagd", func(t *testing.T) {
		boom := errors.New("flagd down")
		f := New(stubEvaluator{errs: map[string]error{
			patterngame.SelectionFlagKey: boom, patterngame.CapFlagKey: boom,
		}}, log)
		if got := f.MockArchetypes(context.Background()); got != "" {
			t.Errorf("selection = %q, want none", got)
		}
		if got := f.MaxGameSeconds(context.Background()); got != 300 {
			t.Errorf("cap = %d, want 300", got)
		}
		f = New(stubEvaluator{
			strings: map[string]string{patterngame.SelectionFlagKey: "a,b"},
			ints:    map[string]int64{patterngame.CapFlagKey: 600},
		}, log)
		if f.MockArchetypes(context.Background()) != "a,b" || f.MaxGameSeconds(context.Background()) != 600 {
			t.Error("live values not returned")
		}
	})
}
