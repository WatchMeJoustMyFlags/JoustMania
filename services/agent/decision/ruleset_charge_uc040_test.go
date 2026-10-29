package decision

import (
	"context"
	"math/rand/v2"
	"sync/atomic"
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// ruleset_charge_uc040_test.go covers applyChargePolicy (UC-040 A31; #1870): the rules
// engine drops a grant_shield candidate whose target the loop's charge ledger holds;
// another player's R4 candidate may then be the one kept (A41, #2395).

// chargeRulesCtx is a real FFA game in which R4 shields WEAK (the weakest while the field
// shrinks) and R3 reins in STRONG (skill gap above the threshold).
func chargeRulesCtx() gamecontext.GameContext {
	c := gameCtx(30, []string{"GONE"}, map[string]testPlayer{
		"WEAK":   {skill: fptr(0.1), active: true},
		"MID":    {skill: fptr(0.3), active: true},
		"STRONG": {skill: fptr(0.9), active: true},
	})
	c.GameKind = gamecontext.GameKindReal
	mode := shieldTriggerGameMode
	c.Session.GameMode = &mode
	return c
}

func balancedRulesConfig() StaticConfig {
	cfg := DefaultStaticConfig()
	cfg.ObjectiveWeights = balancedFocused()
	return cfg
}

func shieldTargets(ds []Decision) []string {
	var out []string
	for _, d := range ds {
		if d.Intervention == InterventionGrantShield {
			out = append(out, d.TargetSerial)
		}
	}
	return out
}

// UC: UC-040 A31 A41
func TestApplyChargePolicy_R4SkipsChargedWeakest(t *testing.T) {
	t.Run("UC-040/A31 A41 a charged weakest gets no grant_shield; the other struggling player stays a candidate", func(t *testing.T) {
		cands := balancedCandidates(chargeRulesCtx(), defaultFit())
		// Mean skill 0.43: WEAK and MID are below it; STRONG never is (A41 step 2).
		if got := shieldTargets(decisionsOf(cands)); !equalStrings(got, []string{"MID", "WEAK"}) {
			t.Fatalf("precondition: R4 candidates %v, want [MID WEAK]", got)
		}
		kept := applyChargePolicy(cands, map[string]bool{"WEAK": true})
		if got := shieldTargets(decisionsOf(kept)); !equalStrings(got, []string{"MID"}) {
			t.Fatalf("grant_shield candidates after the charge policy = %v, want [MID] (WEAK dropped, A41: a non-candidate holds nobody back)", got)
		}
		if len(kept) == 0 {
			t.Fatal("the charge policy must keep the other candidates")
		}
	})
	t.Run("UC-040/A31 nil or empty charged keeps every candidate", func(t *testing.T) {
		for _, charged := range []map[string]bool{nil, {}} {
			want := interventionsOf(balancedCandidates(chargeRulesCtx(), defaultFit()))
			got := interventionsOf(applyChargePolicy(balancedCandidates(chargeRulesCtx(), defaultFit()), charged))
			if !equalStrings(got, want) {
				t.Fatalf("candidates = %v, want %v", got, want)
			}
		}
	})
	t.Run("UC-040/A31 the engine reads its charged source once per ranking", func(t *testing.T) {
		clock := time.Unix(10_000, 0)
		r := newObjectiveRules(balancedRulesConfig(), balancedRulesConfig(), balancedRulesConfig(),
			func() time.Time { return clock }, rand.New(rand.NewPCG(1, 2)))
		if got := shieldTargets(r.Evaluate(context.Background(), chargeRulesCtx())); !equalStrings(got, []string{"WEAK"}) {
			t.Fatalf("without a source the engine shields %v, want [WEAK]", got)
		}
		var reads atomic.Int32
		r.SetChargedSource(func() map[string]bool { reads.Add(1); return map[string]bool{"WEAK": true} })
		clock = clock.Add(evalInterval)
		if got := shieldTargets(r.Evaluate(context.Background(), chargeRulesCtx())); !equalStrings(got, []string{"MID"}) {
			t.Fatalf("with WEAK charged the engine shields %v, want [MID] (A41)", got)
		}
		_ = r.Evaluate(context.Background(), chargeRulesCtx()) // throttled: no ranking, no read
		if reads.Load() != 1 {
			t.Fatalf("charged source reads = %d, want 1 (one snapshot per ranking)", reads.Load())
		}
	})
}

func decisionsOf(cands []candidate) []Decision {
	out := make([]Decision, 0, len(cands))
	for _, cd := range cands {
		out = append(out, cd.decision)
	}
	return out
}
