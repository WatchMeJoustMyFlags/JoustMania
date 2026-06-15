package decision

import (
	"reflect"
	"testing"

	"github.com/joustmania/agent/gamecontext"
)

func fp(v float64) *float64 { return &v }
func bp(v bool) *bool       { return &v }

// ctxWithDuration builds a session-only context with the given duration.
func ctxWithDuration(dur *float64) gamecontext.GameContext {
	return gamecontext.GameContext{
		SessionID: "s1",
		Session:   gamecontext.SessionSignals{DurationSeconds: dur},
		Players:   map[string]*gamecontext.PlayerSignals{},
	}
}

func TestEvaluateEndurance(t *testing.T) {
	fit := defaultFit() // min_session_seconds = 120
	tests := []struct {
		name          string
		dur           *float64
		wantEvaluated bool
		wantSatisfied bool
		wantProgress  float64
	}{
		{name: "missing duration skipped", dur: nil, wantEvaluated: false},
		{name: "empty session zero progress", dur: fp(0), wantEvaluated: true, wantSatisfied: false, wantProgress: 0},
		{name: "halfway", dur: fp(60), wantEvaluated: true, wantSatisfied: false, wantProgress: 0.5},
		{name: "at threshold satisfied", dur: fp(120), wantEvaluated: true, wantSatisfied: true, wantProgress: 1},
		{name: "past threshold clamped", dur: fp(300), wantEvaluated: true, wantSatisfied: true, wantProgress: 1},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			r, ok := evaluateEndurance(ctxWithDuration(tc.dur), fit)
			if ok != tc.wantEvaluated {
				t.Fatalf("evaluated = %v, want %v", ok, tc.wantEvaluated)
			}
			if !ok {
				return
			}
			if r.Satisfied != tc.wantSatisfied {
				t.Errorf("satisfied = %v, want %v", r.Satisfied, tc.wantSatisfied)
			}
			if r.Progress != tc.wantProgress {
				t.Errorf("progress = %v, want %v", r.Progress, tc.wantProgress)
			}
			if r.Pressure != 1-tc.wantProgress {
				t.Errorf("pressure = %v, want %v", r.Pressure, 1-tc.wantProgress)
			}
			if r.Values["endurance.min_session_seconds"] != 120 {
				t.Errorf("threshold not recorded: %v", r.Values)
			}
		})
	}
}

func TestEvaluateAccelerate(t *testing.T) {
	fit := defaultFit() // target_session_seconds = 60
	tests := []struct {
		name          string
		dur           *float64
		wantEvaluated bool
		wantSatisfied bool
		wantProgress  float64
	}{
		{name: "missing duration skipped", dur: nil, wantEvaluated: false},
		{name: "under target satisfied", dur: fp(30), wantEvaluated: true, wantSatisfied: true, wantProgress: 1},
		{name: "at target boundary", dur: fp(60), wantEvaluated: true, wantSatisfied: true, wantProgress: 1},
		{name: "half target overshoot", dur: fp(90), wantEvaluated: true, wantSatisfied: false, wantProgress: 0.5},
		{name: "full target overshoot fails", dur: fp(120), wantEvaluated: true, wantSatisfied: false, wantProgress: 0},
		{name: "way past clamped to zero", dur: fp(300), wantEvaluated: true, wantSatisfied: false, wantProgress: 0},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			r, ok := evaluateAccelerate(ctxWithDuration(tc.dur), fit)
			if ok != tc.wantEvaluated {
				t.Fatalf("evaluated = %v, want %v", ok, tc.wantEvaluated)
			}
			if !ok {
				return
			}
			if r.Satisfied != tc.wantSatisfied {
				t.Errorf("satisfied = %v, want %v", r.Satisfied, tc.wantSatisfied)
			}
			if r.Progress != tc.wantProgress {
				t.Errorf("progress = %v, want %v", r.Progress, tc.wantProgress)
			}
		})
	}
}

// playersCtx builds a context with the given players (active by default).
func playersCtx(players ...*gamecontext.PlayerSignals) gamecontext.GameContext {
	m := make(map[string]*gamecontext.PlayerSignals, len(players))
	for _, p := range players {
		if p.Active == nil {
			p.Active = bp(true)
		}
		m[p.Serial] = p
	}
	return gamecontext.GameContext{SessionID: "s1", Players: m}
}

