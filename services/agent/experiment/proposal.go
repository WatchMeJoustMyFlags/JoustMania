// Package experiment is the FOUNDATION of the agent's M7 self-improvement track
// (#931 / #932 — M7-4 + M7-5): the agent runs shadow-scoped GAME-FLAG
// experiments. It observes game narratives, forms a hypothesis, and proposes a
// structured {flag, experimental_value} change to the `game` flagset
// (services/flagd/game.json) — scoped to SHADOW games via a `game_kind`
// evaluation-context targeting rule. Real games are untouched.
//
// This package is JUST the Writer + Gate, built and proven in isolation. The
// narrative→hypothesis proposal GENERATION (the LLM/agent side) is a later
// issue; here the Proposal type, Writer, and Gate are the whole surface.
//
// Direction-of-control split (agreed design 2026-06-12, #931/#932):
//
//   - services/flagd/agent.json (`agent` flagSetId) is the agent's OWN
//     governance (kill-switch, mode, bounds). It is `:ro`-mounted into the agent
//     container — the kernel refuses agent writes regardless of any app/LLM bug.
//     The agent READS it, NEVER writes it. This package hardcodes a deny on it.
//   - services/flagd/game.json (`game` flagSetId) is the flagset the agent
//     WRITES to run experiments. A single flagSetId means game services need no
//     second OpenFeature client.
//
// THE INVARIANT (the Gate's whole job): an agent write must NEVER change what a
// `game_kind="real"` evaluation resolves to, for any flag. Shadow-vs-real
// isolation is achieved by construction — the Writer only ever emits a flagd
// targeting rule conditioned on `game_kind != "real"` (i.e. shadow). Shadow
// games resolve the experimental variant; real games fall through to the
// existing defaultVariant (and any pre-existing targeting), untouched.
package experiment

// Proposal is the structured experiment the agent emits: try ExperimentalValue
// for FlagKey, scoped to shadow games. It is deliberately tiny — the agent
// reasons about WHICH flag and WHAT value from the game narrative (later issue),
// and the Writer/Gate turn that into a shadow-scoped flagd targeting rule.
//
// ExperimentalValue is the raw value (number/string/bool/object) the experiment
// wants shadow games to resolve. The Writer adds it as a NEW variant — it never
// mutates an existing variant or the defaultVariant (that would risk the real
// resolution; see Gate).
type Proposal struct {
	// FlagKey is the game flag to experiment on. It MUST already exist in
	// game.json — the agent explores the existing parameterized surface; a
	// brand-new flag nothing reads is a no-op, and unbounded key creation is a
	// smell. The Gate rejects unknown keys (#932 design: "experiment on the
	// parameterized surface").
	FlagKey string

	// ExperimentID scopes this proposal to one experiment so K concurrent
	// experiments coexist on the same flag without clobbering (#977). Empty = the
	// LEGACY unscoped binary rule (game_kind != "real" selects the single reserved
	// agent_experiment variant) — byte-identical to the pre-#977 writer. Non-empty
	// = an experiment-scoped rule: experiment_id == ExperimentID AND arm ==
	// "experimental" selects a per-experiment agent_experiment__<id> variant;
	// control + real + every OTHER experiment fall through to the existing default.
	// The id is opaque to the Writer/Gate; the spawn step (#978) owns allocation.
	ExperimentID string

	// ExperimentalValue is the value shadow games should resolve for FlagKey. It
	// is added as a new variant; its JSON kind MUST match the flag's existing
	// variants (the game-side reader expects a consistent type). The Gate enforces
	// this (ReasonTypeMismatch) — e.g. a string on a numeric flag is rejected
	// before the write. JSON has one number type, so int-vs-float (6 vs 4.0) is
	// fine.
	ExperimentalValue any

	// Rationale is the agent's hypothesis ("why this value might improve the
	// game"). It is recorded for the audit trail / later fitness attribution; it
	// does not affect the write.
	Rationale string
}
