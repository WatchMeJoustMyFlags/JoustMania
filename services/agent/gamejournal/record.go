// Package gamejournal is the agent's calibration journal (#1055, UC-047 step 1,
// BR-009): one small record per real game end, kept on the device's
// agent-experiments volume, so a later calibration reads its eligibility inputs
// (BR-002) and holdout metrics (BR-006) from the device itself. VictoriaMetrics
// keeps only 7 days on the Pi, shorter than the window N = 100 eligible games
// needs (maintainer decision 2026-11-01).
//
// This package only collects. It runs no fit, decides no eligibility and serves
// no endpoint; those belong to the later UC-047 work.
//
// No player identity (C-009): a record holds per-player numbers but never a
// serial, so nothing in it ties a value to a controller across games.
package gamejournal

import (
	"math"
	"sort"
	"time"

	"github.com/joustmania/agent/characteristics"
	"github.com/joustmania/agent/gamecontext"
)

// SchemaVersion is the record layout version. A reader skips a line with a
// version it does not know.
const SchemaVersion = 1

// Player is one player of a journaled game: whole-game numbers only, no serial.
type Player struct {
	// MeanMovementVariance is the mean of the player's 1 s movement-variance
	// samples over the game (BR-002 "moved" and "dominates"); nil without one.
	MeanMovementVariance *float64 `json:"mean_movement_variance"`
	VarianceSamples      int      `json:"variance_samples"`
	// LastMovementVariance is the last variance sample (frozen at elimination),
	// the value A22's movement_variance reads (last_over_time).
	LastMovementVariance *float64 `json:"last_movement_variance"`
	// MeanIntensity is the mean of the player's 1 s intensity samples (A22 intensity).
	MeanIntensity    *float64 `json:"mean_intensity"`
	IntensitySamples int      `json:"intensity_samples"`
	Eliminated       bool     `json:"eliminated"`
}

// Metrics are the BR-006 fit metrics, measured per game. A metric the agent
// cannot measure from its game context is null: near_death_rate always (the
// agent does not ingest the coordinator's near-death counter), activity and
// change without a death threshold or with fewer than two sampled players.
type Metrics struct {
	Intensity        *float64 `json:"intensity"`
	MovementVariance *float64 `json:"movement_variance"`
	NearDeathRate    *float64 `json:"near_death_rate"`
	EliminationPace  *float64 `json:"elimination_pace"`
	Activity         *float64 `json:"activity"`
	Change           *float64 `json:"change"`
}

// Record is one real game end.
type Record struct {
	SchemaVersion   int       `json:"schema_version"`
	GameID          string    `json:"game_id"`
	Mode            string    `json:"mode"`
	EndedAt         time.Time `json:"ended_at"`
	DurationSeconds *float64  `json:"duration_seconds"`
	// TestSession is calibration.test_session at the game end; null when the flag
	// could not be read (UC-047 BR-003: such a game is never treated as non-test).
	TestSession  *bool    `json:"test_session"`
	Eliminations int      `json:"eliminations"`
	Players      []Player `json:"players"`
	// HistoryTruncated is true when a player's 1 s history ring was full and
	// started after the game start, so activity and change cover only the last
	// gamecontext.PlayerHistoryCap seconds. The per-player means never truncate.
	HistoryTruncated bool    `json:"history_truncated"`
	Metrics          Metrics `json:"metrics"`
}

// BuildRecord turns a game-end snapshot into its journal record. testSession is
// the calibration.test_session value read at the end (nil = unreadable); now is
// the fallback end time when the snapshot's timeline holds no "end" event.
func BuildRecord(gc gamecontext.GameContext, testSession *bool, now time.Time) Record {
	end := endTime(gc, now)
	rec := Record{
		SchemaVersion:   SchemaVersion,
		GameID:          gc.SessionID,
		EndedAt:         end.UTC(),
		DurationSeconds: copyFloat(gc.Session.DurationSeconds),
		TestSession:     testSession,
		Eliminations:    len(gc.Session.EliminationSequence),
	}
	if gc.Session.GameMode != nil {
		rec.Mode = *gc.Session.GameMode
	}
	var start time.Time
	if d := gc.Session.DurationSeconds; d != nil && *d > 0 {
		start = end.Add(-time.Duration(*d * float64(time.Second)))
	}

	eliminated := make(map[string]bool, len(gc.Session.EliminationSequence))
	for _, s := range gc.Session.EliminationSequence {
		eliminated[s] = true
	}
	var intensities, variances []float64
	for _, p := range gc.Players {
		if p == nil {
			continue
		}
		pl := Player{
			VarianceSamples:      p.Game.VarianceSamples,
			IntensitySamples:     p.Game.IntensitySamples,
			LastMovementVariance: copyFloat(p.MovementVariance),
			Eliminated:           eliminated[p.Serial],
		}
		if v, ok := p.Game.MeanVariance(); ok {
			pl.MeanMovementVariance = &v
			variances = append(variances, v)
		}
		if v, ok := p.Game.MeanIntensity(); ok {
			pl.MeanIntensity = &v
			intensities = append(intensities, v)
		}
		if !start.IsZero() && len(p.History) >= gamecontext.PlayerHistoryCap &&
			p.History[0].At.After(start.Add(time.Second)) {
			rec.HistoryTruncated = true
		}
		rec.Players = append(rec.Players, pl)
	}
	sortPlayers(rec.Players)

	rec.Metrics.Intensity = mean(intensities)
	rec.Metrics.MovementVariance = mean(variances)
	if d := gc.Session.DurationSeconds; d != nil && *d > 0 {
		pace := float64(rec.Eliminations) / (*d / 60)
		rec.Metrics.EliminationPace = &pace
	}
	if !start.IsZero() {
		rec.Metrics.Activity, rec.Metrics.Change = fieldMeasures(gc, start, end)
	}
	return rec
}

