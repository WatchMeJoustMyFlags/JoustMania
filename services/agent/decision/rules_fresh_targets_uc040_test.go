package decision

import (
	"testing"

	"github.com/joustmania/agent/gamecontext"
)

// rules_fresh_targets_uc040_test.go covers #2517: R6 and R9 pick their target only
// among the alive players with fresh readings (UC-040 A46 step 5, A49 step 4,
// BR-037). A Fight Club or Tournament player who just went back to waiting is
// still alive in the context until it is evicted (about 5 s), but its readings
// stopped with its gauges, so neither rule may pick it.

// waitingCtx is freshCtx plus W, a player who went back to waiting: still alive in
// the context, its readings stopped 4 s ago, and the lowest intensity of all.
func waitingCtx(duration float64, fresh map[string]testPlayer) (c gamecontext.GameContext) {
	players := map[string]testPlayer{"W": {intensity: fptr(0.01), active: true}}
	for k, v := range fresh {
		players[k] = v
	}
	c = freshCtx(duration, nil, players)
	setTrack(&c, "W", trackOld)
	return c
}

// UC: UC-040 A46 A49 BR-037
func TestFreshTargets_R6(t *testing.T) {
	accelDominant := map[string]float64{ObjectiveAccelerate: 1}
	r6 := func(cands []candidate) *candidate {
		for i := range cands {
			if cands[i].rule == "R6" {
				return &cands[i]
			}
		}
		return nil
	}
	t.Run("UC-040/A46 R6 never eliminates a waiting player with stopped readings", func(t *testing.T) {
		c := waitingCtx(100, map[string]testPlayer{
			"A": {intensity: fptr(0.2), active: true},
			"B": {intensity: fptr(0.9), active: true},
			"C": {intensity: fptr(1.4), active: true},
		})
		cd := r6(accelerateCandidates(c, defaultFit(), accelDominant))
		if cd == nil {
			t.Fatal("no R6 candidate with three fresh players")
		}
		if cd.decision.TargetSerial != "A" {
			t.Fatalf("R6 target = %s, want A (the least active fresh player, not waiting W)", cd.decision.TargetSerial)
		}
	})
	t.Run("UC-040/A49 R6's more-than-two guard counts only fresh players", func(t *testing.T) {
		c := waitingCtx(100, map[string]testPlayer{
			"A": {intensity: fptr(0.2), active: true},
			"B": {intensity: fptr(0.9), active: true},
		})
		if cd := r6(accelerateCandidates(c, defaultFit(), accelDominant)); cd != nil {
			t.Fatalf("R6 proposed %s in a duel with one waiting player, want none", cd.decision.TargetSerial)
		}
	})
}

// UC: UC-040 A46 A49 BR-037
func TestFreshTargets_R9(t *testing.T) {
	t.Run("UC-040/A46 R9 never draws a waiting player with stopped readings", func(t *testing.T) {
		c := waitingCtx(5, map[string]testPlayer{"A": {active: true}, "B": {active: true}})
		cfg := DefaultStaticConfig()
		r := NewObjectiveRules(&cfg)
		seen := map[string]int{}
		for i := 0; i < 200; i++ {
			_, target := r.chaosRoll(c)
			seen[target]++
		}
		if seen["W"] != 0 {
			t.Fatalf("R9 drew waiting W %d times of 200, want 0 (%v)", seen["W"], seen)
		}
		if seen["A"] == 0 || seen["B"] == 0 {
			t.Fatalf("R9 draws = %v, want both fighters drawn", seen)
		}
	})
	t.Run("UC-040/BR-037 R9 proposes nothing when no player has fresh readings", func(t *testing.T) {
		c := waitingCtx(5, nil)
		cfg := DefaultStaticConfig()
		r := NewObjectiveRules(&cfg)
		if _, target := r.chaosRoll(c); target != "" {
			t.Fatalf("R9 target = %q, want none", target)
		}
	})
}
