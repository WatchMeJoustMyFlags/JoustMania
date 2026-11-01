package decision

import (
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// rules_freshness_store_uc040_test.go builds the rules' context through the real
// gamecontext.Store (#2511 review): SetPlayerIntensity is the LAST store call, the
// way an OTLP batch that ends with an accel data point leaves the store. The store
// must stamp the touch and the movement sample from one clock read; with two reads
// the newest sample is newer than UpdatedAt and decide.FreshReadings drops the
// player from R3, R4 (both paths, R4d included) and R8.

// tickingClock advances on every read, like time.Now between two calls.
type tickingClock struct{ t time.Time }

func (c *tickingClock) now() time.Time {
	c.t = c.t.Add(time.Microsecond)
	return c.t
}

type storePlayer struct {
	serial      string
	skill       *float64
	variance    *float64
	intensities []float64 // one per second, oldest first
}

// storeCtx feeds players into a Store and returns its snapshot. The intensities go
// in last, one round per second in the given player order, so the last player's
// SetPlayerIntensity is the store's last call.
func storeCtx(t *testing.T, duration float64, elims []string, threshold *float64, players []storePlayer) gamecontext.GameContext {
	t.Helper()
	start := time.Unix(50_000, 0)
	clk := &tickingClock{t: start}
	s := gamecontext.NewStore(time.Hour, time.Hour, clk.now)
	s.SetGameActive(true)
	s.SetSessionDuration(duration)
	if threshold != nil {
		s.SetDeathThreshold(*threshold)
	}
	for _, p := range players {
		s.SetPlayerAlive(p.serial, true)
		if p.skill != nil {
			s.SetPlayerSkill(p.serial, *p.skill, true)
		}
		if p.variance != nil {
			s.SetPlayerVariance(p.serial, *p.variance)
		}
	}
	for i, serial := range elims {
		s.SetEliminationOrder(serial, i+1)
	}
	for i := range players[0].intensities {
		clk.t = start.Add(time.Duration(i+1) * time.Second)
		for _, p := range players {
			s.SetPlayerIntensity(p.serial, p.intensities[i])
		}
	}
	c := s.Snapshot()
	for _, p := range players {
		if !readingsFresh(c, c.Players[p.serial]) {
			t.Errorf("player %s is stale straight after its accel reading (UpdatedAt %v, history %v)",
				p.serial, c.UpdatedAt, c.Players[p.serial].History)
		}
	}
	return c
}

var calmTrack = []float64{1.01, 1.02, 1.03}

// UC: UC-040 A46 BR-037
func TestRulesFreshness_ThroughTheStore(t *testing.T) {
	t.Run("UC-040/A46 R3 handicaps the outlier whose accel reading came last", func(t *testing.T) {
		c := storeCtx(t, 30, nil, nil, []storePlayer{
			{serial: "LOW", skill: fptr(0.2), intensities: calmTrack},
			{serial: "HIGH", skill: fptr(0.9), intensities: calmTrack},
		})
		if got := targetsOf(balancedCandidates(c, defaultFit()), "R3"); len(got) != 1 || got[0] != "HIGH" {
			t.Fatalf("R3 targets = %v, want [HIGH]", got)
		}
	})
	t.Run("UC-040/A46 R4 shields the struggling player whose accel reading came last", func(t *testing.T) {
		c := storeCtx(t, 30, []string{"GONE"}, nil, []storePlayer{
			{serial: "MID", skill: fptr(0.5), intensities: calmTrack},
			{serial: "STRONG", skill: fptr(0.9), intensities: calmTrack},
			{serial: "WEAK", skill: fptr(0.2), intensities: calmTrack},
		})
		if got := targetsOf(balancedCandidates(c, defaultFit()), "R4"); !hasTarget(got, "WEAK") {
			t.Fatalf("R4 targets = %v, want WEAK among them", got)
		}
	})
	t.Run("UC-040/A46 R8 nudges the statue whose accel reading came last", func(t *testing.T) {
		c := storeCtx(t, 20, nil, nil, []storePlayer{
			{serial: "T", variance: fptr(0.30), intensities: calmTrack},
			{serial: "S", variance: fptr(0.01), intensities: calmTrack},
		})
		if got := targetsOf(chaosCandidates(c, defaultPol(), 0, ""), "R8"); len(got) != 1 || got[0] != "S" {
			t.Fatalf("R8 targets = %v, want [S]", got)
		}
	})
	t.Run("UC-040/A43 R4d shields the endangered player whose accel reading came last", func(t *testing.T) {
		c := storeCtx(t, 20, nil, fptr(dangerThreshold), []storePlayer{
			{serial: "B", intensities: []float64{0.4, 0.5, 0.4}},
			{serial: "A", intensities: []float64{1.8, 1.9, 2.0}},
		})
		got := dangerTargets(c, defaultFit())
		if len(got) != 1 || got[0] != "A" {
			t.Fatalf("danger targets = %v, want [A]", got)
		}
	})
}
