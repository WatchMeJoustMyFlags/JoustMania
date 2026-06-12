package llm

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
)

// decode.go is the DEFENSIVE response parser for the #739 llm decision path. The
// model is asked (by the RESPONSE CONTRACT in buildSystem) to reply with EXACTLY
// ONE JSON object shaped like Response below. A real model does not always
// comply: it may wrap the JSON in prose or ```json fences, omit required fields,
// emit malformed JSON, or name an intervention that is not in the allow-list.
//
// The cardinal safety rule of the llm path (#739 acceptance): an unparseable,
// invalid, or out-of-vocabulary response must NEVER dispatch an arbitrary action.
// So Decode is strict and total — it returns a usable Response ONLY when the
// reply is well-formed AND complete; on anything else it returns a typed error
// and the caller (decision.llmDecide) falls back to the rules engine with a
// recorded reason. Decode does NOT know the per-session allow-list (that lives in
// the flags snapshot in the decision package); it validates STRUCTURE and the
// fixed objective vocabulary here, and the decision package validates the chosen
// intervention against interventions.allowed via the SAME permission gate the
// rules engine uses (so an out-of-vocab/disallowed intervention is blocked
// identically — decision.blocked=true — rather than special-cased here).
//
// This file is pure and has no decision-package dependency (avoids an import
// cycle: decision imports llm, never the reverse), so it is exhaustively
// table-tested in decode_test.go independent of the loop.

// Response is the strict schema the model must return — one JSON object mirroring
// the dispatchable fields of decision.Decision (intervention/target_serial/value/
// reason/objective_served). decision.llmDecide maps a decoded Response onto a
// decision.Decision; it deliberately does NOT mirror Decision's internal Fitness/
// Objectives maps (those are scoring inputs the agent owns, not model output).
type Response struct {
	// Intervention is the chosen action id. It must be present and non-empty; the
	// decision package validates it against interventions.allowed (out-of-vocab =>
	// blocked, never dispatched). "noop" is the contracted "do nothing" sentinel.
	Intervention string `json:"intervention"`
	// TargetSerial scopes a player-targeted intervention; "" = session-scoped.
	// Optional (a session-scoped action legitimately omits it).
	TargetSerial string `json:"target_serial"`
	// Value is the optional per-intervention payload string (see decision.Decision).
	Value string `json:"value"`
	// Reason is the model's one-sentence justification, recorded as decision.reason.
	// Required: a dispatched action with no recorded reason is not auditable.
	Reason string `json:"reason"`
	// ObjectiveServed names which session objective the choice serves. Required and
	// validated against the fixed objective vocabulary (endurance/balanced/
	// accelerate/chaos) — a value outside it rejects the whole response, since a
	// model that invents an objective is not following the contract.
	ObjectiveServed string `json:"objective_served"`
}

// IsNoop reports whether the model chose to do nothing this cycle. A noop is a
// VALID, contract-following response (the System prompt explicitly offers it), but
// it dispatches no action — the caller treats it as "the llm decided, and decided
// to intervene with nothing", distinct from a parse failure.
func (r Response) IsNoop() bool { return r.Intervention == noopIntervention }

// noopIntervention is the contracted "do nothing" intervention id (see the
// RESPONSE CONTRACT in buildSystem). It is always a valid model choice and never
// dispatches an action.
const noopIntervention = "noop"

// objectiveVocabulary is the fixed set of session objectives the model may claim
// to serve, matching the objectives flag schema (#725) the prompt is weighted by.
// A response naming anything outside it is rejected (ErrInvalidObjective): an
// out-of-vocab objective signals the model is not following the contract, so the
// whole response is untrusted and the cycle falls back to rules. This is a CLOSED
// set on purpose — it is the same vocabulary buildSystem tells the model to use.
var objectiveVocabulary = map[string]struct{}{
	"endurance":  {},
	"balanced":   {},
	"accelerate": {},
	"chaos":      {},
}

