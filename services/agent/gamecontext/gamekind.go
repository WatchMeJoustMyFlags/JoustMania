package gamecontext

// Game-kind vocabulary (#1249).
//
// Two vocabularies describe the same real/shadow split:
//
//   - Telemetry (what the agent ingests): the game coordinator labels its
//     metrics (game_kind datapoint label) and spans (game.kind attribute) with
//     "primary" for the player-facing menu game and "shadow" for a parallel
//     evaluation game (services/game_coordinator/game_session.py
//     GAME_KIND_PRIMARY / GAME_KIND_SHADOW). Dashboards and alerts depend on
//     "primary", so the coordinator keeps it.
//   - Flag evaluation (what the agent reasons in): "real" / "shadow"
//     (lib/feature_flags.py GAME_KIND_REAL / GAME_KIND_SHADOW,
//     experiment.GameKindReal). llm.eligible_game_kinds, the experiment
//     baseline and every shadow-isolation targeting rule use this vocabulary.
//
// The agent normalises once, at ingest, so GameContext.GameKind is always in the
// flag-evaluation vocabulary.
const (
	// GameKindReal is a live, player-facing game (telemetry "primary").
	GameKindReal = "real"
	// GameKindShadow is a parallel evaluation game (same in both vocabularies).
	GameKindShadow = "shadow"
	// TelemetryGameKindPrimary is the coordinator's telemetry label for a real game.
	TelemetryGameKindPrimary = "primary"
)

// NormalizeGameKind maps a telemetry game kind onto the flag-evaluation
// vocabulary: "primary" -> "real"; "real" and "shadow" pass through.
//
// An empty kind stays EMPTY ("unknown"). It is deliberately not mapped to
// "real": many signals (per-player metrics, unlabeled spans) carry no game_kind
// at all, and an unlabeled signal from a SHADOW game must never make that game
// look real. Empty keeps today's fail-safes: Store.SetGameKind ignores it (an
// unlabeled signal never clobbers an observed kind) and LLMGate.EligibleFor("")
// is false (unknown games stay rules-only). The experiment baseline applies the
// same rule: an empty kind never seeds the recent-real baseline.
//
// Any other value is passed through unchanged, so an unrecognised label matches
// neither "real" nor "shadow" and fails closed for eligibility.
func NormalizeGameKind(kind string) string {
	if kind == TelemetryGameKindPrimary {
		return GameKindReal
	}
	return kind
}

// TelemetryGameKind maps the flag-evaluation vocabulary back to the telemetry
// vocabulary for the agent's OWN metric labels (agent_decisions_total and
// agent_intervention_writes_total, #2294): "real" -> "primary", everything else
// -> "shadow". The label has exactly two values; an empty or unrecognised kind
// folds into "shadow" so an unknown game can never inflate the real-game counts
// (the same fail-safe as NormalizeGameKind). "primary" passes through.
func TelemetryGameKind(kind string) string {
	if kind == GameKindReal || kind == TelemetryGameKindPrimary {
		return TelemetryGameKindPrimary
	}
	return GameKindShadow
}
