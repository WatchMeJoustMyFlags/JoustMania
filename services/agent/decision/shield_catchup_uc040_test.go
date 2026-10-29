package decision

import (
	"context"
	"encoding/json"
	"math/rand/v2"
	"sort"
	"strings"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// shield_catchup_uc040_test.go covers #2395 on the agent side: R4's catch-up among the
// candidates with the fewest agent shields in this game (UC-040 A41, BR-030) and the grant_shield length
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

// r4Ranked is the R4 part of a ranking of c: R4's candidates scored, minScore
// applied, the no-shield set dropped, as rankLocked builds them before
// pickShieldCatchUp.
func r4Ranked(c gamecontext.GameContext, noShield map[string]bool) []scoredCandidate {
	cands := applyChargePolicy(balancedCandidates(c, defaultFit()), noShield)
	var r4 []candidate
	for _, cd := range cands {
		if cd.shieldCatch {
			r4 = append(r4, cd)
		}
	}
	return scoreAndSort(r4, map[string]float64{ObjectiveBalanced: 1}, FitnessEvaluation{})
}

// UC: UC-040 A41 BR-030
func TestPickShieldCatchUp(t *testing.T) {
	// GONE out of 4: ef 0.25, so urgency (1-skill)*0.5: WEAK 0.45, MID 0.35,
	// STRONG 0.05 (below minScore 0.10: never a candidate).
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
		name     string
		extra    map[string]testPlayer
		grants   map[string]int
		noShield map[string]bool
		want     string
		catchUp  bool
	}{
		{name: "no grant view: the weakest", want: "WEAK"},
		{name: "nobody helped yet: the weakest", grants: map[string]int{}, want: "WEAK"},
		{name: "the weakest helped: the next candidate with none", grants: map[string]int{"WEAK": 1}, want: "MID", catchUp: true},
		{name: "a non-candidate with none does not hold the others back", grants: map[string]int{"WEAK": 1, "MID": 1}, want: "WEAK"},
		{name: "the fewest grants first", grants: map[string]int{"WEAK": 2, "MID": 1}, want: "MID", catchUp: true},
		{name: "the fewest grants first, however many", grants: map[string]int{"WEAK": 3, "MID": 5}, want: "WEAK"},
		{name: "a filtered player (cap, cooldown, charge) is not a candidate", noShield: map[string]bool{"WEAK": true}, want: "MID"},
		{name: "every candidate filtered: no shield", noShield: map[string]bool{"WEAK": true, "MID": true}, want: ""},
		{name: "an alive player without a skill level does not hold the others back",
			extra: map[string]testPlayer{"NEW": {active: true}}, grants: map[string]int{"WEAK": 1, "MID": 1}, want: "WEAK"},
	} {
		t.Run("UC-040/A41 BR-030 "+tc.name, func(t *testing.T) {
			c := field(tc.extra)
			got := pickShieldCatchUp(r4Ranked(c, tc.noShield), c, tc.grants)
			if tc.want == "" {
				if len(got) != 0 {
					t.Fatalf("kept %v, want no R4 candidate", decisionsOf(candidatesOf(got)))
				}
				return
			}
			if len(got) != 1 || got[0].decision.TargetSerial != tc.want {
				t.Fatalf("kept %v, want one R4 candidate on %s", shieldTargets(decisionsOf(candidatesOf(got))), tc.want)
			}
			_, hasGrants := got[0].decision.Fitness["shield_grants"]
			isCatchUp := strings.HasPrefix(got[0].decision.Reason, "catch-up:")
			if hasGrants != tc.catchUp || isCatchUp != tc.catchUp {
				t.Fatalf("reason %q fitness %v, want catch-up %v", got[0].decision.Reason, got[0].decision.Fitness, tc.catchUp)
			}
		})
	}
	t.Run("UC-040/A41 BR-030 the kept candidate keeps its place among the other rules", func(t *testing.T) {
		other := scoredCandidate{candidate{decision: Decision{Intervention: InterventionPlayAudioCue}}, 0.4}
		c := field(nil)
		ranked := append(r4Ranked(c, nil), other)
		sortRanked(ranked)
		got := pickShieldCatchUp(ranked, c, map[string]int{"WEAK": 1})
		if len(got) != 2 || got[0].decision.Intervention != InterventionPlayAudioCue || got[1].decision.TargetSerial != "MID" {
			t.Fatalf("ranked = %v, want the audio cue (0.40) then MID's shield (0.35)", decisionsOf(candidatesOf(got)))
		}
	})
}

