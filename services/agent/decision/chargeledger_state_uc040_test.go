package decision

import (
	"math/rand/v2"
	"strings"
	"testing"
	"time"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/gamecontext"
)

// chargeledger_state_uc040_test.go covers the shield_charged marker of the decide state
// and the rules engine's charge policy (UC-040 main step 4 and A31 step 1; #1870): both
// read the agent's own charge ledger, so the state marks a player charged exactly while
// the ledger would refuse a grant_shield to them.

func chargedIn(t *testing.T, st decide.State, serial string) bool {
	t.Helper()
	for _, p := range st.Players {
		if p.Serial == serial {
			return p.ShieldCharged
		}
	}
	t.Fatalf("player %s missing from the state", serial)
	return false
}

// TestChargeLedger_DecideStateMarksChargedForTheWholeWindow pins the #1870 invariant:
// at every instant the state's shield_charged equals whether the ledger refuses a
// second grant. One fresh loop per offset, so a probe that dispatches cannot re-arm the
// ledger for the next offset.
//
// UC: UC-040 A31 BR-020
func TestChargeLedger_DecideStateMarksChargedForTheWholeWindow(t *testing.T) {
	for _, tc := range []struct {
		name    string
		offset  time.Duration
		charged bool
	}{
		{"UC-040/A31 at the grant", 0, true},
		{"UC-040/A31 at 9.999 s", 9999 * time.Millisecond, true},
		{"UC-040/A31 at 10.001 s (past the shielded window)", 10001 * time.Millisecond, true},
		{"UC-040/A31 at 21.999 s", 21999 * time.Millisecond, true},
		{"UC-040/A31 at 22.000 s (window end)", 22 * time.Second, false},
		{"UC-040/A31 at 22.001 s", 22001 * time.Millisecond, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			l, _, clock, _ := ledgerLoop(t, 100, triggerMode(20*time.Second, nil), budgetShield("WEAK"))
			start := *clock
			if got := outcomeSummary(runCycle(l, ffaRealCtx())); !equalStrings(got, []string{"grant_shield/WEAK:dispatched"}) {
				t.Fatalf("first grant outcomes = %v, want dispatched", got)
			}
			*clock = start.Add(tc.offset)
			st := l.buildDecideRequest(ffaRealCtx(), allInterventions(), false).State
			charged := chargedIn(t, st, "WEAK")
			if charged != tc.charged {
				t.Fatalf("WEAK shield_charged = %v, want %v", charged, tc.charged)
			}
			if chargedIn(t, st, "S") {
				t.Fatal("S was never granted: shield_charged must be false")
			}
			probe := outcomeSummary(runCycle(l, ffaRealCtx()))
			refused := equalStrings(probe, []string{"grant_shield/WEAK:already_charged"})
			if refused != charged {
				t.Fatalf("state shield_charged = %v but the ledger's second grant = %v", charged, probe)
			}
		})
	}
}

// TestChargeLedger_InertStateUnchanged: outside trigger mode the ledger holds nothing,
// so the decide state is byte-identical to decide.BuildState.
//
// UC: UC-040 A31 BR-019
func TestChargeLedger_InertStateUnchanged(t *testing.T) {
	werewolf := "Werewolf"
	for _, tc := range []struct {
		name string
		mode ShieldChargeMode
		ctx  func() gamecontext.GameContext
	}{
		{"UC-040/A31 instant mode", instantMode, ffaRealCtx},
		{"UC-040/A31 nil mode source", nil, ffaRealCtx},
		{"UC-040/A31 BR-019 a non-FFA mode", triggerMode(20*time.Second, nil),
			func() gamecontext.GameContext { c := ffaRealCtx(); c.Session.GameMode = &werewolf; return c }},
		{"UC-040/A31 BR-019 a shadow game", triggerMode(20*time.Second, nil),
			func() gamecontext.GameContext { c := ffaRealCtx(); c.GameKind = gamecontext.GameKindShadow; return c }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			l, _, clock, _ := ledgerLoop(t, 100, tc.mode, budgetShield("WEAK"))
			if got := outcomeSummary(runCycle(l, tc.ctx())); !equalStrings(got, []string{"grant_shield/WEAK:dispatched"}) {
				t.Fatalf("grant outcomes = %v, want dispatched", got)
			}
			*clock = clock.Add(time.Second)
			c := tc.ctx()
			got := l.buildDecideRequest(c, allInterventions(), false).State
			want := decide.BuildState(c)
			if got.Narrative != want.Narrative || got.Digest != want.Digest {
				t.Fatalf("state differs from BuildState:\n%s\nwant\n%s", got.Narrative, want.Narrative)
			}
		})
	}
}

