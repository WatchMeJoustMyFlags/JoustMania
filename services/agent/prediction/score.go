package prediction

import (
	"time"

	"github.com/joustmania/agent/characteristics"
	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/gamecontext"
)

// score.go holds the pure per-template scoring (#2479, UC-040 A44, BR-034). Every
// time is agent receipt time; an elimination counts for a horizon when its timeline
// time is at or before registration + horizon, so the 3 s grace of the horizon
// check (HorizonGrace) only absorbs the export lag, it never widens the horizon.

// Outcomes.
const (
	OutcomeHit        = "hit"
	OutcomeMiss       = "miss"
	OutcomeUnscorable = "unscorable"
)

// Unscorable reasons.
const (
	ReasonGameEnded      = "game_ended"
	ReasonDataMissing    = "data_missing"
	ReasonStaleEnd       = "stale_end"
	ReasonOverflow       = "overflow"
	ReasonNoTarget       = "no_target"
	ReasonTargetNotAlive = "target_not_alive"
)

// riseFactor / closerFactor are the activity_rises and closeness_rises margins.
const (
	riseFactor   = 1.05
	closerFactor = 0.95
)

// baseline is what a template needs from decision time.
type baseline struct {
	at        time.Time
	horizon   time.Duration
	elimCount int
	// before is the activity (activity_rises) or the spread (closeness_rises) over
	// (at - horizon, at]; beforeOK false means data_missing.
	before   float64
	beforeOK bool
	// players is the fixed player set of a field measure (activity_rises field,
	// closeness_rises): the alive players covered before the decision. The after
	// window is measured on the same set; if one of them died it is data_missing.
	players []string
}

func (b baseline) horizonEnd() time.Time { return b.at.Add(b.horizon) }

// verdict is one scoring result; decided false means "not yet".
type verdict struct {
	decided      bool
	outcome      string
	reason       string
	before       *float64
	after        *float64
	eliminations *int
	approx       bool
}

func hit() verdict  { return verdict{decided: true, outcome: OutcomeHit} }
func miss() verdict { return verdict{decided: true, outcome: OutcomeMiss} }
func unscorable(reason string) verdict {
	return verdict{decided: true, outcome: OutcomeUnscorable, reason: reason}
}

// takeBaseline captures the decision-time values p needs from c at t.
func takeBaseline(p decide.Prediction, c gamecontext.GameContext, t time.Time) baseline {
	h := time.Duration(p.HorizonS) * time.Second
	b := baseline{at: t, horizon: h, elimCount: len(c.Session.EliminationSequence)}
	before := characteristics.Window{From: t.Add(-h), To: t}
	switch p.Template {
	case decide.TemplateActivityRises:
		if p.Scope == decide.ScopeTarget {
			b.before, b.beforeOK = characteristics.PlayerActivity(c, p.Target, before)
		} else {
			b.players = characteristics.CoveredPlayers(c, before)
			b.before, b.beforeOK = characteristics.FieldActivityOf(c, b.players, before)
		}
	case decide.TemplateClosenessRises:
		b.players = characteristics.CoveredPlayers(c, before)
		b.before, b.beforeOK = characteristics.ClosenessOf(c, b.players, before)
	}
	return b
}

// registrationVerdict scores what is already settled at registration: a target
// template without a target, or with a target that is not alive.
func registrationVerdict(p decide.Prediction, c gamecontext.GameContext) verdict {
	if p.Scope != decide.ScopeTarget {
		return verdict{}
	}
	if p.Target == "" {
		return unscorable(ReasonNoTarget)
	}
	if alive, _ := characteristics.Alive(c, p.Target); !alive {
		return unscorable(ReasonTargetNotAlive)
	}
	return verdict{}
}

// horizonEliminations counts the eliminations in (at, until] (until capped at the
// horizon end). When the timeline may have lost entries it falls back to the
// growth of the elimination sequence since registration, flagged approx.
func horizonEliminations(b baseline, c gamecontext.GameContext, until time.Time) (int, bool) {
	if until.After(b.horizonEnd()) {
		until = b.horizonEnd()
	}
	n, complete := characteristics.Eliminations(c, characteristics.Window{From: b.at, To: until})
	if complete {
		return n, false
	}
	return max(0, len(c.Session.EliminationSequence)-b.elimCount), true
}

