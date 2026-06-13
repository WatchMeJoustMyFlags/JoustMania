package experiment

import (
	"encoding/json"
	"fmt"
	"strings"
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

	// ExperimentIDVar is the evaluation-context key carrying the experiment a
	// game belongs to. It is a CROSS-SERVICE CONTRACT: #975 sets this same key on
	// the game-side OpenFeature context (mirroring game_kind). A real game has NO
	// experiment_id (the API-level default omits it), so every experiment
	// condition is false for real → existing default. Must match #975's lib value.
	ExperimentIDVar = "experiment_id"

	// ArmVar is the evaluation-context key carrying which arm of an experiment a
	// game is in: "experimental" (resolve the override) or "control" (fall
	// through to the existing default). Cross-service contract with #975.
	ArmVar = "arm"

	// ArmExperimental is the arm value that resolves the experimental variant. The
	// control arm is NOT a distinct value the targeting matches — a control game
	// simply is NOT ArmExperimental, so the `and` is false and it falls through to
	// the existing else (same trick as the game_kind != real fall-through).
	// Cross-service contract with #975.
	ArmExperimental = "experimental"

	// ExperimentVariant is the LEGACY single reserved variant name used by the
	// binary (experiment-unscoped) shadow rule when a Proposal carries no
	// ExperimentID. Experiment-scoped proposals use experimentVariantName(id) =
	// ExperimentVariant + "__" + id so K experiments on one flag never collide.
	// It is namespaced so it can never collide with a game-authored variant.
	ExperimentVariant = "agent_experiment"

	// experimentVariantSep separates the ExperimentVariant prefix from the
	// experiment id in a scoped variant name (agent_experiment__exp_X).
	experimentVariantSep = "__"
)

// experimentVariantName returns the reserved variant name for an experiment-
// scoped proposal: "agent_experiment__<experiment_id>". An empty id yields the
// legacy unscoped ExperimentVariant so the pre-experiment rule round-trips
// byte-for-byte (regression contract).
func experimentVariantName(experimentID string) string {
	if experimentID == "" {
		return ExperimentVariant
	}
	return ExperimentVariant + experimentVariantSep + experimentID
}

// isAnyExperimentVariant reports whether name is a reserved experiment variant —
// the legacy ExperimentVariant or any scoped agent_experiment__<id>. The Gate
// uses this to recognise every agent-owned variant (immutable across re-runs)
// and to forbid any OTHER newly added variant.
func isAnyExperimentVariant(name string) bool {
	return name == ExperimentVariant || strings.HasPrefix(name, ExperimentVariant+experimentVariantSep)
}

