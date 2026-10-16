package decide

import (
	"errors"
	"fmt"
	"math"
	"strconv"
)

// ProbabilityTolerance is how far a choice answer's probabilities may sum from
// 1. It is loose enough for a remote model that rounds to three decimals in
// JSON, and tight enough to reject a distribution that is plainly wrong.
const ProbabilityTolerance = 1e-3

// ErrInvalid wraps every Validate failure, so a caller can tell an invalid
// answer (fall back to rules, fallback_reason=invalid_answer) apart from a
// transport error.
var ErrInvalid = errors.New("decide: invalid response")

// Validate checks resp against req. Backends are untrusted, so every Response is
// validated before anything acts on it:
//   - the questions themselves are well formed (unique IDs, known kinds, a
//     choice has unique non-empty options, a score range is finite and ordered);
//   - every question is answered exactly once and no unknown question is;
//   - every number (confidence, score, probabilities) is finite (no NaN/Inf);
//   - every confidence is in [0,1];
//   - a choice answer picks one of the options and carries a probability
//     distribution over the options (each >= 0, summing to 1 within
//     ProbabilityTolerance) in which the choice is the argmax: p(choice) > 0
//     and p(choice) >= max(p) - ProbabilityTolerance. Its Score must be 0.
//     Confidence is NOT tied to p(choice): it is the backend's confidence that
//     acting on the answer is right (see Answer.Confidence);
//   - a score answer is finite, within the inclusive range, and carries no
//     choice or probabilities;
//   - a noul answer carries a finite Noul in [0,1] and no choice, probabilities
//     or score; only noul answers may carry a Noul.
//
// The returned error wraps ErrInvalid and names the first offending question.
// Backend-supplied strings in the message are truncated (see clip), so a
// hostile model cannot blow up a log line or span attribute.
func Validate(req Request, resp Response) error {
	questions := make(map[string]Question, len(req.Questions))
	for _, q := range req.Questions {
		if err := validateQuestion(q); err != nil {
			return invalid("question %s: %v", clip(q.ID), err)
		}
		if _, dup := questions[q.ID]; dup {
			return invalid("question %s: duplicate id", clip(q.ID))
		}
		questions[q.ID] = q
	}

	answered := make(map[string]bool, len(resp.Answers))
	for _, a := range resp.Answers {
		q, ok := questions[a.QuestionID]
		if !ok {
			return invalid("answer to unknown question %s", clip(a.QuestionID))
		}
		if answered[a.QuestionID] {
			return invalid("question %s answered more than once", clip(a.QuestionID))
		}
		answered[a.QuestionID] = true
		if err := validateAnswer(q, a); err != nil {
			return invalid("question %s: %v", clip(q.ID), err)
		}
	}
	for _, q := range req.Questions {
		if !answered[q.ID] {
			return invalid("question %s not answered", clip(q.ID))
		}
	}
	return nil
}

func invalid(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrInvalid, fmt.Sprintf(format, args...))
}

// maxErrString caps how much of a backend-supplied string an error message
// quotes.
const maxErrString = 64

// clip quotes s for an error message, truncated to maxErrString runes.
func clip(s string) string {
	r := []rune(s)
	if len(r) > maxErrString {
		return strconv.Quote(string(r[:maxErrString])) + "..."
	}
	return strconv.Quote(s)
}

func validateQuestion(q Question) error {
	if q.ID == "" {
		return errors.New("empty id")
	}
	switch q.Kind {
	case KindChoice:
		if len(q.Options) == 0 {
			return errors.New("choice without options")
		}
		seen := make(map[string]bool, len(q.Options))
		for _, o := range q.Options {
			if o == "" {
				return errors.New("empty option")
			}
			if seen[o] {
				return fmt.Errorf("duplicate option %s", clip(o))
			}
			seen[o] = true
		}
	case KindScore:
		lo, hi := q.Range[0], q.Range[1]
		if !finite(lo) || !finite(hi) || lo > hi {
			return fmt.Errorf("bad range [%v, %v]", lo, hi)
		}
	case KindNoul:
		// Text only.
	default:
		return fmt.Errorf("unknown kind %s", clip(string(q.Kind)))
	}
	return nil
}

func validateAnswer(q Question, a Answer) error {
	if !finite(a.Confidence) || a.Confidence < 0 || a.Confidence > 1 {
		return fmt.Errorf("confidence %v outside [0,1]", a.Confidence)
	}
	if q.Kind != KindNoul && a.Noul != nil {
		return errors.New("non-noul answer carries a noul")
	}
	switch q.Kind {
	case KindChoice:
		return validateChoice(q, a)
	case KindNoul:
		if a.Choice != "" || len(a.Probabilities) != 0 || a.Score != 0 {
			return errors.New("noul answer carries a choice, probabilities or a score")
		}
		if a.Noul == nil {
			return errors.New("noul answer without a noul")
		}
		if p := *a.Noul; !finite(p) || p < 0 || p > 1 {
			return fmt.Errorf("noul %v outside [0,1]", p)
		}
	case KindScore:
		if a.Choice != "" || len(a.Probabilities) != 0 {
			return errors.New("score answer carries a choice or probabilities")
		}
		if !finite(a.Score) || a.Score < q.Range[0] || a.Score > q.Range[1] {
			return fmt.Errorf("score %v outside [%v, %v]", a.Score, q.Range[0], q.Range[1])
		}
	}
	return nil
}

func validateChoice(q Question, a Answer) error {
	if a.Score != 0 {
		return fmt.Errorf("choice answer carries a score (%v)", a.Score)
	}
	options := make(map[string]bool, len(q.Options))
	for _, o := range q.Options {
		options[o] = true
	}
	if !options[a.Choice] {
		return fmt.Errorf("choice %s not among the options", clip(a.Choice))
	}
	if len(a.Probabilities) == 0 {
		return errors.New("choice answer without probabilities")
	}
	var sum, maxP float64
	for o, p := range a.Probabilities {
		if !options[o] {
			return fmt.Errorf("probability for unknown option %s", clip(o))
		}
		if !finite(p) || p < 0 {
			return fmt.Errorf("probability %v for %s is not a finite non-negative number", p, clip(o))
		}
		sum += p
		maxP = math.Max(maxP, p)
	}
	if math.Abs(sum-1) > ProbabilityTolerance {
		return fmt.Errorf("probabilities sum to %v, want 1", sum)
	}
	pc := a.Probabilities[a.Choice]
	if pc <= 0 {
		return fmt.Errorf("choice %s has probability 0", clip(a.Choice))
	}
	if pc < maxP-ProbabilityTolerance {
		return fmt.Errorf("choice %s (p=%v) is not the most probable option (p=%v)", clip(a.Choice), pc, maxP)
	}
	return nil
}

func finite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }
