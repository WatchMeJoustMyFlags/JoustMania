// Package decide is the in-game decision backend contract (epic #1225): one
// typed request/response shape that every decision backend — the deterministic
// rules engine today, remote models (claude, jev, openai) later — implements, so
// an OpenFeature flag can pick the implementation without a gateway in between.
//
// A Request carries the game State (a deterministic narrative plus a sha256
// Digest of its canonical form) and a list of typed Questions. A backend returns
// one Answer per Question; every Answer carries a confidence in [0,1] and, for
// choice questions, a probability distribution over the options in which the
// choice is the most probable option.
//
// A noul question (KindNoul) is a yes/no question answered with P(yes) in
// Answer.Noul. Its Confidence is backend-defined like every other; the jev
// backend uses |2p-1| (TypeSafe's documented choice formula for two outcomes).
//
// Only the routing answer's confidence (ffa.intervention, what the router
// gates on) is required to follow the "acting is right" semantics below; other
// answers (target, magnitude, noul) may carry backend-native confidence.
//
// Confidence is the backend's confidence that ACTING on the answer is right. Its
// derivation is backend-defined (the rules adapter uses 1 - p(noop) for an
// intervention, a model may report its own), but it is always in [0,1] and is
// what the confidence router (#1225 PR E) compares against
// decision.act_confidence_threshold. It is deliberately NOT required to equal or
// stay below p(choice): two strong competing interventions split the
// probability mass yet still make acting a confident call. Backends are
// UNTRUSTED (remote models can return anything), so callers must run Validate on
// every Response before acting on it.
//
// This package is a leaf: it imports gamecontext only, never decision. The
// decision package imports it (the rules adapter lives there, next to the
// unexported candidate scores it needs), which keeps the dependency one-way so
// decision's decide() can call any Backend (#1225 PR B).
package decide