func TestEvaluateBalanced_SkillGap(t *testing.T) {
	fit := defaultFit() // max_skill_gap = 0.4
	tests := []struct {
		name          string
		players       []*gamecontext.PlayerSignals
		wantEvaluated bool
		wantSatisfied bool
		wantGap       float64
	}{
		{name: "no skills skipped", players: []*gamecontext.PlayerSignals{{Serial: "a"}}, wantEvaluated: false},
		{
			name:          "single skill skipped",
			players:       []*gamecontext.PlayerSignals{{Serial: "a", SkillLevel: fp(0.5)}},
			wantEvaluated: false,
		},
		{
			name: "small gap satisfied",
			players: []*gamecontext.PlayerSignals{
				{Serial: "a", SkillLevel: fp(0.5)}, {Serial: "b", SkillLevel: fp(0.7)},
			},
			wantEvaluated: true, wantSatisfied: true, wantGap: 0.2,
		},
		{
			name: "gap at threshold satisfied",
			players: []*gamecontext.PlayerSignals{
				{Serial: "a", SkillLevel: fp(0.5)}, {Serial: "b", SkillLevel: fp(0.9)},
			},
			wantEvaluated: true, wantSatisfied: true, wantGap: 0.4,
		},
		{
			name: "gap over threshold fails",
			players: []*gamecontext.PlayerSignals{
				{Serial: "a", SkillLevel: fp(0.1)}, {Serial: "b", SkillLevel: fp(0.9)},
			},
			wantEvaluated: true, wantSatisfied: false, wantGap: 0.8,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			r, ok := evaluateBalanced(playersCtx(tc.players...), fit)
			if ok != tc.wantEvaluated {
				t.Fatalf("evaluated = %v, want %v", ok, tc.wantEvaluated)
			}
			if !ok {
				return
			}
			if r.Satisfied != tc.wantSatisfied {
				t.Errorf("satisfied = %v, want %v (values %v)", r.Satisfied, tc.wantSatisfied, r.Values)
			}
			if got := r.Values["balanced.skill_gap"]; got < tc.wantGap-1e-9 || got > tc.wantGap+1e-9 {
				t.Errorf("skill_gap = %v, want %v", got, tc.wantGap)
			}
		})
	}
}

// TestEvaluateBalanced_SpikeSurvival asserts the Option-2 contract: spike-survival
// is RECORDED for observability (#1125) but does NOT gate balanced. Balanced binds
// on skill-gap only. Here a small skill gap (0.2 <= 0.4) satisfies balanced even
// though spike-survival ratio (0.667 < 0.8 threshold) would have failed the old
// two-signal check — proving the ratio no longer sets satisfied=false.
func TestEvaluateBalanced_SpikeSurvival(t *testing.T) {
	fit := defaultFit() // spike_survival_threshold = 0.8, max_skill_gap = 0.4
	// Three players carrying RETAINED whole-game aggregates + skill levels. The
	// spike-survival ratio is recorded: a player "survives" when std=sqrt(varAgg)
	// <= 0.30*peak. With peak=2.0 the survive bound is varAgg <= (0.3*2)^2 = 0.36.
	// Two survive, one does not -> ratio 2/3 = 0.667 (< 0.8) — recorded but NOT
	// gating. Skill gap is 0.2 (within 0.4) -> balanced satisfied on skill-gap.
	c := playersCtx(
		&gamecontext.PlayerSignals{Serial: "a", SkillLevel: fp(0.5), PeakAccel: fp(2.0), MovementVarianceAggregate: fp(0.09)}, // std 0.3 <= 0.6 -> survive
		&gamecontext.PlayerSignals{Serial: "b", SkillLevel: fp(0.7), PeakAccel: fp(2.0), MovementVarianceAggregate: fp(0.25)}, // std 0.5 <= 0.6 -> survive
		&gamecontext.PlayerSignals{Serial: "c", SkillLevel: fp(0.6), PeakAccel: fp(2.0), MovementVarianceAggregate: fp(1.0)},  // std 1.0 > 0.6 -> spike-thrown
	)
	r, ok := evaluateBalanced(c, fit)
	if !ok {
		t.Fatal("expected evaluation")
	}
	// Spike-survival ratio is RECORDED for observability...
	if got := r.Values["balanced.spike_survival_ratio"]; got < 0.66 || got > 0.67 {
		t.Errorf("survival_ratio = %v, want ~0.667 (recorded for observability)", got)
	}
	// ...but does NOT gate: balanced is satisfied on the small skill gap (0.2 <= 0.4)
	// even though the recorded ratio 0.667 is below the 0.8 threshold.
	if !r.Satisfied {
		t.Error("balanced must be satisfied on skill-gap (0.2 <= 0.4); spike-survival must NOT gate")
	}
	// Progress derives from skill-gap only, not min(skill, survival).
	if got := r.Values["balanced.skill_gap_progress"]; r.Progress != got {
		t.Errorf("progress = %v, want skill_gap_progress %v (spike-survival must not bind progress)", r.Progress, got)
	}
}

