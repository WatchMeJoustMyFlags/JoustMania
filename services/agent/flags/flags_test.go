package flags

import (
	"context"
	"errors"
	"reflect"
	"testing"
	"time"

	"github.com/open-feature/go-sdk/openfeature"
)

// stubEvaluator is a hand-rolled FlagSource backend that returns canned values
// (or errors) per flag key, letting tests exercise the coercion and
// fallback-to-default paths without a live flagd.
type stubEvaluator struct {
	booleans map[string]bool
	strings  map[string]string
	ints     map[string]int64
	floats   map[string]float64
	objects  map[string]any
	errs     map[string]error
}

func (s stubEvaluator) BooleanValue(_ context.Context, flag string, def bool, _ openfeature.EvaluationContext, _ ...openfeature.Option) (bool, error) {
	if err := s.errs[flag]; err != nil {
		return def, err
	}
	if v, ok := s.booleans[flag]; ok {
		return v, nil
	}
	return def, nil
}

func (s stubEvaluator) StringValue(_ context.Context, flag string, def string, _ openfeature.EvaluationContext, _ ...openfeature.Option) (string, error) {
	if err := s.errs[flag]; err != nil {
		return def, err
	}
	if v, ok := s.strings[flag]; ok {
		return v, nil
	}
	return def, nil
}

func (s stubEvaluator) IntValue(_ context.Context, flag string, def int64, _ openfeature.EvaluationContext, _ ...openfeature.Option) (int64, error) {
	if err := s.errs[flag]; err != nil {
		return def, err
	}
	if v, ok := s.ints[flag]; ok {
		return v, nil
	}
	return def, nil
}

func (s stubEvaluator) FloatValue(_ context.Context, flag string, def float64, _ openfeature.EvaluationContext, _ ...openfeature.Option) (float64, error) {
	if err := s.errs[flag]; err != nil {
		return def, err
	}
	if v, ok := s.floats[flag]; ok {
		return v, nil
	}
	return def, nil
}

func (s stubEvaluator) ObjectValue(_ context.Context, flag string, def any, _ openfeature.EvaluationContext, _ ...openfeature.Option) (any, error) {
	if err := s.errs[flag]; err != nil {
		return def, err
	}
	if v, ok := s.objects[flag]; ok {
		return v, nil
	}
	return def, nil
}

func TestEvaluate_FlagdShape(t *testing.T) {
	stub := stubEvaluator{
		booleans: map[string]bool{keyEnabled: true},
		strings: map[string]string{
			keyMode:          "llm",
			keyModel:         "claude",
			keyPromptVariant: "aggressive",
		},
		ints: map[string]int64{
			keyBatteryThreshold:            30,
			keyMovementVarianceWindow:      20,
			keyMaxInterventionsPerMinute:   4,
			keyEnduranceMinSessionSeconds:  300,
			keyAccelerateTargetSessionSecs: 30,
		},
		floats: map[string]float64{
			keyBalancedMaxSkillGap:          0.2,
			keyBalancedSpikeSurvival:        0.9,
			keyBluetoothMaxEventGapMs:       25,
			keyBluetoothMaxDroppedEventsPct: 0.05,
			keyBluetoothMinMovementUpdateHz: 20,
		},
		objects: map[string]any{
			// flagd surfaces object flags as map[string]any / []any.
			keyObjectives:           map[string]any{"endurance": 0.7, "chaos": 0.3},
			keyInterventionsAllowed: []any{"play_audio_cue", "grant_shield"},
		},
	}
	f := New(stub, nil)
	got := f.Evaluate(context.Background())

	if !got.Enabled {
		t.Errorf("Enabled = false, want true")
	}
	if got.Mode != "llm" {
		t.Errorf("Mode = %q, want llm", got.Mode)
	}
	wantObj := map[string]float64{"endurance": 0.7, "chaos": 0.3}
	if !reflect.DeepEqual(got.Objectives, wantObj) {
		t.Errorf("Objectives = %v, want %v", got.Objectives, wantObj)
	}
	wantCap := Capability{Model: "claude", PromptVariant: "aggressive"}
	if got.Capability != wantCap {
		t.Errorf("Capability = %+v, want %+v", got.Capability, wantCap)
	}
	wantAllowed := []string{"play_audio_cue", "grant_shield"}
	if !reflect.DeepEqual(got.InterventionsAllowed, wantAllowed) {
		t.Errorf("InterventionsAllowed = %v, want %v", got.InterventionsAllowed, wantAllowed)
	}
	wantPolicy := Policy{BatteryThreshold: 30, MovementVarianceWindow: 20, MaxInterventionsPerMinute: 4}
	if got.Policy != wantPolicy {
		t.Errorf("Policy = %+v, want %+v", got.Policy, wantPolicy)
	}
	wantFitness := Fitness{
		EnduranceMinSessionSeconds:     300,
		BalancedMaxSkillGap:            0.2,
		BalancedSpikeSurvivalThreshold: 0.9,
		AccelerateTargetSessionSeconds: 30,
	}
	if got.Fitness != wantFitness {
		t.Errorf("Fitness = %+v, want %+v", got.Fitness, wantFitness)
	}
	wantBT := BluetoothFitness{MaxEventGapMs: 25, MaxDroppedEventsPct: 0.05, MinMovementUpdateHz: 20}
	if got.BluetoothFitness != wantBT {
		t.Errorf("BluetoothFitness = %+v, want %+v", got.BluetoothFitness, wantBT)
	}
}

