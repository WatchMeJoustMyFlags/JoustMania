package experiment

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
)

// proposer_decode.go is the DEFENSIVE response parser for the M7-4 (#931)
// flag-experiment PROPOSAL path. It is the exact analogue of the #739 intervention
// decoder (services/agent/llm/decode.go): the model is asked (by the RESPONSE
// CONTRACT the Proposer builds) to reply with EXACTLY ONE JSON object shaped like
// proposalResponse below. A real model does not always comply — it may wrap the
// JSON in prose or ```json fences, omit a field, emit malformed JSON, or name a
// flag that does not exist on the game surface.
//
// The cardinal safety rule (same untrusted-output discipline as #739/#932): an
// unparseable, invalid, or out-of-vocabulary response must NEVER produce a
// Proposal. So decodeProposal is strict and total — it returns a usable Proposal
// ONLY when the reply is well-formed, complete, AND names a flag that exists in
// the supplied vocabulary; on anything else it returns a typed error and the
// Proposer records "no proposal this cycle" (no Gate.Review, no write). We never
// fabricate a proposal from a broken response.
//
// Defence in depth: even though the Gate independently rejects unknown flags
// (ReasonUnknownFlag), type mismatches (ReasonTypeMismatch), and non-game targets
// (ReasonNonGameTarget), we reject an out-of-vocab flag HERE too so a malformed
// model reply never even reaches Gate.Review — the Gate's blocked span is for
// genuine invariant violations, not for routine model garbage. The flag
// vocabulary is passed in (the live set of game.json flag keys), so this file has
// no flagd dependency and is exhaustively table-tested, exactly like decode.go.

// proposalResponse is the strict JSON schema the model must return — one object
// naming the game flag to tune, the value to try, and the hypothesis. It maps 1:1
// onto experiment.Proposal{FlagKey, ExperimentalValue, Rationale}; the experiment
// package owns the Proposal carrier, so the decoder produces a Proposal directly.
type proposalResponse struct {
	// Flag is the game.json flag key to experiment on. Required, non-empty, and
	// validated against the known-flag vocabulary (out-of-vocab => rejected, never
	// proposed). The model must pick an EXISTING flag — the agent explores the
	// parameterized surface, it does not invent flags (see Proposal.FlagKey doc).
	Flag string `json:"flag"`
	// Value is the experimental value to try for Flag. Required (a proposal with no
	// value is meaningless). Left as a raw message so any JSON kind round-trips
	// unchanged into Proposal.ExperimentalValue — the Gate's type guard
	// (ReasonTypeMismatch) is the authority on whether the kind matches the flag's
	// existing variants; the decoder only insists the field is PRESENT.
	Value json.RawMessage `json:"value"`
	// Rationale is the model's one-sentence hypothesis ("why this value might
	// improve the game"). Required: a proposed experiment with no recorded
	// rationale is not auditable (it lands on the code_improvement.proposed span).
	Rationale string `json:"rationale"`
}

// Decode-time rejection reasons for the proposal path. All are wrapped in the
// returned error so the Proposer can log the precise cause; they collapse to a
// single "no proposal" outcome on the span (the model failed the contract).
var (
	// errEmptyProposal: the model returned nothing usable (empty/whitespace-only).
	errEmptyProposal = errors.New("experiment: empty proposal response")
	// errNotJSONProposal: no JSON object could be extracted/parsed from the reply.
	errNotJSONProposal = errors.New("experiment: proposal response is not a single JSON object")
	// errMissingProposalField: a required field (flag, value, rationale) was absent
	// or empty.
	errMissingProposalField = errors.New("experiment: proposal response missing required field")
	// errUnknownProposalFlag: flag is not in the supplied game-flag vocabulary. The
	// model named a flag nothing reads — reject before Gate.Review (which would
	// independently reject it as ReasonUnknownFlag, but we keep model garbage off
	// the blocked span).
	errUnknownProposalFlag = errors.New("experiment: proposal names an unknown game flag")
)

