package decision

import (
	"context"
	"math"
	"strconv"

	"github.com/joustmania/agent/decide"
)

// decide_rules.go is the `rules` decision backend of epic #1225: an adapter that
// answers the decide contract's FFA questions from the ObjectiveRules engine. It
// is the default backend and the floor every other backend falls back to, so it
// never returns an error.
//
// It lives in package decision (not in decide or a decide/rules subpackage)
// because it needs the engine's unexported candidate scores, and because PR B's
// decide() — in this package — must call it: a decide/rules package importing
// decision would make that a cycle. decide stays a leaf contract package.
//
// Probabilities are a softmax over the ranked candidate scores (score =
// urgency x objective weight x fitness amplification, see scoreAndSort), with
// noop competing at minScore: a candidate below minScore is dropped by the
// engine, so noop is exactly "the score the engine needs before it acts".
//
// Confidence follows the contract's "confidence that ACTING is right" (see the
// decide package doc): for ffa.intervention it is 1 - p(noop) when the choice is
// an intervention and p(noop) when it is noop; for ffa.target it is p(chosen
// target); for ffa.magnitude it is the intervention's. A top-1 margin was used
// first and rejected: two strong competing interventions split the mass and the
// rules declined to act although either one clearly beat doing nothing.

// RulesBackendName is the rules backend's Name() and its decision.backend flag
// value.
const RulesBackendName = "rules"

// rulesSoftmaxTemperature scales candidate scores before the softmax. It is set
// to minScore so one minScore of score difference is one nat: a candidate 0.1
// above noop gets ~0.73 vs ~0.27 (confidence ~0.73), one 0.5 above gets ~0.99.
// With T=1 the [0,~2] score range would collapse every distribution towards
// uniform and the rules engine could never clear a confidence threshold.
const rulesSoftmaxTemperature = minScore

// RulesBackend adapts ObjectiveRules to decide.Backend.
type RulesBackend struct {
	engine *ObjectiveRules
}

// NewRulesBackend wraps engine. Tests inject a seeded engine (newObjectiveRules
// with a fixed rng and clock) to keep the chaos rule deterministic.
func NewRulesBackend(engine *ObjectiveRules) *RulesBackend {
	return &RulesBackend{engine: engine}
}

// Name implements decide.Backend.
func (b *RulesBackend) Name() string { return RulesBackendName }

// Decide implements decide.Backend. It ranks the candidates for req.Context
// exactly as Evaluate does (same rules, policy filters, scoring and ordering)
// and answers with the best candidate whose intervention and target are among
// the offered options — Evaluate's first decision whenever the allow-list
// offers it. Unlike Evaluate it is not throttled by evalInterval, answers one
// decision rather than up to maxDecisionsPerEval, and records no engine state
// (lastEval, lastFitness, lastDifficultyAt): it decides, it does not act. It
// does draw the chaos roll from the engine's rng.
//
// It never returns an error. A missing Context (or engine) yields noop with
// confidence 0; a question it does not know gets a neutral answer with
// confidence 0, so the Response always validates against a valid Request.
func (b *RulesBackend) Decide(_ context.Context, req decide.Request) (decide.Response, error) {
	var ranked []scoredCandidate
	known := req.Context != nil && b.engine != nil
	if known {
		b.engine.mu.Lock()
		// Same no-shield set as EvaluateDecide: the ledger's charged, capped and
		// cooling-down players, plus the charges the request's state shows.
		_, noShield := b.engine.shieldLocked()
		for serial := range chargedFromState(req.State) {
			if noShield == nil {
				noShield = map[string]bool{}
			}
			noShield[serial] = true
		}
		ranked, _, _ = b.engine.rankLocked(*req.Context, b.engine.now(), noShield)
		b.engine.mu.Unlock()
	}
	return answerFromRanked(ranked, req, known), nil
}

// chargedFromState is the shield-charge snapshot the request's state was built
// with (#1870): the rules backend ranks on the same view the state shows.
func chargedFromState(st decide.State) map[string]bool {
	var out map[string]bool
	for _, p := range st.Players {
		if p.ShieldCharged {
			if out == nil {
				out = map[string]bool{}
			}
			out[p.Serial] = true
		}
	}
	return out
}