func TestEvaluate_DefaultsWhenMissing(t *testing.T) {
	// Empty stub returns the supplied defaults for every key, simulating
	// undefined flags. The wrapper must surface its safe defaults.
	f := New(stubEvaluator{}, nil)
	got := f.Evaluate(context.Background())

	if got.Enabled != DefaultEnabled {
		t.Errorf("Enabled = %v, want %v (fail-closed)", got.Enabled, DefaultEnabled)
	}
	if got.Mode != DefaultMode {
		t.Errorf("Mode = %q, want %q", got.Mode, DefaultMode)
	}
	if !reflect.DeepEqual(got.Objectives, map[string]float64{"endurance": 1.0}) {
		t.Errorf("Objectives = %v, want endurance:1.0", got.Objectives)
	}
	if len(got.InterventionsAllowed) != 0 {
		t.Errorf("InterventionsAllowed = %v, want empty", got.InterventionsAllowed)
	}
	wantCap := Capability{Model: DefaultModel, PromptVariant: DefaultPromptVariant}
	if got.Capability != wantCap {
		t.Errorf("Capability = %+v, want defaults %+v", got.Capability, wantCap)
	}
	wantPolicy := Policy{
		BatteryThreshold:          DefaultBatteryThreshold,
		MovementVarianceWindow:    DefaultMovementVarianceWindow,
		MaxInterventionsPerMinute: DefaultMaxInterventionsPerMinute,
	}
	if got.Policy != wantPolicy {
		t.Errorf("Policy = %+v, want defaults %+v", got.Policy, wantPolicy)
	}
	if got.Fitness != defaultFitness() {
		t.Errorf("Fitness = %+v, want defaults %+v", got.Fitness, defaultFitness())
	}
	if got.BluetoothFitness != defaultBluetoothFitness() {
		t.Errorf("BluetoothFitness = %+v, want defaults %+v", got.BluetoothFitness, defaultBluetoothFitness())
	}
}

// defaultFitness is the expected fitness snapshot when every flag falls back.
func defaultFitness() Fitness {
	return Fitness{
		EnduranceMinSessionSeconds:     DefaultEnduranceMinSessionSeconds,
		BalancedMaxSkillGap:            DefaultBalancedMaxSkillGap,
		BalancedSpikeSurvivalThreshold: DefaultBalancedSpikeSurvivalThreshold,
		AccelerateTargetSessionSeconds: DefaultAccelerateTargetSessionSeconds,
	}
}

// defaultBluetoothFitness is the expected infra fitness snapshot when every
// fitness.bluetooth.* flag falls back to its safe default (#735).
func defaultBluetoothFitness() BluetoothFitness {
	return BluetoothFitness{
		MaxEventGapMs:       DefaultBluetoothMaxEventGapMs,
		MaxDroppedEventsPct: DefaultBluetoothMaxDroppedEventsPct,
		MinMovementUpdateHz: DefaultBluetoothMinMovementUpdateHz,
	}
}

