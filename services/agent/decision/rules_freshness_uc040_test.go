package decision

import (
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// rules_freshness_uc040_test.go covers #2511: R3, R4's elimination path and R8 read a
// player's skill level or movement variance only while that player's movement
// readings are fresh, the R4d signal (decide.FreshReadings; UC-040 A46, BR-037).

// freshNow is the fixtures' context time (UpdatedAt).
var freshNow = time.Unix(90_000, 0)

// staleness names how a player's movement track is built relative to freshNow.
type staleness int

const (
	trackFresh   staleness = iota // a new value each second, the newest 1 s old
	trackFrozen                   // the same value re-exported for 5 s: last new 5 s ago
	trackOld                      // new values, but the newest is 4 s old
	trackMissing                  // no readings at all (freshness unknown)
)

// setTrack gives serial a movement track of the given staleness.
func setTrack(c *gamecontext.GameContext, serial string, s staleness) {
	p := c.Players[serial]
	p.History = nil
	sample := func(age time.Duration, v float64) gamecontext.MovementSample {
		return gamecontext.MovementSample{At: freshNow.Add(-age), Intensity: v}
	}
	switch s {
	case trackFresh:
		p.History = []gamecontext.MovementSample{sample(3*time.Second, 1.01), sample(2*time.Second, 1.02), sample(time.Second, 1.03)}
	case trackFrozen:
		for age := 5; age >= 0; age-- {
			p.History = append(p.History, sample(time.Duration(age)*time.Second, 1.04))
		}
	case trackOld:
		p.History = []gamecontext.MovementSample{sample(6*time.Second, 1.01), sample(5*time.Second, 1.02), sample(4*time.Second, 1.03)}
	case trackMissing:
	}
}

// freshCtx is gameCtx with UpdatedAt at freshNow and every player's track fresh.
func freshCtx(duration float64, elims []string, players map[string]testPlayer) gamecontext.GameContext {
	c := gameCtx(duration, elims, players)
	c.UpdatedAt = freshNow
	for serial := range c.Players {
		setTrack(&c, serial, trackFresh)
	}
	return c
}

var staleKinds = []struct {
	name string
	s    staleness
}{
	{"frozen", trackFrozen},
	{"old", trackOld},
	{"missing", trackMissing},
}

func targetsOf(cands []candidate, rule string) []string {
	var out []string
	for _, cd := range cands {
		if cd.rule == rule {
			out = append(out, cd.decision.TargetSerial)
		}
	}
	return out
}

func hasTarget(targets []string, serial string) bool {
	for _, t := range targets {
		if t == serial {
			return true
		}
	}
	return false
}

// UC: UC-040 A46 BR-037
func TestRulesFreshness_R3(t *testing.T) {
	players := map[string]testPlayer{
		"LOW":  {skill: fptr(0.2), active: true},
		"HIGH": {skill: fptr(0.9), active: true},
	}
	t.Run("UC-040/A46 R3 handicaps a fresh outlier", func(t *testing.T) {
		got := targetsOf(balancedCandidates(freshCtx(30, nil, players), defaultFit()), "R3")
		if len(got) != 1 || got[0] != "HIGH" {
			t.Fatalf("R3 targets = %v, want [HIGH]", got)
		}
	})
	for _, k := range staleKinds {
		t.Run("UC-040/A46 R3 skips a "+k.name+" outlier", func(t *testing.T) {
			c := freshCtx(30, nil, players)
			setTrack(&c, "HIGH", k.s)
			if got := targetsOf(balancedCandidates(c, defaultFit()), "R3"); len(got) != 0 {
				t.Fatalf("R3 targets = %v, want none (HIGH is %s)", got, k.name)
			}
		})
		t.Run("UC-040/A46 R3 skips the spread to a "+k.name+" low player", func(t *testing.T) {
			c := freshCtx(30, nil, players)
			setTrack(&c, "LOW", k.s)
			if got := targetsOf(balancedCandidates(c, defaultFit()), "R3"); len(got) != 0 {
				t.Fatalf("R3 targets = %v, want none (LOW is %s)", got, k.name)
			}
		})
	}
	t.Run("UC-040/A46 R3 fails closed with no context time", func(t *testing.T) {
		c := freshCtx(30, nil, players)
		c.UpdatedAt = time.Time{}
		if got := targetsOf(balancedCandidates(c, defaultFit()), "R3"); len(got) != 0 {
			t.Fatalf("R3 targets = %v, want none (UpdatedAt unknown)", got)
		}
	})
}

// UC: UC-040 A46 BR-037
func TestRulesFreshness_R4(t *testing.T) {
	players := map[string]testPlayer{
		"WEAK":   {skill: fptr(0.2), active: true},
		"MID":    {skill: fptr(0.5), active: true},
		"STRONG": {skill: fptr(0.9), active: true},
	}
	t.Run("UC-040/A46 R4 shields a fresh struggling player", func(t *testing.T) {
		got := targetsOf(balancedCandidates(freshCtx(30, []string{"GONE"}, players), defaultFit()), "R4")
		if !hasTarget(got, "WEAK") {
			t.Fatalf("R4 targets = %v, want WEAK among them", got)
		}
	})
	for _, k := range staleKinds {
		t.Run("UC-040/A46 R4 skips a "+k.name+" struggling player", func(t *testing.T) {
			c := freshCtx(30, []string{"GONE"}, players)
			setTrack(&c, "WEAK", k.s)
			got := targetsOf(balancedCandidates(c, defaultFit()), "R4")
			if hasTarget(got, "WEAK") {
				t.Fatalf("R4 targets = %v, want no WEAK (%s)", got, k.name)
			}
			// The field's mean comes from the fresh players: MID (0.5) is below
			// the mean of MID and STRONG (0.7), so the rules still shield.
			if !hasTarget(got, "MID") {
				t.Fatalf("R4 targets = %v, want MID (fresh, below the fresh mean)", got)
			}
		})
	}
}

// UC: UC-040 A46 BR-037
func TestRulesFreshness_R8(t *testing.T) {
	players := map[string]testPlayer{
		"S": {variance: fptr(0.01), active: true},
		"T": {variance: fptr(0.30), active: true},
	}
	t.Run("UC-040/A46 R8 nudges a fresh statue", func(t *testing.T) {
		got := targetsOf(chaosCandidates(freshCtx(20, nil, players), defaultPol(), 0, ""), "R8")
		if len(got) != 1 || got[0] != "S" {
			t.Fatalf("R8 targets = %v, want [S]", got)
		}
	})
	for _, k := range staleKinds {
		t.Run("UC-040/A46 R8 skips a "+k.name+" statue", func(t *testing.T) {
			c := freshCtx(20, nil, players)
			setTrack(&c, "S", k.s)
			if got := targetsOf(chaosCandidates(c, defaultPol(), 0, ""), "R8"); len(got) != 0 {
				t.Fatalf("R8 targets = %v, want none (S is %s)", got, k.name)
			}
		})
	}
}