// TestEvaluateBalanced_SpikeSurvivalAtConclusion asserts the Option-2 contract at
// game conclusion: every player is eliminated (Active=false) and the instantaneous
// MovementIntensity/MovementVariance are frozen, yet the spike-survival ratio is
// still RECORDED from the RETAINED whole-game aggregates (PeakAccel +
// MovementVarianceAggregate) — present in Values for observability/#1125 — while
// balanced's verdict binds on skill-gap only.
func TestEvaluateBalanced_SpikeSurvivalAtConclusion(t *testing.T) {
	fit := defaultFit() // spike_survival_threshold = 0.8
	dead := func(serial string, skill, peak, varAgg float64) *gamecontext.PlayerSignals {
		return &gamecontext.PlayerSignals{
			Serial:                    serial,
			Active:                    bp(false), // eliminated at conclusion
			SkillLevel:                fp(skill),
			PeakAccel:                 fp(peak),
			MovementVarianceAggregate: fp(varAgg),
			// Frozen last-sample instantaneous values are deliberately set; the
			// spike-survival computation ignores them entirely.
			MovementIntensity: fp(0.1),
			MovementVariance:  fp(5.0),
		}
	}
	c := playersCtx(
		dead("a", 0.5, 2.0, 0.09), // survives: std 0.3 <= 0.30*2.0 = 0.6
		dead("b", 0.7, 2.0, 0.25), // survives: std 0.5 <= 0.6
		dead("c", 0.6, 2.0, 1.0),  // thrown by spikes: std 1.0 > 0.6
	)
	r, ok := evaluateBalanced(c, fit)
	if !ok {
		t.Fatal("expected evaluation at conclusion (skill-gap binds balanced)")
	}
	if _, present := r.Values["balanced.spike_survival_ratio"]; !present {
		t.Fatal("spike_survival_ratio must be RECORDED at conclusion for observability/#1125")
	}
	if got := r.Values["balanced.spike_survival_ratio"]; got < 0.66 || got > 0.67 {
		t.Errorf("conclusion survival_ratio = %v, want ~0.667 from retained aggregates", got)
	}
	// Verdict binds on skill-gap only (gap 0.2 <= 0.4 -> satisfied), not the
	// recorded sub-threshold ratio.
	if !r.Satisfied {
		t.Error("balanced must be satisfied on skill-gap at conclusion; spike-survival must NOT gate")
	}
}

func TestEvaluateBalanced_MissingSignalsSkipped(t *testing.T) {
	// Players are active but have neither skill nor movement signals -> nothing
	// computable -> skipped entirely.
	c := playersCtx(
		&gamecontext.PlayerSignals{Serial: "a"},
		&gamecontext.PlayerSignals{Serial: "b"},
	)
	if _, ok := evaluateBalanced(c, defaultFit()); ok {
		t.Error("balanced must be skipped when no sub-check is computable")
	}
}

func TestEvaluateFitness_EmptySession(t *testing.T) {
	// Empty session (no duration, no players): every function is skipped, so no
	// fitness is fabricated.
	eval := EvaluateFitness(gamecontext.GameContext{SessionID: "s1"}, defaultFit())
	if len(eval.Results) != 0 {
		t.Errorf("empty session produced %d fitness results, want 0", len(eval.Results))
	}
	if len(eval.Evaluated()) != 0 {
		t.Errorf("empty session produced span values, want none")
	}
	// chaos never has a fitness function.
	if eval.Pressure(ObjectiveChaos) != 0 {
		t.Error("chaos must never report fitness pressure")
	}
}

func TestEvaluateFitness_KeyVocabulary(t *testing.T) {
	// A fully-populated context exercises every function and pins the dotted key
	// vocabulary recorded on the span.
	c := playersCtx(
		&gamecontext.PlayerSignals{Serial: "a", SkillLevel: fp(0.1), PeakAccel: fp(1.0), MovementVarianceAggregate: fp(0.5)},
		&gamecontext.PlayerSignals{Serial: "b", SkillLevel: fp(0.9), PeakAccel: fp(1.0), MovementVarianceAggregate: fp(2.0)},
	)
	c.Session.DurationSeconds = fp(90)
	eval := EvaluateFitness(c, defaultFit())

	want := []string{
		"accelerate.session_progress",
		"accelerate.session_seconds",
		"accelerate.target_session_seconds",
		"balanced.max_skill_gap",
		"balanced.skill_gap",
		"balanced.skill_gap_progress",
		"balanced.spike_survival_progress",
		"balanced.spike_survival_ratio",
		"balanced.spike_survival_threshold",
		"endurance.min_session_seconds",
		"endurance.session_progress",
		"endurance.session_seconds",
	}
	if got := sortedFitnessKeys(eval); !reflect.DeepEqual(got, want) {
		t.Errorf("fitness key vocabulary =\n%v\nwant\n%v", got, want)
	}
}

func TestFitnessPressure_FailingObjective(t *testing.T) {
	// A young session fails endurance (progress < 1 -> pressure > 0) and passes
	// accelerate (under target -> pressure 0).
	eval := EvaluateFitness(ctxWithDuration(fp(30)), defaultFit())
	if p := eval.Pressure(ObjectiveEndurance); p <= 0 {
		t.Errorf("endurance pressure = %v, want > 0 (failing)", p)
	}
	if p := eval.Pressure(ObjectiveAccelerate); p != 0 {
		t.Errorf("accelerate pressure = %v, want 0 (satisfied)", p)
	}
}