func TestEvaluate_DefaultsOnError(t *testing.T) {
	// Every evaluation errors (e.g. flagd unreachable). Defaults must apply and
	// the agent must come up disabled with no permitted interventions.
	boom := errors.New("flagd unreachable")
	stub := stubEvaluator{errs: map[string]error{
		keyEnabled:                      boom,
		keyMode:                         boom,
		keyObjectives:                   boom,
		keyModel:                        boom,
		keyPromptVariant:                boom,
		keyInterventionsAllowed:         boom,
		keyBatteryThreshold:             boom,
		keyMovementVarianceWindow:       boom,
		keyMaxInterventionsPerMinute:    boom,
		keyEnduranceMinSessionSeconds:   boom,
		keyBalancedMaxSkillGap:          boom,
		keyBalancedSpikeSurvival:        boom,
		keyAccelerateTargetSessionSecs:  boom,
		keyBluetoothMaxEventGapMs:       boom,
		keyBluetoothMaxDroppedEventsPct: boom,
		keyBluetoothMinMovementUpdateHz: boom,
	}}
	f := New(stub, nil)
	got := f.Evaluate(context.Background())

	if got.Enabled {
		t.Errorf("Enabled = true on error, want false (fail-closed)")
	}
	if got.Mode != DefaultMode {
		t.Errorf("Mode = %q, want %q", got.Mode, DefaultMode)
	}
	if !reflect.DeepEqual(got.Objectives, map[string]float64{"endurance": 1.0}) {
		t.Errorf("Objectives = %v, want endurance:1.0", got.Objectives)
	}
	if len(got.InterventionsAllowed) != 0 {
		t.Errorf("InterventionsAllowed = %v, want empty", got.InterventionsAllowed)
	}
	wantCap := Capability{Model: DefaultModel, PromptVariant: DefaultPromptVariant}
	if got.Capability != wantCap {
		t.Errorf("Capability = %+v on error, want defaults %+v", got.Capability, wantCap)
	}
	wantPolicy := Policy{
		BatteryThreshold:          DefaultBatteryThreshold,
		MovementVarianceWindow:    DefaultMovementVarianceWindow,
		MaxInterventionsPerMinute: DefaultMaxInterventionsPerMinute,
	}
	if got.Policy != wantPolicy {
		t.Errorf("Policy = %+v on error, want defaults %+v", got.Policy, wantPolicy)
	}
	if got.Fitness != defaultFitness() {
		t.Errorf("Fitness = %+v on error, want defaults %+v", got.Fitness, defaultFitness())
	}
	if got.BluetoothFitness != defaultBluetoothFitness() {
		t.Errorf("BluetoothFitness = %+v on error, want defaults %+v", got.BluetoothFitness, defaultBluetoothFitness())
	}
}

func TestEvaluate_UnexpectedObjectShapeFallsBack(t *testing.T) {
	stub := stubEvaluator{objects: map[string]any{
		keyObjectives:           "not-a-map",
		keyInterventionsAllowed: map[string]any{"unexpected": true},
	}}
	f := New(stub, nil)
	got := f.Evaluate(context.Background())

	if !reflect.DeepEqual(got.Objectives, map[string]float64{"endurance": 1.0}) {
		t.Errorf("Objectives = %v, want default on bad shape", got.Objectives)
	}
	if len(got.InterventionsAllowed) != 0 {
		t.Errorf("InterventionsAllowed = %v, want empty on bad shape", got.InterventionsAllowed)
	}
}

func TestDefaultObjectivesIsCopied(t *testing.T) {
	// Mutating one default must not leak into the next call's default.
	a := defaultObjectives()
	a["endurance"] = 99
	b := defaultObjectives()
	if b["endurance"] != 1.0 {
		t.Errorf("defaultObjectives shared state: got %v", b["endurance"])
	}
}