// Decode-time rejection reasons. All are wrapped in the returned error so the
// caller can log the precise cause, but they collapse to a single fallback class
// (FallbackUnparseable) on the span — the model failed the contract, full stop.
var (
	// ErrEmptyResponse: the model returned nothing usable (empty/whitespace-only).
	ErrEmptyResponse = errors.New("llm: empty response")
	// ErrNotJSON: no JSON object could be extracted/parsed from the response.
	ErrNotJSON = errors.New("llm: response is not a single JSON object")
	// ErrMissingField: a required field (intervention, reason, objective_served)
	// was absent or empty.
	ErrMissingField = errors.New("llm: response missing required field")
	// ErrInvalidObjective: objective_served is not in the fixed vocabulary.
	ErrInvalidObjective = errors.New("llm: objective_served outside vocabulary")
)

// Decode parses a raw model response into a validated Response, or returns a
// typed error. It is intentionally strict: the result is safe to act on (subject
// to the allow-list check the decision package still applies) ONLY when err is
// nil. Validation, in order:
//
//  1. non-empty                         -> ErrEmptyResponse
//  2. extractable + parseable JSON obj  -> ErrNotJSON
//  3. intervention present              -> ErrMissingField
//  4. reason present                    -> ErrMissingField
//  5. objective_served present & in set -> ErrMissingField / ErrInvalidObjective
//
// A "noop" intervention passes (it is the contracted do-nothing choice) but still
// requires a reason and a valid objective — even doing nothing must be auditable.
func Decode(raw string) (Response, error) {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return Response{}, ErrEmptyResponse
	}

	obj, err := extractJSONObject(trimmed)
	if err != nil {
		return Response{}, err
	}

	var r Response
	// DisallowUnknownFields is intentionally NOT used: a compliant model may add
	// commentary keys, and we only consume the contracted ones. Extra fields are
	// harmless; MISSING required fields are the failure mode we guard below.
	if err := json.Unmarshal(obj, &r); err != nil {
		return Response{}, fmt.Errorf("%w: %v", ErrNotJSON, err)
	}

	// Required fields. An action with no intervention id is meaningless; a
	// dispatched action with no reason is not auditable; the objective must be one
	// the agent recognizes.
	if strings.TrimSpace(r.Intervention) == "" {
		return Response{}, fmt.Errorf("%w: intervention", ErrMissingField)
	}
	if strings.TrimSpace(r.Reason) == "" {
		return Response{}, fmt.Errorf("%w: reason", ErrMissingField)
	}
	if strings.TrimSpace(r.ObjectiveServed) == "" {
		return Response{}, fmt.Errorf("%w: objective_served", ErrMissingField)
	}
	if _, ok := objectiveVocabulary[r.ObjectiveServed]; !ok {
		return Response{}, fmt.Errorf("%w: %q", ErrInvalidObjective, r.ObjectiveServed)
	}

	return r, nil
}

// extractJSONObject pulls the single JSON object out of a model reply. Models
// frequently wrap the object in prose or in a ```json fence despite the "no
// prose" instruction, so rather than demand a byte-perfect object we locate the
// first '{' and the matching last '}' and parse the span between them. This is
// deliberately forgiving about SURROUNDING text but strict about the object
// itself: the extracted span must parse as a JSON object (json.Valid + leading
// '{'), otherwise ErrNotJSON. It does not attempt to find a SECOND object — the
// contract is exactly one — so trailing junk after the object is ignored, which
// is the common, benign case (a fence close or a stray newline).
func extractJSONObject(s string) ([]byte, error) {
	start := strings.IndexByte(s, '{')
	end := strings.LastIndexByte(s, '}')
	if start < 0 || end < start {
		return nil, ErrNotJSON
	}
	candidate := s[start : end+1]
	if !json.Valid([]byte(candidate)) {
		return nil, ErrNotJSON
	}
	return []byte(candidate), nil
}
