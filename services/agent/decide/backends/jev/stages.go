package jev

// stages.go splits one decide.Request into Jev's two-stage round (#1262).
//
// Jev answers every question of a call independently, so in one call
// ffa.target and ffa.magnitude cannot depend on the chosen ffa.intervention
// (their confidence came out near uniform). Jev is fast (~250 ms), so Decide
// asks in two calls, inside one ctx deadline:
//
//	stage "intervention": ffa.intervention plus every question that is not
//	                      ffa.target / ffa.magnitude (e.g. nouls).
//	stage "details":      ffa.target and ffa.magnitude CONDITIONED on the chosen
//	                      intervention: the instructions and state name it, the
//	                      target options are the alive players described by their
//	                      suitability for THAT intervention, and the magnitude
//	                      levels are that intervention's own values.
//
// The details call is skipped when nothing is left to ask: for noop (target
// none, magnitude at the low end of its range, the rules adapter's noop
// convention), for a session-scoped intervention (target none) without numeric
// bounds, and so on. Locally answered questions carry the same conventions as
// the rules adapter (decision/decide_rules.go): target none with certainty
// (confidence 1), magnitude at Range[0] with the intervention's confidence.
//
// A failed details call fails the whole Decide (the caller falls back to rules)
// rather than returning a half answer: the intervention alone is not
// actionable for a player-targeted intervention (decision.mapAnswers rejects a
// player intervention without a target), and for a session intervention it
// would apply an unconditioned default magnitude the model never chose.

import (
	"fmt"
	"strconv"

	"github.com/joustmania/agent/decide"
)

// Stage values of the jev.stage span attribute.
const (
	// StageIntervention is the first call: the intervention (and nouls).
	StageIntervention = "intervention"
	// StageDetails is the second call: target and magnitude conditioned on the
	// chosen intervention.
	StageDetails = "details"
	// StageSingle is a request without an ffa.intervention question: one call
	// with every question, as before #1262.
	StageSingle = "single"
)

// AttrStage is the chat-span attribute naming the stage of the call.
const AttrStage = "jev.stage"

// isDetail reports whether q belongs to the details stage.
func isDetail(q decide.Question) bool {
	return (q.ID == decide.QuestionFFATarget && q.Kind == decide.KindChoice) ||
		(q.ID == decide.QuestionFFAMagnitude && q.Kind == decide.KindScore)
}

// splitStages returns the first-stage request and the detail questions. With no
// choice-kind ffa.intervention question there is a single stage: first is req
// itself and details is empty.
func splitStages(req decide.Request) (first decide.Request, details []decide.Question, stage string) {
	hasIntervention := false
	for _, q := range req.Questions {
		if q.ID == decide.QuestionFFAIntervention && q.Kind == decide.KindChoice {
			hasIntervention = true
		}
	}
	if !hasIntervention {
		return req, nil, StageSingle
	}
	first = decide.Request{State: req.State}
	for _, q := range req.Questions {
		if isDetail(q) {
			details = append(details, q)
		} else {
			first.Questions = append(first.Questions, q)
		}
	}
	return first, details, StageIntervention
}

// detailsPlan is what the details stage asks Jev (ask) and what it answers
// locally (local), for the chosen intervention.
type detailsPlan struct {
	intervention string
	ask          decide.Request
	local        []decide.Answer
}

// planDetails decides which detail questions need Jev for the chosen
// intervention iv (with acting confidence ivConfidence).
func planDetails(st decide.State, iv decide.Answer, details []decide.Question) detailsPlan {
	p := detailsPlan{intervention: iv.Choice, ask: decide.Request{State: st}}
	acting := iv.Choice != decide.OptionNoop
	for _, q := range details {
		switch q.ID {
		case decide.QuestionFFATarget:
			alive := aliveOptions(q, st)
			if acting && decide.PlayerTargeted(iv.Choice) && len(alive) > 1 {
				p.ask.Questions = append(p.ask.Questions, decide.Question{
					ID: q.ID, Kind: decide.KindChoice, Text: targetInstructions(iv.Choice), Options: alive,
				})
				continue
			}
			p.local = append(p.local, localTarget(q, iv.Choice, alive))
		case decide.QuestionFFAMagnitude:
			if _, ok := decide.MagnitudeBounds(iv.Choice); acting && ok {
				p.ask.Questions = append(p.ask.Questions, decide.Question{
					ID: q.ID, Kind: decide.KindScore, Text: q.Text, Range: q.Range,
				})
				continue
			}
			p.local = append(p.local, decide.Answer{QuestionID: q.ID, Score: q.Range[0], Confidence: iv.Confidence})
		}
	}
	return p
}

