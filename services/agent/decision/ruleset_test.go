package decision

import (
	"testing"

	"github.com/joustmania/agent/gamecontext"
)

func fptr(v float64) *float64 { return &v }
func bptr(v bool) *bool       { return &v }

// gameCtx builds a GameContext with active players defined as
// serial -> {skill, intensity, battery} (nil pointer = unset).
type testPlayer struct {
	skill, intensity, battery *float64
	variance                  *float64
	active                    bool
}

func gameCtx(duration float64, elims []string, players map[string]testPlayer) gamecontext.GameContext {
	c := gamecontext.GameContext{
		SessionID: "s-1",
		Players:   map[string]*gamecontext.PlayerSignals{},
	}
	c.Session.GameActive = bptr(true)
	c.Session.DurationSeconds = fptr(duration)
	c.Session.EliminationSequence = elims
	for serial, tp := range players {
		c.Players[serial] = &gamecontext.PlayerSignals{
			Serial:            serial,
			SkillLevel:        tp.skill,
			MovementIntensity: tp.intensity,
			BatteryPct:        tp.battery,
			MovementVariance:  tp.variance,
			Active:            bptr(tp.active),
		}
	}
	return c
}

func defaultFit() FitnessThresholds { return DefaultStaticConfig().FitnessThresholds }
func defaultPol() Policy            { return DefaultStaticConfig().PolicyValues }

func interventionsOf(cands []candidate) []string {
	out := make([]string, len(cands))
	for i, cd := range cands {
		out[i] = cd.decision.Intervention
	}
	return out
}

func TestEnduranceCandidates(t *testing.T) {
	players := map[string]testPlayer{
		"A": {active: true}, "B": {active: true},
	}

	t.Run("fires when young session loses players", func(t *testing.T) {
		cands := enduranceCandidates(gameCtx(30, []string{"C"}, players), defaultFit())
		if len(cands) != 2 {
			t.Fatalf("candidates = %d, want 2 (tempo + cue)", len(cands))
		}
		tempo, cue := cands[0], cands[1]
		if tempo.decision.Intervention != InterventionAdjustMusicTempo || !tempo.difficulty {
			t.Errorf("first candidate = %+v, want difficulty tempo adjust", tempo.decision.Intervention)
		}
		if cue.decision.Intervention != InterventionPlayAudioCue {
			t.Errorf("second candidate = %v, want audio cue", cue.decision.Intervention)
		}
		if cue.urgency >= tempo.urgency {
			t.Errorf("cue urgency %v must be below tempo urgency %v", cue.urgency, tempo.urgency)
		}
		for _, k := range []string{"session_duration", "min_session_seconds", "eliminations"} {
			if _, ok := tempo.decision.Fitness[k]; !ok {
				t.Errorf("tempo fitness missing %s", k)
			}
		}
	})

	t.Run("silent without eliminations", func(t *testing.T) {
		if cands := enduranceCandidates(gameCtx(30, nil, players), defaultFit()); cands != nil {
			t.Fatalf("candidates = %v, want none", interventionsOf(cands))
		}
	})

	t.Run("silent past the endurance target", func(t *testing.T) {
		if cands := enduranceCandidates(gameCtx(150, []string{"C"}, players), defaultFit()); cands != nil {
			t.Fatalf("candidates = %v, want none (session no longer young)", interventionsOf(cands))
		}
	})
}

func TestBalancedCandidates(t *testing.T) {
	t.Run("skill gap above threshold handicaps the outlier", func(t *testing.T) {
		c := gameCtx(30, nil, map[string]testPlayer{
			"LOW":  {skill: fptr(0.2), active: true},
			"HIGH": {skill: fptr(0.9), active: true},
		})
		cands := balancedCandidates(c, defaultFit())
		if len(cands) != 1 {
			t.Fatalf("candidates = %v, want only the gap rule", interventionsOf(cands))
		}
		if cands[0].decision.Intervention != InterventionAdjustPlayerSensitivity ||
			cands[0].decision.TargetSerial != "HIGH" {
			t.Fatalf("candidate = %+v, want sensitivity on HIGH", cands[0].decision)
		}
		if got := cands[0].decision.Fitness["skill_gap"]; got < 0.69 || got > 0.71 {
			t.Errorf("skill_gap fitness = %v, want 0.7", got)
		}
	})

	t.Run("gap at threshold is silent", func(t *testing.T) {
		c := gameCtx(30, nil, map[string]testPlayer{
			"A": {skill: fptr(0.3), active: true},
			"B": {skill: fptr(0.7), active: true}, // gap exactly 0.4
		})
		if cands := balancedCandidates(c, defaultFit()); len(cands) != 0 {
			t.Fatalf("candidates = %v, want none at exact threshold", interventionsOf(cands))
		}
	})

	t.Run("shields the weakest while the field shrinks", func(t *testing.T) {
		c := gameCtx(30, []string{"GONE"}, map[string]testPlayer{
			"WEAK":   {skill: fptr(0.1), active: true},
			"STRONG": {skill: fptr(0.4), active: true},
		})
		cands := balancedCandidates(c, defaultFit())
		var shield *candidate
		for i := range cands {
			if cands[i].decision.Intervention == InterventionGrantShield {
				shield = &cands[i]
			}
		}
		if shield == nil || shield.decision.TargetSerial != "WEAK" {
			t.Fatalf("candidates = %v, want grant_shield on WEAK", interventionsOf(cands))
		}
	})
}