func TestSnapshotPermits(t *testing.T) {
	s := Snapshot{InterventionsAllowed: []string{"play_audio_cue", "grant_shield"}}
	if !s.Permits("grant_shield") {
		t.Errorf("Permits(grant_shield) = false, want true")
	}
	if s.Permits("end_game") {
		t.Errorf("Permits(end_game) = true, want false")
	}
	empty := Snapshot{}
	if empty.Permits("play_audio_cue") {
		t.Errorf("empty allow-list permitted an intervention")
	}
}

// defaultLifecycle is the expected lifecycle snapshot when every flag falls back
// to its safe default (the former hardcoded constants; #766 F5).
func defaultLifecycle() Lifecycle {
	return Lifecycle{
		PlayerTTL:        time.Duration(DefaultPlayerTTLSeconds * float64(time.Second)),
		SessionGrace:     time.Duration(DefaultSessionGraceSeconds * float64(time.Second)),
		EvictInterval:    time.Duration(DefaultEvictIntervalSeconds * float64(time.Second)),
		DecisionThrottle: time.Duration(DefaultDecisionThrottleSeconds * float64(time.Second)),
	}
}

func TestLifecycle_FlagdShape(t *testing.T) {
	stub := stubEvaluator{floats: map[string]float64{
		keyPlayerTTLSeconds:     3,
		keySessionGraceSeconds:  30,
		keyEvictIntervalSeconds: 0.5,
		keyDecisionThrottleSecs: 5,
	}}
	got := New(stub, nil).Lifecycle(context.Background())

	want := Lifecycle{
		PlayerTTL:        3 * time.Second,
		SessionGrace:     30 * time.Second,
		EvictInterval:    500 * time.Millisecond,
		DecisionThrottle: 5 * time.Second,
	}
	if got != want {
		t.Errorf("Lifecycle = %+v, want %+v", got, want)
	}
}

func TestLifecycle_DefaultsWhenMissing(t *testing.T) {
	// Empty stub: every flag undefined. The former hardcoded constants must apply
	// (promotion is behavior-neutral).
	got := New(stubEvaluator{}, nil).Lifecycle(context.Background())
	if got != defaultLifecycle() {
		t.Errorf("Lifecycle = %+v, want defaults %+v", got, defaultLifecycle())
	}
	// Characterize the actual prior values: 5s / 15s / 1s / 1s.
	want := Lifecycle{
		PlayerTTL:        5 * time.Second,
		SessionGrace:     15 * time.Second,
		EvictInterval:    1 * time.Second,
		DecisionThrottle: 1 * time.Second,
	}
	if got != want {
		t.Errorf("Lifecycle defaults = %+v, want former constants %+v", got, want)
	}
}

func TestLifecycle_DefaultsOnError(t *testing.T) {
	boom := errors.New("flagd unreachable")
	stub := stubEvaluator{errs: map[string]error{
		keyPlayerTTLSeconds:     boom,
		keySessionGraceSeconds:  boom,
		keyEvictIntervalSeconds: boom,
		keyDecisionThrottleSecs: boom,
	}}
	got := New(stub, nil).Lifecycle(context.Background())
	if got != defaultLifecycle() {
		t.Errorf("Lifecycle on error = %+v, want defaults %+v", got, defaultLifecycle())
	}
}

func TestLifecycle_NonPositiveFallsBack(t *testing.T) {
	// A zero or negative duration would break the store TTL / ticker, so the
	// wrapper must fall back to the default for non-positive values.
	stub := stubEvaluator{floats: map[string]float64{
		keyPlayerTTLSeconds:     0,
		keySessionGraceSeconds:  -5,
		keyEvictIntervalSeconds: 2,
		keyDecisionThrottleSecs: 0,
	}}
	got := New(stub, nil).Lifecycle(context.Background())
	want := Lifecycle{
		PlayerTTL:        time.Duration(DefaultPlayerTTLSeconds * float64(time.Second)),
		SessionGrace:     time.Duration(DefaultSessionGraceSeconds * float64(time.Second)),
		EvictInterval:    2 * time.Second,
		DecisionThrottle: time.Duration(DefaultDecisionThrottleSeconds * float64(time.Second)),
	}
	if got != want {
		t.Errorf("Lifecycle = %+v, want %+v", got, want)
	}
}
