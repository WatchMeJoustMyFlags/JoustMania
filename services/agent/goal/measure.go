package goal

import (
	"math"
	"time"

	"github.com/joustmania/agent/characteristics"
	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gamesummary"
)

// Sampling (decide-contract.md §14.4): the movement history holds only about 32 s,
// so a whole-game activity, closeness or change is the mean of samples taken
// during the game, every SampleEvery of game time, each with the same
// characteristics functions the predictions use.
const (
	// SampleEvery is the game-time interval between two samples.
	SampleEvery = 5 * time.Second
	// SampleWindow is the window of one activity / closeness sample.
	SampleWindow = 10 * time.Second
	// ChangeWindow is the window of one change sample (three 5 s sub-windows).
	ChangeWindow = 15 * time.Second
	// endgameAlive is the endgame phase: at most this many players alive.
	endgameAlive = 2
)

type stat struct {
	sum float64
	n   int
}

func (s *stat) add(v float64) { s.sum += v; s.n++ }

func (s stat) mean() (float64, bool) {
	if s.n == 0 {
		return 0, false
	}
	return s.sum / float64(s.n), true
}

// Accumulator is one game's running goal measurements. The zero value is ready.
// It is not safe for concurrent use: the prediction Book guards it with its lock.
type Accumulator struct {
	nextSampleS float64
	whole       map[string]*stat
	endgame     map[string]*stat
	// endgameAtS is the game second the endgame was first observed (nil: not yet);
	// endgameElims the elimination count then.
	endgameAtS   *float64
	endgameElims int
}

func (a *Accumulator) statFor(phase, char string) *stat {
	m := &a.whole
	if phase == PhaseEndgame {
		m = &a.endgame
	}
	if *m == nil {
		*m = map[string]*stat{}
	}
	s, ok := (*m)[char]
	if !ok {
		s = &stat{}
		(*m)[char] = s
	}
	return s
}

// Sample takes the due sample of c at now (agent receipt time): at most one per
// SampleEvery of game time. It records field activity and closeness over the last
// SampleWindow and change over the last ChangeWindow, into the whole game and,
// with 1 or 2 players alive, the endgame too. A measure without enough data
// (characteristics: data_missing) is skipped.
func (a *Accumulator) Sample(c gamecontext.GameContext, now time.Time) {
	dur, ok := characteristics.Duration(c)
	if !ok || dur < a.nextSampleS {
		return
	}
	a.nextSampleS = dur + SampleEvery.Seconds()
	alive := len(characteristics.AlivePlayers(c))
	endgame := alive >= 1 && alive <= endgameAlive
	if endgame && a.endgameAtS == nil {
		at := dur
		a.endgameAtS, a.endgameElims = &at, len(c.Session.EliminationSequence)
	}
	w := characteristics.Window{From: now.Add(-SampleWindow), To: now}
	record := func(char string, v float64, ok bool) {
		if !ok {
			return
		}
		a.statFor(PhaseWhole, char).add(v)
		if endgame {
			a.statFor(PhaseEndgame, char).add(v)
		}
	}
	v, ok := characteristics.FieldActivity(c, w)
	record(decide.CharActivity, v, ok)
	v, ok = characteristics.Closeness(c, w)
	record(decide.CharCloseness, v, ok)
	v, ok = characteristics.Change(c, characteristics.Window{From: now.Add(-ChangeWindow), To: now})
	record(decide.CharChange, v, ok)
}

// Sampled reports whether at least one sample was taken (tests, diagnostics).
func (a *Accumulator) Sampled() bool { return a.nextSampleS > 0 }

// Measure is the value of char in phase for c (the live context, or the game-end
// snapshot): duration and elimination_pace from c (whole game, or since the
// endgame began), activity, closeness and change as the mean of the samples.
// survival has no game-level value. ok is false without data.
func (a *Accumulator) Measure(c gamecontext.GameContext, char, phase string) (float64, bool) {
	switch char {
	case decide.CharDuration, decide.CharEliminationPace:
		dur, ok := characteristics.Duration(c)
		if !ok {
			return 0, false
		}
		elims := len(c.Session.EliminationSequence)
		if phase == PhaseEndgame {
			if a.endgameAtS == nil {
				return 0, false
			}
			dur -= *a.endgameAtS
			elims -= a.endgameElims
		}
		if char == decide.CharDuration {
			return math.Max(0, dur), true
		}
		if dur <= 0 {
			return 0, false
		}
		return float64(elims) / dur * 60, true
	case decide.CharActivity, decide.CharCloseness, decide.CharChange:
		m := a.whole
		if phase == PhaseEndgame {
			m = a.endgame
		}
		if s, ok := m[char]; ok {
			return s.mean()
		}
	}
	return 0, false
}