func TestAccelerateCandidates(t *testing.T) {
	accelDominant := map[string]float64{ObjectiveAccelerate: 0.7, ObjectiveEndurance: 0.3}
	players := map[string]testPlayer{
		"A": {intensity: fptr(1.5), active: true},
		"B": {intensity: fptr(0.4), active: true},
		"C": {intensity: fptr(0.9), active: true},
	}

	t.Run("silent at or below target", func(t *testing.T) {
		if cands := accelerateCandidates(gameCtx(60, nil, players), defaultFit(), accelDominant); cands != nil {
			t.Fatalf("candidates = %v, want none at target", interventionsOf(cands))
		}
	})

	t.Run("past target speeds up only", func(t *testing.T) {
		cands := accelerateCandidates(gameCtx(80, nil, players), defaultFit(), accelDominant)
		if len(cands) != 1 || cands[0].decision.Intervention != InterventionAdjustMusicTempo {
			t.Fatalf("candidates = %v, want only tempo", interventionsOf(cands))
		}
	})

	t.Run("1.5x past target eliminates the least active", func(t *testing.T) {
		cands := accelerateCandidates(gameCtx(100, nil, players), defaultFit(), accelDominant)
		var elim *candidate
		for i := range cands {
			if cands[i].decision.Intervention == InterventionEliminatePlayer {
				elim = &cands[i]
			}
		}
		if elim == nil || elim.decision.TargetSerial != "B" {
			t.Fatalf("candidates = %v, want eliminate_player on B (lowest intensity)", interventionsOf(cands))
		}
	})

	t.Run("2x past target ends the game only when accelerate dominant", func(t *testing.T) {
		c := gameCtx(130, nil, players)
		withDominant := interventionsOf(accelerateCandidates(c, defaultFit(), accelDominant))
		if !contains(withDominant, InterventionEndGame) {
			t.Fatalf("candidates = %v, want end_game with accelerate dominant", withDominant)
		}
		tie := map[string]float64{ObjectiveAccelerate: 0.5, ObjectiveEndurance: 0.5}
		withTie := interventionsOf(accelerateCandidates(c, defaultFit(), tie))
		if contains(withTie, InterventionEndGame) {
			t.Fatalf("candidates = %v: a weight TIE must never end a game", withTie)
		}
	})
}

func TestChaosCandidates(t *testing.T) {
	t.Run("statue rule no-ops while variance is nil", func(t *testing.T) {
		c := gameCtx(30, nil, map[string]testPlayer{"A": {active: true}})
		cands := chaosCandidates(c, defaultPol(), 0, "")
		if len(cands) != 0 {
			t.Fatalf("candidates = %v, want none with nil variance and no roll", interventionsOf(cands))
		}
	})

	t.Run("statue rule fires on near-zero variance after the window", func(t *testing.T) {
		c := gameCtx(30, nil, map[string]testPlayer{"A": {variance: fptr(0.01), active: true}})
		cands := chaosCandidates(c, defaultPol(), 0, "")
		if len(cands) != 1 || cands[0].decision.TargetSerial != "A" {
			t.Fatalf("candidates = %v, want statue nudge on A", interventionsOf(cands))
		}
		if cands[0].urgency < 0.7 {
			t.Errorf("urgency = %v, want high for near-zero variance", cands[0].urgency)
		}
	})

	t.Run("statue rule respects the variance window", func(t *testing.T) {
		c := gameCtx(5, nil, map[string]testPlayer{"A": {variance: fptr(0.01), active: true}}) // 5s < 10s window
		if cands := chaosCandidates(c, defaultPol(), 0, ""); len(cands) != 0 {
			t.Fatalf("candidates = %v, want none before the variance window fills", interventionsOf(cands))
		}
	})

	t.Run("periodic roll targets the picked player", func(t *testing.T) {
		c := gameCtx(30, nil, map[string]testPlayer{"A": {active: true}})
		cands := chaosCandidates(c, defaultPol(), 0.9, "A")
		if len(cands) != 1 || cands[0].decision.Fitness["chaos_roll"] != 0.9 {
			t.Fatalf("candidates = %+v, want one roll candidate with chaos_roll fitness", cands)
		}
	})
}