// targetEliminatedBy reports whether the target's elimination is known at or
// before until (from the timeline; when the timeline lost it, membership in the
// elimination sequence counts, flagged approx).
func targetEliminatedBy(p decide.Prediction, c gamecontext.GameContext, until time.Time) (bool, bool) {
	if at, ok := characteristics.EliminatedAt(c, p.Target); ok {
		return !at.After(until), false
	}
	for _, s := range c.Session.EliminationSequence {
		if s == p.Target {
			return true, true
		}
	}
	return false, false
}

// earlyVerdict decides what can be decided before the horizon from c: a survives
// or no_elimination miss, an elimination hit.
func earlyVerdict(p decide.Prediction, b baseline, c gamecontext.GameContext, now time.Time) verdict {
	switch p.Template {
	case decide.TemplateSurvives:
		if gone, approx := targetEliminatedBy(p, c, b.horizonEnd()); gone {
			v := miss()
			v.approx = approx
			return v
		}
	case decide.TemplateNoElimination, decide.TemplateElimination:
		n, approx := horizonEliminations(b, c, now)
		if n == 0 {
			return verdict{}
		}
		v := miss()
		if p.Template == decide.TemplateElimination {
			v = hit()
		}
		v.eliminations, v.approx = &n, approx
		return v
	}
	return verdict{}
}

// horizonVerdict scores p at or after its horizon from c, a context of the
// running game (or the end snapshot of a game that ended after the horizon).
// gameRunning is false only for an end snapshot.
func horizonVerdict(p decide.Prediction, b baseline, c gamecontext.GameContext, now time.Time) verdict {
	if v := earlyVerdict(p, b, c, now); v.decided {
		return v
	}
	after := characteristics.Window{From: b.at, To: b.horizonEnd()}
	switch p.Template {
	case decide.TemplateSurvives:
		if alive, known := characteristics.Alive(c, p.Target); known && alive {
			return hit()
		}
		if _, gone := characteristics.EliminatedAt(c, p.Target); gone {
			// Eliminated after the horizon (earlyVerdict saw no earlier one).
			return hit()
		}
		return unscorable(ReasonDataMissing)
	case decide.TemplateNoElimination:
		v := hit()
		n, approx := horizonEliminations(b, c, now)
		v.eliminations, v.approx = &n, approx
		return v
	case decide.TemplateElimination:
		v := miss()
		zero := 0
		v.eliminations = &zero
		return v
	case decide.TemplateActivityRises:
		if !b.beforeOK {
			return unscorable(ReasonDataMissing)
		}
		var val float64
		var ok bool
		if p.Scope == decide.ScopeTarget {
			if gone, _ := targetEliminatedBy(p, c, b.horizonEnd()); gone {
				return unscorable(ReasonTargetNotAlive)
			}
			val, ok = characteristics.PlayerActivity(c, p.Target, after)
		} else {
			val, ok = characteristics.FieldActivityOf(c, b.players, after)
		}
		return compare(b.before, val, ok, func(before, after float64) bool { return after > before*riseFactor })
	case decide.TemplateClosenessRises:
		if !b.beforeOK {
			return unscorable(ReasonDataMissing)
		}
		val, ok := characteristics.ClosenessOf(c, b.players, after)
		return compare(b.before, val, ok, func(before, after float64) bool { return after < before*closerFactor })
	case decide.TemplateGameEnds:
		return miss()
	}
	return unscorable(ReasonDataMissing)
}

func compare(before, after float64, ok bool, isHit func(before, after float64) bool) verdict {
	if !ok {
		v := unscorable(ReasonDataMissing)
		v.before = &before
		return v
	}
	v := miss()
	if isHit(before, after) {
		v = hit()
	}
	v.before, v.after = &before, &after
	return v
}

// endVerdict scores p at a normal game end at endAt from the end snapshot c: a
// game that ended at or after the horizon is scored like the horizon check
// (game_ends misses unless it ended exactly at the horizon); a game that ended
// before it decides what is decidable (game_ends hits) and leaves the rest
// unscorable game_ended.
func endVerdict(p decide.Prediction, b baseline, c gamecontext.GameContext, endAt time.Time) verdict {
	if p.Template == decide.TemplateGameEnds {
		if !endAt.After(b.horizonEnd()) {
			return hit()
		}
		return miss()
	}
	if !endAt.Before(b.horizonEnd()) {
		return horizonVerdict(p, b, c, endAt)
	}
	if v := earlyVerdict(p, b, c, endAt); v.decided {
		return v
	}
	return unscorable(ReasonGameEnded)
}
