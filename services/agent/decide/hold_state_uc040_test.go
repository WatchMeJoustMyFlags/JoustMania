package decide

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// The hold-mode fields of the decide state (#2096; UC-040 A36, A37;
// decide-contract.md §1.2): present only in a game whose game_shield_activation_mode
// is hold, so every other state, wire and digest stays byte-identical.

// withHoldGauges adds the hold gauges of a game in mode to c (A: 2.4 s of 5,
// 9.1 s budget left; B active).
func withHoldGauges(c gamecontext.GameContext, mode string) gamecontext.GameContext {
	c.Session.ShieldActivation = &mode
	c.Session.ShieldSettings = map[string]float64{
		gamecontext.ShieldSettingEnergyMax:    5,
		gamecontext.ShieldSettingEnergyBudget: 15,
		gamecontext.ShieldSettingRecharge:     0.25,
		gamecontext.ShieldSettingMinBurst:     0.3,
	}
	a, b := *c.Players["A"], *c.Players["B"]
	a.ShieldEnergy, a.ShieldBudgetRemaining, a.ShieldActive = fptr(2.4), fptr(9.1), bptr(false)
	b.ShieldEnergy, b.ShieldBudgetRemaining, b.ShieldActive = fptr(1), fptr(6), bptr(true)
	c.Players["A"], c.Players["B"] = &a, &b
	return c
}

// UC: UC-040 main step 4 A36 A37
func TestBuildState_HoldFields(t *testing.T) {
	t.Run("UC-040/A37 a trigger game with shield gauges keeps its state, wire and digest", func(t *testing.T) {
		plain := BuildStateWith(sampleCtx(), Annotations{Charged: map[string]bool{"B": true}})
		trigger := BuildStateWith(withHoldGauges(sampleCtx(), "trigger"), Annotations{Charged: map[string]bool{"B": true}})
		if trigger.Digest != plain.Digest || trigger.Narrative != plain.Narrative {
			t.Fatalf("a non-hold game changed with the shield gauges:\n%s\nvs\n%s", trigger.Narrative, plain.Narrative)
		}
		raw, _ := json.Marshal(trigger)
		if strings.Contains(string(raw), "shield_energy") || strings.Contains(string(raw), "shield_activation") {
			t.Fatalf("non-hold wire carries hold fields: %s", raw)
		}
		if !playerByID(t, trigger, "B").ShieldCharged {
			t.Fatal("trigger mode keeps shield_charged")
		}
	})
	t.Run("UC-040/A36 a hold game carries the pool, the latched values and the narrative token", func(t *testing.T) {
		c := withHoldGauges(sampleCtx(), "hold")
		st := BuildStateWith(c, Annotations{Charged: map[string]bool{"B": true}})
		a, b := playerByID(t, st, "A"), playerByID(t, st, "B")
		if a.ShieldEnergyS == nil || *a.ShieldEnergyS != 2.4 || a.ShieldBudgetRemainingS == nil || *a.ShieldBudgetRemainingS != 9.1 || a.ShieldActive {
			t.Fatalf("A = %+v, want energy 2.4, budget 9.1, inactive", a)
		}
		if b.ShieldCharged {
			t.Fatal("shield_charged is not emitted in a hold game")
		}
		if !b.ShieldActive || !b.Shielded {
			t.Fatalf("B = %+v, want active and shielded", b)
		}
		if playerByID(t, st, "C").ShieldEnergyS != nil {
			t.Fatal("a dead player carries no pool")
		}
		if st.ShieldActivation != "hold" || *st.ShieldEnergyMaxS != 5 || *st.ShieldBudgetS != 15 || *st.ShieldRechargePerS != 0.25 || *st.ShieldMinBurstS != 0.3 {
			t.Fatalf("latched values = %+v", st)
		}
		for _, want := range []string{" shield=hold shield_cap=5 shield_budget=15 shield_recharge=0.25 shield_min_burst=0.3\n", " shield_energy=2.4/5 budget=9.1", " shield_energy=1.0/5 budget=6.0 shield_active"} {
			if !strings.Contains(st.Narrative, want) {
				t.Errorf("narrative lacks %q:\n%s", want, st.Narrative)
			}
		}
		other := withHoldGauges(sampleCtx(), "hold")
		other.Players["A"].ShieldEnergy = fptr(2.5)
		if BuildState(other).Digest == BuildState(c).Digest {
			t.Error("the digest must change with the energy")
		}
	})
	t.Run("UC-040/A36 a grant in a hold game is energy, not protection", func(t *testing.T) {
		c := withHoldGauges(sampleCtx(), "hold")
		c.Timeline = append(c.Timeline, gamecontext.TimelineEvent{At: c.UpdatedAt.Add(-time.Second), Kind: gamecontext.EventIntervention, Detail: "grant_shield", Serial: "A"})
		if playerByID(t, BuildState(c), "A").Shielded {
			t.Fatal("a grant_shield in a hold game must not mark the player shielded")
		}
	})
	t.Run("UC-040/A37 grants in flight add to the energy, at most the cap", func(t *testing.T) {
		st := BuildStateWith(withHoldGauges(sampleCtx(), "hold"), Annotations{ShieldInFlight: map[string]float64{"A": 2, "B": 9}})
		if got := *playerByID(t, st, "A").ShieldEnergyS; got < 4.4-1e-9 || got > 4.4+1e-9 {
			t.Fatalf("A energy = %v, want 4.4", got)
		}
		if got := st.HoldRefusal(playerByID(t, st, "A")); got != "" {
			t.Fatalf("A refusal = %q, want none at 4.4 of 5", got)
		}
		if got := *playerByID(t, st, "B").ShieldEnergyS; got != 5 {
			t.Fatalf("B energy = %v, want the cap 5", got)
		}
		st = BuildStateWith(withHoldGauges(sampleCtx(), "hold"), Annotations{ShieldInFlight: map[string]float64{"A": 2.2}})
		if got := st.HoldRefusal(playerByID(t, st, "A")); got != HoldRefusalPoolFull {
			t.Fatalf("A refusal = %q, want shield_pool_full at 4.6 of 5", got)
		}
	})
}

