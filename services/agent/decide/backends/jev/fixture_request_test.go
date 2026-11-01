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

// freshTrack is a movement track whose one reading, v, is live at at (the
// context's UpdatedAt), so decide.BuildState keeps the player's readings
// (decide.FreshReadings, UC-040 A50, #2514).
func freshTrack(at time.Time, v float64) []gamecontext.MovementSample {
	return []gamecontext.MovementSample{{At: at, Intensity: v}}
}

// lopsidedContext is a late FFA round: three players left, MOCK0002 dominating
// (low intensity, high skill), MOCK0003 one twitch from elimination, MOCK0004
// in between, MOCK0001 already out. It is the state the recorded fixtures in
// testdata/ were answered against (live_test.go). #2514 gave every alive player a
// one-reading track live at UpdatedAt (freshTrack), so the state keeps their
// readings; the request fixtures' narratives gained the "movement=" token it
// renders, and the recorded responses are unchanged.
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
			"MOCK0002": {Serial: "MOCK0002", Active: boolp(true), MovementIntensity: f64(0.6), MovementVariance: f64(0.05), SkillLevel: f64(0.9), BatteryPct: f64(72), History: freshTrack(at, 0.6)},
			"MOCK0003": {Serial: "MOCK0003", Active: boolp(true), MovementIntensity: f64(2.2), MovementVariance: f64(0.4), SkillLevel: f64(0.25), BatteryPct: f64(64), History: freshTrack(at, 2.2)},
			"MOCK0004": {Serial: "MOCK0004", Active: boolp(true), MovementIntensity: f64(1.4), MovementVariance: f64(0.2), SkillLevel: f64(0.5), BatteryPct: f64(12), History: freshTrack(at, 1.4)},
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

// calmContext is an early, even FFA round: four players alive, everyone well
// below the death threshold, nobody out yet. Doing nothing is right.
func calmContext() gamecontext.GameContext {
	at := time.Date(2026, 10, 16, 12, 0, 0, 0, time.UTC)
	return gamecontext.GameContext{
		SessionID: "game_1c0d5e7a9b42",
		GameKind:  "real",
		UpdatedAt: at,
		Session: gamecontext.SessionSignals{
			DurationSeconds: f64(18),
			GameMode:        strp("FFA"),
			MusicTempo:      f64(1.05),
			DeathThreshold:  f64(2.3),
		},
		Players: map[string]*gamecontext.PlayerSignals{
			"MOCK0001": {Serial: "MOCK0001", Active: boolp(true), MovementIntensity: f64(0.7), MovementVariance: f64(0.1), SkillLevel: f64(0.55), BatteryPct: f64(80), History: freshTrack(at, 0.7)},
			"MOCK0002": {Serial: "MOCK0002", Active: boolp(true), MovementIntensity: f64(0.8), MovementVariance: f64(0.12), SkillLevel: f64(0.5), BatteryPct: f64(75), History: freshTrack(at, 0.8)},
			"MOCK0003": {Serial: "MOCK0003", Active: boolp(true), MovementIntensity: f64(0.9), MovementVariance: f64(0.1), SkillLevel: f64(0.6), BatteryPct: f64(70), History: freshTrack(at, 0.9)},
			"MOCK0004": {Serial: "MOCK0004", Active: boolp(true), MovementIntensity: f64(0.75), MovementVariance: f64(0.09), SkillLevel: f64(0.5), BatteryPct: f64(90), History: freshTrack(at, 0.75)},
		},
	}
}

// calmRequest asks the FFA questions about calmContext.
func calmRequest() decide.Request {
	st := decide.BuildState(calmContext())
	return decide.Request{State: st, Questions: decide.FFAQuestions(st, fullInterventions)}
}

