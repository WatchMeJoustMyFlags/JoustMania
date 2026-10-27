package gamecontext

import "testing"

// UC: UC-040 A37
// UC: UC-042 BR-007
// TestApplyMetrics_ShieldHoldGauges: the hold-mode gauges (#2096, telemetry.md §6)
// land in the game's context: the latched mode and values on the session, the
// energy, budget left, active flag and totals on the player. A snapshot handed out
// before a later update keeps its values.
func TestApplyMetrics_ShieldHoldGauges(t *testing.T) {
	game := map[string]string{"game_id": "g1", "game_kind": "primary"}
	with := func(extra map[string]string) map[string]string {
		out := map[string]string{}
		for k, v := range game {
			out[k] = v
		}
		for k, v := range extra {
			out[k] = v
		}
		return out
	}
	t.Run("UC-040/A37 session mode and settings", func(t *testing.T) {
		s := newTestStore()
		if !s.ApplyMetrics(metricsWith(metricShieldActivationMode, 1, with(map[string]string{"mode": "hold"}))) {
			t.Fatal("mode gauge not applied")
		}
		if !s.ApplyMetrics(metricsWith(metricShieldHoldSetting, 5, with(map[string]string{"setting": ShieldSettingEnergyMax}))) {
			t.Fatal("setting gauge not applied")
		}
		s.ApplyMetrics(metricsWith(metricShieldHoldSetting, 0.3, with(map[string]string{"setting": ShieldSettingMinBurst})))
		c := s.Snapshot()
		if !c.Session.IsShieldHold() {
			t.Fatalf("ShieldActivation = %v, want hold", c.Session.ShieldActivation)
		}
		if v := c.Session.ShieldSetting(ShieldSettingEnergyMax); v == nil || *v != 5 {
			t.Fatalf("energy max = %v, want 5", v)
		}
		if v := c.Session.ShieldSetting(ShieldSettingMinBurst); v == nil || *v != 0.3 {
			t.Fatalf("min burst = %v, want 0.3", v)
		}
		if c.SessionID != "g1" {
			t.Fatalf("session id = %q, want the adopted g1", c.SessionID)
		}
	})
	t.Run("UC-040/A37 a zero mode datapoint or a missing label is ignored", func(t *testing.T) {
		s := newTestStore()
		if s.ApplyMetrics(metricsWith(metricShieldActivationMode, 0, with(map[string]string{"mode": "hold"}))) {
			t.Fatal("a 0 mode datapoint applied")
		}
		if s.ApplyMetrics(metricsWith(metricShieldHoldSetting, 5, game)) {
			t.Fatal("a setting without its label applied")
		}
		if s.ApplyMetrics(metricsWith(metricPlayerShieldTotal, 2, with(map[string]string{"serial": "A"}))) {
			t.Fatal("a total without its label applied")
		}
		if s.Snapshot().Session.IsShieldHold() {
			t.Fatal("hold without a hold datapoint")
		}
	})
	t.Run("UC-042/BR-007 player pool and totals", func(t *testing.T) {
		s := newTestStore()
		a := with(map[string]string{"serial": "A"})
		s.ApplyMetrics(metricsWith(metricPlayerShieldEnergy, 2.4, a))
		s.ApplyMetrics(metricsWith(metricPlayerShieldBudgetLeft, 9.1, a))
		s.ApplyMetrics(metricsWith(metricPlayerShieldActive, 1, a))
		s.ApplyMetrics(metricsWith(metricPlayerShieldTotal, 3, with(map[string]string{"serial": "A", "total": "activations"})))
		before := s.Snapshot()
		s.ApplyMetrics(metricsWith(metricPlayerShieldTotal, 4, with(map[string]string{"serial": "A", "total": "activations"})))
		s.ApplyMetrics(metricsWith(metricPlayerShieldActive, 0, a))
		p := before.Players["A"]
		if p == nil || p.ShieldEnergy == nil || *p.ShieldEnergy != 2.4 {
			t.Fatalf("energy = %+v, want 2.4", p)
		}
		if p.ShieldBudgetRemaining == nil || *p.ShieldBudgetRemaining != 9.1 {
			t.Fatalf("budget = %v, want 9.1", p.ShieldBudgetRemaining)
		}
		if p.ShieldActive == nil || !*p.ShieldActive {
			t.Fatal("active not recorded")
		}
		if p.ShieldTotals["activations"] != 3 {
			t.Fatalf("handed-out snapshot changed: activations = %v, want 3", p.ShieldTotals["activations"])
		}
		after := s.Snapshot().Players["A"]
		if after.ShieldTotals["activations"] != 4 || *after.ShieldActive {
			t.Fatalf("after = %+v, want activations 4 and inactive", after)
		}
	})
}
