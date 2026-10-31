package decision

import (
	"context"
	"math"
	"math/rand/v2"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// danger_shield_uc040_test.go covers #2461: R4's danger path before the first
// elimination (UC-040 A43, BR-032), so a two-player game can get rules shields.

// dangerThreshold is the session death threshold of the danger fixtures (g).
const dangerThreshold = 2.0

// dangerNow is the fixtures' context time (UpdatedAt).
var dangerNow = time.Unix(80_000, 0)

// withTrack gives serial a one-reading-per-second movement track ending at
// newestAge before c.UpdatedAt, oldest first.
func withTrack(c *gamecontext.GameContext, serial string, newestAge time.Duration, intensities ...float64) {
	p := c.Players[serial]
	p.History = nil
	newest := c.UpdatedAt.Add(-newestAge)
	for i, v := range intensities {
		at := newest.Add(-time.Duration(len(intensities)-1-i) * time.Second)
		p.History = append(p.History, gamecontext.MovementSample{At: at, Intensity: v})
	}
	last := intensities[len(intensities)-1]
	p.MovementIntensity = &last
}

// duelCtx is a two-player FFA before any elimination: A in sustained danger
// (0.90, 0.95, 1.00 of the threshold over the last 3 s), B calm. No skill levels.
func duelCtx() gamecontext.GameContext {
	c := gameCtx(20, nil, map[string]testPlayer{"A": {active: true}, "B": {active: true}})
	c.UpdatedAt = dangerNow
	c.Session.DeathThreshold = fptr(dangerThreshold)
	withTrack(&c, "A", 0, 1.8, 1.9, 2.0)
	withTrack(&c, "B", 0, 0.4, 0.5, 0.4)
	return c
}

// dangerTargets is every R4 danger-path target for c with the default thresholds.
func dangerTargets(c gamecontext.GameContext, fit FitnessThresholds) []string {
	var out []string
	for _, cd := range balancedCandidates(c, fit) {
		if cd.shieldCatch {
			out = append(out, cd.decision.TargetSerial)
		}
	}
	return out
}

// dangerLoop is a rules loop with the shield ledger wired into the engine as main.go
// does, the given policy, and a live fitness source (the loop publishes the
// policy.shield_danger_* values into it every cycle).
func dangerLoop(t *testing.T, pol flags.Policy) (*Loop, *capturingSink, *time.Time) {
	t.Helper()
	snap := rulesSnapshot()
	snap.Objectives = balancedFocused()
	snap.Policy = pol
	l, _ := decideRecordingLoop(t, snap)
	clock := time.Unix(50_000, 0)
	l.now = func() time.Time { return clock }
	sink := &capturingSink{}
	l.Actions = sink
	rules := newObjectiveRules(balancedRulesConfig(), balancedRulesConfig(), NewLiveFitness(),
		func() time.Time { return clock }, rand.New(rand.NewPCG(1, 2)))
	rules.SetShieldSource(l.ShieldSerials)
	rules.SetShieldGrantSource(l.ShieldGrants)
	l.Rules = rules
	return l, sink, &clock
}

// runDangerCycles runs n cycles every step, each on a context whose readings are as
// fresh as at the start (the telemetry keeps arriving).
func runDangerCycles(l *Loop, clock *time.Time, c gamecontext.GameContext, n int, step time.Duration) {
	for i := 0; i < n; i++ {
		runCycle(l, c)
		*clock = clock.Add(step)
	}
}

// UC: UC-040 A43 BR-032
func TestDangerShield_DuelGetsAShield(t *testing.T) {
	t.Run("UC-040/A43 BR-032 a two-player game with sustained danger: R4 proposes a shield for the endangered player", func(t *testing.T) {
		c := duelCtx()
		cands := balancedCandidates(c, defaultFit())
		if len(cands) != 1 || cands[0].decision.Intervention != InterventionGrantShield || cands[0].decision.TargetSerial != "A" {
			t.Fatalf("candidates = %v, want one grant_shield to A", decisionsOf(cands))
		}
		cd := cands[0]
		if !cd.shieldCatch || !cd.danger || cd.urgency != 0.9 {
			t.Fatalf("candidate %+v: want shieldCatch, danger and urgency 0.9 (the lowest ratio)", cd)
		}
		if cd.decision.Fitness["danger_ratio"] != 0.9 || cd.decision.Fitness["danger_samples"] != 3 {
			t.Fatalf("fitness = %v, want danger_ratio 0.9, danger_samples 3", cd.decision.Fitness)
		}
		if !strings.Contains(cd.decision.Reason, "sustained danger") {
			t.Fatalf("reason = %q", cd.decision.Reason)
		}
	})
	t.Run("UC-040/A43 BR-032 through the loop: the duel's endangered player is shielded and written", func(t *testing.T) {
		l, sink, clock := dangerLoop(t, flags.Policy{MaxInterventionsPerMinute: 50})
		runDangerCycles(l, clock, duelCtx(), 1, time.Second)
		if got := shieldWriteOrder(sink); !equalStrings(got, []string{"A"}) {
			t.Fatalf("shield writes = %v, want [A]", got)
		}
	})
	t.Run("UC-040/A43 BR-032 in a hold game the grant goes through the pool check: a full pool gets nothing", func(t *testing.T) {
		r := newObjectiveRules(balancedRulesConfig(), balancedRulesConfig(), balancedRulesConfig(),
			func() time.Time { return dangerNow }, rand.New(rand.NewPCG(1, 2)))
		r.SetShieldSource(func(gamecontext.GameContext) (map[string]bool, map[string]bool) {
			return nil, map[string]bool{"A": true} // A's pool full / budget out (A37)
		})
		for _, d := range r.Evaluate(context.Background(), duelCtx()) {
			if d.Intervention == InterventionGrantShield {
				t.Fatalf("the rules shield %s although A is in the no-shield set: %+v", d.TargetSerial, d)
			}
		}
	})
}

// UC: UC-040 A43 BR-032
func TestDangerShield_NotASpike(t *testing.T) {
	for _, tc := range []struct {
		name   string
		readAt time.Duration
		track  []float64
	}{
		{"a single warning spike", 0, []float64{1.0, 1.1, 2.1}},
		{"two readings over the level are fewer than 3", 0, []float64{1.0, 1.9, 2.1}},
		{"a dip below the level breaks the window", 0, []float64{1.9, 1.6, 1.9}},
		{"one coordinator sample seen in three buckets counts once", 0, []float64{1.9, 1.9, 1.9}},
		{"fewer readings than the window", 0, []float64{1.9, 2.0}},
	} {
		t.Run("UC-040/A43 BR-032 "+tc.name+": no danger shield", func(t *testing.T) {
			c := duelCtx()
			withTrack(&c, "A", tc.readAt, tc.track...)
			if got := dangerTargets(c, defaultFit()); len(got) != 0 {
				t.Fatalf("R4 candidates = %v, want none", got)
			}
		})
	}
}

// UC: UC-040 A43 BR-032
func TestDangerShield_FailClosed(t *testing.T) {
	for _, tc := range []struct {
		name  string
		setup func(c *gamecontext.GameContext)
	}{
		{"the newest reading is stale (4 s old)", func(c *gamecontext.GameContext) {
			withTrack(c, "A", 4*time.Second, 1.8, 1.9, 2.0)
		}},
		{"a gap between two readings", func(c *gamecontext.GameContext) {
			c.Players["A"].History[0].At = c.Players["A"].History[0].At.Add(-3 * time.Second)
		}},
		{"no death threshold observed", func(c *gamecontext.GameContext) { c.Session.DeathThreshold = nil }},
		{"a non-positive death threshold", func(c *gamecontext.GameContext) { c.Session.DeathThreshold = fptr(0) }},
		{"no movement track (an agent restart)", func(c *gamecontext.GameContext) { c.Players["A"].History = nil }},
		{"a non-finite reading", func(c *gamecontext.GameContext) { c.Players["A"].History[1].Intensity = math.NaN() }},
		{"no context time", func(c *gamecontext.GameContext) { c.UpdatedAt = time.Time{} }},
		{"a frozen gauge: the newest reading re-exported for 5 s", func(c *gamecontext.GameContext) {
			withTrack(c, "A", 0, 1.8, 1.9, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0)
		}},
		{"the other player is gone: one alive player", func(c *gamecontext.GameContext) { c.Players["B"].Active = bptr(false) }},
	} {
		t.Run("UC-040/A43 BR-032 "+tc.name+": no danger shield", func(t *testing.T) {
			c := duelCtx()
			tc.setup(&c)
			if got := dangerTargets(c, defaultFit()); len(got) != 0 {
				t.Fatalf("R4 candidates = %v, want none", got)
			}
		})
	}
}

// UC: UC-040 A43 BR-032
func TestDangerShield_GatesStillBlock(t *testing.T) {
	t.Run("UC-040/A43 BR-022 the per-game cap: one shield, however long the danger lasts", func(t *testing.T) {
		l, sink, clock := dangerLoop(t, flags.Policy{MaxInterventionsPerMinute: 50, MaxShieldsPerPlayerPerGame: 1})
		runDangerCycles(l, clock, duelCtx(), 12, 5*time.Second)
		if n := shieldWritesTo(sink, "A"); n != 1 {
			t.Fatalf("grant_shield writes to A = %d, want 1 (cap 1)", n)
		}
	})
	t.Run("UC-040/A43 BR-022 the cooldown: no second shield before the last one ended plus the cooldown", func(t *testing.T) {
		l, sink, clock := dangerLoop(t, flags.Policy{MaxInterventionsPerMinute: 50, ShieldCooldownSeconds: 30})
		// The first shield ends after 5 s + 2 s; the cooldown runs to 37 s.
		runDangerCycles(l, clock, duelCtx(), 7, 5*time.Second) // 0..30 s
		if n := shieldWritesTo(sink, "A"); n != 1 {
			t.Fatalf("grant_shield writes to A in 30 s = %d, want 1 (cooldown)", n)
		}
		runDangerCycles(l, clock, duelCtx(), 2, 5*time.Second) // 35, 40 s
		if n := shieldWritesTo(sink, "A"); n != 2 {
			t.Fatalf("grant_shield writes to A after the cooldown = %d, want 2", n)
		}
	})
	t.Run("UC-040/A43 BR-014 the budget: a budget of one shield per minute writes one shield in a minute", func(t *testing.T) {
		l, sink, clock := dangerLoop(t, flags.Policy{MaxInterventionsPerMinute: int(interventionCost(InterventionGrantShield))})
		runDangerCycles(l, clock, duelCtx(), 12, 5*time.Second) // 0..55 s
		if n := shieldWritesTo(sink, "A"); n != 1 {
			t.Fatalf("grant_shield writes to A in a minute = %d, want 1", n)
		}
	})
	t.Run("UC-040/A43 BR-030 the rotation: two endangered players, the one with fewer shields first", func(t *testing.T) {
		c := duelCtx()
		withTrack(&c, "B", 0, 1.8, 1.9, 2.0)
		got := pickShieldCatchUp(r4Ranked(c, nil), c, map[string]int{"A": 1})
		if len(got) != 1 || got[0].decision.TargetSerial != "B" || !strings.HasPrefix(got[0].decision.Reason, "catch-up:") {
			t.Fatalf("kept %v, want one catch-up shield for B", decisionsOf(candidatesOf(got)))
		}
	})
}

// UC: UC-040 A43 BR-032
func TestDangerShield_EliminationPathUnchanged(t *testing.T) {
	field := func(gone []string) gamecontext.GameContext {
		c := gameCtx(30, gone, map[string]testPlayer{
			"WEAK":   {skill: fptr(0.1), active: true},
			"MID":    {skill: fptr(0.3), active: true},
			"STRONG": {skill: fptr(0.9), active: true},
		})
		c.UpdatedAt = dangerNow
		c.Session.DeathThreshold = fptr(dangerThreshold)
		return c
	}
	t.Run("UC-040/A43 BR-030 3+ players after an elimination: the danger readings change nothing", func(t *testing.T) {
		plain := field([]string{"GONE"})
		endangered := field([]string{"GONE"})
		withTrack(&endangered, "STRONG", 0, 1.8, 1.9, 2.0) // STRONG in danger: no struggling player
		withTrack(&endangered, "WEAK", 0, 1.9, 1.95, 2.0)
		want, got := balancedCandidates(plain, defaultFit()), balancedCandidates(endangered, defaultFit())
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("candidates with danger readings = %v, want the elimination path's %v", decisionsOf(got), decisionsOf(want))
		}
		for _, cd := range got {
			if cd.danger {
				t.Fatalf("a danger candidate after the first elimination: %+v", cd)
			}
		}
	})
	t.Run("UC-040/A43 3+ players before the first elimination: the endangered player is a candidate", func(t *testing.T) {
		c := field(nil)
		withTrack(&c, "STRONG", 0, 1.8, 1.9, 2.0)
		if got := dangerTargets(c, defaultFit()); !equalStrings(got, []string{"STRONG"}) {
			t.Fatalf("R4 candidates = %v, want [STRONG]", got)
		}
	})
	t.Run("UC-040/A43 3+ players before the first elimination without danger: no R4 candidate, as before", func(t *testing.T) {
		if got := dangerTargets(field(nil), defaultFit()); len(got) != 0 {
			t.Fatalf("R4 candidates = %v, want none", got)
		}
	})
}

