package jev

import (
	"time"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/gamecontext"
)

// fullInterventions is the "full" interventions_allowed variant of
// services/flagd/agent.json.
var fullInterventions = []string{
	"play_audio_cue", "send_controller_effect", "adjust_volume", "adjust_music_tempo",
	"set_pacing_profile", "adjust_player_sensitivity", "grant_shield", "adjust_global_sensitivity",
	"adjust_global_difficulty", "eliminate_player", "revive_player", "end_game",
}

func f64(v float64) *float64 { return &v }
func boolp(v bool) *bool     { return &v }
func strp(v string) *string  { return &v }

// lopsidedContext is a late FFA round: three players left, MOCK0002 dominating
// (low intensity, high skill), MOCK0003 one twitch from elimination, MOCK0004
// in between, MOCK0001 already out. It is the state the recorded fixtures in
// testdata/ were answered against (live_test.go).
func lopsidedContext() gamecontext.GameContext {
	at := time.Date(2026, 10, 16, 12, 0, 0, 0, time.UTC)
	return gamecontext.GameContext{
		SessionID: "game_7f3a9c21d4e8",
		GameKind:  "real",
		UpdatedAt: at,
		Session: gamecontext.SessionSignals{
			DurationSeconds:     f64(74),
			EliminationSequence: []string{"MOCK0001"},
			GameMode:            strp("FFA"),
			MusicTempo:          f64(1.15),
			DeathThreshold:      f64(2.3),
		},
		Players: map[string]*gamecontext.PlayerSignals{
			"MOCK0001": {Serial: "MOCK0001", Active: boolp(false), SkillLevel: f64(0.2), BatteryPct: f64(80)},
			"MOCK0002": {Serial: "MOCK0002", Active: boolp(true), MovementIntensity: f64(0.6), MovementVariance: f64(0.05), SkillLevel: f64(0.9), BatteryPct: f64(72)},
			"MOCK0003": {Serial: "MOCK0003", Active: boolp(true), MovementIntensity: f64(2.2), MovementVariance: f64(0.4), SkillLevel: f64(0.25), BatteryPct: f64(64)},
			"MOCK0004": {Serial: "MOCK0004", Active: boolp(true), MovementIntensity: f64(1.4), MovementVariance: f64(0.2), SkillLevel: f64(0.5), BatteryPct: f64(12)},
		},
		Timeline: []gamecontext.TimelineEvent{
			{At: at.Add(-50 * time.Second), Kind: gamecontext.EventElimination, Serial: "MOCK0001", Order: 1},
		},
	}
}

// testRequest is the FFA request the fixtures in testdata/ answer: the
// lopsided state and the "full" allow-list. Context carries a sentinel that
// must never reach the wire.
func testRequest() decide.Request {
	st := decide.BuildState(lopsidedContext())
	return decide.Request{
		State:     st,
		Questions: decide.FFAQuestions(st, fullInterventions),
		Context:   &gamecontext.GameContext{SessionID: "CONTEXT-SENTINEL"},
	}
}

// noulRequest asks two yes/no questions about the same state (#1229 KindNoul).
func noulRequest() decide.Request {
	st := decide.BuildState(lopsidedContext())
	return decide.Request{
		State: st,
		Questions: []decide.Question{
			{ID: "lopsided", Kind: decide.KindNoul, Text: "Is one player clearly dominating the remaining players?"},
			{ID: "about_to_end", Kind: decide.KindNoul, Text: "Is the round likely to end within the next few seconds?"},
		},
	}
}