// draggingContext is a long FFA round that has stalled: two careful players
// left, nobody eliminated for two minutes, both far below the threshold. A
// session-wide nudge (tempo, difficulty) is warranted.
func draggingContext() gamecontext.GameContext {
	at := time.Date(2026, 10, 16, 12, 0, 0, 0, time.UTC)
	return gamecontext.GameContext{
		SessionID: "game_5e2b8d0c7f13",
		GameKind:  "real",
		UpdatedAt: at,
		Session: gamecontext.SessionSignals{
			DurationSeconds:     f64(185),
			EliminationSequence: []string{"MOCK0001", "MOCK0004"},
			GameMode:            strp("FFA"),
			MusicTempo:          f64(1.0),
			DeathThreshold:      f64(1.8),
		},
		Players: map[string]*gamecontext.PlayerSignals{
			"MOCK0001": {Serial: "MOCK0001", Active: boolp(false), SkillLevel: f64(0.3)},
			"MOCK0002": {Serial: "MOCK0002", Active: boolp(true), MovementIntensity: f64(0.35), MovementVariance: f64(0.02), SkillLevel: f64(0.85), BatteryPct: f64(60), History: freshTrack(at, 0.35)},
			"MOCK0003": {Serial: "MOCK0003", Active: boolp(true), MovementIntensity: f64(0.4), MovementVariance: f64(0.03), SkillLevel: f64(0.8), BatteryPct: f64(55), History: freshTrack(at, 0.4)},
			"MOCK0004": {Serial: "MOCK0004", Active: boolp(false), SkillLevel: f64(0.4)},
		},
		Timeline: []gamecontext.TimelineEvent{
			{At: at.Add(-170 * time.Second), Kind: gamecontext.EventElimination, Serial: "MOCK0001", Order: 1},
			{At: at.Add(-130 * time.Second), Kind: gamecontext.EventElimination, Serial: "MOCK0004", Order: 2},
		},
	}
}

// draggingRequest asks the FFA questions about draggingContext.
func draggingRequest() decide.Request {
	st := decide.BuildState(draggingContext())
	return decide.Request{State: st, Questions: decide.FFAQuestions(st, fullInterventions)}
}

// detailsRequest is the details-stage request of req for a chosen
// intervention, exactly as Decide builds it after stage 1 (#1262).
func detailsRequest(req decide.Request, iv string) detailsPlan {
	details := splitStages(req).details
	return planDetails(req, decide.Answer{QuestionID: decide.QuestionFFAIntervention, Choice: iv, Confidence: 0.5}, details)
}

// tooFastContext is an FFA round that is collapsing: four of six players out
// within 20 seconds, both survivors near the death threshold. Without a
// session-wide relief (faster music raises the threshold, higher sensitivity)
// the game is over in seconds.
func tooFastContext() gamecontext.GameContext {
	at := time.Date(2026, 10, 16, 12, 0, 0, 0, time.UTC)
	return gamecontext.GameContext{
		SessionID: "game_9a4f1e6b2c80",
		GameKind:  "real",
		UpdatedAt: at,
		Session: gamecontext.SessionSignals{
			DurationSeconds:     f64(20),
			EliminationSequence: []string{"MOCK0001", "MOCK0004", "MOCK0005", "MOCK0006"},
			GameMode:            strp("FFA"),
			MusicTempo:          f64(1.0),
			DeathThreshold:      f64(1.5),
		},
		Players: map[string]*gamecontext.PlayerSignals{
			"MOCK0001": {Serial: "MOCK0001", Active: boolp(false)},
			"MOCK0002": {Serial: "MOCK0002", Active: boolp(true), MovementIntensity: f64(1.35), MovementVariance: f64(0.3), SkillLevel: f64(0.5), History: freshTrack(at, 1.35)},
			"MOCK0003": {Serial: "MOCK0003", Active: boolp(true), MovementIntensity: f64(1.4), MovementVariance: f64(0.35), SkillLevel: f64(0.45), History: freshTrack(at, 1.4)},
			"MOCK0004": {Serial: "MOCK0004", Active: boolp(false)},
			"MOCK0005": {Serial: "MOCK0005", Active: boolp(false)},
			"MOCK0006": {Serial: "MOCK0006", Active: boolp(false)},
		},
		Timeline: []gamecontext.TimelineEvent{
			{At: at.Add(-16 * time.Second), Kind: gamecontext.EventElimination, Serial: "MOCK0001", Order: 1},
			{At: at.Add(-12 * time.Second), Kind: gamecontext.EventElimination, Serial: "MOCK0004", Order: 2},
			{At: at.Add(-7 * time.Second), Kind: gamecontext.EventElimination, Serial: "MOCK0005", Order: 3},
			{At: at.Add(-3 * time.Second), Kind: gamecontext.EventElimination, Serial: "MOCK0006", Order: 4},
		},
	}
}

// tooFastRequest asks the FFA questions about tooFastContext.
func tooFastRequest() decide.Request {
	st := decide.BuildState(tooFastContext())
	return decide.Request{State: st, Questions: decide.FFAQuestions(st, fullInterventions)}
}
