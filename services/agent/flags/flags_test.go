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
			// interventions_allowed is a STRING flag: comma-separated ids (#1127),
			// read cleanly by the flagd RPC resolver (a LIST flag TYPE_MISMATCHes
			// there and silently blocks every intervention).
			keyInterventionsAllowed: "play_audio_cue,grant_shield",
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
			keyObjectives: map[string]any{"endurance": 0.7, "chaos": 0.3},
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

// TestBluetoothFitness_Accessor pins the narrow #735 accessor the infra loop
// reads each cycle: it returns the three fitness.bluetooth.* thresholds and
// nothing else, falling back to defaults on an evaluation error.
func TestBluetoothFitness_Accessor(t *testing.T) {
	stub := stubEvaluator{
		floats: map[string]float64{
			keyBluetoothMaxEventGapMs:       25,
			keyBluetoothMaxDroppedEventsPct: 0.05,
			keyBluetoothMinMovementUpdateHz: 20,
		},
	}
	f := New(stub, nil)
	got := f.BluetoothFitness(context.Background())
	want := BluetoothFitness{MaxEventGapMs: 25, MaxDroppedEventsPct: 0.05, MinMovementUpdateHz: 20}
	if got != want {
		t.Errorf("BluetoothFitness = %+v, want %+v", got, want)
	}

	// On error every threshold falls back to its flagd-schema default, so a down
	// flagd reverts the infra loop to safe defaults rather than failing.
	boom := errors.New("flagd down")
	errStub := stubEvaluator{errs: map[string]error{
		keyBluetoothMaxEventGapMs:       boom,
		keyBluetoothMaxDroppedEventsPct: boom,
		keyBluetoothMinMovementUpdateHz: boom,
	}}
	if got := New(errStub, nil).BluetoothFitness(context.Background()); got != defaultBluetoothFitness() {
		t.Errorf("BluetoothFitness on error = %+v, want defaults %+v", got, defaultBluetoothFitness())
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
		keyObjectives: "not-a-map",
	}}
	f := New(stub, nil)
	got := f.Evaluate(context.Background())

	if !reflect.DeepEqual(got.Objectives, map[string]float64{"endurance": 1.0}) {
		t.Errorf("Objectives = %v, want default on bad shape", got.Objectives)
	}
}

// TestInterventionsAllowed_StringFlag pins the #1127 fix: interventions_allowed
// is a STRING flag (comma-separated ids), NOT a LIST/object flag. A LIST flag
// read via the flagd RPC resolver's ObjectValue silently TYPE_MISMATCHes to its
// passed default — for an allow-list that empty default blocks EVERY
// intervention even with a non-`none` variant active. Reading the string variant
// must resolve to the full id list so a permitted intervention can dispatch.
func TestInterventionsAllowed_StringFlag(t *testing.T) {
	t.Run("non-none variant resolves to full list", func(t *testing.T) {
		stub := stubEvaluator{strings: map[string]string{
			// the shadow_experimental variant, comma-separated as in agent.json.
			keyInterventionsAllowed: "play_audio_cue,grant_shield,ramp_tempo",
		}}
		got := New(stub, nil).Evaluate(context.Background())
		want := []string{"play_audio_cue", "grant_shield", "ramp_tempo"}
		if !reflect.DeepEqual(got.InterventionsAllowed, want) {
			t.Fatalf("InterventionsAllowed = %v, want %v", got.InterventionsAllowed, want)
		}
		// A permitted intervention passes the allow-list gate (NOT blocked).
		if !got.Permits("grant_shield") {
			t.Errorf("grant_shield should be allowed under a non-none variant")
		}
	})

	t.Run("none variant (empty string) blocks all", func(t *testing.T) {
		stub := stubEvaluator{strings: map[string]string{keyInterventionsAllowed: ""}}
		got := New(stub, nil).Evaluate(context.Background())
		if len(got.InterventionsAllowed) != 0 {
			t.Errorf("InterventionsAllowed = %v, want empty for the none variant", got.InterventionsAllowed)
		}
		if got.Permits("grant_shield") {
			t.Errorf("grant_shield must be blocked when allow-list is empty (fail-closed)")
		}
	})

	t.Run("unreadable flag fails closed", func(t *testing.T) {
		stub := stubEvaluator{errs: map[string]error{keyInterventionsAllowed: errors.New("flagd down")}}
		got := New(stub, nil).Evaluate(context.Background())
		if len(got.InterventionsAllowed) != 0 {
			t.Errorf("InterventionsAllowed = %v, want empty on read error (fail-closed)", got.InterventionsAllowed)
		}
	})
}