func TestBatteryPolicy(t *testing.T) {
	accelDominant := map[string]float64{ObjectiveAccelerate: 1.0}
	enduranceOnly := DefaultObjectiveWeights()
	pol := defaultPol() // threshold 20

	lowBattery := map[string]testPlayer{
		"LOW": {battery: fptr(10), intensity: fptr(0.2), skill: fptr(0.1), active: true},
		"OK":  {battery: fptr(90), intensity: fptr(1.0), skill: fptr(0.9), active: true},
	}

	mk := func(intervention, target string, difficulty bool) candidate {
		return candidate{
			decision:   Decision{Intervention: intervention, TargetSerial: target, Fitness: map[string]float64{}},
			objective:  ObjectiveBalanced,
			urgency:    1,
			cost:       costOf(intervention),
			difficulty: difficulty,
		}
	}
	c := gameCtx(30, nil, lowBattery)

	t.Run("controller effect on low-battery player dropped", func(t *testing.T) {
		got := applyBatteryPolicy([]candidate{mk(InterventionSendControllerEffect, "LOW", false)}, c, pol, enduranceOnly)
		if len(got) != 0 {
			t.Fatal("effect on low-battery player must be dropped")
		}
	})

	t.Run("controller effect on healthy player kept", func(t *testing.T) {
		got := applyBatteryPolicy([]candidate{mk(InterventionSendControllerEffect, "OK", false)}, c, pol, enduranceOnly)
		if len(got) != 1 {
			t.Fatal("effect on healthy player must be kept")
		}
	})

	t.Run("targeted difficulty raise on low-battery player dropped", func(t *testing.T) {
		got := applyBatteryPolicy([]candidate{mk(InterventionAdjustPlayerSensitivity, "LOW", true)}, c, pol, enduranceOnly)
		if len(got) != 0 {
			t.Fatal("difficulty on low-battery player must be dropped")
		}
	})

	t.Run("session difficulty raise dropped while anyone is low", func(t *testing.T) {
		got := applyBatteryPolicy([]candidate{mk(InterventionAdjustMusicTempo, "", true)}, c, pol, enduranceOnly)
		if len(got) != 0 {
			t.Fatal("session-wide demand raise must be dropped while a player is low")
		}
	})

	t.Run("shield and audio kept for low-battery player", func(t *testing.T) {
		got := applyBatteryPolicy([]candidate{
			mk(InterventionGrantShield, "LOW", false),
			mk(InterventionPlayAudioCue, "", false),
		}, c, pol, enduranceOnly)
		if len(got) != 2 {
			t.Fatalf("kept = %v, want shield + audio kept", interventionsOf(got))
		}
	})

	t.Run("eliminate low-battery player is accelerate-only graceful exit", func(t *testing.T) {
		cand := []candidate{mk(InterventionEliminatePlayer, "LOW", false)}
		if got := applyBatteryPolicy(cand, c, pol, enduranceOnly); len(got) != 0 {
			t.Fatal("low-battery eliminate must be dropped without accelerate dominance")
		}
		got := applyBatteryPolicy(cand, c, pol, accelDominant)
		if len(got) != 1 {
			t.Fatal("low-battery eliminate must be kept under accelerate dominance")
		}
		if got[0].decision.Fitness["battery_pct"] != 10 || got[0].decision.Fitness["battery_threshold"] != 20 {
			t.Errorf("graceful-exit fitness = %v, want battery values recorded", got[0].decision.Fitness)
		}
	})

	t.Run("threshold 0 disables the policy", func(t *testing.T) {
		off := pol
		off.BatteryThreshold = 0
		got := applyBatteryPolicy([]candidate{mk(InterventionSendControllerEffect, "LOW", false)}, c, off, enduranceOnly)
		if len(got) != 1 {
			t.Fatal("policy off must keep all candidates")
		}
	})
}

func TestAccelerateDominant(t *testing.T) {
	cases := []struct {
		name    string
		weights map[string]float64
		want    bool
	}{
		{"strictly dominant", map[string]float64{ObjectiveAccelerate: 0.7, ObjectiveEndurance: 0.3}, true},
		{"tie is not dominance", map[string]float64{ObjectiveAccelerate: 0.5, ObjectiveChaos: 0.5}, false},
		{"absent", map[string]float64{ObjectiveEndurance: 1.0}, false},
		{"sole weight", map[string]float64{ObjectiveAccelerate: 1.0}, true},
	}
	for _, tc := range cases {
		if got := accelerateDominant(tc.weights); got != tc.want {
			t.Errorf("%s: accelerateDominant = %v, want %v", tc.name, got, tc.want)
		}
	}
}

func contains(list []string, v string) bool {
	for _, s := range list {
		if s == v {
			return true
		}
	}
	return false
}