// answerFromRanked answers req's questions from an already-ranked candidate list.
// It is shared by RulesBackend.Decide and ObjectiveRules.EvaluateDecide, so the
// decide answers and the emitted decisions can come from ONE ranking (#1226).
// known=false (no context or engine) zeroes every confidence.
func answerFromRanked(ranked []scoredCandidate, req decide.Request, known bool) decide.Response {
	qs := indexQuestions(req.Questions)
	eligible := filterEligible(ranked, qs)

	resp := decide.Response{Backend: RulesBackendName}
	iv := answerIntervention(qs[decide.QuestionFFAIntervention], eligible)
	for _, q := range req.Questions {
		var a decide.Answer
		switch {
		case q.ID == decide.QuestionFFAIntervention && iv.ok:
			a = iv.answer
		case q.ID == decide.QuestionFFATarget && iv.ok && q.Kind == decide.KindChoice:
			a = answerTarget(q, iv, eligible)
		case q.ID == decide.QuestionFFAMagnitude && iv.ok && q.Kind == decide.KindScore:
			a = answerMagnitude(q, iv)
		default:
			a = neutralAnswer(q)
		}
		if !known {
			a.Confidence = 0
		}
		resp.Answers = append(resp.Answers, a)
	}
	return resp
}

// interventionPick is the adapter's answer to ffa.intervention plus the
// candidate it came from (nil for noop).
type interventionPick struct {
	ok     bool
	answer decide.Answer
	best   *scoredCandidate
}

func indexQuestions(qs []decide.Question) map[string]decide.Question {
	out := make(map[string]decide.Question, len(qs))
	for _, q := range qs {
		out[q.ID] = q
	}
	return out
}

// filterEligible keeps the ranked candidates the questions can express: the
// intervention is an ffa.intervention option and the target (TargetNone for a
// session-scoped candidate) is an ffa.target option. Order is preserved.
func filterEligible(ranked []scoredCandidate, qs map[string]decide.Question) []scoredCandidate {
	ivOpts := optionSet(qs[decide.QuestionFFAIntervention])
	tgtQ, hasTarget := qs[decide.QuestionFFATarget]
	tgtOpts := optionSet(tgtQ)
	var out []scoredCandidate
	for _, sc := range ranked {
		if !ivOpts[sc.decision.Intervention] {
			continue
		}
		if hasTarget && !tgtOpts[targetOption(sc.decision.TargetSerial)] {
			continue
		}
		out = append(out, sc)
	}
	return out
}

// answerIntervention softmaxes the best score per intervention against noop at
// minScore. ok is false when the question is missing, not a choice, or lacks the
// noop option (then the neutral answer applies).
func answerIntervention(q decide.Question, eligible []scoredCandidate) interventionPick {
	if q.Kind != decide.KindChoice || !optionSet(q)[decide.OptionNoop] {
		return interventionPick{}
	}
	logits := map[string]float64{decide.OptionNoop: minScore}
	for _, sc := range eligible {
		if _, seen := logits[sc.decision.Intervention]; !seen {
			logits[sc.decision.Intervention] = sc.score // ranked: first is best
		}
	}
	probs := softmax(q.Options, logits)
	pick := interventionPick{ok: true}
	choice := decide.OptionNoop
	if len(eligible) > 0 {
		pick.best = &eligible[0]
		choice = eligible[0].decision.Intervention
	}
	confidence := probs[decide.OptionNoop]
	if choice != decide.OptionNoop {
		confidence = 1 - probs[decide.OptionNoop]
	}
	pick.answer = decide.Answer{
		QuestionID:    q.ID,
		Choice:        choice,
		Probabilities: probs,
		Confidence:    clamp01(confidence),
	}
	return pick
}

// answerTarget is the target distribution CONDITIONED on the chosen
// intervention: the best score per target among that intervention's candidates.
// noop targets TargetNone with certainty.
func answerTarget(q decide.Question, iv interventionPick, eligible []scoredCandidate) decide.Answer {
	logits := map[string]float64{}
	choice := decide.TargetNone
	if iv.best != nil {
		choice = targetOption(iv.best.decision.TargetSerial)
		for _, sc := range eligible {
			if sc.decision.Intervention != iv.best.decision.Intervention {
				continue
			}
			t := targetOption(sc.decision.TargetSerial)
			if _, seen := logits[t]; !seen {
				logits[t] = sc.score
			}
		}
	} else {
		logits[decide.TargetNone] = 0
	}
	if !optionSet(q)[choice] {
		return neutralAnswer(q) // a caller-built target question without TargetNone
	}
	probs := softmax(q.Options, logits)
	return decide.Answer{QuestionID: q.ID, Choice: choice, Probabilities: probs, Confidence: probs[choice]}
}

