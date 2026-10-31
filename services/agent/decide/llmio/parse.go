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

// Limits on model output. A legitimate FFA answer is well under 1 KiB with 3
// answers and at most 3 probability entries each (the prompt asks for the top
// 3); the caps only stop a hostile or runaway model from costing memory or CPU.
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

// truncatedMinSum is the smallest sum a TRUNCATED probability list (fewer
// entries than the question has options) may have and still be rescaled to sum
// to 1. The prompt asks for the top 3 options only, and models sometimes list
// just the chosen option at p=0.95 and drop the tail instead of spreading it
// (seen live on claude-haiku-4-5, #1231). Rescaling keeps the ranking (the
// choice must still be the argmax for decide.Validate). It is only safe when the
// list really is the top: its most probable entry must beat any single omitted
// option even if all the missing mass went to that option (maxListed > 1-sum),
// see rescalable. The floor is a further sanity check: a list carrying less
// than half the mass is not a top-k answer and is left for Validate to reject.
const truncatedMinSum = 0.5

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

// wireOutput keeps each answer raw so the ffa.prediction entry is decoded on its
// own (#2479): a wrongly typed field in it drops the prediction, never the
// decision.
type wireOutput struct {
	Answers   []json.RawMessage `json:"answers"`
	Rationale string            `json:"rationale"`
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
//   - choice probabilities summing to within 0.02 of 1 are rescaled to sum to 1;
//   - a truncated choice distribution (fewer entries than options, summing to
//     at least 0.5 but less than 1, whose top entry exceeds the missing mass
//     1-sum) is rescaled to sum to 1: the omitted options keep probability 0
//     and the listed ones keep their ranking.
//
// The ffa.prediction answer is removed before decide.Validate (#2479). With
// req.PredictionAsk set it is checked by decide.ValidatePrediction: a passing one
// becomes resp.Prediction (Source model), anything else sets
// resp.PredictionDropped; the decision is valid or invalid exactly as without
// it. Without the ask it is ignored.
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
	// Anything but whitespace after the top-level value (including a stray
	// "}" or "]", which dec.More would not report) is rejected.
	if len(bytes.TrimSpace(body[dec.InputOffset():])) != 0 {
		return decide.Response{}, fmt.Errorf("%w: trailing data after the JSON object", ErrMalformed)
	}
	if len(out.Answers) == 0 {
		return decide.Response{}, fmt.Errorf("%w: no answers", ErrMalformed)
	}
	if len(out.Answers) > MaxAnswers {
		return decide.Response{}, fmt.Errorf("%w: %d answers, limit %d", ErrMalformed, len(out.Answers), MaxAnswers)
	}

	questions := make(map[string]decide.Question, len(req.Questions))
	for _, q := range req.Questions {
		questions[q.ID] = q
	}

	resp := decide.Response{
		Backend:   backend,
		Rationale: SanitizeRationale(out.Rationale),
		Answers:   make([]decide.Answer, 0, len(out.Answers)),
	}
	// The prediction answer is split off BEFORE anything else looks at it
	// (#2479, UC-040 A45, BR-034): it is checked on its own below and can never
	// make the decision malformed or invalid.
	var predictions []json.RawMessage
	for i, raw := range out.Answers {
		var id struct {
			ID string `json:"id"`
		}
		if err := json.Unmarshal(raw, &id); err != nil {
			return decide.Response{}, fmt.Errorf("%w: answer %d: %v", ErrMalformed, i, clipErr(err))
		}
		if id.ID == decide.QuestionFFAPrediction {
			predictions = append(predictions, raw)
			continue
		}
		var wa wireAnswer
		if err := json.Unmarshal(raw, &wa); err != nil {
			return decide.Response{}, fmt.Errorf("%w: answer %d: %v", ErrMalformed, i, clipErr(err))
		}
		a, err := toAnswer(wa, questions[wa.ID])
		if err != nil {
			return decide.Response{}, fmt.Errorf("%w: answer %d: %v", ErrMalformed, i, err)
		}
		resp.Answers = append(resp.Answers, a)
	}
	if err := decide.Validate(req, resp); err != nil {
		return decide.Response{}, err
	}
	if req.PredictionAsk != nil {
		resp.Prediction, resp.PredictionDropped = splitPrediction(*req.PredictionAsk, predictions, resp)
	}
	return resp, nil
}