// endTime is the timeline's game-end event (the store stamps it just before the
// game-end snapshot), else now.
func endTime(gc gamecontext.GameContext, now time.Time) time.Time {
	for i := len(gc.Timeline) - 1; i >= 0; i-- {
		ev := gc.Timeline[i]
		if ev.Kind == gamecontext.EventPhase && ev.Detail == "end" {
			return ev.At
		}
	}
	return now
}

// fieldMeasures ports A22's activity and change (scripts/agent_shadow_realism.py
// game_measures, decide-contract §14.1) to the agent's 1 s history, so the
// journal and the realism tool measure a game the same way: each second's field
// activity is the mean of intensity / death threshold over the players alive and
// sampled in that second (at least 2); activity is the mean over the seconds;
// change is the mean, over 5 s steps, of the swing (max - min) of three
// consecutive 5 s sub-window means, a sub-window counting with at least half its
// seconds. Like A22 it reads one threshold per game (the last effective one).
func fieldMeasures(gc gamecontext.GameContext, start, end time.Time) (activity, change *float64) {
	th := gc.Session.DeathThreshold
	if th == nil || *th <= 0 || math.IsNaN(*th) || math.IsInf(*th, 0) {
		return nil, nil
	}
	type series struct {
		norm   map[int64]float64
		elimAt float64
		elim   bool
	}
	players := make([]series, 0, len(gc.Players))
	for serial, p := range gc.Players {
		if p == nil || len(p.History) == 0 {
			continue
		}
		s := series{norm: make(map[int64]float64, len(p.History))}
		for _, sm := range p.History {
			if math.IsNaN(sm.Intensity) || math.IsInf(sm.Intensity, 0) {
				continue
			}
			s.norm[sm.At.Unix()] = sm.Intensity / *th
		}
		if at, ok := characteristics.EliminatedAt(gc, serial); ok {
			s.elim, s.elimAt = true, unixSeconds(at)
		}
		players = append(players, s)
	}

	startS, endS := unixSeconds(start), unixSeconds(end)
	first, last := int64(math.Ceil(startS)), int64(math.Floor(endS))
	field := map[int64]float64{}
	var fieldVals []float64
	for t := first; t <= last; t++ {
		sum, n := 0.0, 0
		for _, s := range players {
			v, ok := s.norm[t]
			if !ok || (s.elim && float64(t) >= s.elimAt) {
				continue
			}
			sum += v
			n++
		}
		if n >= 2 {
			field[t] = sum / float64(n)
			fieldVals = append(fieldVals, field[t])
		}
	}
	activity = mean(fieldVals)

	subMean := func(a, b int64) (float64, bool) {
		sum, n := 0.0, 0
		for t := a + 1; t <= b; t++ {
			if v, ok := field[t]; ok {
				sum += v
				n++
			}
		}
		if n == 0 || int64(n)*2 < b-a {
			return 0, false
		}
		return sum / float64(n), true
	}
	var swings []float64
	for t := first + 15; t <= last; t += 5 {
		a, okA := subMean(t-15, t-10)
		b, okB := subMean(t-10, t-5)
		c, okC := subMean(t-5, t)
		if okA && okB && okC {
			swings = append(swings, math.Max(a, math.Max(b, c))-math.Min(a, math.Min(b, c)))
		}
	}
	return activity, mean(swings)
}

// sortPlayers orders players by mean movement variance, highest first, players
// without one last, so a record is deterministic without naming a serial.
func sortPlayers(ps []Player) {
	sort.SliceStable(ps, func(i, j int) bool {
		a, b := ps[i].MeanMovementVariance, ps[j].MeanMovementVariance
		switch {
		case a == nil:
			return false
		case b == nil:
			return true
		default:
			return *a > *b
		}
	})
}

func unixSeconds(t time.Time) float64 { return float64(t.UnixNano()) / 1e9 }

func mean(xs []float64) *float64 {
	if len(xs) == 0 {
		return nil
	}
	s := 0.0
	for _, x := range xs {
		s += x
	}
	m := s / float64(len(xs))
	return &m
}

func copyFloat(p *float64) *float64 {
	if p == nil {
		return nil
	}
	v := *p
	return &v
}