// aliveOptions are q's options that are alive serials of st, in option order.
func aliveOptions(q decide.Question, st decide.State) []string {
	alive := map[string]bool{}
	for _, s := range st.AliveSerials() {
		alive[s] = true
	}
	var out []string
	for _, o := range q.Options {
		if alive[o] {
			out = append(out, o)
		}
	}
	return out
}

// localTarget answers ffa.target without Jev: none for noop and session
// interventions; the only alive player for a player intervention with one
// candidate; none at confidence 0 when a player intervention has no candidate
// (decision.mapAnswers then rejects the round as invalid, which is right).
// Every option is covered by the distribution.
func localTarget(q decide.Question, iv string, alive []string) decide.Answer {
	choice, confidence := decide.TargetNone, 1.0
	if iv != decide.OptionNoop && decide.PlayerTargeted(iv) {
		if len(alive) == 1 {
			choice = alive[0]
		} else {
			confidence = 0
		}
	}
	if !contains(q.Options, choice) {
		// A caller-built target question without "none": nothing sensible to
		// pick; Validate rejects the round.
		choice = ""
	}
	probs := make(map[string]float64, len(q.Options))
	for _, o := range q.Options {
		probs[o] = 0
	}
	if choice != "" {
		probs[choice] = 1
	}
	return decide.Answer{QuestionID: q.ID, Choice: choice, Probabilities: probs, Confidence: confidence}
}

// mergeTarget maps the details-stage target answer (over the alive serials)
// back onto the original question, giving every other option (none) an
// explicit 0 so the distribution covers all options.
func mergeTarget(q decide.Question, a decide.Answer) decide.Answer {
	probs := make(map[string]float64, len(q.Options))
	for _, o := range q.Options {
		probs[o] = a.Probabilities[o]
	}
	a.Probabilities = probs
	return a
}

// mergeAnswers assembles the final Response in req's question order from the
// two stages' answers and the local ones.
func mergeAnswers(req decide.Request, parts ...[]decide.Answer) (decide.Response, error) {
	byID := map[string]decide.Answer{}
	for _, part := range parts {
		for _, a := range part {
			byID[a.QuestionID] = a
		}
	}
	resp := decide.Response{Backend: Name, Answers: make([]decide.Answer, 0, len(req.Questions))}
	for _, q := range req.Questions {
		a, ok := byID[q.ID]
		if !ok {
			return decide.Response{}, fmt.Errorf("jev: %w: question %q not answered", decide.ErrInvalid, q.ID)
		}
		if q.ID == decide.QuestionFFATarget && q.Kind == decide.KindChoice && a.Choice != "" {
			a = mergeTarget(q, a)
		}
		resp.Answers = append(resp.Answers, a)
	}
	if err := decide.Validate(req, resp); err != nil {
		return decide.Response{}, fmt.Errorf("jev: %w", err)
	}
	return resp, nil
}

// rationale is a one-line, model-free summary of the round for
// Decision.Reason, e.g. "jev: grant_shield (p=0.62) on MOCK0003 (p=0.81)".
func rationale(resp decide.Response) string {
	iv, ok := resp.Find(decide.QuestionFFAIntervention)
	if !ok || iv.Choice == "" {
		return ""
	}
	s := "jev: " + iv.Choice + " (p=" + strconv.FormatFloat(iv.Probabilities[iv.Choice], 'f', 2, 64) + ")"
	if t, ok := resp.Find(decide.QuestionFFATarget); ok && t.Choice != "" && t.Choice != decide.TargetNone {
		s += " on " + t.Choice + " (p=" + strconv.FormatFloat(t.Probabilities[t.Choice], 'f', 2, 64) + ")"
	}
	return s
}

func contains(ss []string, s string) bool {
	for _, x := range ss {
		if x == s {
			return true
		}
	}
	return false
}