// UC: UC-040 A43 BR-032
func TestDangerShield_FlagsClamped(t *testing.T) {
	for _, tc := range []struct {
		name        string
		ratio       float64
		samples     int
		wantRatio   float64
		wantSamples int
	}{
		{"zero values (a hand-built snapshot) give the defaults", 0, 0, 0.85, 3},
		{"in range values are kept", 0.95, 5, 0.95, 5},
		{"a ratio below 0.7 is raised to 0.7", 0.3, 3, 0.7, 3},
		{"a ratio above 1 is lowered to 1", 1.5, 3, 1.0, 3},
		{"a non-finite ratio gives the default", math.NaN(), 3, 0.85, 3},
		{"one reading is raised to 2: a single reading never triggers", 0.85, 1, 0.85, 2},
		{"a negative count gives the default", 0.85, -4, 0.85, 3},
		{"a long window is lowered to 10", 0.85, 50, 0.85, 10},
	} {
		t.Run("UC-040/A43 BR-032 "+tc.name, func(t *testing.T) {
			level, n := shieldDangerParams(FitnessThresholds{ShieldDangerRatio: tc.ratio, ShieldDangerSamples: tc.samples})
			if level != tc.wantRatio || n != tc.wantSamples {
				t.Fatalf("params = %v / %d, want %v / %d", level, n, tc.wantRatio, tc.wantSamples)
			}
		})
	}
	t.Run("UC-040/A43 BR-032 the live flags reach the rules: policy.shield_danger_ratio late (0.95) drops a 0.90 window", func(t *testing.T) {
		l, sink, clock := dangerLoop(t, flags.Policy{MaxInterventionsPerMinute: 50, ShieldDangerRatio: 0.95})
		runDangerCycles(l, clock, duelCtx(), 1, time.Second)
		if got := shieldWriteOrder(sink); len(got) != 0 {
			t.Fatalf("shield writes = %v, want none at level 0.95", got)
		}
	})
	t.Run("UC-040/A43 BR-032 the live flags reach the rules: policy.shield_danger_samples 2 shields after two readings", func(t *testing.T) {
		c := duelCtx()
		withTrack(&c, "A", 0, 1.0, 1.9, 2.0)
		l, sink, clock := dangerLoop(t, flags.Policy{MaxInterventionsPerMinute: 50, ShieldDangerSamples: 2})
		runDangerCycles(l, clock, c, 1, time.Second)
		if got := shieldWriteOrder(sink); !equalStrings(got, []string{"A"}) {
			t.Fatalf("shield writes = %v, want [A] with 2 readings", got)
		}
	})
}

