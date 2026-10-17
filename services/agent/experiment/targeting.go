package experiment

import "encoding/json"

// Shared constants for the experiment-scoping contract (FR-128). The flag
// service renders an experiment-arm value as the rung
// `game_kind != "real" AND experiment_id == <id> AND arm == "experimental"`
// (docs/specs/flags.md §13); the game services put these keys in the
// OpenFeature evaluation context.
const (
	// GameKindVar is the evaluation-context key the shadow/real split keys on.
	GameKindVar = "game_kind"

	// GameKindReal is the eval-context value marking a REAL (player-facing) game.
	// The rung is scoped on "!= real" (not "== shadow") deliberately: the
	// invariant to protect is "real resolution unchanged", so the condition is
	// defined relative to real.
	GameKindReal = "real"

	// ExperimentIDVar is the evaluation-context key carrying the experiment a
	// game belongs to (cross-service contract with #975). A real game has no
	// experiment_id, so every experiment rung is false for it.
	ExperimentIDVar = "experiment_id"

	// ArmVar is the evaluation-context key carrying which arm of an experiment a
	// game is in: "experimental" (resolve the experiment value) or "control"
	// (fall through to the default).
	ArmVar = "arm"

	// ArmExperimental is the arm value that resolves the experiment value. The
	// control arm is not matched by the rung: a control game falls through.
	ArmExperimental = "experimental"
)

// jsonEqual reports whether two raw JSON values are semantically equal
// (key-order- and whitespace-independent) by decoding both into any and
// comparing. Used by the Gate's rendered-flag check.
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
