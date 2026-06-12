package experiment

import (
	"encoding/json"
	"fmt"
)

// Shared constants for the shadow-scoping contract. These are the single source
// of truth for both the Writer (which constructs the rule) and the Gate (which
// verifies it).
const (
	// GameKindVar is the evaluation-context key the shadow/real split keys on.
	// PREREQUISITE: the game services must put `game_kind` in the OpenFeature
	// evaluation context for this targeting to take effect at runtime — the
	// #838 per-game context threads game_id; a follow-up adds game_kind. Until
	// then real games still resolve defaultVariant (false fall-through), so this
	// writer is safe to ship ahead of the wiring. (#931/#932 build order.)
	GameKindVar = "game_kind"

	// GameKindReal is the eval-context value marking a REAL (player-facing) game.
	// Anything that is NOT this value is treated as shadow. We scope on
	// "!= real" (not "== shadow") deliberately: the invariant we must protect is
	// "real resolution unchanged", so the condition is defined relative to real.
	// A missing/unknown game_kind therefore resolves the experimental variant
	// (shadow-by-default), which is correct — only an explicit real game is
	// protected, and real games always set game_kind="real".
	GameKindReal = "real"

	// ExperimentVariant is the single variant name the Writer adds/overwrites
	// with the experimental value. One reserved name keeps re-experimentation on
	// the same flag idempotent (overwrite, not accumulate) and makes the Gate's
	// "only this variant is new" check trivial. It is namespaced so it can never
	// collide with a game-authored variant.
	ExperimentVariant = "agent_experiment"
)

// buildShadowTargeting constructs the flagd JSONLogic targeting block that
// selects ExperimentVariant for shadow games (game_kind != "real") and falls
// through to whatever real games resolved before (existingTargeting, or null →
// defaultVariant) for real games.
//
// Shape (matches the flagd targeting precedent in game.json, e.g. the
// `sensitivity` flag's {"if":[cond, "fast", null]}):
//
//	{"if": [ {"!=": [{"var":"game_kind"}, "real"]}, "agent_experiment", <else> ]}
//
// where <else> is the flag's PRE-EXISTING targeting block verbatim (so a flag
// that already targets game_mode keeps doing so for real games), or null when
// the flag had no targeting (→ flagd falls through to defaultVariant).
//
// This is shadow-scoped BY CONSTRUCTION: for a `real` context the "!=" condition
// is false, so flagd evaluates <else> — byte-identical to the pre-write
// resolution. That is the structural half of the Gate's invariant.
func buildShadowTargeting(existingTargeting json.RawMessage) (json.RawMessage, error) {
	var elseBranch json.RawMessage
	if len(existingTargeting) > 0 {
		elseBranch = existingTargeting
	} else {
		elseBranch = json.RawMessage("null")
	}

	cond := map[string]any{
		"!=": []any{
			map[string]any{"var": GameKindVar},
			GameKindReal,
		},
	}
	condRaw, err := json.Marshal(cond)
	if err != nil {
		return nil, fmt.Errorf("marshal shadow condition: %w", err)
	}
	thenRaw, err := json.Marshal(ExperimentVariant)
	if err != nil {
		return nil, fmt.Errorf("marshal shadow variant: %w", err)
	}

	// Assemble {"if": [cond, then, else]} from raw parts so the pre-existing
	// targeting (else) round-trips byte-for-byte.
	var buf []byte
	buf = append(buf, []byte(`{"if":[`)...)
	buf = append(buf, condRaw...)
	buf = append(buf, ',')
	buf = append(buf, thenRaw...)
	buf = append(buf, ',')
	buf = append(buf, elseBranch...)
	buf = append(buf, []byte(`]}`)...)
	return json.RawMessage(buf), nil
}

// jsonEqual reports whether two raw JSON values are semantically equal
// (key-order- and whitespace-independent) by decoding both into any and
// comparing. Used by the Gate's invariant check and by re-experiment detection.
func jsonEqual(a, b json.RawMessage) bool {
	var av, bv any
	if err := json.Unmarshal(a, &av); err != nil {
		return false
	}
	if err := json.Unmarshal(b, &bv); err != nil {
		return false
	}
	ab, err := json.Marshal(av)
	if err != nil {
		return false
	}
	bb, err := json.Marshal(bv)
	if err != nil {
		return false
	}
	return string(ab) == string(bb)
}
