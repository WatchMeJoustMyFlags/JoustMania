package flags

import (
	"context"

	"github.com/open-feature/go-sdk/openfeature"

	"github.com/joustmania/agent/patterngame"
)

// Pattern-game flags (UC-046, flags.md §3.1). Read at an experiment's first spawn
// and pinned, never per decision.

// MockArchetypes returns the raw experiment.mock_archetypes selection ("" = none,
// also on any evaluation error: an unreachable flagd means no pattern games).
func (f *Flags) MockArchetypes(ctx context.Context) string {
	return f.stringFlag(ctx, patterngame.SelectionFlagKey, "")
}

// MockArchetype evaluates the object flag experiment.mock_archetype for one
// archetype name: the variant is chosen by the evaluation-context attribute
// "archetype" (the flag's targeting), so an unknown name is a flagd evaluation
// error. The flag is a JSON object (not a list), so the RPC resolver returns it
// through ObjectValue without a TYPE_MISMATCH.
func (f *Flags) MockArchetype(ctx context.Context, name string) (any, error) {
	evalCtx := openfeature.NewTargetlessEvaluationContext(map[string]any{
		patterngame.ArchetypeContextKey: name,
	})
	return f.client.ObjectValue(ctx, patterngame.ArchetypeFlagKey, map[string]any{}, evalCtx)
}

// MaxGameSeconds returns the safety cap experiment.max_game_seconds (default 300).
func (f *Flags) MaxGameSeconds(ctx context.Context) int {
	return f.intFlag(ctx, patterngame.CapFlagKey, patterngame.DefaultMaxGameSeconds)
}
