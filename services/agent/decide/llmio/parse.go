package llmio

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strings"
	"unicode"

	"github.com/joustmania/agent/decide"
)

// Limits on model output. A legitimate FFA answer is well under 2 KiB with 3
// answers and at most 5 probability entries each; the caps only stop a hostile
// or runaway model from costing memory or CPU.
const (
	// MaxOutputBytes caps the raw JSON Parse accepts.
	MaxOutputBytes = 64 << 10
	// MaxAnswers caps the answers array.
	MaxAnswers = 32
	// MaxProbabilities caps one answer's probability entries.
	MaxProbabilities = 64
	// MaxRationaleRunes caps Response.Rationale (longer text is truncated, not
	// rejected: the rationale is informational).
	MaxRationaleRunes = 200
)

// renormalizeTolerance is how far a model's probabilities may sum from 1 and
// still be rescaled to sum to exactly 1. Models round to two decimals, so 0.99
// or 1.01 are honest answers; anything further off is left for decide.Validate
// to reject.
const renormalizeTolerance = 0.02

// ErrMalformed wraps every structural Parse failure (not JSON, wrong shape,
// too large). It wraps decide.ErrInvalid, so a caller maps it to the same
// "invalid answer" fallback reason as a Validate failure.
var ErrMalformed = fmt.Errorf("%w: malformed model output", decide.ErrInvalid)

// wireAnswer and wireOutput mirror AnswerSchema. Unknown fields are ignored.
type wireProbability struct {
	Option string   `json:"option"`
	P      *float64 `json:"p"`
}

type wireAnswer struct {
	ID            string            `json:"id"`
	Choice        string            `json:"choice"`
	Score         *float64          `json:"score"`
	Probabilities []wireProbability `json:"probabilities"`
	Confidence    *float64          `json:"confidence"`
}

type wireOutput struct {
	Answers   []wireAnswer `json:"answers"`
	Rationale string       `json:"rationale"`
}

// Parse decodes raw model output (one JSON object matching AnswerSchema) into a
// decide.Response for req, tagged with backend, and validates it with
// decide.Validate. Any error wraps decide.ErrInvalid. Parse never panics.
//
// Normalization is deliberately narrow:
//   - a single markdown code fence around the JSON is stripped (some
//     OpenAI-compatible servers add one even in JSON mode);
//   - for a score question the choice/probabilities fields are dropped, and for
//     a choice question the score field is dropped (the schema forces every
//     field to be present, so the "unused" value is noise, not a contradiction);
//   - choice probabilities summing to within 0.02 of 1 are rescaled to sum to 1.
//
// Everything else (unknown question ids, duplicate answers, out-of-range
// numbers, a choice that is not the most probable option) is left for
// decide.Validate to reject.
func Parse(req decide.Request, raw []byte, backend string) (decide.Response, error) {
	if len(raw) > MaxOutputBytes {
		return decide.Response{}, fmt.Errorf("%w: output is %d bytes, limit %d", ErrMalformed, len(raw), MaxOutputBytes)
	}
	body := stripFence(bytes.TrimSpace(raw))
	if len(body) == 0 {
		return decide.Response{}, fmt.Errorf("%w: empty output", ErrMalformed)
	}

	var out wireOutput
	dec := json.NewDecoder(bytes.NewReader(body))
	if err := dec.Decode(&out); err != nil {
		return decide.Response{}, fmt.Errorf("%w: %v", ErrMalformed, clipErr(err))
	}
	if dec.More() {
		return decide.Response{}, fmt.Errorf("%w: trailing data after the JSON object", ErrMalformed)
	}
	if len(out.Answers) == 0 {
		return decide.Response{}, fmt.Errorf("%w: no answers", ErrMalformed)
	}
	if len(out.Answers) > MaxAnswers {
		return decide.Response{}, fmt.Errorf("%w: %d answers, limit %d", ErrMalformed, len(out.Answers), MaxAnswers)
	}

	kinds := make(map[string]decide.QuestionKind, len(req.Questions))
	for _, q := range req.Questions {
		kinds[q.ID] = q.Kind
	}

	resp := decide.Response{
		Backend:   backend,
		Rationale: SanitizeRationale(out.Rationale),
		Answers:   make([]decide.Answer, 0, len(out.Answers)),
	}
	for i, wa := range out.Answers {
		a, err := toAnswer(wa, kinds[wa.ID])
		if err != nil {
			return decide.Response{}, fmt.Errorf("%w: answer %d: %v", ErrMalformed, i, err)
		}
		resp.Answers = append(resp.Answers, a)
	}
	if err := decide.Validate(req, resp); err != nil {
		return decide.Response{}, err
	}
	return resp, nil
}

func toAnswer(wa wireAnswer, kind decide.QuestionKind) (decide.Answer, error) {
	if wa.Confidence == nil {
		return decide.Answer{}, errors.New("missing confidence")
	}
	a := decide.Answer{QuestionID: wa.ID, Confidence: *wa.Confidence}
	switch kind {
	case decide.KindScore:
		if wa.Score == nil {
			return decide.Answer{}, errors.New("score question without a score")
		}
		a.Score = *wa.Score
	case decide.KindChoice:
		a.Choice = wa.Choice
		probs, err := toProbabilities(wa.Probabilities)
		if err != nil {
			return decide.Answer{}, err
		}
		a.Probabilities = probs
	default:
		// Unknown question id: keep what was sent so decide.Validate names it.
		a.Choice = wa.Choice
		if wa.Score != nil {
			a.Score = *wa.Score
		}
	}
	return a, nil
}

func toProbabilities(in []wireProbability) (map[string]float64, error) {
	if len(in) > MaxProbabilities {
		return nil, fmt.Errorf("%d probabilities, limit %d", len(in), MaxProbabilities)
	}
	out := make(map[string]float64, len(in))
	var sum float64
	for _, p := range in {
		if p.P == nil {
			return nil, errors.New("probability entry without p")
		}
		if _, dup := out[p.Option]; dup {
			return nil, fmt.Errorf("duplicate probability for option %q", clipRunes(p.Option, 64))
		}
		out[p.Option] = *p.P
		sum += *p.P
	}
	if sum > 0 && !math.IsInf(sum, 0) && !math.IsNaN(sum) &&
		sum != 1 && math.Abs(sum-1) <= renormalizeTolerance {
		for k, v := range out {
			if v > 0 {
				out[k] = v / sum
			}
		}
	}
	return out, nil
}

// stripFence removes one surrounding ``` / ```json fence.
func stripFence(b []byte) []byte {
	if !bytes.HasPrefix(b, []byte("```")) {
		return b
	}
	b = b[3:]
	if nl := bytes.IndexByte(b, '\n'); nl >= 0 {
		b = b[nl+1:]
	} else {
		return nil
	}
	b = bytes.TrimSpace(b)
	b = bytes.TrimSuffix(b, []byte("```"))
	return bytes.TrimSpace(b)
}

// SanitizeRationale turns untrusted model text into a single line of at most
// MaxRationaleRunes runes: control characters and newlines become spaces, runs
// of whitespace collapse, and the result is trimmed.
func SanitizeRationale(s string) string {
	s = strings.Map(func(r rune) rune {
		if unicode.IsControl(r) || unicode.IsSpace(r) {
			return ' '
		}
		return r
	}, s)
	s = strings.Join(strings.Fields(s), " ")
	return clipRunes(s, MaxRationaleRunes)
}

func clipRunes(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}

func clipErr(err error) string { return clipRunes(err.Error(), 160) }
