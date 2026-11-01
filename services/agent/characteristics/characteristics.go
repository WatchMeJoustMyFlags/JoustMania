// Package characteristics is the one measurable vocabulary of a game (#2479,
// UC-040 A44, BR-034): duration, activity, change, elimination pace, survival and
// closeness, each measured over a time window from the agent's GameContext.
// Predictions are scored with these functions, and goal profiles (later) score the
// same functions, so the two can never drift.
//
// It is a pure leaf: it imports gamecontext only. Every time is agent receipt time
// (the GameContext timestamps). A window is (From, To]: a sample at From belongs to
// the window before.
package characteristics

import (
	"math"
	"sort"
	"strings"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// Window is the half-open interval (From, To].
type Window struct {
	From, To time.Time
}

// Seconds is the window length.
func (w Window) Seconds() float64 { return w.To.Sub(w.From).Seconds() }

func (w Window) contains(t time.Time) bool { return t.After(w.From) && !t.After(w.To) }

// MinCoverage is the share of a window's 1 s buckets a player must have samples
// for; below it the player's window is data_missing.
const MinCoverage = 0.5

// MinFieldPlayers is how many covered players a field measure needs.
const MinFieldPlayers = 2

// changeSubWindow is the sub-window length of Change.
const changeSubWindow = 5 * time.Second

// Duration is the game seconds elapsed, false when not observed.
func Duration(c gamecontext.GameContext) (float64, bool) {
	if c.Session.DurationSeconds == nil {
		return 0, false
	}
	return *c.Session.DurationSeconds, true
}

// Alive reports whether serial is alive: Active and not in the elimination
// sequence. known is false when the player is not in the context or Active was
// never observed.
func Alive(c gamecontext.GameContext, serial string) (alive, known bool) {
	if eliminated(c, serial) {
		return false, true
	}
	p := c.Players[serial]
	if p == nil || p.Active == nil {
		return false, false
	}
	return *p.Active, true
}

func eliminated(c gamecontext.GameContext, serial string) bool {
	for _, s := range c.Session.EliminationSequence {
		if s == serial {
			return true
		}
	}
	return false
}

// RespawnMode reports whether c's game mode keeps a dying player in the game:
// NonStop (a respawn), Zombies (a human turns zombie, a zombie respawns), Swapper
// (a swap) and Fight Club (a round loser goes back to the queue). There the
// elimination sequence holds only the players who left for good (#2517, #2524),
// so the elimination-based measures (survival, elimination_pace) say nothing about
// play and are not scored (UC-040 BR-043, #2532). The mode is the coordinator's
// get_game_name() ("Nonstop Joust", "Zombie", "Swapper", "Fight Club"; pinned by
// tests/contract/test_respawn_mode_names.py) or a menu alias; an unobserved mode
// is not a respawn mode.
func RespawnMode(c gamecontext.GameContext) bool {
	if c.Session.GameMode == nil {
		return false
	}
	switch strings.ToLower(strings.TrimSpace(*c.Session.GameMode)) {
	case "nonstop joust", "nonstop", "non stop joust", "zombie", "zombies", "swapper",
		"fight club", "fightclub", "fight_club":
		return true
	}
	return false
}

// EliminatedAt is when serial's elimination entered the timeline, false when the
// timeline holds no elimination of serial (not eliminated, or the ring evicted it).
func EliminatedAt(c gamecontext.GameContext, serial string) (time.Time, bool) {
	for _, ev := range c.Timeline {
		if ev.Kind == gamecontext.EventElimination && ev.Serial == serial {
			return ev.At, true
		}
	}
	return time.Time{}, false
}

// Eliminations counts the timeline eliminations inside w. complete is false when
// the timeline may have lost entries of w: its oldest entry is after w.From while
// the ring is full, or it is empty although the elimination sequence is not.
func Eliminations(c gamecontext.GameContext, w Window) (n int, complete bool) {
	for _, ev := range c.Timeline {
		if ev.Kind == gamecontext.EventElimination && w.contains(ev.At) {
			n++
		}
	}
	complete = true
	switch {
	case len(c.Timeline) == 0:
		complete = len(c.Session.EliminationSequence) == 0
	case c.Timeline[0].At.After(w.From) && len(c.Timeline) >= TimelineCap:
		complete = false
	}
	return n, complete
}

// TimelineCap mirrors the gamecontext timeline ring size (64): a full ring may have
// evicted older entries.
const TimelineCap = 64

// EliminationPace is the eliminations per minute inside w.
func EliminationPace(c gamecontext.GameContext, w Window) (float64, bool) {
	n, complete := Eliminations(c, w)
	if !complete || w.Seconds() <= 0 {
		return 0, false
	}
	return float64(n) / w.Seconds() * 60, true
}

// PlayerActivity is the mean of intensity / death threshold over serial's samples
// inside w, each sample read against the threshold in force at its time. ok is
// false when the player has samples for fewer than MinCoverage of the window's
// seconds (a sample without a known threshold does not count).
func PlayerActivity(c gamecontext.GameContext, serial string, w Window) (float64, bool) {
	p := c.Players[serial]
	if p == nil || w.Seconds() <= 0 {
		return 0, false
	}
	sum, n := 0.0, 0
	seconds := map[int64]bool{}
	for _, s := range p.History {
		if !w.contains(s.At) {
			continue
		}
		th := gamecontext.ThresholdAt(c.SpeedTrack, c.Session.DeathThreshold, s.At)
		if th == nil || *th <= 0 || math.IsNaN(s.Intensity) || math.IsInf(s.Intensity, 0) {
			continue
		}
		sum += s.Intensity / *th
		n++
		seconds[s.At.Unix()] = true
	}
	// Coverage counts distinct 1 s buckets: the history may hold more than one
	// sample per second.
	if n == 0 || float64(len(seconds)) < MinCoverage*w.Seconds() {
		return 0, false
	}
	return sum / float64(n), true
}

// alivePlayers returns the alive serials, sorted.
func alivePlayers(c gamecontext.GameContext) []string {
	out := make([]string, 0, len(c.Players))
	for serial := range c.Players {
		if alive, _ := Alive(c, serial); alive {
			out = append(out, serial)
		}
	}
	sort.Strings(out)
	return out
}

// AlivePlayers returns the alive serials, sorted.
func AlivePlayers(c gamecontext.GameContext) []string { return alivePlayers(c) }

// CoveredPlayers returns the alive players covered in w, sorted: the fixed player
// set a field measure is taken over before and after a decision.
func CoveredPlayers(c gamecontext.GameContext, w Window) []string {
	var out []string
	for _, serial := range alivePlayers(c) {
		if _, ok := PlayerActivity(c, serial, w); ok {
			out = append(out, serial)
		}
	}
	return out
}

// perPlayer returns the activity of each of serials covered in w; ok is false
// when one of them is no longer alive (the set changed) or not covered.
func perPlayer(c gamecontext.GameContext, serials []string, w Window) ([]float64, bool) {
	out := make([]float64, 0, len(serials))
	for _, serial := range serials {
		if alive, _ := Alive(c, serial); !alive {
			return nil, false
		}
		v, ok := PlayerActivity(c, serial, w)
		if !ok {
			return nil, false
		}
		out = append(out, v)
	}
	return out, true
}

// FieldActivity is the mean activity of the alive players covered in w; false
// with fewer than MinFieldPlayers covered players.
func FieldActivity(c gamecontext.GameContext, w Window) (float64, bool) {
	return FieldActivityOf(c, CoveredPlayers(c, w), w)
}

// FieldActivityOf is the mean activity of exactly serials in w; false with fewer
// than MinFieldPlayers serials, or when one of them died or is not covered.
func FieldActivityOf(c gamecontext.GameContext, serials []string, w Window) (float64, bool) {
	vals, ok := perPlayer(c, serials, w)
	if !ok {
		return 0, false
	}
	if len(vals) < MinFieldPlayers {
		return 0, false
	}
	sum := 0.0
	for _, v := range vals {
		sum += v
	}
	return sum / float64(len(vals)), true
}

// Closeness is the spread (max - min) of per-player activity among the alive
// players covered in w: lower is closer. False with fewer than MinFieldPlayers.
func Closeness(c gamecontext.GameContext, w Window) (float64, bool) {
	return ClosenessOf(c, CoveredPlayers(c, w), w)
}

// ClosenessOf is the spread of exactly serials in w; false as FieldActivityOf.
func ClosenessOf(c gamecontext.GameContext, serials []string, w Window) (float64, bool) {
	vals, ok := perPlayer(c, serials, w)
	if !ok || len(vals) < MinFieldPlayers {
		return 0, false
	}
	lo, hi := vals[0], vals[0]
	for _, v := range vals[1:] {
		lo, hi = math.Min(lo, v), math.Max(hi, v)
	}
	return hi - lo, true
}

// Change is the swing of field activity inside w: max - min of the field activity
// of its consecutive 5 s sub-windows. False when fewer than two sub-windows can
// be measured.
func Change(c gamecontext.GameContext, w Window) (float64, bool) {
	var vals []float64
	for from := w.From; from.Add(changeSubWindow).Compare(w.To) <= 0; from = from.Add(changeSubWindow) {
		if v, ok := FieldActivity(c, Window{From: from, To: from.Add(changeSubWindow)}); ok {
			vals = append(vals, v)
		}
	}
	if len(vals) < 2 {
		return 0, false
	}
	lo, hi := vals[0], vals[0]
	for _, v := range vals[1:] {
		lo, hi = math.Min(lo, v), math.Max(hi, v)
	}
	return hi - lo, true
}