// TestEvaluate_LLMGate_FlagdShape: the three #847 gate flags resolve through the
// typed getters into the LLMGate struct. eligible_game_kinds is an array object,
// min_decision_interval_seconds is float seconds -> Duration, max_requests_per_minute
// is an int.
func TestEvaluate_LLMGate_FlagdShape(t *testing.T) {
	stub := stubEvaluator{
		objects: map[string]any{
			keyLLMEligibleGameKinds: []any{"real", "shadow"},
		},
		floats: map[string]float64{keyLLMMinDecisionInterval: 30, keyLLMLatencyBudget: 4},
		ints:   map[string]int64{keyLLMMaxRequestsPerMin: 12},
	}
	got := New(stub, nil).Evaluate(context.Background()).LLMGate
	if !reflect.DeepEqual(got.EligibleGameKinds, []string{"real", "shadow"}) {
		t.Errorf("EligibleGameKinds = %v, want [real shadow]", got.EligibleGameKinds)
	}
	if got.MinDecisionInterval != 30*time.Second {
		t.Errorf("MinDecisionInterval = %v, want 30s", got.MinDecisionInterval)
	}
	if got.MaxRequestsPerMinute != 12 {
		t.Errorf("MaxRequestsPerMinute = %d, want 12", got.MaxRequestsPerMinute)
	}
	// #917: latency budget is float seconds -> Duration, like the cadence interval.
	if got.LatencyBudget != 4*time.Second {
		t.Errorf("LatencyBudget = %v, want 4s", got.LatencyBudget)
	}
}

// TestEvaluate_LLMGate_Defaults: with no flags defined, the gate falls back to the
// fail-closed schema defaults — eligible only for ["real"], 10s cadence, 6/min.
func TestEvaluate_LLMGate_Defaults(t *testing.T) {
	got := New(stubEvaluator{}, nil).Evaluate(context.Background()).LLMGate
	if !reflect.DeepEqual(got.EligibleGameKinds, []string{"real"}) {
		t.Errorf("EligibleGameKinds = %v, want default [real]", got.EligibleGameKinds)
	}
	if got.MinDecisionInterval != time.Duration(DefaultLLMMinDecisionIntervalSeconds*float64(time.Second)) {
		t.Errorf("MinDecisionInterval = %v, want %ds", got.MinDecisionInterval, int(DefaultLLMMinDecisionIntervalSeconds))
	}
	if got.MaxRequestsPerMinute != DefaultLLMMaxRequestsPerMinute {
		t.Errorf("MaxRequestsPerMinute = %d, want %d", got.MaxRequestsPerMinute, DefaultLLMMaxRequestsPerMinute)
	}
	// #917: latency budget defaults to the fail-safe 8s when undefined.
	if got.LatencyBudget != time.Duration(DefaultLLMLatencyBudgetSeconds*float64(time.Second)) {
		t.Errorf("LatencyBudget = %v, want %vs", got.LatencyBudget, DefaultLLMLatencyBudgetSeconds)
	}
}

// TestEvaluate_LLMGate_NonPositiveLatencyBudgetFallsBack: a zero/negative latency
// budget must NEVER disable the timeout (which would leak an inference goroutine);
// durationFlag floors it at the safe default (#917).
func TestEvaluate_LLMGate_NonPositiveLatencyBudgetFallsBack(t *testing.T) {
	stub := stubEvaluator{floats: map[string]float64{keyLLMLatencyBudget: 0}}
	got := New(stub, nil).Evaluate(context.Background()).LLMGate
	if got.LatencyBudget != time.Duration(DefaultLLMLatencyBudgetSeconds*float64(time.Second)) {
		t.Errorf("LatencyBudget = %v, want default %vs (non-positive floored)", got.LatencyBudget, DefaultLLMLatencyBudgetSeconds)
	}
}

