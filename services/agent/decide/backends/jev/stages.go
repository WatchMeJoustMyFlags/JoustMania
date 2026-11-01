package jev

// stages.go splits one decide.Request into Jev's two-stage round (#1262).
//
// Jev answers every question of a call independently, so in one call
// ffa.target and ffa.magnitude cannot depend on the chosen ffa.intervention
// (their confidence came out near uniform). Jev is fast (~250 ms), so Decide
// asks in two calls, inside one ctx deadline:
//
//	stage "intervention": the gate noul ("Does the game need an intervention
//	                      right now?"), ffa.intervention WITHOUT noop, plus every
//	                      question that is not ffa.target / ffa.magnitude (e.g.
//	                      nouls). See "The gate" below (#1281).
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
// The gate (#1281). Asked "which intervention, noop included", Jev spreads the
// acting mass over several interventions, so noop wins the argmax even when
// most of the mass says "act" (near the death threshold: p(noop) 0.67, the
// rest split). Stage 1 therefore asks two independent questions in one call:
// the gate noul (whether to act) and ffa.intervention without noop (what to
// do if acting). The ffa.intervention answer Decide returns is SYNTHESISED
// from them (gateAnswer):
//
//	{noop: 1 - p_yes, argmax_iv: p_yes, every other option: 0}
//
// so the acting confidence 1 - p(noop) equals p_yes, gateAnswer picks the
// intervention iff p_yes > 0.5 (its own strict >; decide.Validate's argmax
// check merely accepts that choice, within its 1e-3 tolerance), and every
// option is covered. It is deliberately NOT the factorised p_yes * q_i, which would
// split the acting mass again. Jev's real answers (p_yes and the distribution
// over the interventions) are recorded on the stage-1 chat span
// (jev.gate.p_yes, jev.stage1.choice, jev.stage1.distribution). The gate
// applies only when ffa.intervention offers noop and at least one other
// option; otherwise stage 1 asks the question as is.
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

// stagePlan is how Decide splits one request into stages.
type stagePlan struct {
	// first is the stage-1 (or single-stage) request sent to Jev.
	first decide.Request
	// details are the detail questions of the original request.
	details []decide.Question
	// stage is StageIntervention or StageSingle.
	stage string
	// gated is the original ffa.intervention question when stage 1 asks the
	// gate noul and ffa.intervention without noop instead of it; nil when the
	// question is asked as is.
	gated *decide.Question
}

// splitStages returns the stage plan of req. With no choice-kind
// ffa.intervention question there is a single stage: first is req itself.
func splitStages(req decide.Request) stagePlan {
	hasIntervention := false
	for _, q := range req.Questions {
		if q.ID == decide.QuestionFFAIntervention && q.Kind == decide.KindChoice {
			hasIntervention = true
		}
	}
	if !hasIntervention {
		return stagePlan{first: req, stage: StageSingle}
	}
	p := stagePlan{first: decide.Request{State: req.State, PromptProfile: req.PromptProfile, Goal: req.Goal}, stage: StageIntervention}
	for _, q := range req.Questions {
		switch {
		case isDetail(q):
			p.details = append(p.details, q)
		case q.ID == decide.QuestionFFAIntervention && gateable(q):
			orig := q
			p.gated = &orig
			p.first.Questions = append(p.first.Questions, gateQuestion(), withoutNoop(q))
		default:
			p.first.Questions = append(p.first.Questions, q)
		}
	}
	return p
}

// GateQuestionID is the internal id of the gate noul. It never leaves the
// backend: Decide answers the caller's ffa.intervention from it.
const GateQuestionID = "jev.gate"

// gateable reports whether ffa.intervention q can be gated: it offers noop and
// at least one intervention.
func gateable(q decide.Question) bool {
	return contains(q.Options, decide.OptionNoop) && len(q.Options) > 1
}

// gateQuestion is the stage-1 gate noul (criteria: criteria.go gateCriteria).
func gateQuestion() decide.Question {
	return decide.Question{ID: GateQuestionID, Kind: decide.KindNoul, Text: gateInstructions}
}

// withoutNoop is ffa.intervention q without the noop option, asked as "which
// intervention, if the agent acts now".
func withoutNoop(q decide.Question) decide.Question {
	out := decide.Question{ID: q.ID, Kind: q.Kind, Text: gatedInterventionInstructions}
	for _, o := range q.Options {
		if o != decide.OptionNoop {
			out.Options = append(out.Options, o)
		}
	}
	return out
}