// UC: UC-040 A43 BR-032
func TestDangerShield_OffSwitch(t *testing.T) {
	t.Run("UC-040/A43 BR-032 policy.shield_danger_enabled off: no danger shield in a duel in danger", func(t *testing.T) {
		l, sink, clock := dangerLoop(t, flags.Policy{MaxInterventionsPerMinute: 50, ShieldDangerOff: true})
		runDangerCycles(l, clock, duelCtx(), 3, 5*time.Second)
		if got := shieldWriteOrder(sink); len(got) != 0 {
			t.Fatalf("shield writes = %v, want none with the danger path off", got)
		}
	})
	t.Run("UC-040/A43 A41 policy.shield_danger_enabled off: the elimination path still shields", func(t *testing.T) {
		off := defaultFit()
		off.ShieldDangerOff = true
		c := gameCtx(30, []string{"GONE"}, map[string]testPlayer{
			"WEAK":   {skill: fptr(0.1), active: true},
			"MID":    {skill: fptr(0.3), active: true},
			"STRONG": {skill: fptr(0.9), active: true},
		})
		if want, got := balancedCandidates(c, defaultFit()), balancedCandidates(c, off); !reflect.DeepEqual(got, want) || len(shieldTargets(decisionsOf(got))) == 0 {
			t.Fatalf("candidates with the danger path off = %v, want the elimination path's %v", decisionsOf(got), decisionsOf(want))
		}
	})
}