// TestEvaluate_LLMGate_BadEligibilityShapeFallsBack: an unparseable
// eligible_game_kinds value falls back to ["real"], NOT empty (fail-closed to
// shadow-rules-only, not to llm-disabled).
func TestEvaluate_LLMGate_BadEligibilityShapeFallsBack(t *testing.T) {
	stub := stubEvaluator{objects: map[string]any{
		keyLLMEligibleGameKinds: map[string]any{"unexpected": true},
	}}
	got := New(stub, nil).Evaluate(context.Background()).LLMGate
	if !reflect.DeepEqual(got.EligibleGameKinds, []string{"real"}) {
		t.Errorf("EligibleGameKinds = %v, want default [real] on bad shape", got.EligibleGameKinds)
	}
}

// TestLLMGate_EligibleFor: membership test, including the empty-list-admits-nothing
// reading.
func TestLLMGate_EligibleFor(t *testing.T) {
	g := LLMGate{EligibleGameKinds: []string{"real"}}
	if !g.EligibleFor("real") {
		t.Error("EligibleFor(real) = false, want true")
	}
	if g.EligibleFor("shadow") {
		t.Error("EligibleFor(shadow) = true, want false")
	}
	empty := LLMGate{}
	if empty.EligibleFor("real") {
		t.Error("empty eligibility list admitted a kind, want none admitted")
	}
}