// gateOpen is the p_yes above which the synthesised choice is the
// intervention. gateAnswer decides with a strict > (at exactly 0.5 the round
// is noop); decide.Validate only accepts the result (the choice is the argmax
// of {noop: 1-p, iv: p} within its 1e-3 tolerance), it does not decide it.
const gateOpen = 0.5

// gateAnswer synthesises the caller's ffa.intervention answer from the gate's
// p_yes and Jev's stage-1 answer over the interventions (iv):
// {noop: 1 - p_yes, iv.Choice: p_yes, every other option of orig: 0}. The
// choice is iv.Choice iff p_yes > 0.5, else noop; the confidence is the
// acting confidence (p_yes for an intervention, 1 - p_yes for noop).
func gateAnswer(orig decide.Question, pYes float64, iv decide.Answer) decide.Answer {
	probs := make(map[string]float64, len(orig.Options))
	for _, o := range orig.Options {
		probs[o] = 0
	}
	probs[decide.OptionNoop] = 1 - pYes
	probs[iv.Choice] = pYes
	choice := decide.OptionNoop
	if pYes > gateOpen {
		choice = iv.Choice
	}
	return decide.Answer{
		QuestionID:    orig.ID,
		Choice:        choice,
		Probabilities: probs,
		Confidence:    actingConfidence(choice, probs),
	}
}

// resolveGate replaces stage 1's gate and gated ffa.intervention answers with
// the synthesised ffa.intervention answer. It returns the answers to merge and
// Jev's real stage-1 intervention answer (zero when not gated).
func (p stagePlan) resolveGate(answers []decide.Answer) ([]decide.Answer, decide.Answer, error) {
	if p.gated == nil {
		return answers, decide.Answer{}, nil
	}
	var gate, iv *decide.Answer
	out := make([]decide.Answer, 0, len(answers))
	for i := range answers {
		switch answers[i].QuestionID {
		case GateQuestionID:
			gate = &answers[i]
		case decide.QuestionFFAIntervention:
			iv = &answers[i]
		default:
			out = append(out, answers[i])
		}
	}
	if gate == nil || gate.Noul == nil || iv == nil {
		return nil, decide.Answer{}, fmt.Errorf("jev: %w: gate or intervention not answered", decide.ErrInvalid)
	}
	return append(out, gateAnswer(*p.gated, *gate.Noul, *iv)), *iv, nil
}

// detailsPlan is what the details stage asks Jev (ask) and what it answers
// locally (local), for the chosen intervention.
type detailsPlan struct {
	intervention string
	ask          decide.Request
	local        []decide.Answer
	// magnitude is the original ffa.magnitude question when the intervention
	// is a two-way lever: its answer is picked from the strength question of
	// the chosen target's direction (resolveTwoWay).
	magnitude *decide.Question
}

// planDetails decides which detail questions need Jev for the chosen
// intervention iv (with acting confidence iv.Confidence). req is the caller's
// request: its state, its prompt profile (#2467) and its goal lines (#2498) are
// carried into the details request.
func planDetails(req decide.Request, iv decide.Answer, details []decide.Question) detailsPlan {
	st, profile := req.State, req.PromptProfile
	p := detailsPlan{intervention: iv.Choice, ask: decide.Request{State: st, PromptProfile: profile, Goal: req.Goal}}
	acting := iv.Choice != decide.OptionNoop
	playerScoped := acting && decide.PlayerTargeted(iv.Choice)
	groupScoped := acting && decide.GroupTargeted(iv.Choice)
	var alive, group []string
	for _, q := range details {
		if q.ID == decide.QuestionFFATarget {
			alive = aliveOptions(q, st)
			if decide.NeedsFreshReadings(iv.Choice) {
				alive = freshOptions(alive, st)
			}
			group = groupOptions(q)
		}
	}
	// A player intervention without a candidate cannot be applied: answer
	// everything locally (target none at confidence 0, which the decision hook
	// rejects as player_intervention_without_target) without calling Jev.
	noCandidate := playerScoped && len(alive) == 0
	for _, q := range details {
		switch q.ID {
		case decide.QuestionFFATarget:
			// A group lever (#2467, UC-040 A47): the target options are the group
			// options, each described by the players it covers. One option is
			// answered locally; none answers "none" at confidence 0, which the
			// decision hook rejects (group_intervention_without_group).
			if groupScoped {
				if len(group) > 1 {
					p.ask.Questions = append(p.ask.Questions, decide.Question{
						ID: q.ID, Kind: decide.KindChoice, Text: groupTargetInstructions(iv.Choice, profile), Options: group,
					})
					continue
				}
				p.local = append(p.local, localGroupTarget(q, group))
				continue
			}
			if playerScoped && len(alive) > 1 {
				p.ask.Questions = append(p.ask.Questions, decide.Question{
					ID: q.ID, Kind: decide.KindChoice, Text: targetInstructions(iv.Choice, profile), Options: alive,
				})
				continue
			}
			p.local = append(p.local, localTarget(q, iv.Choice, alive))
		case decide.QuestionFFAMagnitude:
			_, hasBounds := decide.MagnitudeBounds(iv.Choice)
			switch {
			case !acting || !hasBounds || noCandidate:
				p.local = append(p.local, decide.Answer{QuestionID: q.ID, Score: q.Range[0], Confidence: iv.Confidence})
			case isTwoWay(iv.Choice):
				mq := q
				p.magnitude = &mq
				dirs := []string{directionHelp, directionReinIn}
				if len(alive) == 1 {
					dirs = []string{direction(st, alive[0])}
				}
				for _, dir := range dirs {
					p.ask.Questions = append(p.ask.Questions, decide.Question{
						ID: strengthQuestionID(dir), Kind: decide.KindScore, Text: q.Text, Range: q.Range,
					})
				}
			default:
				p.ask.Questions = append(p.ask.Questions, decide.Question{
					ID: q.ID, Kind: decide.KindScore, Text: q.Text, Range: q.Range,
				})
			}
		}
	}
	return p
}

