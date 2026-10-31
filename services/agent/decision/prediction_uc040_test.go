package decision

import (
	"context"
	"testing"
	"time"

	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/prediction"
	"github.com/joustmania/agent/telemetry/keys"
)

// prediction_uc040_test.go covers the decision side of #2479 (UC-040 A44, BR-034):
// the fixed rules prediction table, the rule ids on the candidates, registration
// (acted vs counterfactual), the flag, and that predictions never change what the
// rules decide or the permission chain does.

// testBook is a prediction book on clock whose timers never fire; its goroutines
// are joined at cleanup.
func testBook(t *testing.T, clock *time.Time) *prediction.Book {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	b := prediction.New(ctx, nil)
	b.SetClock(func() time.Time { return *clock })
	b.SetAfterFunc(func(time.Duration, func()) func() bool { return func() bool { return true } })
	t.Cleanup(func() { cancel(); b.Await() })
	return b
}

// UC: UC-040 A44 BR-034
// TestRulesPathGolden_PredictionsOnAndOff: with a prediction book attached, the
// rules path's outcomes and applied decisions are byte-identical to the golden,
// with decision.predictions_enabled on and off (the prediction lives on
// decide.Response and decideRecord, never on Decision or DecisionOutcome).
func TestRulesPathGolden_PredictionsOnAndOff(t *testing.T) {
	plain := rulesGoldenJSON(t, false, nil)
	registered := 0
	for _, off := range []bool{false, true} {
		var books []*prediction.Book
		got := rulesGoldenJSON(t, off, func(l *Loop, clock *time.Time) {
			b := testBook(t, clock)
			l.SetPredictionBook(b)
			books = append(books, b)
		})
		if string(got) != string(plain) {
			t.Fatalf("predictions off=%v changed the rules path:\n got: %s\nwant: %s", off, got, plain)
		}
		n := 0
		for _, b := range books {
			n += b.Pending()
		}
		if off && n != 0 {
			t.Fatalf("predictions off registered %d predictions", n)
		}
		if !off {
			registered = n
		}
	}
	if registered == 0 {
		t.Fatal("predictions on registered nothing: the comparison is vacuous")
	}
}

// UC: UC-040 A44 BR-034
func TestPredictions_FlagGatesRegistration(t *testing.T) {
	fx := decideFixtures()[2] // accelerate: R5 wins
	for _, off := range []bool{false, true} {
		clock := time.Unix(10_000, 0)
		snap := rulesSnapshot()
		snap.Decision.PredictionsOff = off
		l, sr := decideRecordingLoop(t, snap)
		l.Rules = newTestEngine(fx.cfg, &clock)
		l.now = func() time.Time { return clock }
		l.Actions = &capturingSink{}
		b := testBook(t, &clock)
		l.SetPredictionBook(b)
		c := fx.ctx
		l.OnEvaluate(context.Background(), c, testTrigger())

		id := ""
		for _, s := range sr.Ended() {
			if s.Name() != SpanDecide {
				continue
			}
			for _, a := range s.Attributes() {
				switch a.Key {
				case keys.AttrDecidePredictionID:
					id = a.Value.AsString()
				case keys.AttrDecidePredictionRule:
					if a.Value.AsString() != "R5" {
						t.Errorf("rule = %s, want R5", a.Value.AsString())
					}
				}
			}
		}
		if off {
			if id != "" || b.Pending() != 0 {
				t.Fatalf("UC-040/A44 flag off: id %q, pending %d, want nothing registered", id, b.Pending())
			}
			continue
		}
		// R5's prediction plus the base-rate control due at 100 s of play.
		if id == "" || b.Pending() != 2 {
			t.Fatalf("UC-040/A44 flag on: id %q, pending %d, want the decision's prediction and one control", id, b.Pending())
		}
	}
}