// TestChargeLedger_RulesDoNotReproposeChargedTarget: with the engine wired to the
// loop's ledger, the rules path never proposes a grant the ledger refuses; the shield
// goes to another R4 candidate (A41) or the slot to the next-ranked candidate, and the
// agent.decide state marks the charged players.
//
// UC: UC-040 A31 A41
func TestChargeLedger_RulesDoNotReproposeChargedTarget(t *testing.T) {
	t.Run("UC-040/A31 no already_charged refusals inside the window", func(t *testing.T) {
		snap := rulesSnapshot()
		snap.Objectives = balancedFocused()
		l, sr := decideRecordingLoop(t, snap)
		clock := time.Unix(50_000, 0)
		l.now = func() time.Time { return clock }
		l.Actions = &capturingSink{}
		l.SetShieldChargeMode(triggerMode(20*time.Second, nil))
		rules := newObjectiveRules(balancedRulesConfig(), balancedRulesConfig(), balancedRulesConfig(),
			func() time.Time { return clock }, rand.New(rand.NewPCG(1, 2)))
		rules.SetChargedSource(l.ChargedSerials)
		l.Rules = rules

		first := outcomeSummary(runCycle(l, chargeRulesCtx()))
		if !containsString(first, "grant_shield/WEAK:dispatched") {
			t.Fatalf("first cycle outcomes = %v, want grant_shield/WEAK dispatched", first)
		}
		charged := map[string]bool{"WEAK": true}
		for i := 1; i <= 3; i++ {
			clock = clock.Add(2 * time.Second)
			got := outcomeSummary(runCycle(l, chargeRulesCtx()))
			if len(got) == 0 {
				t.Fatalf("cycle %d: no decision, want the next-ranked candidate", i)
			}
			for _, o := range got {
				serial, _, _ := strings.Cut(strings.TrimPrefix(o, "grant_shield/"), ":")
				if (strings.HasPrefix(o, "grant_shield/") && charged[serial]) || strings.HasSuffix(o, ":already_charged") {
					t.Fatalf("cycle %d outcomes = %v: a charged target was proposed", i, got)
				}
				if strings.HasPrefix(o, "grant_shield/") && strings.HasSuffix(o, ":dispatched") {
					charged[serial] = true
				}
			}
		}
		// A41: MID (the other R4 candidate) gets the next shield; STRONG is below minScore.
		if !charged["MID"] || charged["STRONG"] {
			t.Fatalf("charged after 4 cycles = %v, want WEAK and MID", charged)
		}
		decides := spansByName(sr.Ended(), SpanDecide)
		if len(decides) < 2 {
			t.Fatalf("agent.decide spans = %d, want one per ranking", len(decides))
		}
		want := decide.BuildStateWith(chargeRulesCtx(), decide.Annotations{Charged: map[string]bool{"WEAK": true, "MID": true}}).Digest
		v, ok := attrValue(decides[len(decides)-1], AttrDecideStateDigest)
		if !ok || v.AsString() != want {
			t.Fatalf("agent.decide state digest = %q, want the state with WEAK and MID shield_charged (%q)", v.AsString(), want)
		}
	})
}

func containsString(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}

// TestChargeLedger_LiveMatchesReserve: the ledger view holds a reserved entry (a write
// in flight) as well as a committed one, and drops a released or ended one, exactly
// like reserve's refusal.
//
// UC: UC-040 A31
func TestChargeLedger_LiveMatchesReserve(t *testing.T) {
	t.Run("UC-040/A31 a reservation in flight counts as charged", func(t *testing.T) {
		var cl chargeLedger
		now := time.Unix(50_000, 0)
		res, _, _ := cl.reserve("WEAK", now, chargeOnly(22*time.Second))
		if res == nil {
			t.Fatal("reserve refused on an empty ledger")
		}
		if !cl.live(now)["WEAK"] {
			t.Fatal("a reserved entry must be live")
		}
		if again, _, _ := cl.reserve("WEAK", now, chargeOnly(22*time.Second)); again != nil {
			t.Fatal("precondition: reserve must refuse while the reservation is live")
		}
		cl.release("WEAK", res.gen)
		if cl.live(now)["WEAK"] {
			t.Fatal("a released entry must not be live")
		}
	})
	t.Run("UC-040/A31 a committed entry ends with its window", func(t *testing.T) {
		var cl chargeLedger
		now := time.Unix(50_000, 0)
		res, _, _ := cl.reserve("WEAK", now, chargeOnly(22*time.Second))
		cl.commit("WEAK", res.gen, now, chargeOnly(22*time.Second))
		if !cl.live(now.Add(22*time.Second - time.Millisecond))["WEAK"] {
			t.Fatal("a committed entry must be live until its window ends")
		}
		if got := cl.live(now.Add(22 * time.Second)); got["WEAK"] {
			t.Fatal("an entry must end with its window")
		}
	})
}
