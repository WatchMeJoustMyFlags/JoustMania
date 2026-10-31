package decide

import "testing"

// UC: UC-040 A44 BR-034
// TestLeverPrediction: the lever default follows each lever's direction and
// returns nil for the levers with nothing measurable to expect (#2479).
func TestLeverPrediction(t *testing.T) {
	tempo := 1.15
	for _, tc := range []struct {
		name, intervention, value, target string
		tempo                             *float64
		wantTemplate                      string // "" = nil
		wantHorizon                       int
		wantScope                         string
	}{
		{"UC-040/A44 grant_shield survives 15", IntvGrantShield, "5", "P1", nil, TemplateSurvives, 15, ScopeTarget},
		{"UC-040/A44 toughness_boost survives 15", IntvToughnessBoost, "", "P1", nil, TemplateSurvives, 15, ScopeTarget},
		{"UC-040/A44 shield without target none", IntvGrantShield, "5", "", nil, "", 0, ""},
		{"UC-040/A44 player sensitivity protect survives", IntvAdjustPlayerSensitivity, "0.7", "P1", nil, TemplateSurvives, 15, ScopeTarget},
		{"UC-040/A44 player sensitivity handicap none", IntvAdjustPlayerSensitivity, "1.5", "P1", nil, "", 0, ""},
		{"UC-040/A44 handicap help survives", IntvSetPlayerHandicap, "1.25", "P1", nil, TemplateSurvives, 15, ScopeTarget},
		{"UC-040/A44 handicap rein in none", IntvSetPlayerHandicap, "0.75", "P1", nil, "", 0, ""},
		{"UC-040/A44 tempo down none (confounded with the threshold)", IntvAdjustMusicTempo, "1.0", "", &tempo, "", 0, ""},
		{"UC-040/A44 tempo up none (confounded with the threshold)", IntvAdjustMusicTempo, "1.3", "", &tempo, "", 0, ""},
		{"UC-040/A44 tempo unknown none", IntvAdjustMusicTempo, "1.3", "", nil, "", 0, ""},
		{"UC-040/A44 tempo equal none", IntvAdjustMusicTempo, "1.15", "", &tempo, "", 0, ""},
		{"UC-040/A44 controller effect activity target", IntvSendControllerEffect, "", "P1", nil, TemplateActivityRises, 10, ScopeTarget},
		{"UC-040/A44 difficulty easier no_elimination", IntvAdjustGlobalDifficulty, "0.5", "", nil, TemplateNoElimination, 15, ScopeGame},
		{"UC-040/A44 difficulty harder elimination", IntvAdjustGlobalDifficulty, "1.5", "", nil, TemplateElimination, 30, ScopeGame},
		{"UC-040/A44 global sensitivity high no_elimination", IntvAdjustGlobalSensitivity, "4", "", nil, TemplateNoElimination, 15, ScopeGame},
		{"UC-040/A44 global sensitivity low elimination", IntvAdjustGlobalSensitivity, "0", "", nil, TemplateElimination, 30, ScopeGame},
		{"UC-040/A44 pacing calm no_elimination", IntvSetPacingProfile, "calm", "", nil, TemplateNoElimination, 15, ScopeGame},
		{"UC-040/A44 pacing frantic elimination", IntvSetPacingProfile, "frantic", "", nil, TemplateElimination, 30, ScopeGame},
		{"UC-040/A44 audio cue none", IntvPlayAudioCue, "", "", nil, "", 0, ""},
		{"UC-040/A44 eliminate none", IntvEliminatePlayer, "", "P1", nil, "", 0, ""},
		{"UC-040/A44 revive none", IntvRevivePlayer, "", "P1", nil, "", 0, ""},
		{"UC-040/A44 end_game none", IntvEndGame, "", "", nil, "", 0, ""},
		{"UC-040/A44 noop none", IntvNoop, "", "", nil, "", 0, ""},
		{"UC-040/A44 unparseable none", IntvAdjustGlobalDifficulty, "x", "", nil, "", 0, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := LeverPrediction(tc.intervention, tc.value, tc.target, tc.tempo)
			if tc.wantTemplate == "" {
				if got != nil {
					t.Fatalf("got %+v, want nil", *got)
				}
				return
			}
			if got == nil {
				t.Fatalf("got nil, want %s", tc.wantTemplate)
			}
			if got.Template != tc.wantTemplate || got.HorizonS != tc.wantHorizon || got.Scope != tc.wantScope ||
				got.Source != SourceLeverDefault || got.Rule != tc.intervention || got.P != LeverDefaultP {
				t.Fatalf("got %+v", *got)
			}
			if !ValidPrediction(*got) {
				t.Fatalf("lever prediction %+v is not valid", *got)
			}
		})
	}
}

// UC: UC-040 A44 BR-034
// TestValidPrediction: the vocabulary is closed (template, horizon set,
// characteristic, scope, probability).
func TestValidPrediction(t *testing.T) {
	ok := NewPrediction(TemplateSurvives, 15, 0.7, SourceRules, "R4", "P1")
	if !ValidPrediction(ok) {
		t.Fatalf("%+v should be valid", ok)
	}
	for name, mut := range map[string]func(*Prediction){
		"UC-040/A44 BR-034 unknown template":        func(p *Prediction) { p.Template = "wins" },
		"UC-040/A44 BR-034 horizon outside set":     func(p *Prediction) { p.HorizonS = 20 },
		"UC-040/A44 BR-034 wrong characteristic":    func(p *Prediction) { p.Characteristic = CharActivity },
		"UC-040/A44 BR-034 p above 1":               func(p *Prediction) { p.P = 1.2 },
		"UC-040/A44 BR-034 survives without target": func(p *Prediction) { p.Target = "" },
		"UC-040/A44 BR-034 survives as field":       func(p *Prediction) { p.Scope = ScopeField; p.Target = "" },
	} {
		t.Run(name, func(t *testing.T) {
			p := ok
			mut(&p)
			if ValidPrediction(p) {
				t.Fatalf("%+v should be invalid", p)
			}
		})
	}
	for _, tpl := range Templates() {
		target := ""
		if tpl == TemplateSurvives {
			target = "P1"
		}
		p := NewPrediction(tpl, templates[tpl].horizons[0], 0.5, SourceControl, "", target)
		if !ValidPrediction(p) {
			t.Errorf("NewPrediction(%s) = %+v is not valid", tpl, p)
		}
	}
}