// answerMagnitude reports the magnitude the rules decision would be applied
// with: its numeric Value, or the action sink's default when Value is empty (the
// rules engine always leaves it empty today), normalized over the bounds. It is
// 0 for noop and bounds-less interventions. The rules engine has no separate
// view on magnitude, so the confidence is the intervention's.
func answerMagnitude(q decide.Question, iv interventionPick) decide.Answer {
	a := decide.Answer{QuestionID: q.ID, Score: q.Range[0], Confidence: iv.answer.Confidence}
	if iv.best == nil {
		return a
	}
	name := iv.best.decision.Intervention
	bounds, ok := decide.MagnitudeBounds(name)
	if !ok {
		return a
	}
	value := bounds.Default
	if v, err := strconv.ParseFloat(iv.best.decision.Value, 64); err == nil {
		value = v
	}
	norm, _ := decide.NormalizeMagnitude(name, value)
	a.Score = q.Range[0] + norm*(q.Range[1]-q.Range[0])
	return a
}

// neutralAnswer answers a question the adapter has no opinion on: the first
// option with a uniform distribution, the low end of a score range, or an even
// 0.5 for a noul, at confidence 0. Unknown kinds get an empty answer (Validate rejects the Request
// itself in that case).
func neutralAnswer(q decide.Question) decide.Answer {
	a := decide.Answer{QuestionID: q.ID}
	switch q.Kind {
	case decide.KindChoice:
		if len(q.Options) == 0 {
			return a
		}
		a.Choice = q.Options[0]
		a.Probabilities = make(map[string]float64, len(q.Options))
		for _, o := range q.Options {
			a.Probabilities[o] = 1 / float64(len(q.Options))
		}
	case decide.KindScore:
		a.Score = q.Range[0]
	case decide.KindNoul:
		even := 0.5
		a.Noul = &even
	}
	return a
}

// softmax returns a distribution over options: options with a logit share the
// mass by exp(logit/T); options without one get probability 0.
func softmax(options []string, logits map[string]float64) map[string]float64 {
	maxLogit := math.Inf(-1)
	for _, o := range options {
		if l, ok := logits[o]; ok && l > maxLogit {
			maxLogit = l
		}
	}
	probs := make(map[string]float64, len(options))
	var sum float64
	for _, o := range options {
		l, ok := logits[o]
		if !ok {
			probs[o] = 0
			continue
		}
		e := math.Exp((l - maxLogit) / rulesSoftmaxTemperature)
		probs[o] = e
		sum += e
	}
	if sum > 0 {
		for o := range probs {
			probs[o] /= sum
		}
	}
	return probs
}

func optionSet(q decide.Question) map[string]bool {
	out := make(map[string]bool, len(q.Options))
	for _, o := range q.Options {
		out[o] = true
	}
	return out
}

// emissionRequest widens req's ffa.intervention and ffa.target options with every
// intervention and target in ranked (#1295), so answerFromRanked answers with the
// engine's top candidate, i.e. the decision Evaluate emits, even when the
// allow-list (which shaped req) or the alive-player list would have excluded it.
// The permission chain applies the allow-list afterwards and its verdict rides
// agent.decide as decide.blocked. Only the in-loop rules path uses it; a remote
// backend and RulesBackend.Decide keep the caller's options. The question slices
// are copied, never mutated in place.
func emissionRequest(req decide.Request, ranked []scoredCandidate) decide.Request {
	if len(ranked) == 0 {
		return req
	}
	qs := make([]decide.Question, len(req.Questions))
	copy(qs, req.Questions)
	for i, q := range qs {
		if q.Kind != decide.KindChoice {
			continue
		}
		var option func(scoredCandidate) string
		switch q.ID {
		case decide.QuestionFFAIntervention:
			option = func(sc scoredCandidate) string { return sc.decision.Intervention }
		case decide.QuestionFFATarget:
			option = func(sc scoredCandidate) string { return targetOption(sc.decision.TargetSerial) }
		default:
			continue
		}
		have := optionSet(q)
		opts := append([]string(nil), q.Options...)
		for _, sc := range ranked {
			if o := option(sc); !have[o] {
				have[o] = true
				opts = append(opts, o)
			}
		}
		qs[i].Options = opts
	}
	req.Questions = qs
	return req
}

func targetOption(serial string) string {
	if serial == "" {
		return decide.TargetNone
	}
	return serial
}
