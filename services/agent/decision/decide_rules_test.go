package decision

import (
	"context"
	"math"
	"reflect"
	"sort"
	"testing"
	"time"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/gamecontext"
)

// allInterventions is the full intervention vocabulary, i.e. the most permissive
// allow-list, so the adapter can express anything Evaluate emits.
func allInterventions() []string {
	out := make([]string, 0, len(interventionWeights))
	for k := range interventionWeights {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func ffaRequest(c gamecontext.GameContext, allowed []string) decide.Request {
	st := decide.BuildState(c)
	return decide.Request{State: st, Questions: decide.FFAQuestions(st, allowed), Context: &c}
}

// decideFixture is one characterization scenario: a config and a context.
type decideFixture struct {
	name string
	cfg  StaticConfig
	ctx  gamecontext.GameContext
}

func withWeights(w map[string]float64) StaticConfig {
	cfg := DefaultStaticConfig()
	cfg.ObjectiveWeights = w
	return cfg
}

func decideFixtures() []decideFixture {
	return []decideFixture{
		{
			// R1 (slow tempo) and R2 (calming cue) fire; R1 wins.
			name: "endurance",
			cfg:  DefaultStaticConfig(),
			ctx: gameCtx(20, []string{"GONE"}, map[string]testPlayer{
				"A": {active: true}, "B": {active: true},
			}),
		},
		{
			// R3 (handicap the outlier) and R4 (shield the weakest) compete.
			name: "balanced",
			cfg:  withWeights(map[string]float64{ObjectiveBalanced: 1}),
			ctx: gameCtx(30, []string{"GONE"}, map[string]testPlayer{
				"WEAK":   {skill: fptr(0.2), active: true},
				"MID":    {skill: fptr(0.5), active: true},
				"STRONG": {skill: fptr(0.9), active: true},
			}),
		},
		{
			// R5 (speed up) and R6 (eliminate the least active) fire.
			name: "accelerate",
			cfg:  withWeights(map[string]float64{ObjectiveAccelerate: 1}),
			ctx: gameCtx(100, nil, map[string]testPlayer{
				"A": {intensity: fptr(0.2), active: true},
				"B": {intensity: fptr(0.9), active: true},
				"C": {intensity: fptr(1.4), active: true},
			}),
		},
		{
			// R9 only (variance window not filled yet): the seeded rng picks the
			// roll and the target.
			name: "chaos random",
			cfg:  withWeights(map[string]float64{ObjectiveChaos: 1}),
			ctx: gameCtx(5, nil, map[string]testPlayer{
				"A": {active: true}, "B": {active: true}, "C": {active: true}, "D": {active: true},
			}),
		},
		{
			// R8 statue on S plus the seeded R9: two controller effects on
			// (possibly) different targets.
			name: "chaos statue",
			cfg:  withWeights(map[string]float64{ObjectiveChaos: 1}),
			ctx: gameCtx(20, nil, map[string]testPlayer{
				"S": {variance: fptr(0.01), active: true},
				"T": {variance: fptr(0.30), active: true},
			}),
		},
		{
			// Endurance-only weights on a long session: accelerate/balanced
			// candidates all score 0, nothing clears minScore.
			name: "no candidates",
			cfg:  DefaultStaticConfig(),
			ctx: gameCtx(200, nil, map[string]testPlayer{
				"WEAK":   {skill: fptr(0.2), intensity: fptr(0.3), active: true},
				"STRONG": {skill: fptr(0.9), intensity: fptr(1.5), active: true},
			}),
		},
	}
}

// TestRulesBackend_MatchesEvaluate is the characterization test: on the same
// context and an identically seeded engine, the adapter picks the intervention
// and target that ObjectiveRules.Evaluate emits first (noop when it emits none).
func TestRulesBackend_MatchesEvaluate(t *testing.T) {
	for _, fx := range decideFixtures() {
		t.Run(fx.name, func(t *testing.T) {
			clock := time.Unix(10_000, 0)
			want := newTestEngine(fx.cfg, &clock).Evaluate(context.Background(), fx.ctx)

			b := NewRulesBackend(newTestEngine(fx.cfg, &clock))
			req := ffaRequest(fx.ctx, allInterventions())
			resp, err := b.Decide(context.Background(), req)
			if err != nil {
				t.Fatalf("Decide error: %v", err)
			}
			if err := decide.Validate(req, resp); err != nil {
				t.Fatalf("response does not validate: %v", err)
			}
			if resp.Backend != RulesBackendName {
				t.Errorf("Backend = %q", resp.Backend)
			}
			iv, _ := resp.Find(decide.QuestionFFAIntervention)
			tgt, _ := resp.Find(decide.QuestionFFATarget)

			wantIV, wantTgt := decide.OptionNoop, decide.TargetNone
			if len(want) > 0 {
				wantIV = want[0].Intervention
				wantTgt = targetOption(want[0].TargetSerial)
			}
			if iv.Choice != wantIV || tgt.Choice != wantTgt {
				t.Fatalf("adapter picked %s -> %s, Evaluate picked %s -> %s (all: %+v)",
					iv.Choice, tgt.Choice, wantIV, wantTgt, want)
			}
			t.Logf("%s -> %s (confidence %.4f, target confidence %.4f)", iv.Choice, tgt.Choice, iv.Confidence, tgt.Confidence)
			if fx.name != "no candidates" && len(want) == 0 {
				t.Fatalf("fixture %q should produce a decision", fx.name)
			}
			if fx.name == "no candidates" && len(want) != 0 {
				t.Fatalf("fixture %q should produce no decision, got %+v", fx.name, want)
			}
		})
	}
}

// TestRulesBackend_ProbabilitiesAreSoftmaxOfScores checks the probability and
// confidence math against the engine's own ranked scores.
func TestRulesBackend_ProbabilitiesAreSoftmaxOfScores(t *testing.T) {
	for _, fx := range decideFixtures() {
		t.Run(fx.name, func(t *testing.T) {
			clock := time.Unix(10_000, 0)
			twin := newTestEngine(fx.cfg, &clock)
			ranked, _, _ := twin.rankLocked(fx.ctx, clock, nil)

			resp, _ := NewRulesBackend(newTestEngine(fx.cfg, &clock)).Decide(context.Background(), ffaRequest(fx.ctx, allInterventions()))
			iv, _ := resp.Find(decide.QuestionFFAIntervention)

			// Best score per intervention, noop at minScore.
			best := map[string]float64{decide.OptionNoop: minScore}
			for _, sc := range ranked {
				if _, ok := best[sc.decision.Intervention]; !ok {
					best[sc.decision.Intervention] = sc.score
				}
			}
			for name, s := range best {
				ratio := iv.Probabilities[name] / iv.Probabilities[decide.OptionNoop]
				want := math.Exp((s - minScore) / rulesSoftmaxTemperature)
				if math.Abs(ratio-want) > 1e-9*want {
					t.Errorf("p(%s)/p(noop) = %v, want exp((%.3f-%.2f)/T) = %v", name, ratio, s, minScore, want)
				}
			}
			for name, p := range iv.Probabilities {
				if _, ok := best[name]; !ok && p != 0 {
					t.Errorf("p(%s) = %v for an intervention without candidates", name, p)
				}
				if p > iv.Probabilities[iv.Choice] {
					t.Errorf("choice %s (p=%v) is not the argmax (%s p=%v)", iv.Choice, iv.Probabilities[iv.Choice], name, p)
				}
			}
			// Confidence that acting is right: 1 - p(noop) for an intervention,
			// p(noop) for noop.
			want := 1 - iv.Probabilities[decide.OptionNoop]
			if iv.Choice == decide.OptionNoop {
				want = iv.Probabilities[decide.OptionNoop]
			}
			if math.Abs(iv.Confidence-want) > 1e-12 {
				t.Errorf("intervention confidence = %v, want %v", iv.Confidence, want)
			}
			tgt, _ := resp.Find(decide.QuestionFFATarget)
			if tgt.Confidence != tgt.Probabilities[tgt.Choice] {
				t.Errorf("target confidence = %v, want p(%s) = %v", tgt.Confidence, tgt.Choice, tgt.Probabilities[tgt.Choice])
			}
		})
	}
}

func TestRulesBackend_NoCandidatesIsConfidentNoop(t *testing.T) {
	fx := decideFixtures()[5]
	clock := time.Unix(10_000, 0)
	resp, _ := NewRulesBackend(newTestEngine(fx.cfg, &clock)).Decide(context.Background(), ffaRequest(fx.ctx, allInterventions()))
	iv, _ := resp.Find(decide.QuestionFFAIntervention)
	tgt, _ := resp.Find(decide.QuestionFFATarget)
	mag, _ := resp.Find(decide.QuestionFFAMagnitude)
	if iv.Choice != decide.OptionNoop || iv.Probabilities[decide.OptionNoop] != 1 || iv.Confidence != 1 {
		t.Errorf("intervention = %+v, want certain noop", iv)
	}
	if tgt.Choice != decide.TargetNone || tgt.Confidence != 1 {
		t.Errorf("target = %+v, want certain none", tgt)
	}
	if mag.Score != 0 {
		t.Errorf("magnitude = %+v, want 0 for noop", mag)
	}
}

func TestRulesBackend_EnduranceDetails(t *testing.T) {
	fx := decideFixtures()[0]
	clock := time.Unix(10_000, 0)
	resp, _ := NewRulesBackend(newTestEngine(fx.cfg, &clock)).Decide(context.Background(), ffaRequest(fx.ctx, allInterventions()))
	iv, _ := resp.Find(decide.QuestionFFAIntervention)
	tgt, _ := resp.Find(decide.QuestionFFATarget)
	mag, _ := resp.Find(decide.QuestionFFAMagnitude)
	if iv.Choice != InterventionAdjustMusicTempo {
		t.Fatalf("choice = %s", iv.Choice)
	}
	// Session-scoped: the only target among tempo candidates is "none".
	if tgt.Choice != decide.TargetNone || tgt.Probabilities[decide.TargetNone] != 1 || tgt.Confidence != 1 {
		t.Errorf("target = %+v", tgt)
	}
	// R1 writes the fast end of [1.0, 1.3] (#2487, UC-040 A51, BR-040), so the
	// magnitude is 1.3, not the 1.15 writer default an empty Value would get.
	if v, _ := decide.ResolveMagnitude(iv.Choice, mag.Score); math.Abs(v-1.3) > 1e-9 {
		t.Errorf("magnitude resolves to %v, want 1.3", v)
	}
	if mag.Confidence != iv.Confidence {
		t.Errorf("magnitude confidence %v, want the intervention's %v", mag.Confidence, iv.Confidence)
	}
}

func TestRulesBackend_ChaosTargetIsConditional(t *testing.T) {
	fx := decideFixtures()[4]
	clock := time.Unix(10_000, 0)
	resp, _ := NewRulesBackend(newTestEngine(fx.cfg, &clock)).Decide(context.Background(), ffaRequest(fx.ctx, allInterventions()))
	iv, _ := resp.Find(decide.QuestionFFAIntervention)
	tgt, _ := resp.Find(decide.QuestionFFATarget)
	if iv.Choice != InterventionSendControllerEffect {
		t.Fatalf("choice = %s", iv.Choice)
	}
	// The statue S (R8, urgency 0.8) beats the R9 nudge (urgency <= 0.6).
	if tgt.Choice != "S" || tgt.Probabilities["S"] <= tgt.Probabilities["T"] {
		t.Errorf("target = %+v, want S as argmax", tgt)
	}
	if tgt.Probabilities[decide.TargetNone] != 0 {
		t.Errorf("p(none) = %v for a player-targeted intervention", tgt.Probabilities[decide.TargetNone])
	}
}

// TestRulesBackend_AllowListFilters: when the allow-list does not offer
// Evaluate's top pick, the adapter answers with the best candidate it can
// express (Evaluate's runner-up here) instead of an invalid option.
func TestRulesBackend_AllowListFilters(t *testing.T) {
	fx := decideFixtures()[0]
	clock := time.Unix(10_000, 0)
	want := newTestEngine(fx.cfg, &clock).Evaluate(context.Background(), fx.ctx)
	if len(want) < 2 || want[0].Intervention != InterventionAdjustMusicTempo {
		t.Fatalf("fixture changed: %+v", want)
	}

	allowed := []string{InterventionPlayAudioCue, InterventionGrantShield}
	req := ffaRequest(fx.ctx, allowed)
	resp, _ := NewRulesBackend(newTestEngine(fx.cfg, &clock)).Decide(context.Background(), req)
	if err := decide.Validate(req, resp); err != nil {
		t.Fatalf("invalid: %v", err)
	}
	iv, _ := resp.Find(decide.QuestionFFAIntervention)
	if iv.Choice != want[1].Intervention {
		t.Fatalf("choice = %s, want Evaluate's runner-up %s", iv.Choice, want[1].Intervention)
	}

	// Nothing allowed: noop.
	req = ffaRequest(fx.ctx, nil)
	resp, _ = NewRulesBackend(newTestEngine(fx.cfg, &clock)).Decide(context.Background(), req)
	if iv, _ := resp.Find(decide.QuestionFFAIntervention); iv.Choice != decide.OptionNoop {
		t.Fatalf("empty allow-list: choice = %s, want noop", iv.Choice)
	}
}

func TestRulesBackend_NeverErrors(t *testing.T) {
	clock := time.Unix(10_000, 0)
	fx := decideFixtures()[0]

	// No Context: noop at confidence 0, still valid.
	st := decide.BuildState(fx.ctx)
	req := decide.Request{State: st, Questions: decide.FFAQuestions(st, allInterventions())}
	resp, err := NewRulesBackend(newTestEngine(fx.cfg, &clock)).Decide(context.Background(), req)
	if err != nil {
		t.Fatalf("nil context: %v", err)
	}
	if err := decide.Validate(req, resp); err != nil {
		t.Fatalf("nil context: invalid: %v", err)
	}
	for _, a := range resp.Answers {
		if a.Confidence != 0 {
			t.Errorf("nil context: %s confidence %v, want 0", a.QuestionID, a.Confidence)
		}
	}
	if iv, _ := resp.Find(decide.QuestionFFAIntervention); iv.Choice != decide.OptionNoop {
		t.Errorf("nil context: choice %s, want noop", iv.Choice)
	}

	// Nil engine behaves like a missing context.
	if _, err := NewRulesBackend(nil).Decide(context.Background(), ffaRequest(fx.ctx, nil)); err != nil {
		t.Fatalf("nil engine: %v", err)
	}

	// Questions the adapter does not know get a neutral, valid answer.
	req = ffaRequest(fx.ctx, allInterventions())
	req.Questions = append(req.Questions,
		decide.Question{ID: "ffa.mood", Kind: decide.KindChoice, Options: []string{"calm", "wild", "odd"}},
		decide.Question{ID: "ffa.volume", Kind: decide.KindScore, Range: [2]float64{2, 5}},
	)
	resp, err = NewRulesBackend(newTestEngine(fx.cfg, &clock)).Decide(context.Background(), req)
	if err != nil {
		t.Fatalf("extra questions: %v", err)
	}
	if err := decide.Validate(req, resp); err != nil {
		t.Fatalf("extra questions: invalid: %v", err)
	}
	if a, _ := resp.Find("ffa.mood"); a.Choice != "calm" || a.Confidence != 0 {
		t.Errorf("unknown choice answer = %+v", a)
	}
	if a, _ := resp.Find("ffa.volume"); a.Score != 2 || a.Confidence != 0 {
		t.Errorf("unknown score answer = %+v", a)
	}
}

// TestRulesBackend_RecordsNoEngineState: deciding is not acting, so the adapter
// must not throttle or arm the variance cooldown for a later Evaluate.
func TestRulesBackend_RecordsNoEngineState(t *testing.T) {
	fx := decideFixtures()[0]
	clock := time.Unix(10_000, 0)
	engine := newTestEngine(fx.cfg, &clock)
	sentinel := FitnessEvaluation{Results: map[string]FitnessResult{"sentinel": {}}}
	engine.lastFitness = sentinel
	b := NewRulesBackend(engine)
	for i := 0; i < 3; i++ {
		if _, err := b.Decide(context.Background(), ffaRequest(fx.ctx, allInterventions())); err != nil {
			t.Fatal(err)
		}
	}
	if !engine.lastEval.IsZero() || !engine.lastDifficultyAt.IsZero() {
		t.Fatalf("adapter touched engine state: lastEval=%v lastDifficultyAt=%v", engine.lastEval, engine.lastDifficultyAt)
	}
	if !reflect.DeepEqual(engine.LastFitness(), sentinel) {
		t.Fatalf("adapter overwrote lastFitness: %+v", engine.LastFitness())
	}
	if out := engine.Evaluate(context.Background(), fx.ctx); len(out) == 0 {
		t.Fatal("Evaluate throttled after adapter calls")
	}
}

// TestDecideContract_VocabularyMatches keeps the leaf decide package's string
// ids in sync with this package's intervention constants.
func TestDecideContract_VocabularyMatches(t *testing.T) {
	if decide.OptionNoop != InterventionNoop {
		t.Errorf("decide.OptionNoop = %q, InterventionNoop = %q", decide.OptionNoop, InterventionNoop)
	}
	for _, iv := range decide.MagnitudeInterventions() {
		if _, ok := interventionWeights[iv]; !ok {
			t.Errorf("bounds table names unknown intervention %q", iv)
		}
	}
}

// rankedCandidate builds a scored candidate directly, for the probability edge
// cases that are awkward to reach through a full GameContext.
func rankedCandidate(intervention, target string, score float64) scoredCandidate {
	return scoredCandidate{
		candidate: candidate{decision: Decision{Intervention: intervention, TargetSerial: target}},
		score:     score,
	}
}

// ffaQuestionsFor builds the FFA question set over the given alive serials.
func ffaQuestionsFor(allowed []string, alive ...string) map[string]decide.Question {
	c := gamecontext.GameContext{Players: map[string]*gamecontext.PlayerSignals{}}
	for _, s := range alive {
		c.Players[s] = &gamecontext.PlayerSignals{Serial: s, Active: bptr(true)}
	}
	return indexQuestions(decide.FFAQuestions(decide.BuildState(c), allowed))
}

// TestRulesAnswer_CompetingStrongInterventions: two strong candidates split the
// probability mass, but both clearly beat noop, so acting is a confident call.
// (A top-1 margin would have reported ~0.05 here and declined to act.)
func TestRulesAnswer_CompetingStrongInterventions(t *testing.T) {
	qs := ffaQuestionsFor(allInterventions(), "A")
	eligible := []scoredCandidate{
		rankedCandidate(InterventionAdjustMusicTempo, "", 0.90),
		rankedCandidate(InterventionGrantShield, "A", 0.89),
	}
	pick := answerIntervention(qs[decide.QuestionFFAIntervention], eligible)
	a := pick.answer
	if a.Choice != InterventionAdjustMusicTempo {
		t.Fatalf("choice = %s", a.Choice)
	}
	if p1, p2 := a.Probabilities[InterventionAdjustMusicTempo], a.Probabilities[InterventionGrantShield]; p1-p2 > 0.1 {
		t.Fatalf("fixture should split the mass: p=%v vs %v", p1, p2)
	}
	if a.Confidence < 0.99 {
		t.Errorf("confidence = %v, want >= 0.99 (1 - p(noop))", a.Confidence)
	}
	req := decide.Request{Questions: []decide.Question{qs[decide.QuestionFFAIntervention]}}
	if err := decide.Validate(req, decide.Response{Answers: []decide.Answer{a}}); err != nil {
		t.Fatalf("invalid: %v", err)
	}
}

// TestRulesAnswer_ExactMinScoreTie: a candidate at exactly minScore survives the
// engine's filter (s < minScore is dropped), so the rules act on it; it ties
// noop at p=0.5 and the answer still validates (the choice is an argmax).
func TestRulesAnswer_ExactMinScoreTie(t *testing.T) {
	qs := ffaQuestionsFor(allInterventions(), "A")
	eligible := []scoredCandidate{rankedCandidate(InterventionPlayAudioCue, "", minScore)}
	pick := answerIntervention(qs[decide.QuestionFFAIntervention], eligible)
	a := pick.answer
	if a.Choice != InterventionPlayAudioCue {
		t.Fatalf("choice = %s, want the candidate (rules act at score == minScore)", a.Choice)
	}
	if a.Probabilities[InterventionPlayAudioCue] != 0.5 || a.Probabilities[decide.OptionNoop] != 0.5 {
		t.Errorf("probabilities = %v, want an even split with noop", a.Probabilities)
	}
	if a.Confidence != 0.5 {
		t.Errorf("confidence = %v, want 0.5", a.Confidence)
	}
	req := decide.Request{Questions: []decide.Question{qs[decide.QuestionFFAIntervention]}}
	if err := decide.Validate(req, decide.Response{Answers: []decide.Answer{a}}); err != nil {
		t.Fatalf("invalid: %v", err)
	}
}

// TestRulesBackend_NoulIsNeutral: the rules adapter has no view on a noul
// question (#1229): it answers an even 0.5 at confidence 0, and the response
// still validates.
// UC: UC-040 A12
func TestRulesBackend_NoulIsNeutral(t *testing.T) {
	fx := decideFixtures()[0]
	clock := time.Unix(10_000, 0)
	req := ffaRequest(fx.ctx, allInterventions())
	req.Questions = append(req.Questions, decide.Question{ID: "close", Kind: decide.KindNoul, Text: "Is the game close?"})
	resp, err := NewRulesBackend(newTestEngine(fx.cfg, &clock)).Decide(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	a, ok := resp.Find("close")
	if !ok || a.Noul == nil || *a.Noul != 0.5 || a.Confidence != 0 {
		t.Errorf("noul answer = %+v", a)
	}
	if err := decide.Validate(req, resp); err != nil {
		t.Errorf("Validate: %v", err)
	}
}