// Clone is a deep copy (the prompt measures a copy outside the Book's lock).
func (a *Accumulator) Clone() Accumulator {
	out := *a
	out.whole, out.endgame = cloneStats(a.whole), cloneStats(a.endgame)
	if a.endgameAtS != nil {
		at := *a.endgameAtS
		out.endgameAtS = &at
	}
	return out
}

func cloneStats(m map[string]*stat) map[string]*stat {
	if m == nil {
		return nil
	}
	out := make(map[string]*stat, len(m))
	for k, s := range m {
		cp := *s
		out[k] = &cp
	}
	return out
}

// scoreFloor is the smallest distance scale per characteristic, so a band edge at
// or near 0 still decays over a sensible range.
var scoreFloor = map[string]float64{
	decide.CharDuration:        30,
	decide.CharEliminationPace: 1,
	decide.CharActivity:        0.1,
	decide.CharChange:          0.1,
	decide.CharCloseness:       0.1,
	decide.CharSurvival:        1,
}

// TermScore is 1 inside the band (edges included), otherwise
// max(0, 1 - d / max(|edge|, floor)) with d the distance to the violated edge.
func TermScore(t Term, v float64) float64 {
	var edge float64
	switch {
	case t.Min != nil && v < *t.Min:
		edge = *t.Min
	case t.Max != nil && v > *t.Max:
		edge = *t.Max
	default:
		return 1
	}
	scale := math.Max(math.Abs(edge), scoreFloor[t.Characteristic])
	return math.Max(0, 1-math.Abs(v-edge)/scale)
}

// TermResult is one term with its measured value and score (Measured false: no
// data, the term is left out of the attainment).
type TermResult struct {
	Term
	Value    float64
	Score    float64
	Measured bool
}

// Result is a profile scored against one game.
type Result struct {
	Profile    Profile
	Terms      []TermResult
	Attainment float64
	// HasAttainment is false when no term could be measured.
	HasAttainment bool
}

// Score scores every term of p with measure and takes the weighted mean of the
// measured terms as the attainment.
func Score(p Profile, measure func(char, phase string) (float64, bool)) Result {
	r := Result{Profile: p, Terms: make([]TermResult, 0, len(p.Terms))}
	sum, weights := 0.0, 0.0
	for _, t := range p.Terms {
		tr := TermResult{Term: t}
		if v, ok := measure(t.Characteristic, t.Phase); ok {
			tr.Value, tr.Measured, tr.Score = v, true, TermScore(t, v)
			sum += t.Weight * tr.Score
			weights += t.Weight
		}
		r.Terms = append(r.Terms, tr)
	}
	if weights > 0 {
		r.Attainment, r.HasAttainment = sum/weights, true
	}
	return r
}

// Summary is r in the game summary's shape (UC-042 A12).
func (r Result) Summary() *gamesummary.Goal {
	g := &gamesummary.Goal{
		Profile: r.Profile.Name, FromObjectives: r.Profile.FromObjectives,
		Terms: make([]gamesummary.GoalTerm, 0, len(r.Terms)),
	}
	if r.HasAttainment {
		a := r.Attainment
		g.Attainment = &a
	}
	for _, t := range r.Terms {
		gt := gamesummary.GoalTerm{
			Characteristic: t.Characteristic, Phase: t.Phase, Weight: t.Weight,
			Min: copyPtr(t.Min), Max: copyPtr(t.Max),
		}
		if t.Measured {
			v, s := t.Value, t.Score
			gt.Value, gt.Score = &v, &s
		}
		g.Terms = append(g.Terms, gt)
	}
	return g
}

func copyPtr(p *float64) *float64 {
	if p == nil {
		return nil
	}
	v := *p
	return &v
}