// splitPrediction checks the model's answers to the prediction ask against the
// validated decision in resp: exactly one answer, decodable, with a choice and
// a numeric confidence, passing decide.ValidatePrediction. Only the choice and the
// confidence are read. Without the ask (Parse's caller) a stray ffa.prediction
// answer is ignored.
func splitPrediction(ask decide.Question, answers []json.RawMessage, resp decide.Response) (*decide.Prediction, string) {
	switch {
	case len(answers) == 0:
		return nil, decide.PredictionDropMissing
	case len(answers) > 1:
		return nil, decide.PredictionDropInconsistent
	}
	var choice struct {
		Choice string `json:"choice"`
	}
	if err := json.Unmarshal(answers[0], &choice); err != nil {
		return nil, decide.PredictionDropBadOption
	}
	var conf struct {
		Confidence *float64 `json:"confidence"`
	}
	if err := json.Unmarshal(answers[0], &conf); err != nil || conf.Confidence == nil {
		return nil, decide.PredictionDropBadConfidence
	}
	iv, _ := resp.Find(decide.QuestionFFAIntervention)
	tgt, _ := resp.Find(decide.QuestionFFATarget)
	a := decide.Answer{QuestionID: decide.QuestionFFAPrediction, Choice: choice.Choice, Confidence: *conf.Confidence}
	return decide.ValidatePrediction(ask, a, iv.Choice, tgt.Choice)
}

func toAnswer(wa wireAnswer, q decide.Question) (decide.Answer, error) {
	if wa.Confidence == nil {
		return decide.Answer{}, errors.New("missing confidence")
	}
	a := decide.Answer{QuestionID: wa.ID, Confidence: *wa.Confidence}
	switch q.Kind {
	case decide.KindScore:
		if wa.Score == nil {
			return decide.Answer{}, errors.New("score question without a score")
		}
		a.Score = *wa.Score
	case decide.KindChoice:
		a.Choice = wa.Choice
		probs, err := toProbabilities(wa.Probabilities, len(q.Options))
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

// toProbabilities decodes one choice distribution; options is how many options
// the question has (to tell a truncated top-k list from a full one).
func toProbabilities(in []wireProbability, options int) (map[string]float64, error) {
	if len(in) > MaxProbabilities {
		return nil, fmt.Errorf("%d probabilities, limit %d", len(in), MaxProbabilities)
	}
	out := make(map[string]float64, len(in))
	var sum, maxListed float64
	for _, p := range in {
		if p.P == nil {
			return nil, errors.New("probability entry without p")
		}
		if _, dup := out[p.Option]; dup {
			return nil, fmt.Errorf("duplicate probability for option %q", clipRunes(p.Option, 64))
		}
		out[p.Option] = *p.P
		sum += *p.P
		maxListed = math.Max(maxListed, *p.P)
	}
	if rescalable(sum, maxListed, len(out) < options) {
		for k, v := range out {
			if v > 0 {
				out[k] = v / sum
			}
		}
	}
	return out, nil
}

// rescalable reports whether a distribution summing to sum is rescaled to 1:
// within renormalizeTolerance of 1 (rounding), or a truncated top-k list
// carrying at least truncatedMinSum of the mass whose most probable entry
// (maxListed) strictly beats any single omitted option, even one that took all
// of the missing mass 1-sum. Without that, rescaling could crown an option that
// is not the model's real argmax.
func rescalable(sum, maxListed float64, truncated bool) bool {
	if sum <= 0 || sum == 1 || math.IsInf(sum, 0) || math.IsNaN(sum) {
		return false
	}
	if math.Abs(sum-1) <= renormalizeTolerance {
		return true
	}
	return truncated && sum >= truncatedMinSum && sum < 1 && maxListed > 1-sum
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