// UC: UC-040 A37 BR-024
func TestHoldShieldRefusal(t *testing.T) {
	for _, tc := range []struct {
		name                    string
		energy, budget, cap, mb *float64
		active                  bool
		inFlight                float64
		want                    string
	}{
		{"UC-040/A37 room is no refusal", fptr(1), fptr(10), fptr(5), fptr(0.3), false, 0, ""},
		{"UC-040/A37 within 0.5 s of the cap", fptr(4.5), fptr(10), fptr(5), fptr(0.3), false, 0, HoldRefusalPoolFull},
		{"UC-040/A37 in flight counts", fptr(2), fptr(10), fptr(5), fptr(0.3), false, 2.5, HoldRefusalPoolFull},
		{"UC-040/A37 full is min(cap, budget left)", fptr(1.5), fptr(2), fptr(5), fptr(0.3), false, 0, HoldRefusalPoolFull},
		{"UC-040/A37 budget below the minimum burst", fptr(0), fptr(0.29), fptr(5), fptr(0.3), false, 0, HoldRefusalBudgetExhausted},
		{"UC-040/A37 active skips", fptr(5), fptr(0), fptr(5), fptr(0.3), true, 0, ""},
		{"UC-040/A37 no energy fails open for the pool", nil, fptr(10), fptr(5), fptr(0.3), false, 0, ""},
		{"UC-040/A37 no cap fails open", fptr(5), fptr(10), nil, fptr(0.3), false, 0, ""},
		{"UC-040/A37 no minimum burst skips the budget check", fptr(0), fptr(0.1), fptr(5), nil, false, 0, HoldRefusalPoolFull},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := HoldShieldRefusal(tc.energy, tc.budget, tc.active, tc.cap, tc.mb, tc.inFlight); got != tc.want {
				t.Fatalf("refusal = %q, want %q", got, tc.want)
			}
		})
	}
}