// buildShadowTargeting constructs the flagd JSONLogic targeting block for a
// proposal, wrapping any pre-existing targeting (existingTargeting, or null) as
// the else branch.
//
// Unscoped (experimentID == ""), the binary shadow rule — byte-identical to the
// pre-#977 writer so the regression round-trips exactly:
//
//	{"if": [ {"!=": [{"var":"game_kind"}, "real"]}, "agent_experiment", <else> ]}
//
// Experiment-scoped (experimentID == "exp_X"):
//
//	{"if": [
//	  {"and": [ {"==": [{"var":"experiment_id"}, "exp_X"]},
//	            {"==": [{"var":"arm"}, "experimental"]} ]},
//	  "agent_experiment__exp_X",
//	  <else>
//	]}
//
// In both cases <else> is the flag's PRE-EXISTING targeting verbatim (so a flag
// that already targets game_mode, OR carries another experiment's branch, keeps
// doing so), or null when the flag had no targeting. K experiments on the SAME
// flag therefore nest as chained ifs: this experiment's branch is outermost, the
// previous targeting (its own chain) is the else.
//
// Shadow/experiment-scoped BY CONSTRUCTION: a real context has no experiment_id
// (and game_kind="real"), so every condition is false → flagd evaluates <else>,
// byte-identical to the pre-write real resolution. That is the structural half
// of the Gate's invariant, and it now holds across ALL chained branches.
func buildShadowTargeting(experimentID string, existingTargeting json.RawMessage) (json.RawMessage, error) {
	var elseBranch json.RawMessage
	if len(existingTargeting) > 0 {
		elseBranch = existingTargeting
	} else {
		elseBranch = json.RawMessage("null")
	}

	condRaw, err := json.Marshal(shadowCondition(experimentID))
	if err != nil {
		return nil, fmt.Errorf("marshal shadow condition: %w", err)
	}
	thenRaw, err := json.Marshal(experimentVariantName(experimentID))
	if err != nil {
		return nil, fmt.Errorf("marshal shadow variant: %w", err)
	}

	// Assemble {"if": [cond, then, else]} from raw parts so the else (any
	// pre-existing targeting, including other experiments' branches) round-trips
	// byte-for-byte.
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

// shadowCondition returns the JSONLogic condition that selects the experimental
// variant for a proposal. Unscoped: game_kind != real. Scoped: experiment_id ==
// id AND arm == experimental.
func shadowCondition(experimentID string) map[string]any {
	if experimentID == "" {
		return map[string]any{
			"!=": []any{
				map[string]any{"var": GameKindVar},
				GameKindReal,
			},
		}
	}
	return map[string]any{
		"and": []any{
			map[string]any{"==": []any{map[string]any{"var": ExperimentIDVar}, experimentID}},
			map[string]any{"==": []any{map[string]any{"var": ArmVar}, ArmExperimental}},
		},
	}
}

// isShadowCondition reports whether raw is the LEGACY unscoped shadow condition
// {"!=":[{"var":"game_kind"},"real"]}.
func isShadowCondition(raw json.RawMessage) bool {
	want, err := json.Marshal(shadowCondition(""))
	if err != nil {
		return false
	}
	return jsonEqual(raw, want)
}

// experimentIDFromCondition returns (id, true) when raw is an experiment-scoped
// condition {"and":[{"==":[{"var":"experiment_id"}, id]}, {"==":[{"var":"arm"},
// "experimental"]}]}, extracting the experiment id. Otherwise ("", false). The
// arm clause must be exactly == experimental so the control arm cannot match.
func experimentIDFromCondition(raw json.RawMessage) (string, bool) {
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(raw, &obj); err != nil || len(obj) != 1 {
		return "", false
	}
	andRaw, ok := obj["and"]
	if !ok {
		return "", false
	}
	var clauses []json.RawMessage
	if err := json.Unmarshal(andRaw, &clauses); err != nil || len(clauses) != 2 {
		return "", false
	}
	id, ok := equalsVarLiteral(clauses[0], ExperimentIDVar)
	if !ok {
		return "", false
	}
	arm, ok := equalsVarLiteral(clauses[1], ArmVar)
	if !ok || arm != ArmExperimental {
		return "", false
	}
	if id == "" {
		return "", false
	}
	return id, true
}

// equalsVarLiteral parses {"==":[{"var": wantVar}, "<literal>"]} and returns the
// string literal if the var matches wantVar. Returns ("", false) on any other
// shape (non-string literal, wrong var, wrong op).
func equalsVarLiteral(raw json.RawMessage, wantVar string) (string, bool) {
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(raw, &obj); err != nil || len(obj) != 1 {
		return "", false
	}
	eqRaw, ok := obj["=="]
	if !ok {
		return "", false
	}
	var operands []json.RawMessage
	if err := json.Unmarshal(eqRaw, &operands); err != nil || len(operands) != 2 {
		return "", false
	}
	var v struct {
		Var string `json:"var"`
	}
	if err := json.Unmarshal(operands[0], &v); err != nil || v.Var != wantVar {
		return "", false
	}
	var lit string
	if err := json.Unmarshal(operands[1], &lit); err != nil {
		return "", false
	}
	return lit, true
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