import (
	"context"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// QuestionKind is the type of answer a Question expects. It is an OPEN string
// type so more kinds can be added later without breaking the wire shape; Validate
// rejects kinds it does not know.
type QuestionKind string

const (
	// KindChoice asks for exactly one of Question.Options, with a probability
	// distribution over the options. A yes/no question is a two-option choice.
	KindChoice QuestionKind = "choice"
	// KindScore asks for a number within Question.Range (inclusive).
	KindScore QuestionKind = "score"
	// KindNoul is a yes/no question answered with the probability of "yes"
	// (Answer.Noul), the "noul" primitive of TypeSafe's Jev API (#1229). It
	// carries only Text. Not every backend supports it: the LLM backends
	// (claude, openai) reject a Request containing one with an error wrapping
	// ErrInvalid, and the rules adapter answers it neutrally (0.5, confidence 0).
	// No FFA question uses it yet.
	KindNoul QuestionKind = "noul"
)

// Question is one typed question a backend must answer.
type Question struct {
	// ID is stable and unique within a Request, e.g. "ffa.intervention".
	ID   string       `json:"id"`
	Kind QuestionKind `json:"kind"`
	// Text is the human/model-readable prompt for the question.
	Text string `json:"text"`
	// Options are the allowed answers of a KindChoice question (unused otherwise).
	Options []string `json:"options,omitempty"`
	// Range is the inclusive [min, max] of a KindScore question (unused otherwise).
	Range [2]float64 `json:"range"`
}

// Answer is a backend's answer to one Question.
type Answer struct {
	QuestionID string `json:"question_id"`
	// Choice is the picked option of a KindChoice question.
	Choice string `json:"choice,omitempty"`
	// Probabilities is the distribution over the options of a KindChoice question
	// (non-negative, summing to 1). Options missing from the map have probability
	// 0. Must be empty for a KindScore question.
	Probabilities map[string]float64 `json:"probabilities,omitempty"`
	// Score is the value of a KindScore question, within its Range.
	Score float64 `json:"score"`
	// Noul is the probability, in [0,1], that the answer to a KindNoul
	// question is "yes". It is nil for every other kind (and omitted from JSON,
	// so choice/score answers serialize exactly as before).
	Noul *float64 `json:"noul,omitempty"`
	// Confidence is the backend's confidence that acting on this answer is right,
	// in [0,1] (see the package doc).
	Confidence float64 `json:"confidence"`
}

// PlayerState is the per-player slice of State. Pointer fields are nil when the
// signal was never observed (mirroring gamecontext.PlayerSignals), so a backend
// never reads a missing reading as a genuine zero.
type PlayerState struct {
	Serial string `json:"serial"`
	// Alive mirrors the rules engine's notion of an active player
	// (PlayerSignals.Active == true).
	Alive      bool     `json:"alive"`
	Intensity  *float64 `json:"intensity,omitempty"`
	Variance   *float64 `json:"variance,omitempty"`
	Skill      *float64 `json:"skill,omitempty"`
	BatteryPct *float64 `json:"battery_pct,omitempty"`
	// EliminationOrder is the 1-based position in the session's elimination
	// sequence, 0 when the player has not been eliminated.
	EliminationOrder int `json:"elimination_order,omitempty"`

	// Explicit danger signals (#1262), computed in Go from the readings above and
	// the session's death threshold (see danger.go for the cut-offs), so no
	// backend has to compare numbers itself. Set for alive players only.
	//
	// IntensityRank is 1 for the highest current intensity among the alive
	// players with an observed intensity, 0 when unranked.
	IntensityRank int `json:"intensity_rank,omitempty"`
	// ThresholdMargin is death threshold - intensity (g-force, 2 decimals),
	// negative when over it; nil when either is unknown.
	ThresholdMargin *float64 `json:"threshold_margin,omitempty"`
	// Danger is DangerSafe, DangerElevated or DangerNearThreshold; "" unknown.
	Danger string `json:"danger,omitempty"`
	// Shielded is true when a grant_shield / toughness_boost was dispatched to
	// this player within the last ShieldWindow (the longest shield), read from
	// the timeline, except a grant_shield to a ShieldCharged player (in trigger
	// mode a charge is not protection). Danger ignores shields (see danger.go).
	Shielded bool `json:"shielded,omitempty"`
	// ShieldCharged is true while the agent's charge ledger holds a live entry
	// for this alive player at the moment the state is built (trigger mode,
	// UC-040 A31; #1870): the player holds an agent shield charge or has just
	// used it, and another grant_shield is refused.
	ShieldCharged bool `json:"shield_charged,omitempty"`
	// ShieldLimited is true while the agent would refuse another grant_shield to
	// this alive player for a reason other than already_charged at the moment the
	// state is built: the per-game cap or the cooldown (UC-040 A33), and in a hold
	// game a full pool or a used-up budget (A37). Never set with ShieldCharged
	// (#2066; decide-contract.md §1.2).
	ShieldLimited bool `json:"shield_limited,omitempty"`
	// SensitivityFactor and HandicapFactor are the per-player levels the agent
	// itself last wrote to this game (adjust_player_sensitivity,
	// set_player_handicap; applied-value ledger, UC-040 A35), set for an alive
	// player only when not the neutral 1.0 (#2066).
	SensitivityFactor *float64 `json:"sensitivity_factor,omitempty"`
	HandicapFactor    *float64 `json:"handicap_factor,omitempty"`

	// Hold mode only (#2096, UC-040 A36, A37; decide-contract.md §1.2), set for alive
	// players of a game whose game_shield_activation_mode is hold, from the
	// game_player_shield_* gauges (up to one export interval old). ShieldEnergyS is
	// the energy plus the agent's grants in flight, at most the cap; ShieldActive
	// means the player holds the trigger and is protected now.
	ShieldEnergyS          *float64 `json:"shield_energy_s,omitempty"`
	ShieldBudgetRemainingS *float64 `json:"shield_budget_remaining_s,omitempty"`
	ShieldActive           bool     `json:"shield_active,omitempty"`
}

// State is the game state a backend decides on, built from a
// gamecontext.GameContext by BuildState.
type State struct {
	GameID   string `json:"game_id"`
	GameKind string `json:"game_kind"`
	// Digest is the lowercase hex sha256 of the canonical serialization of every
	// other field (see BuildState). Equal digests mean equal states.
	Digest string `json:"digest"`
	// Narrative is the deterministic, line-oriented text rendering of the state
	// (session summary, players, recent timeline) that remote models read.
	Narrative string `json:"narrative"`
	// Players are sorted by serial.
	Players []PlayerState `json:"players"`
	// ElapsedSeconds is the session duration in seconds; 0 when not yet observed.
	ElapsedSeconds float64 `json:"elapsed_s"`
	// MostHeadroom is the alive player with the strictly lowest current
	// intensity (the most headroom below the death threshold right now), ""
	// when undetermined (#1262). A momentary reading, not "winning".
	MostHeadroom string `json:"most_headroom,omitempty"`
	// Struggling is the alive player with the strictly highest current
	// intensity when their danger is elevated or near_threshold, else "".
	Struggling string `json:"struggling,omitempty"`

	// Hold mode only (#2096; decide-contract.md §1.2): ShieldActivation is "hold"
	// and the latched hold values come from the game_shield_hold_setting gauge.
	// Empty / nil in every other game, so their wire and digest are unchanged.
	ShieldActivation   string   `json:"shield_activation,omitempty"`
	ShieldEnergyMaxS   *float64 `json:"shield_energy_max_s,omitempty"`
	ShieldBudgetS      *float64 `json:"shield_budget_s,omitempty"`
	ShieldRechargePerS *float64 `json:"shield_recharge_per_s,omitempty"`
	ShieldMinBurstS    *float64 `json:"shield_min_burst_s,omitempty"`
}

// Elapsed returns ElapsedSeconds as a time.Duration.
func (s State) Elapsed() time.Duration {
	return time.Duration(s.ElapsedSeconds * float64(time.Second))
}

// Request is one decision request: a State and the Questions to answer about it.
type Request struct {
	State     State      `json:"state"`
	Questions []Question `json:"questions"`
	// PromptProfile is the decision.prompt_profile the agent read for this game
	// (profile.go; "" and unknown values render as v1). Set on the remote path only.
	// It is outside State, so the Digest does not cover it.
	PromptProfile string `json:"prompt_profile,omitempty"`
	// Context is the raw snapshot State was built from. Only in-process backends
	// (the rules adapter, which reasons over the full GameContext) may read it.
	// Remote backends MUST NOT read it: State is the entire contract they get, and
	// it is what the Digest covers. It is excluded from JSON, but it is not
	// redacted from fmt, so a Request must never be logged with %v/%+v (that
	// would dump the whole GameContext); log State.Digest and the Questions.
	Context *gamecontext.GameContext `json:"-"`
	// PredictionAsk is the optional ffa.prediction question (#2479, UC-040 A45,
	// BR-034; PredictionQuestion): what the model expects to happen if its answer
	// is applied. It is OUTSIDE Questions, so Validate, the questions span
	// attribute, the digest and jev never see it. The decision loop sets it only
	// when decision.predictions.ask_model is on and the backend is claude or
	// openai; nil (the default) leaves the rendered prompt byte-identical to v1.
	PredictionAsk *Question `json:"prediction_ask,omitempty"`
	// Goal is the optional pair of goal lines (#2479, decide-contract.md §14.4),
	// set on a claude, openai or jev (#2498) request only under
	// decision.goal_in_prompt, and
	// only for a goal with terms. It is outside State,
	// so the Digest does not cover it; with it nil the user message is
	// byte-identical to v1.
	Goal *GoalLines `json:"goal,omitempty"`
}

// GoalLines are the rendered goal lines of a request: Goal is the profile's
// target bands ("long_running_game duration>=180s ..."), Now the measured values
// ("duration=95s activity=0.42 ..."), both without their "goal: " / "now: "
// prefix. Profile is the profile name (decide.goal.profile on agent.decide),
// Source named or from_objectives (decide.goal.source).
type GoalLines struct {
	Profile string `json:"profile"`
	Source  string `json:"source"`
	Goal    string `json:"goal"`
	Now     string `json:"now"`
	// OffBand are the labels of the terms outside their band now that can already
	// be judged (goal.OffBand, #2498); only jev renders them, llmio does not.
	OffBand []string `json:"off_band,omitempty"`
}

// Response is a backend's answers to a Request.
type Response struct {
	Answers []Answer `json:"answers"`
	// Backend is the Name() of the backend that produced the answers.
	Backend string `json:"backend"`
	// Rationale is an optional one-line "why" from the backend. It is
	// untrusted model text: truncate before putting it on spans or logs.
	Rationale string `json:"rationale,omitempty"`
	// Prediction is what the backend expects to happen if its answer is applied
	// (#2479, UC-040 A44, BR-034; prediction.go). It is outside Answers, so
	// Validate never reads it and a missing or bad prediction never blocks a
	// decision. nil when the backend stated none (and for every noop).
	Prediction *Prediction `json:"prediction,omitempty"`
	// PredictionDropped is the reason a model-stated prediction was dropped
	// (PredictionDrop* constants; UC-040 A45): the ask was sent and the answer to
	// it failed ValidatePrediction. The decision is unaffected and no lever
	// default is substituted. "" when nothing was dropped.
	PredictionDropped string `json:"prediction_dropped,omitempty"`
}

// Backend is a decision backend. Decide must honor ctx cancellation; callers
// run Validate on the Response before acting on it.
type Backend interface {
	Name() string
	Decide(ctx context.Context, req Request) (Response, error)
}

// Find returns the answer to questionID, if present.
func (r Response) Find(questionID string) (Answer, bool) {
	for _, a := range r.Answers {
		if a.QuestionID == questionID {
			return a, true
		}
	}
	return Answer{}, false
}