// decodeProposal parses a raw model response into a validated Proposal, or returns
// a typed error. knownFlags is the live set of game.json flag keys (the
// vocabulary the model was constrained to). The result is safe to submit to
// Gate.Review ONLY when err is nil. Validation, in order (mirrors llm.Decode):
//
//  1. non-empty                         -> errEmptyProposal
//  2. extractable + parseable JSON obj  -> errNotJSONProposal
//  3. flag present                      -> errMissingProposalField
//  4. value present                     -> errMissingProposalField
//  5. rationale present                 -> errMissingProposalField
//  6. flag in knownFlags vocabulary     -> errUnknownProposalFlag
//
// It is intentionally forgiving about SURROUNDING prose/fences (extractJSONObject)
// but strict about the object's required fields and the flag vocabulary. The
// experimental value's TYPE is NOT checked here — that is the Gate's type guard
// (#955); the decoder only insists value is present and JSON-decodable.
func decodeProposal(raw string, knownFlags map[string]struct{}) (Proposal, error) {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return Proposal{}, errEmptyProposal
	}

	obj, err := extractProposalObject(trimmed)
	if err != nil {
		return Proposal{}, err
	}

	var r proposalResponse
	// DisallowUnknownFields is intentionally NOT used: a compliant model may add
	// commentary keys, and we only consume the contracted ones (same call as
	// llm.Decode). MISSING required fields are the failure mode we guard below.
	if err := json.Unmarshal(obj, &r); err != nil {
		return Proposal{}, fmt.Errorf("%w: %v", errNotJSONProposal, err)
	}

	if strings.TrimSpace(r.Flag) == "" {
		return Proposal{}, fmt.Errorf("%w: flag", errMissingProposalField)
	}
	// A missing value field decodes to a nil/empty RawMessage; an explicit JSON
	// null is also rejected (a null experimental value carries no experiment, and
	// the Gate's type guard treats null as kindless — never a useful proposal).
	if len(r.Value) == 0 || string(r.Value) == "null" {
		return Proposal{}, fmt.Errorf("%w: value", errMissingProposalField)
	}
	if strings.TrimSpace(r.Rationale) == "" {
		return Proposal{}, fmt.Errorf("%w: rationale", errMissingProposalField)
	}

	// Out-of-vocab flag: the model named a flag not on the game surface. Reject
	// here so a hallucinated flag never reaches Gate.Review. A nil/empty vocabulary
	// rejects everything (fail-closed: with no known flags, nothing is proposable).
	if _, ok := knownFlags[r.Flag]; !ok {
		return Proposal{}, fmt.Errorf("%w: %q", errUnknownProposalFlag, r.Flag)
	}

	// Unmarshal the raw value into a Go any so ExperimentalValue carries the same
	// shape the Writer will re-marshal (numbers as float64, etc.) — matching how a
	// proposal value flows through the Gate's type guard and the Writer's
	// applyProposal. A value that fails to decode here is malformed JSON we already
	// would have caught, but guard it for totality.
	var value any
	if err := json.Unmarshal(r.Value, &value); err != nil {
		return Proposal{}, fmt.Errorf("%w: value: %v", errNotJSONProposal, err)
	}

	return Proposal{
		FlagKey:           r.Flag,
		ExperimentalValue: value,
		Rationale:         r.Rationale,
	}, nil
}

// extractProposalObject pulls the single JSON object out of a model reply. Models
// frequently wrap the object in prose or a ```json fence despite a "no prose"
// instruction, so we locate the first '{' and the matching last '}' and parse the
// span between them — forgiving about SURROUNDING text, strict about the object
// itself (it must parse as a JSON object). Identical strategy to
// llm.extractJSONObject; duplicated (not shared) to keep this package free of an
// llm-decode dependency and independently table-tested.
func extractProposalObject(s string) ([]byte, error) {
	start := strings.IndexByte(s, '{')
	end := strings.LastIndexByte(s, '}')
	if start < 0 || end < start {
		return nil, errNotJSONProposal
	}
	candidate := s[start : end+1]
	if !json.Valid([]byte(candidate)) {
		return nil, errNotJSONProposal
	}
	return []byte(candidate), nil
}