func candidatesOf(ranked []scoredCandidate) []candidate {
	out := make([]candidate, len(ranked))
	for i, sc := range ranked {
		out[i] = sc.candidate
	}
	return out
}

func sortRanked(ranked []scoredCandidate) {
	sort.SliceStable(ranked, func(i, j int) bool { return ranked[i].score > ranked[j].score })
}

// UC: UC-040 A41 BR-030
func TestCatchUp_RulesRotateShields(t *testing.T) {
	t.Run("UC-040/A41 BR-030 the rules keep shielding and spread the shields over the candidates, fewest first", func(t *testing.T) {
		l, sink, clock := catchUpLoop(t)
		c := catchUpCtx(gamecontext.GameKindReal)
		for i := 0; i < 6; i++ {
			runCycle(l, c)
			*clock = clock.Add(5 * time.Second)
		}
		got := shieldWriteOrder(sink)
		t.Logf("shield writes over 6 cycles: %v", got)
		// STRONG (skill 0.9) is below minScore and never a candidate: it holds nobody back.
		want := []string{"WEAK", "MID", "WEAK", "MID", "WEAK", "MID"}
		if !equalStrings(got, want) {
			t.Fatalf("shield writes = %v, want %v", got, want)
		}
		if grants := l.ShieldGrants(); grants["WEAK"] != 3 || grants["MID"] != 3 || grants["STRONG"] != 0 {
			t.Fatalf("ShieldGrants = %v, want WEAK 3 and MID 3", grants)
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

// UC: UC-040 BR-029
func TestShieldSeconds_RulesMagnitudeReportsTheStampedLength(t *testing.T) {
	t.Run("UC-040/BR-029 the rules record's ffa.magnitude is the stamped length, not the 5 s bounds default", func(t *testing.T) {
		l, sink, _ := catchUpLoop(t)
		sr := tracetest.NewSpanRecorder()
		tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
		t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
		l.Tracer = tp.Tracer("test")
		l.SetShieldSecondsSource(func(context.Context) float64 { return 10 })
		// No skill gap above 0.40, so R4's shield is the top-ranked decision.
		c := catchUpCtx(gamecontext.GameKindReal)
		c.Players["STRONG"].SkillLevel = fptr(0.4)
		runCycle(l, c)
		if ds := sink.decisions(); len(ds) == 0 || ds[0].Intervention != InterventionGrantShield || ds[0].Value != "10" {
			t.Fatalf("written = %+v, want a grant_shield of 10 s first", ds)
		}
		spans := spansByName(sr.Ended(), SpanDecide)
		if len(spans) != 1 {
			t.Fatalf("agent.decide spans = %d, want 1", len(spans))
		}
		var as []decide.Answer
		if err := json.Unmarshal([]byte(strAttr(t, spans[0], AttrDecideAnswers)), &as); err != nil {
			t.Fatalf("decide.answers: %v", err)
		}
		found := false
		for _, a := range as {
			if a.QuestionID == decide.QuestionFFAMagnitude {
				found = true
				// grant_shield bounds 3..10: 10 s is the top of the range.
				if got, _ := decide.ResolveMagnitude(InterventionGrantShield, a.Score); got != 10 {
					t.Fatalf("ffa.magnitude score %v resolves to %v s, want 10 s", a.Score, got)
				}
			}
		}
		if !found {
			t.Fatal("no ffa.magnitude answer on the rules record")
		}
	})
}