// resolveTwoWay turns a two-way lever's strength answers into the ffa.magnitude
// answer: the one on the side direction() picks for the chosen target. The
// other side's answer is dropped. answers are the details and local answers.
func (p detailsPlan) resolveTwoWay(st decide.State, answers []decide.Answer) ([]decide.Answer, error) {
	if p.magnitude == nil {
		return answers, nil
	}
	target := ""
	for _, a := range answers {
		if a.QuestionID == decide.QuestionFFATarget {
			target = a.Choice
		}
	}
	want := strengthQuestionID(direction(st, target))
	out := make([]decide.Answer, 0, len(answers))
	found := false
	for _, a := range answers {
		switch {
		case a.QuestionID == want:
			a.QuestionID = p.magnitude.ID
			out = append(out, a)
			found = true
		case strengthDirection(a.QuestionID) != "":
			// the other side: not applied
		default:
			out = append(out, a)
		}
	}
	if !found {
		return nil, fmt.Errorf("jev: %w: no %s answer for target %q", decide.ErrInvalid, want, target)
	}
	return out, nil
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

// groupOptions are q's group options (decide.IsGroupTarget), in option order.
func groupOptions(q decide.Question) []string {
	var out []string
	for _, o := range q.Options {
		if decide.IsGroupTarget(o) {
			out = append(out, o)
		}
	}
	return out
}

// localGroupTarget answers ffa.target of a group lever without Jev: its only group
// option at confidence 1, or none at confidence 0 when there is none.
func localGroupTarget(q decide.Question, group []string) decide.Answer {
	choice, confidence := decide.TargetNone, 0.0
	if len(group) == 1 {
		choice, confidence = group[0], 1.0
	}
	if !contains(q.Options, choice) {
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
// Decision.Reason, e.g. "jev: grant_shield (p=0.62) on MOCK0003 (p=0.81)". A
// gated round (stage1 is Jev's real answer over the interventions) shows the
// gate's p_yes and Jev's own pick instead, e.g.
// "jev: grant_shield (gate p_yes=0.82; grant_shield p=0.41 among interventions) on MOCK0003 (p=0.99)"
// or "jev: noop (gate p_yes=0.30; grant_shield p=0.41 among interventions)".
func rationale(resp decide.Response, stage1 decide.Answer) string {
	iv, ok := resp.Find(decide.QuestionFFAIntervention)
	if !ok || iv.Choice == "" {
		return ""
	}
	s := "jev: " + iv.Choice + " (p=" + p2(iv.Probabilities[iv.Choice]) + ")"
	if stage1.Choice != "" {
		pYes := 1 - iv.Probabilities[decide.OptionNoop]
		s = "jev: " + iv.Choice + " (gate p_yes=" + p2(pYes) + "; " + stage1.Choice + " p=" +
			p2(stage1.Probabilities[stage1.Choice]) + " among interventions)"
	}
	if t, ok := resp.Find(decide.QuestionFFATarget); ok && t.Choice != "" && t.Choice != decide.TargetNone {
		s += " on " + t.Choice + " (p=" + p2(t.Probabilities[t.Choice]) + ")"
	}
	return s
}

func p2(v float64) string { return strconv.FormatFloat(v, 'f', 2, 64) }

func contains(ss []string, s string) bool {
	for _, x := range ss {
		if x == s {
			return true
		}
	}
	return false
}
