// Package decide is the in-game decision backend contract (epic #1225): one
// typed request/response shape that every decision backend — the deterministic
// rules engine today, remote models (claude, jev, openai) later — implements, so
// an OpenFeature flag can pick the implementation without a gateway in between.
//
// A Request carries the game State (a deterministic narrative plus a sha256
// Digest of its canonical form) and a list of typed Questions. A backend returns
// one Answer per Question; every Answer carries a confidence in [0,1] and, for
// choice questions, a probability distribution over the options. Backends are
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
// rejects kinds it does not know. (The original brief also listed a "Noul" kind;
// it was dropped because nobody knew what it meant.)
type QuestionKind string

const (
	// KindChoice asks for exactly one of Question.Options, with a probability
	// distribution over the options. A yes/no question is a two-option choice.
	KindChoice QuestionKind = "choice"
	// KindScore asks for a number within Question.Range (inclusive).
	KindScore QuestionKind = "score"
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
	// Confidence is the backend's confidence in this answer, in [0,1].
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
	// Elapsed is the session duration; 0 when not yet observed.
	Elapsed time.Duration `json:"elapsed"`
}

// Request is one decision request: a State and the Questions to answer about it.
type Request struct {
	State     State      `json:"state"`
	Questions []Question `json:"questions"`
	// Context is the raw snapshot State was built from. In-process backends (the
	// rules adapter) read it because the rules engine reasons over the full
	// GameContext; remote backends must use State only. It is never serialized.
	Context *gamecontext.GameContext `json:"-"`
}

// Response is a backend's answers to a Request.
type Response struct {
	Answers []Answer `json:"answers"`
	// Backend is the Name() of the backend that produced the answers.
	Backend string `json:"backend"`
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
