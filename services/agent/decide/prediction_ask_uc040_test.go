package decide

import (
	"math"
	"slices"
	"testing"
)

// UC: UC-040 A45 BR-034
// TestPredictionQuestion: the ask offers none plus every template and horizon of
// the closed vocabulary; survives only while a player is alive.
func TestPredictionQuestion(t *testing.T) {
	alive := State{Players: []PlayerState{{Serial: "A", Alive: true}}}
	t.Run("UC-040/A45 BR-034 options are the closed vocabulary", func(t *testing.T) {
		q := PredictionQuestion(alive)
		if q.ID != QuestionFFAPrediction || q.Kind != KindChoice || q.Options[0] != PredictionNone {
			t.Fatalf("ask = %+v", q)
		}
		want := []string{PredictionNone,
			"activity_rises_10s", "activity_rises_15s", "closeness_rises_15s",
			"elimination_15s", "elimination_30s", "elimination_60s",
			"game_ends_30s", "game_ends_60s", "game_ends_120s",
			"no_elimination_10s", "no_elimination_15s", "no_elimination_30s",
			"survives_10s", "survives_15s", "survives_30s"}
		if !slices.Equal(q.Options, want) {
			t.Fatalf("options = %v\nwant %v", q.Options, want)
		}
	})
	t.Run("UC-040/A45 BR-034 no alive player offers no survives", func(t *testing.T) {
		q := PredictionQuestion(State{Players: []PlayerState{{Serial: "A"}}})
		for _, o := range q.Options {
			if o == "survives_15s" {
				t.Fatal("survives offered without an alive player")
			}
		}
	})
}

// UC: UC-040 A45 BR-034
// TestValidatePrediction: every drop reason, the none answer, and the derived
// scope of a valid model prediction.
func TestValidatePrediction(t *testing.T) {
	ask := *PredictionQuestion(State{Players: []PlayerState{{Serial: "A", Alive: true}}})
	ans := func(choice string, conf float64) Answer {
		return Answer{QuestionID: QuestionFFAPrediction, Choice: choice, Confidence: conf}
	}
	for _, tc := range []struct {
		name         string
		a            Answer
		intervention string
		target       string
		wantReason   string
		wantTemplate string
		wantScope    string
	}{
		{"UC-040/A45 BR-034 survives on the target", ans("survives_15s", 0.7), IntvGrantShield, "A", "", TemplateSurvives, ScopeTarget},
		{"UC-040/A45 BR-034 activity_rises without a target is the field", ans("activity_rises_10s", 0.6), IntvAdjustMusicTempo, TargetNone, "", TemplateActivityRises, ScopeField},
		{"UC-040/A45 BR-034 game template", ans("no_elimination_15s", 0.5), IntvAdjustGlobalDifficulty, TargetNone, "", TemplateNoElimination, ScopeGame},
		{"UC-040/A45 BR-034 none states nothing", ans(PredictionNone, 0.9), IntvNoop, TargetNone, "", "", ""},
		{"UC-040/A45 BR-034 bad option", ans("survives_20s", 0.7), IntvGrantShield, "A", PredictionDropBadOption, "", ""},
		{"UC-040/A45 BR-034 empty option", ans("", 0.7), IntvGrantShield, "A", PredictionDropBadOption, "", ""},
		{"UC-040/A45 BR-034 confidence above 1", ans("survives_15s", 1.2), IntvGrantShield, "A", PredictionDropBadConfidence, "", ""},
		{"UC-040/A45 BR-034 confidence below 0", ans("survives_15s", -0.1), IntvGrantShield, "A", PredictionDropBadConfidence, "", ""},
		{"UC-040/A45 BR-034 confidence NaN", ans("survives_15s", math.NaN()), IntvGrantShield, "A", PredictionDropBadConfidence, "", ""},
		{"UC-040/A45 BR-034 survives without a target", ans("survives_15s", 0.7), IntvAdjustMusicTempo, TargetNone, PredictionDropNoTarget, "", ""},
		{"UC-040/A45 BR-034 prediction on noop", ans("elimination_30s", 0.7), IntvNoop, TargetNone, PredictionDropInconsistent, "", ""},
		{"UC-040/A45 BR-034 prediction on end_game", ans("game_ends_30s", 0.9), IntvEndGame, TargetNone, PredictionDropInconsistent, "", ""},
		{"UC-040/A45 BR-034 prediction on eliminate_player", ans("survives_15s", 0.1), IntvEliminatePlayer, "A", PredictionDropInconsistent, "", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p, reason := ValidatePrediction(ask, tc.a, tc.intervention, tc.target)
			if reason != tc.wantReason {
				t.Fatalf("reason = %q, want %q", reason, tc.wantReason)
			}
			if tc.wantTemplate == "" {
				if p != nil {
					t.Fatalf("prediction = %+v, want none", *p)
				}
				return
			}
			if p == nil || p.Template != tc.wantTemplate || p.Scope != tc.wantScope || p.Source != SourceModel ||
				p.Rule != tc.intervention || p.P != tc.a.Confidence || !ValidPrediction(*p) {
				t.Fatalf("prediction = %+v", p)
			}
		})
	}
}