// UC: UC-040 A44 BR-034
// TestRulesPrediction_Table: one row per rule (#2479 plan §2.3).
func TestRulesPrediction_Table(t *testing.T) {
	st := func(alive int) decide.State {
		var s decide.State
		for i := 0; i < alive; i++ {
			s.Players = append(s.Players, decide.PlayerState{Serial: string(rune('A' + i)), Alive: true})
		}
		return s
	}
	for _, tc := range []struct {
		rule, template, scope string
		horizon, alive        int
		target                bool
	}{
		{"R1", "", "", 0, 4, false}, // tempo effect confounded with the threshold
		{"R2", decide.TemplateNoElimination, decide.ScopeGame, 15, 4, false},
		{"R3", decide.TemplateClosenessRises, decide.ScopeField, 15, 4, false},
		{"R4", decide.TemplateSurvives, decide.ScopeTarget, 15, 4, true},
		{"R4d", decide.TemplateSurvives, decide.ScopeTarget, 10, 2, true},
		{"R5", decide.TemplateElimination, decide.ScopeGame, 30, 4, false},
		{"R6", decide.TemplateGameEnds, decide.ScopeGame, 60, 4, false},
		{"R6", "", "", 0, 3, false}, // 2 alive after the elimination: trivially true
		{"R7", "", "", 0, 4, false},
		{"R8", decide.TemplateActivityRises, decide.ScopeTarget, 10, 4, true},
		{"R9", decide.TemplateActivityRises, decide.ScopeTarget, 10, 4, true},
	} {
		t.Run("UC-040/A44 BR-034 "+tc.rule, func(t *testing.T) {
			best := scoredCandidate{candidate: candidate{rule: tc.rule, decision: Decision{TargetSerial: "T"}}}
			got := rulesPrediction(best, st(tc.alive))
			if tc.template == "" {
				if got != nil {
					t.Fatalf("got %+v, want none", *got)
				}
				return
			}
			if got == nil || got.Template != tc.template || got.HorizonS != tc.horizon || got.Scope != tc.scope ||
				got.Source != decide.SourceRules || got.Rule != tc.rule || (got.Target == "T") != tc.target {
				t.Fatalf("got %+v", got)
			}
			if !decide.ValidPrediction(*got) {
				t.Fatalf("%+v is not a valid prediction", *got)
			}
		})
	}
}

// UC: UC-040 A44 BR-034
// TestRules_CandidatesCarryRuleID: every rule tags its candidates.
func TestRules_CandidatesCarryRuleID(t *testing.T) {
	rules := func(cs []candidate) map[string]bool {
		out := map[string]bool{}
		for _, c := range cs {
			out[c.decision.Intervention+"/"+c.rule] = true
		}
		return out
	}
	expect := func(t *testing.T, got map[string]bool, want ...string) {
		t.Helper()
		for _, w := range want {
			if !got[w] {
				t.Errorf("missing %s in %v", w, got)
			}
		}
	}
	fit := defaultFit()
	t.Run("UC-040/A44 R1 R2", func(t *testing.T) {
		c := gameCtx(20, []string{"GONE"}, map[string]testPlayer{"A": {active: true}, "B": {active: true}})
		expect(t, rules(enduranceCandidates(c, fit)), InterventionAdjustMusicTempo+"/R1", InterventionPlayAudioCue+"/R2")
	})
	t.Run("UC-040/A44 R3 R4", func(t *testing.T) {
		c := gameCtx(30, []string{"GONE"}, map[string]testPlayer{
			"WEAK": {skill: fptr(0.2), active: true}, "MID": {skill: fptr(0.5), active: true}, "STRONG": {skill: fptr(0.9), active: true}})
		expect(t, rules(balancedCandidates(c, fit)), InterventionAdjustPlayerSensitivity+"/R3", InterventionGrantShield+"/R4")
	})
	t.Run("UC-040/A44 R4d", func(t *testing.T) {
		expect(t, rules(balancedCandidates(duelCtx(), fit)), InterventionGrantShield+"/R4d")
	})
	t.Run("UC-040/A44 R5 R6 R7", func(t *testing.T) {
		c := gameCtx(3*fit.AccelerateTargetSessionSeconds, nil, map[string]testPlayer{
			"A": {intensity: fptr(0.2), active: true}, "B": {intensity: fptr(0.9), active: true}, "C": {intensity: fptr(1.4), active: true}})
		expect(t, rules(accelerateCandidates(c, fit, map[string]float64{ObjectiveAccelerate: 1})),
			InterventionAdjustMusicTempo+"/R5", InterventionEliminatePlayer+"/R6", InterventionEndGame+"/R7")
	})
	t.Run("UC-040/A44 R8 R9", func(t *testing.T) {
		c := gameCtx(600, nil, map[string]testPlayer{"S": {variance: fptr(0.01), active: true}, "T": {active: true}})
		expect(t, rules(chaosCandidates(c, defaultPol(), 0.9, "T")),
			InterventionSendControllerEffect+"/R8", InterventionSendControllerEffect+"/R9")
	})
}

// UC: UC-040 A44 BR-034
// TestEvaluateDecide_ResponseCarriesRulesPrediction: the rules answer carries the
// winning rule's prediction; a noop answer carries none.
func TestEvaluateDecide_ResponseCarriesRulesPrediction(t *testing.T) {
	for _, fx := range decideFixtures() {
		clock := time.Unix(10_000, 0)
		eng := newTestEngine(fx.cfg, &clock)
		build := func(charged, noShield map[string]bool) decide.Request {
			return decideRequestWith(fx.ctx, allInterventions(), true, decide.Annotations{Charged: charged, NoShield: noShield})
		}
		out, _, resp, ranked := eng.EvaluateDecide(context.Background(), fx.ctx, build)
		if !ranked {
			t.Fatalf("%s: not ranked", fx.name)
		}
		iv, _ := resp.Find(decide.QuestionFFAIntervention)
		switch {
		case len(out) == 0 || iv.Choice == decide.OptionNoop:
			if resp.Prediction != nil {
				t.Errorf("UC-040/A44 %s: a noop answer carries %+v, want no prediction", fx.name, *resp.Prediction)
			}
		case fx.name == "endurance":
			if resp.Prediction != nil {
				t.Errorf("UC-040/A44 endurance: R1 carries %+v, want none", *resp.Prediction)
			}
		case fx.name == "accelerate":
			if resp.Prediction == nil || resp.Prediction.Rule != "R5" || resp.Prediction.Template != decide.TemplateElimination {
				t.Errorf("UC-040/A44 accelerate: prediction %+v, want R5 elimination", resp.Prediction)
			}
		}
	}
}

// UC: UC-040 A44 BR-034
// TestRegisterPrediction_ActedAndCounterfactual: acted only for an applied
// decision; held, blocked and gated-off decisions are counterfactuals; noops and
// records without a context register nothing.
func TestRegisterPrediction_ActedAndCounterfactual(t *testing.T) {
	clock := time.Unix(10_000, 0)
	c := gameCtx(20, nil, map[string]testPlayer{"A": {active: true}, "B": {active: true}})
	shield := decide.NewPrediction(decide.TemplateSurvives, 15, 0.7, decide.SourceRules, "R4", "A")
	span := trace.SpanFromContext(context.Background())
	for _, tc := range []struct {
		name           string
		rec            decideRecord
		wantRegistered bool
		wantActed      bool
	}{
		{"UC-040/A44 applied act is acted", decideRecord{outcome: DecideOutcomeAct, permissionKnown: true, applied: true}, true, true},
		{"UC-040/A44 blocked act is a counterfactual", decideRecord{outcome: DecideOutcomeAct, permissionKnown: true, blocked: true}, true, false},
		{"UC-040/A44 gated-off act is a counterfactual", decideRecord{outcome: DecideOutcomeAct, permissionKnown: true}, true, false},
		{"UC-040/A44 held is a counterfactual", decideRecord{outcome: DecideOutcomeHeld}, true, false},
		{"UC-040/A44 discarded target_gone is a counterfactual", decideRecord{outcome: DecideOutcomeDiscarded, discardedReason: DiscardTargetGone}, true, false},
		{"UC-040/A44 applied rules fallback is acted", decideRecord{outcome: DecideOutcomeFallback, permissionKnown: true, applied: true}, true, true},
		{"UC-040/A44 noop registers nothing", decideRecord{outcome: DecideOutcomeNoop}, false, false},
		{"UC-040/A44 invalid registers nothing", decideRecord{outcome: DecideOutcomeInvalid}, false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			b := testBook(t, &clock)
			l := NewLoop(nil, nil)
			l.SetPredictionBook(b)
			rec := tc.rec
			rec.used, rec.prediction, rec.predictionCtx = "jev", &shield, &c
			l.registerPrediction(span, &rec)
			if (rec.predictionID != "") != tc.wantRegistered {
				t.Fatalf("registered = %v, want %v", rec.predictionID != "", tc.wantRegistered)
			}
			if !tc.wantRegistered {
				return
			}
			end := c
			end.EndReason = gamecontext.EndReasonZero
			b.OnGameEnd(end)
			rs := b.Results(c.SessionID)
			if len(rs) != 1 || rs[0].Acted != tc.wantActed || rs[0].Backend != "jev" {
				t.Fatalf("results = %+v, want acted=%v", rs, tc.wantActed)
			}
		})
	}
	t.Run("UC-040/A44 no context registers nothing", func(t *testing.T) {
		b := testBook(t, &clock)
		l := NewLoop(nil, nil)
		l.SetPredictionBook(b)
		rec := decideRecord{outcome: DecideOutcomeAct, prediction: &shield}
		l.registerPrediction(span, &rec)
		if rec.predictionID != "" || b.Pending() != 0 {
			t.Fatal("a record without a context must not register")
		}
	})
}

// UC: UC-040 A44 BR-034
// TestRemotePrediction_LeverDefault: a remote answer without a prediction gets
// the lever default of its mapped decision; one with a prediction keeps it.
func TestRemotePrediction_LeverDefault(t *testing.T) {
	c := gameCtx(20, nil, map[string]testPlayer{"A": {active: true}})
	got := remotePrediction(decide.Response{}, Decision{Intervention: InterventionGrantShield, TargetSerial: "A"}, c)
	if got == nil || got.Source != decide.SourceLeverDefault || got.Template != decide.TemplateSurvives || got.Target != "A" {
		t.Fatalf("lever default = %+v", got)
	}
	own := decide.NewPrediction(decide.TemplateElimination, 30, 0.4, decide.SourceModel, "", "")
	if got := remotePrediction(decide.Response{Prediction: &own}, Decision{Intervention: InterventionGrantShield, TargetSerial: "A"}, c); got != &own {
		t.Fatalf("the backend's own prediction must win, got %+v", got)
	}
	if got := remotePrediction(decide.Response{}, Decision{Intervention: InterventionEndGame}, c); got != nil {
		t.Fatalf("end_game has no lever default, got %+v", *got)
	}
}

// actedResults ends c's game in b and returns the results of source.
func actedResults(t *testing.T, b *prediction.Book, c gamecontext.GameContext, source string) []prediction.Result {
	t.Helper()
	end := c
	end.EndReason = gamecontext.EndReasonZero
	b.OnGameEnd(end)
	var out []prediction.Result
	for _, r := range b.Results(c.SessionID) {
		if r.Prediction.Source == source {
			out = append(out, r)
		}
	}
	return out
}

// UC: UC-040 A44 BR-034
// TestPredictionActed_RulesPath: through OnEvaluate, an applied write registers
// an acted prediction; a gated-off write and a blocked decision register a
// counterfactual.
func TestPredictionActed_RulesPath(t *testing.T) {
	fx := decideFixtures()[2] // accelerate: R5 adjust_music_tempo wins
	withoutTempo := []string{}
	for _, iv := range allInterventions() {
		if iv != InterventionAdjustMusicTempo {
			withoutTempo = append(withoutTempo, iv)
		}
	}
	for _, tc := range []struct {
		name    string
		sink    ActionSink
		allowed []string
		want    bool
	}{
		{"UC-040/A44 applied write is acted", &capturingSink{}, allInterventions(), true},
		{"UC-040/A44 gated-off write is a counterfactual", &gatedSink{gateOn: false}, allInterventions(), false},
		{"UC-040/A44 blocked decision is a counterfactual", &capturingSink{}, withoutTempo, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			clock := time.Unix(10_000, 0)
			snap := rulesSnapshot()
			snap.InterventionsAllowed = tc.allowed
			l, _ := decideRecordingLoop(t, snap)
			l.Rules = newTestEngine(fx.cfg, &clock)
			l.now = func() time.Time { return clock }
			l.Actions = tc.sink
			b := testBook(t, &clock)
			l.SetPredictionBook(b)
			l.OnEvaluate(context.Background(), fx.ctx, testTrigger())
			rs := actedResults(t, b, fx.ctx, decide.SourceRules)
			if len(rs) != 1 || rs[0].Prediction.Rule != "R5" || rs[0].Acted != tc.want {
				t.Fatalf("rules results = %+v, want one R5 with acted=%v", rs, tc.want)
			}
		})
	}
}

// UC: UC-040 A44 BR-034
// TestPredictionActed_RemotePath: through runDecideRemote and decideRemoteApply,
// an applied remote write registers an acted lever-default prediction; a gated-off
// write and a decision the live allow-list blocks register a counterfactual.
func TestPredictionActed_RemotePath(t *testing.T) {
	for _, tc := range []struct {
		name   string
		gated  bool
		revoke bool
		want   bool
	}{
		{"UC-040/A44 applied remote write is acted", false, false, true},
		{"UC-040/A44 gated-off remote write is a counterfactual", true, false, false},
		{"UC-040/A44 remote decision blocked at landing is a counterfactual", false, true, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.9, "A", 0.5, "shield"))
			be.block = true
			h := newRemoteHarness(t, remoteSnapshot(time.Hour), be)
			b := testBook(t, h.clock)
			h.loop.SetPredictionBook(b)
			if tc.gated {
				h.loop.Actions = &gatedSink{gateOn: false}
			}
			h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
			<-be.started
			if tc.revoke {
				h.flags.mutate(func(s *flags.Snapshot) {
					s.InterventionsAllowed = []string{"noop", "adjust_music_tempo", "play_audio_cue"}
				})
			}
			close(be.release)
			h.loop.AwaitInflight()
			rs := actedResults(t, b, remoteContext(), decide.SourceLeverDefault)
			if len(rs) != 1 || rs[0].Prediction.Template != decide.TemplateSurvives || rs[0].Backend != "claude" || rs[0].Acted != tc.want {
				t.Fatalf("lever results = %+v, want one survives from claude with acted=%v", rs, tc.want)
			}
		})
	}
}