func TestDefaultLLMEligibleGameKindsIsCopied(t *testing.T) {
	a := defaultLLMEligibleGameKinds()
	a[0] = "mutated"
	b := defaultLLMEligibleGameKinds()
	if b[0] != "real" {
		t.Errorf("defaultLLMEligibleGameKinds shared state: got %v", b[0])
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

// TestEvaluate_PromptContextNote pins the M7-3 (#930) operator-note read: the raw
// prompt.context_note string flag is surfaced verbatim on the snapshot (validation is
// the decision side's job, not the flag layer's), and falls back to the empty default
// when undefined or on a flagd error — so an unset or unreachable flag injects no note.
func TestEvaluate_PromptContextNote(t *testing.T) {
	t.Run("live value surfaced verbatim", func(t *testing.T) {
		stub := stubEvaluator{strings: map[string]string{
			keyPromptContextNote: "keep it gentle tonight",
		}}
		got := New(stub, nil).Evaluate(context.Background())
		if got.PromptContextNote != "keep it gentle tonight" {
			t.Errorf("PromptContextNote = %q, want raw flag value", got.PromptContextNote)
		}
	})
	t.Run("default when undefined", func(t *testing.T) {
		got := New(stubEvaluator{}, nil).Evaluate(context.Background())
		if got.PromptContextNote != DefaultPromptContextNote {
			t.Errorf("PromptContextNote = %q, want empty default", got.PromptContextNote)
		}
	})
	t.Run("default on flagd error", func(t *testing.T) {
		stub := stubEvaluator{errs: map[string]error{keyPromptContextNote: errors.New("flagd down")}}
		got := New(stub, nil).Evaluate(context.Background())
		if got.PromptContextNote != DefaultPromptContextNote {
			t.Errorf("PromptContextNote on error = %q, want empty default", got.PromptContextNote)
		}
	})
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

// TestValidationFlags covers the M7-7 synthetic-validation gate flags (#935):
// the typed getters resolve configured values, and each flag falls back to its
// safe default on an evaluation error (flagd unreachable / undefined).
func TestValidation(t *testing.T) {
	t.Run("configured values resolve", func(t *testing.T) {
		ev := stubEvaluator{
			ints:     map[string]int64{keyValidationGames: 8},
			floats:   map[string]float64{keyFitnessImprovementThreshold: 0.2},
			booleans: map[string]bool{keyRevertOnDegradation: false},
		}
		got := New(ev, nil).Validation(context.Background())
		want := ValidationConfig{ValidationGames: 8, ImprovementThreshold: 0.2, RevertOnDegradation: false}
		if got != want {
			t.Errorf("Validation = %+v, want %+v", got, want)
		}
	})

	t.Run("errors fall back to safe defaults", func(t *testing.T) {
		ev := stubEvaluator{errs: map[string]error{
			keyValidationGames:             errors.New("flagd down"),
			keyFitnessImprovementThreshold: errors.New("flagd down"),
			keyRevertOnDegradation:         errors.New("flagd down"),
		}}
		got := New(ev, nil).Validation(context.Background())
		want := ValidationConfig{
			ValidationGames:      DefaultValidationGames,
			ImprovementThreshold: DefaultFitnessImprovementThreshold,
			RevertOnDegradation:  DefaultRevertOnDegradation,
		}
		if got != want {
			t.Errorf("Validation defaults = %+v, want %+v", got, want)
		}
	})
}

// TestCodeImprovement covers the M7-4 propose-stage flags (#931): the engine and
// cadence resolve from configured values, and fall back to safe defaults on an
// evaluation error (flagd unreachable / undefined).
func TestCodeImprovement(t *testing.T) {
	t.Run("configured values resolve", func(t *testing.T) {
		ev := stubEvaluator{
			strings: map[string]string{keyCodeImprovementEngine: "ollama"},
			floats:  map[string]float64{keyCodeImprovementMinInterval: 60},
		}
		got := New(ev, nil).CodeImprovement(context.Background())
		want := CodeImprovementConfig{Engine: "ollama", MinInterval: 60 * time.Second}
		if got != want {
			t.Errorf("CodeImprovement = %+v, want %+v", got, want)
		}
	})

	t.Run("errors fall back to safe defaults", func(t *testing.T) {
		ev := stubEvaluator{errs: map[string]error{
			keyCodeImprovementEngine:      errors.New("flagd down"),
			keyCodeImprovementMinInterval: errors.New("flagd down"),
		}}
		got := New(ev, nil).CodeImprovement(context.Background())
		want := CodeImprovementConfig{
			Engine:      DefaultCodeImprovementEngine,
			MinInterval: time.Duration(DefaultCodeImprovementMinIntervalSeconds * float64(time.Second)),
		}
		if got != want {
			t.Errorf("CodeImprovement defaults = %+v, want %+v", got, want)
		}
	})
}

// TestPromotion covers the M7-8 promotion-flow flags (#936): the live mode/target
// resolve, and — critically for the SAFETY RAIL — each falls back to its SAFE
// default on an evaluation error (mode→issue, NEVER autonomous; target→local,
// NEVER github).
func TestPromotion(t *testing.T) {
	t.Run("configured values resolve live", func(t *testing.T) {
		ev := stubEvaluator{strings: map[string]string{
			keyPromotionMode:   "autonomous",
			keyPromotionTarget: "github",
		}}
		got := New(ev, nil).Promotion(context.Background())
		want := PromotionConfig{Mode: "autonomous", Target: "github"}
		if got != want {
			t.Errorf("Promotion = %+v, want %+v", got, want)
		}
	})

	t.Run("errors fall back to SAFE defaults (issue/local, never autonomous/github)", func(t *testing.T) {
		ev := stubEvaluator{errs: map[string]error{
			keyPromotionMode:   errors.New("flagd down"),
			keyPromotionTarget: errors.New("flagd down"),
		}}
		got := New(ev, nil).Promotion(context.Background())
		want := PromotionConfig{Mode: DefaultPromotionMode, Target: DefaultPromotionTarget}
		if got != want {
			t.Errorf("Promotion defaults = %+v, want %+v", got, want)
		}
		// The safety rail's hard invariant: the fail-closed defaults are the SAFE ones.
		if got.Mode == "autonomous" {
			t.Fatal("SAFETY: promotion mode defaulted to autonomous")
		}
		if got.Target == "github" {
			t.Fatal("SAFETY: promotion target defaulted to github")
		}
	})
}

// TestExperiment_FlagOverridesEnvDefault is the #1044 acceptance: each migrated
// experiment knob takes the agent.json flag value when present and falls back to
// the env-derived bootstrap default (ExperimentDefaults) when the flag is absent.
func TestExperiment_FlagOverridesEnvDefault(t *testing.T) {
	// The env-derived bootstrap defaults the loop hands in.
	def := ExperimentDefaults{
		Enabled:                    false,
		DynamicEnabled:             false,
		Tick:                       30 * time.Second,
		DynamicMaxConcurrent:       3,
		VerdictMinN:                8,
		VerdictMinPairs:            5,
		MaxGames:                   50,
		ShadowEffectiveConcurrency: 4,
		// #1214 verdict-tuning float defaults (the verdict's Default* constants).
		VerdictEffectThreshold: 0.5,
		VerdictMinRawEffect:    0.02,
		VerdictSDFloor:         0.0001,
		VerdictAnchorMargin:    0.02,
	}

	t.Run("flags present override every env default", func(t *testing.T) {
		stub := stubEvaluator{
			booleans: map[string]bool{
				keyExperimentsEnabled:       true,
				keyExperimentDynamicEnabled: true,
			},
			floats: map[string]float64{
				keyExperimentTickSeconds: 10,
				// #1214 verdict-tuning float knobs, read via the TYPED float getter.
				keyVerdictEffectThreshold: 0.8,
				keyVerdictMinRawEffect:    0.05,
				keyVerdictSDFloor:         0.001,
				keyVerdictAnchorMargin:    0.03,
			},
			ints: map[string]int64{
				keyExperimentDynamicMaxConcur:  6,
				keyVerdictMinN:                 16,
				keyVerdictMinPairs:             10,
				keyExperimentMaxGames:          100,
				keyExperimentShadowConcurrency: 8,
			},
		}
		got := New(stub, nil).Experiment(context.Background(), def)
		want := ExperimentConfig{
			Enabled:                    true,
			DynamicEnabled:             true,
			Tick:                       10 * time.Second,
			DynamicMaxConcurrent:       6,
			VerdictMinN:                16,
			VerdictMinPairs:            10,
			MaxGames:                   100,
			ShadowEffectiveConcurrency: 8,
			VerdictEffectThreshold:     0.8,
			VerdictMinRawEffect:        0.05,
			VerdictSDFloor:             0.001,
			VerdictAnchorMargin:        0.03,
		}
		if got != want {
			t.Fatalf("Experiment with flags present = %+v, want %+v", got, want)
		}
	})

	t.Run("flags absent fall back to the env bootstrap defaults", func(t *testing.T) {
		// An empty stub returns the passed default for every key (flag absent).
		got := New(stubEvaluator{}, nil).Experiment(context.Background(), def)
		want := ExperimentConfig{
			Enabled:                    def.Enabled,
			DynamicEnabled:             def.DynamicEnabled,
			Tick:                       def.Tick,
			DynamicMaxConcurrent:       def.DynamicMaxConcurrent,
			VerdictMinN:                def.VerdictMinN,
			VerdictMinPairs:            def.VerdictMinPairs,
			MaxGames:                   def.MaxGames,
			ShadowEffectiveConcurrency: def.ShadowEffectiveConcurrency,
			VerdictEffectThreshold:     def.VerdictEffectThreshold,
			VerdictMinRawEffect:        def.VerdictMinRawEffect,
			VerdictSDFloor:             def.VerdictSDFloor,
			VerdictAnchorMargin:        def.VerdictAnchorMargin,
		}
		if got != want {
			t.Fatalf("Experiment with flags absent = %+v, want env defaults %+v", got, want)
		}
	})

	t.Run("verdict float flags fail open to defaults on flagd error (#1214)", func(t *testing.T) {
		errAll := map[string]error{
			keyVerdictEffectThreshold: errors.New("down"),
			keyVerdictMinRawEffect:    errors.New("down"),
			keyVerdictSDFloor:         errors.New("down"),
			keyVerdictAnchorMargin:    errors.New("down"),
		}
		got := New(stubEvaluator{errs: errAll}, nil).Experiment(context.Background(), def)
		if got.VerdictEffectThreshold != def.VerdictEffectThreshold ||
			got.VerdictMinRawEffect != def.VerdictMinRawEffect ||
			got.VerdictSDFloor != def.VerdictSDFloor ||
			got.VerdictAnchorMargin != def.VerdictAnchorMargin {
			t.Fatalf("verdict float flags under flagd error did not fail open to defaults: %+v", got)
		}
	})

	t.Run("flagd errors fall back to the env bootstrap defaults (fail-safe)", func(t *testing.T) {
		errAll := map[string]error{
			keyExperimentsEnabled:          errors.New("down"),
			keyExperimentDynamicEnabled:    errors.New("down"),
			keyExperimentTickSeconds:       errors.New("down"),
			keyExperimentDynamicMaxConcur:  errors.New("down"),
			keyVerdictMinN:                 errors.New("down"),
			keyVerdictMinPairs:             errors.New("down"),
			keyExperimentMaxGames:          errors.New("down"),
			keyExperimentShadowConcurrency: errors.New("down"),
		}
		got := New(stubEvaluator{errs: errAll}, nil).Experiment(context.Background(), def)
		if got.Enabled != def.Enabled || got.Tick != def.Tick || got.VerdictMinN != def.VerdictMinN ||
			got.MaxGames != def.MaxGames || got.ShadowEffectiveConcurrency != def.ShadowEffectiveConcurrency {
			t.Fatalf("Experiment under flagd error did not fall back to env defaults: %+v", got)
		}
	})

	t.Run("live re-read: a changed flag is reflected on the next call (never cached)", func(t *testing.T) {
		stub := stubEvaluator{booleans: map[string]bool{keyExperimentsEnabled: false}}
		f := New(stub, nil)
		if f.Experiment(context.Background(), def).Enabled {
			t.Fatal("expected disabled on first read")
		}
		// Flip the live flag value and re-read — Experiment must NOT cache.
		stub.booleans[keyExperimentsEnabled] = true
		if !f.Experiment(context.Background(), def).Enabled {
			t.Fatal("Experiment cached the flag; a live change was not reflected on re-read")
		}
	})

	t.Run("max_games=0 (disable budget) is honored verbatim, not floored", func(t *testing.T) {
		stub := stubEvaluator{ints: map[string]int64{keyExperimentMaxGames: 0}}
		got := New(stub, nil).Experiment(context.Background(), def)
		if got.MaxGames != 0 {
			t.Fatalf("max_games=0 must pass through as the explicit disable, got %d", got.MaxGames)
		}
	})
}

// TestInterventionsEnabled covers the #1213 intervention safety gate: a configured
// value resolves via the TYPED bool getter, and the gate FAILS CLOSED (off) when the
// flag is UNDEFINED (no value) or flagd is UNREACHABLE (error). A safety gate must
// resolve to the not-acting state when the control plane cannot confirm it is on.
func TestInterventionsEnabled(t *testing.T) {
	t.Run("configured on resolves", func(t *testing.T) {
		ev := stubEvaluator{booleans: map[string]bool{keyInterventionsEnabled: true}}
		if !New(ev, nil).InterventionsEnabled(context.Background()) {
			t.Fatal("interventions_enabled=true must resolve true")
		}
	})
	t.Run("undefined flag fails closed (off)", func(t *testing.T) {
		// No value set for the key -> stub returns the passed default
		// (DefaultInterventionsEnabled=false). This is the fail-closed safe state.
		ev := stubEvaluator{}
		if New(ev, nil).InterventionsEnabled(context.Background()) {
			t.Fatal("an UNDEFINED interventions_enabled flag must fail closed to false (#1177)")
		}
	})
	t.Run("flagd unreachable fails closed (off)", func(t *testing.T) {
		ev := stubEvaluator{errs: map[string]error{keyInterventionsEnabled: errors.New("flagd unreachable")}}
		if New(ev, nil).InterventionsEnabled(context.Background()) {
			t.Fatal("an UNREACHABLE flagd must fail closed to false (#1177)")
		}
	})
}

// TestRolloutEnabled covers the #1213 rollout safety gate: a configured value
// resolves via the TYPED bool getter, and the gate FAILS CLOSED (off ⇒ dry-run) when
// the flag is UNDEFINED or flagd is UNREACHABLE.
func TestRolloutEnabled(t *testing.T) {
	t.Run("configured on resolves", func(t *testing.T) {
		ev := stubEvaluator{booleans: map[string]bool{keyRolloutEnabled: true}}
		if !New(ev, nil).RolloutEnabled(context.Background()) {
			t.Fatal("rollout_enabled=true must resolve true")
		}
	})
	t.Run("undefined flag fails closed (off)", func(t *testing.T) {
		ev := stubEvaluator{}
		if New(ev, nil).RolloutEnabled(context.Background()) {
			t.Fatal("an UNDEFINED rollout_enabled flag must fail closed to false (#1177)")
		}
	})
	t.Run("flagd unreachable fails closed (off)", func(t *testing.T) {
		ev := stubEvaluator{errs: map[string]error{keyRolloutEnabled: errors.New("flagd unreachable")}}
		if New(ev, nil).RolloutEnabled(context.Background()) {
			t.Fatal("an UNREACHABLE flagd must fail closed to false (#1177)")
		}
	})
}

// TestSafetyGateDefaultsAreFailClosed pins the package-level safe defaults: both
// safety gates default OFF (the not-acting state), matching the fail-closed env
// default (AGENT_*_ENABLED `:-false`) they replace.
func TestSafetyGateDefaultsAreFailClosed(t *testing.T) {
	if DefaultInterventionsEnabled {
		t.Error("DefaultInterventionsEnabled must be false (fail-closed)")
	}
	if DefaultRolloutEnabled {
		t.Error("DefaultRolloutEnabled must be false (fail-closed)")
	}
}
