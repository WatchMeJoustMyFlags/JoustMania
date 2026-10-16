package decide

import (
	"errors"
	"fmt"
	"math"
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
//   - every confidence is finite and in [0,1];
//   - a choice answer picks one of the options and carries a probability
//     distribution over the options (each finite and >= 0, summing to 1 within
//     ProbabilityTolerance);
//   - a score answer is finite, within the inclusive range, and carries no
//     choice or probabilities.
//
// The returned error wraps ErrInvalid and names the first offending question.
func Validate(req Request, resp Response) error {
	questions := make(map[string]Question, len(req.Questions))
	for _, q := range req.Questions {
		if err := validateQuestion(q); err != nil {
			return invalid("question %q: %v", q.ID, err)
		}
		if _, dup := questions[q.ID]; dup {
			return invalid("question %q: duplicate id", q.ID)
		}
		questions[q.ID] = q
	}

	answered := make(map[string]bool, len(resp.Answers))
	for _, a := range resp.Answers {
		q, ok := questions[a.QuestionID]
		if !ok {
			return invalid("answer to unknown question %q", a.QuestionID)
		}
		if answered[a.QuestionID] {
			return invalid("question %q answered more than once", a.QuestionID)
		}
		answered[a.QuestionID] = true
		if err := validateAnswer(q, a); err != nil {
			return invalid("question %q: %v", q.ID, err)
		}
	}
	for _, q := range req.Questions {
		if !answered[q.ID] {
			return invalid("question %q not answered", q.ID)
		}
	}
	return nil
}

func invalid(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrInvalid, fmt.Sprintf(format, args...))
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
				return fmt.Errorf("duplicate option %q", o)
			}
			seen[o] = true
		}
	case KindScore:
		lo, hi := q.Range[0], q.Range[1]
		if !finite(lo) || !finite(hi) || lo > hi {
			return fmt.Errorf("bad range [%v, %v]", lo, hi)
		}
	default:
		return fmt.Errorf("unknown kind %q", q.Kind)
	}
	return nil
}

func validateAnswer(q Question, a Answer) error {
	if !finite(a.Confidence) || a.Confidence < 0 || a.Confidence > 1 {
		return fmt.Errorf("confidence %v outside [0,1]", a.Confidence)
	}
	switch q.Kind {
	case KindChoice:
		return validateChoice(q, a)
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
	options := make(map[string]bool, len(q.Options))
	for _, o := range q.Options {
		options[o] = true
	}
	if !options[a.Choice] {
		return fmt.Errorf("choice %q not among the options", a.Choice)
	}
	if len(a.Probabilities) == 0 {
		return errors.New("choice answer without probabilities")
	}
	var sum float64
	for o, p := range a.Probabilities {
		if !options[o] {
			return fmt.Errorf("probability for unknown option %q", o)
		}
		if !finite(p) || p < 0 {
			return fmt.Errorf("probability %v for %q is not a finite non-negative number", p, o)
		}
		sum += p
	}
	if math.Abs(sum-1) > ProbabilityTolerance {
		return fmt.Errorf("probabilities sum to %v, want 1", sum)
	}
	return nil
}

func finite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }
