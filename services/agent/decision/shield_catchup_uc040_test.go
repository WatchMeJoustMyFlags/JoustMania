package decision

import (
	"context"
	"math/rand/v2"
	"testing"
	"time"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// shield_catchup_uc040_test.go covers #2395 on the agent side: R4's catch-up among the
// players not helped yet in this game (UC-040 A41, BR-030) and the grant_shield length
// from the shield_seconds "default" variant (UC-040 BR-029).

// catchUpLoop is a rules loop with the shield ledger wired into the engine as main.go
// does (no-shield filter and grant view), the limits off, on a controllable clock.
func catchUpLoop(t *testing.T) (*Loop, *capturingSink, *time.Time) {
	t.Helper()
	snap := rulesSnapshot()
	snap.Objectives = balancedFocused()
	snap.Policy = flags.Policy{MaxInterventionsPerMinute: 50}
	l, _ := decideRecordingLoop(t, snap)
	clock := time.Unix(50_000, 0)
	l.now = func() time.Time { return clock }
	sink := &capturingSink{}
	l.Actions = sink
	rules := newObjectiveRules(balancedRulesConfig(), balancedRulesConfig(), balancedRulesConfig(),
		func() time.Time { return clock }, rand.New(rand.NewPCG(1, 2)))
	rules.SetShieldSource(l.ShieldSerials)
	rules.SetShieldGrantSource(l.ShieldGrants)
	l.Rules = rules
	return l, sink, &clock
}

// catchUpCtx is the R4 field: WEAK, MID and STRONG alive, one player gone.
func catchUpCtx(gameKind string) gamecontext.GameContext {
	c := gameCtx(30, []string{"GONE"}, map[string]testPlayer{
		"WEAK":   {skill: fptr(0.1), active: true},
		"MID":    {skill: fptr(0.3), active: true},
		"STRONG": {skill: fptr(0.9), active: true},
	})
	c.GameKind = gameKind
	return c
}

func shieldWriteOrder(sink *capturingSink) []string {
	var out []string
	for _, d := range sink.decisions() {
		if d.Intervention == InterventionGrantShield {
			out = append(out, d.TargetSerial)
		}
	}
	return out
}

func signalsOf(c gamecontext.GameContext) (active, skilled []*gamecontext.PlayerSignals) {
	active = activePlayers(c)
	for _, p := range active {
		if p.SkillLevel != nil {
			skilled = append(skilled, p)
		}
	}
	return active, skilled
}

// UC: UC-040 A41 BR-030
func TestCatchUpTarget(t *testing.T) {
	field := func(extra map[string]testPlayer) gamecontext.GameContext {
		ps := map[string]testPlayer{
			"WEAK":   {skill: fptr(0.1), active: true},
			"MID":    {skill: fptr(0.3), active: true},
			"STRONG": {skill: fptr(0.9), active: true},
		}
		for k, v := range extra {
			ps[k] = v
		}
		return gameCtx(30, []string{"GONE"}, ps)
	}
	for _, tc := range []struct {
		name    string
		extra   map[string]testPlayer
		grants  map[string]int
		want    string
		skipped bool
	}{
		{name: "no grant view: the weakest", grants: nil, want: "WEAK"},
		{name: "nobody helped yet: the weakest", grants: map[string]int{}, want: "WEAK"},
		{name: "the weakest helped: the next weakest not helped", grants: map[string]int{"WEAK": 1}, want: "MID", skipped: true},
		{name: "two helped: the last not helped", grants: map[string]int{"WEAK": 1, "MID": 1}, want: "STRONG", skipped: true},
		{name: "every alive player helped once: everyone eligible again", grants: map[string]int{"WEAK": 1, "MID": 1, "STRONG": 1}, want: "WEAK"},
		{name: "second round: the fewest grants first", grants: map[string]int{"WEAK": 2, "MID": 1, "STRONG": 1}, want: "MID", skipped: true},
		{name: "a dead player's grants do not hold the round", extra: map[string]testPlayer{"DEAD": {skill: fptr(0.0), active: false}},
			grants: map[string]int{"WEAK": 1, "MID": 1, "STRONG": 1}, want: "WEAK"},
		{name: "an alive player without a skill level and no grant: no helped player is eligible",
			extra: map[string]testPlayer{"NEW": {active: true}}, grants: map[string]int{"WEAK": 1, "MID": 1, "STRONG": 1}, want: ""},
	} {
		t.Run("UC-040/A41 BR-030 "+tc.name, func(t *testing.T) {
			active, skilled := signalsOf(field(tc.extra))
			got, skipped := catchUpTarget(active, skilled, tc.grants)
			gotSerial := ""
			if got != nil {
				gotSerial = got.Serial
			}
			if gotSerial != tc.want || skipped != tc.skipped {
				t.Fatalf("target = %q (skipped %v), want %q (skipped %v)", gotSerial, skipped, tc.want, tc.skipped)
			}
		})
	}
}

// UC: UC-040 A41 BR-030
func TestCatchUp_RulesRotateShields(t *testing.T) {
	t.Run("UC-040/A41 BR-030 the rules shield the not-yet-helped players in turn, and a helped one again only once every alive player has had one", func(t *testing.T) {
		l, sink, clock := catchUpLoop(t)
		c := catchUpCtx(gamecontext.GameKindReal)
		for i := 0; i < 6; i++ {
			runCycle(l, c)
			*clock = clock.Add(5 * time.Second)
		}
		got := shieldWriteOrder(sink)
		t.Logf("shield writes over 6 cycles: %v", got)
		if len(got) < 2 || got[0] != "WEAK" || got[1] != "MID" {
			t.Fatalf("shield writes = %v, want WEAK then MID first", got)
		}
		for _, s := range got[2:] {
			if s == "WEAK" || s == "MID" {
				t.Fatalf("shield writes = %v: a helped player was shielded again while STRONG has none", got)
			}
		}
		if grants := l.ShieldGrants(); grants["WEAK"] != 1 || grants["MID"] != 1 {
			t.Fatalf("ShieldGrants = %v, want WEAK 1 and MID 1", grants)
		}

		// STRONG leaves the alive set: every alive player has had one, WEAK is first again.
		c.Players["STRONG"].Active = bptr(false)
		c.Session.EliminationSequence = append(c.Session.EliminationSequence, "STRONG")
		before := len(got)
		runCycle(l, c)
		got = shieldWriteOrder(sink)
		if len(got) != before+1 || got[before] != "WEAK" {
			t.Fatalf("after STRONG died shield writes = %v, want one more to WEAK", got)
		}
	})
}

// UC: UC-040 A41 BR-030
func TestCatchUp_ShadowGameCountsItsOwnGrants(t *testing.T) {
	t.Run("UC-040/A41 BR-030 a shadow game's loop does not see the real game's grants, and a new game starts at 0", func(t *testing.T) {
		real, realSink, _ := catchUpLoop(t)
		runCycle(real, catchUpCtx(gamecontext.GameKindReal))
		if got := shieldWriteOrder(realSink); len(got) != 1 || got[0] != "WEAK" {
			t.Fatalf("real game shield writes = %v, want [WEAK]", got)
		}
		shadow, shadowSink, _ := catchUpLoop(t)
		runCycle(shadow, catchUpCtx(gamecontext.GameKindShadow))
		if got := shieldWriteOrder(shadowSink); len(got) != 1 || got[0] != "WEAK" {
			t.Fatalf("shadow game shield writes = %v, want [WEAK]: its rotation starts empty", got)
		}
		if g := shadow.ShieldGrants(); g["WEAK"] != 1 || len(g) != 1 {
			t.Fatalf("shadow ShieldGrants = %v, want only its own WEAK grant", g)
		}
	})
}

// UC: UC-040 A41 BR-030
func TestCatchUp_GrantsCountInFlight(t *testing.T) {
	t.Run("UC-040/A41 a grant in flight counts, a released one does not", func(t *testing.T) {
		var cl chargeLedger
		now := time.Unix(1, 0)
		res, reason, _ := cl.reserve("A", now, shieldRequest{seconds: 5 * time.Second})
		if reason != "" {
			t.Fatalf("reserve refused %q", reason)
		}
		if g := cl.grants(); g["A"] != 1 {
			t.Fatalf("grants with A in flight = %v, want A 1", g)
		}
		cl.release("A", res.gen)
		if g := cl.grants(); g != nil {
			t.Fatalf("grants after release = %v, want none", g)
		}
	})
}

// UC: UC-040 BR-029
func TestShieldSeconds_StampedFromTheSource(t *testing.T) {
	ctx := context.Background()
	t.Run("UC-040/BR-029 a grant without a value is written with the source's length", func(t *testing.T) {
		l, sink, _, _ := limitLoop(t, 50, 0, 0, budgetShield("WEAK"))
		calls := 0
		l.SetShieldSecondsSource(func(context.Context) float64 { calls++; return 3 })
		runCycle(l, chargeRulesCtx())
		ds := sink.decisions()
		if len(ds) != 1 || ds[0].Value != "3" {
			t.Fatalf("written = %+v, want one grant with Value 3", ds)
		}
		if calls != 1 {
			t.Fatalf("source calls = %d, want 1 (read at use time)", calls)
		}
	})
	t.Run("UC-040/BR-029 BR-022 the cooldown runs from the stamped length", func(t *testing.T) {
		// 3 s shield + 2 s margin + 15 s cooldown: refused at t0+19 s, allowed at t0+21 s
		// (with the 5 s default it would still be refused until t0+22 s).
		l, sink, clock, _ := limitLoop(t, 50, 0, 15, budgetShield("WEAK"))
		l.SetShieldSecondsSource(func(context.Context) float64 { return 3 })
		t0 := *clock
		runCycle(l, chargeRulesCtx())
		*clock = t0.Add(19 * time.Second)
		if got := outcomeSummary(runCycle(l, chargeRulesCtx())); !equalStrings(got, []string{"grant_shield/WEAK:shield_cooldown"}) {
			t.Fatalf("at t0+19 s outcomes = %v, want shield_cooldown", got)
		}
		*clock = t0.Add(21 * time.Second)
		if got := outcomeSummary(runCycle(l, chargeRulesCtx())); !equalStrings(got, []string{"grant_shield/WEAK:dispatched"}) {
			t.Fatalf("at t0+21 s outcomes = %v, want dispatched", got)
		}
		if n := shieldWritesTo(sink, "WEAK"); n != 2 {
			t.Fatalf("writes = %d, want 2", n)
		}
	})
	t.Run("UC-040/BR-029 a value the decision carries is kept and the source is not read", func(t *testing.T) {
		d := budgetShield("WEAK")
		d.Value = "8"
		l, sink, _, _ := limitLoop(t, 50, 0, 0, d)
		l.SetShieldSecondsSource(func(context.Context) float64 { t.Fatal("source read for a valued grant"); return 0 })
		runCycle(l, chargeRulesCtx())
		if ds := sink.decisions(); len(ds) != 1 || ds[0].Value != "8" {
			t.Fatalf("written = %+v, want Value 8", ds)
		}
	})
	t.Run("UC-040/BR-029 an unusable source value fails closed to 5 s", func(t *testing.T) {
		l, sink, _, _ := limitLoop(t, 50, 0, 0, budgetShield("WEAK"))
		l.SetShieldSecondsSource(func(context.Context) float64 { return 0 })
		runCycle(l, chargeRulesCtx())
		if ds := sink.decisions(); len(ds) != 1 || ds[0].Value != "5" {
			t.Fatalf("written = %+v, want Value 5", ds)
		}
	})
	t.Run("UC-040/BR-029 a grant the allow-list refuses does not read the source", func(t *testing.T) {
		l, _, _, _ := limitLoop(t, 50, 0, 0, budgetShield("WEAK"))
		snap := l.Flags.Evaluate(ctx)
		snap.InterventionsAllowed = []string{InterventionPlayAudioCue}
		l.Flags = fakeFlags{snap: snap}
		l.SetShieldSecondsSource(func(context.Context) float64 { t.Fatal("source read for a refused grant"); return 0 })
		runCycle(l, chargeRulesCtx())
	})
}
