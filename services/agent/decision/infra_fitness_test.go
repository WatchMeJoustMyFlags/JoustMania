package decision

import (
	"context"
	"reflect"
	"testing"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/infracontext"
)

// fp (pointer-to-float helper) is defined in fitness_test.go.

// window builds an InfraContext with the given window signals (nil = unobserved)
// and no per-controller records.
func window(gap, drop, hz *float64) infracontext.InfraContext {
	return infracontext.InfraContext{
		Window: infracontext.WindowSignals{
			EventGapMs:       gap,
			DroppedEventsPct: drop,
			MovementUpdateHz: hz,
		},
	}
}

func TestEvaluateInfraFitness_EmptyContextNotEvaluated(t *testing.T) {
	res := EvaluateInfraFitness(infracontext.InfraContext{}, DefaultBluetoothThresholds())
	if res.Evaluated {
		t.Fatalf("Evaluated = true for empty context, want false")
	}
	if len(res.Violations) != 0 {
		t.Errorf("Violations = %v, want none", res.Violations)
	}
	// Thresholds are still recorded even when nothing was evaluated.
	if res.Values["bluetooth.max_event_gap_ms"] != 50 {
		t.Errorf("threshold value missing: %v", res.Values)
	}
}

func TestEvaluateInfraFitness_AllPass(t *testing.T) {
	th := DefaultBluetoothThresholds()
	res := EvaluateInfraFitness(window(fp(20), fp(0.01), fp(15)), th)
	if !res.Evaluated {
		t.Fatalf("Evaluated = false, want true")
	}
	if !res.Passing {
		t.Errorf("Passing = false, want true; violations=%v", res.Violations)
	}
	if res.ViolationsString() != "" {
		t.Errorf("ViolationsString = %q, want empty", res.ViolationsString())
	}
}

func TestEvaluateInfraFitness_IndividualViolations(t *testing.T) {
	th := DefaultBluetoothThresholds()
	tests := []struct {
		name    string
		infra   infracontext.InfraContext
		signal  string
		obs     float64
		cmp     string
		wantStr string
	}{
		{
			name:    "event gap over max",
			infra:   window(fp(87.5), fp(0.01), fp(15)),
			signal:  SignalEventGapMs,
			obs:     87.5,
			cmp:     ComparatorGreater,
			wantStr: "event_gap_ms 87.5>50",
		},
		{
			name:    "dropped pct over max",
			infra:   window(fp(20), fp(0.10), fp(15)),
			signal:  SignalDroppedEventsPct,
			obs:     0.10,
			cmp:     ComparatorGreater,
			wantStr: "dropped_events_pct 0.1>0.02",
		},
		{
			name:    "window hz under min",
			infra:   window(fp(20), fp(0.01), fp(8.3)),
			signal:  SignalMovementUpdateHz,
			obs:     8.3,
			cmp:     ComparatorLess,
			wantStr: "movement_update_hz 8.3<10",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			res := EvaluateInfraFitness(tt.infra, th)
			if res.Passing {
				t.Fatalf("Passing = true, want false")
			}
			if len(res.Violations) != 1 {
				t.Fatalf("Violations = %v, want exactly 1", res.Violations)
			}
			v := res.Violations[0]
			if v.Signal != tt.signal || v.Observed != tt.obs || v.Comparator != tt.cmp {
				t.Errorf("violation = %+v, want signal=%s obs=%v cmp=%s", v, tt.signal, tt.obs, tt.cmp)
			}
			if got := res.ViolationsString(); got != tt.wantStr {
				t.Errorf("ViolationsString = %q, want %q", got, tt.wantStr)
			}
		})
	}
}

func TestEvaluateInfraFitness_UnstableBackendAllThree(t *testing.T) {
	// The "unstable backend reliably violates" case: gap 120ms, drops 0.10,
	// window hz 8.3 → all three window checks fail.
	th := DefaultBluetoothThresholds()
	res := EvaluateInfraFitness(window(fp(120), fp(0.10), fp(8.3)), th)
	if !res.Evaluated || res.Passing {
		t.Fatalf("Evaluated=%v Passing=%v, want Evaluated=true Passing=false", res.Evaluated, res.Passing)
	}
	if len(res.Violations) != 3 {
		t.Fatalf("Violations = %v, want 3", res.Violations)
	}
	want := "dropped_events_pct 0.1>0.02; event_gap_ms 120>50; movement_update_hz 8.3<10"
	if got := res.ViolationsString(); got != want {
		t.Errorf("ViolationsString = %q, want %q", got, want)
	}
}

func TestEvaluateInfraFitness_PerControllerHzNamesSerial(t *testing.T) {
	th := DefaultBluetoothThresholds()
	infra := window(fp(20), fp(0.01), nil) // window hz unobserved
	infra.Controllers = map[string]*infracontext.ControllerHealth{
		"AA:BB": {Serial: "AA:BB", MovementUpdateHz: fp(8.3)},
		"CC:DD": {Serial: "CC:DD", MovementUpdateHz: fp(15)}, // healthy, no violation
	}
	res := EvaluateInfraFitness(infra, th)
	if res.Passing {
		t.Fatalf("Passing = true, want false")
	}
	if len(res.Violations) != 1 {
		t.Fatalf("Violations = %v, want 1 (only AA:BB)", res.Violations)
	}
	v := res.Violations[0]
	if v.Serial != "AA:BB" || v.Signal != SignalMovementUpdateHz {
		t.Errorf("violation = %+v, want serial AA:BB hz", v)
	}
	if got := res.ViolationsString(); got != "movement_update_hz[AA:BB] 8.3<10" {
		t.Errorf("ViolationsString = %q", got)
	}
}

func TestEvaluateInfraFitness_HzDedupWindowVsPerController(t *testing.T) {
	// Dedup decision: when both the window min hz AND a per-controller rate
	// violate, emit ONLY the per-serial violation(s) and skip the redundant
	// window-level one.
	th := DefaultBluetoothThresholds()
	infra := window(fp(20), fp(0.01), fp(8.0)) // window min hz also violates
	infra.Controllers = map[string]*infracontext.ControllerHealth{
		"AA:BB": {Serial: "AA:BB", MovementUpdateHz: fp(8.0)},
	}
	res := EvaluateInfraFitness(infra, th)
	if len(res.Violations) != 1 {
		t.Fatalf("Violations = %v, want 1 (per-serial only, window deduped)", res.Violations)
	}
	if res.Violations[0].Serial != "AA:BB" {
		t.Errorf("violation serial = %q, want AA:BB (per-serial)", res.Violations[0].Serial)
	}
}

func TestEvaluateInfraFitness_WindowHzFiresWhenNoPerController(t *testing.T) {
	// When the window min hz violates but NO per-controller rate is available to
	// attribute it, the window-level hz violation must fire (signal preserved).
	th := DefaultBluetoothThresholds()
	res := EvaluateInfraFitness(window(fp(20), fp(0.01), fp(8.0)), th)
	if len(res.Violations) != 1 {
		t.Fatalf("Violations = %v, want 1 window-level hz", res.Violations)
	}
	if res.Violations[0].Serial != "" {
		t.Errorf("violation serial = %q, want empty (window-level)", res.Violations[0].Serial)
	}
}

func TestEvaluateInfraFitness_MultiplePerControllerSorted(t *testing.T) {
	th := DefaultBluetoothThresholds()
	infra := window(fp(20), fp(0.01), nil)
	infra.Controllers = map[string]*infracontext.ControllerHealth{
		"CC:DD": {Serial: "CC:DD", MovementUpdateHz: fp(5.0)},
		"AA:BB": {Serial: "AA:BB", MovementUpdateHz: fp(7.0)},
	}
	res := EvaluateInfraFitness(infra, th)
	if len(res.Violations) != 2 {
		t.Fatalf("Violations = %v, want 2", res.Violations)
	}
	// Deterministic order: serials sorted ascending.
	want := "movement_update_hz[AA:BB] 7<10; movement_update_hz[CC:DD] 5<10"
	if got := res.ViolationsString(); got != want {
		t.Errorf("ViolationsString = %q, want %q", got, want)
	}
}

func TestEvaluateInfraFitness_MissingSignalsSkipped(t *testing.T) {
	// Only event_gap observed (and violating); the other two window signals are
	// nil and must be skipped, not failed.
	th := DefaultBluetoothThresholds()
	res := EvaluateInfraFitness(window(fp(99), nil, nil), th)
	if !res.Evaluated {
		t.Fatalf("Evaluated = false, want true (one signal present)")
	}
	if len(res.Violations) != 1 || res.Violations[0].Signal != SignalEventGapMs {
		t.Fatalf("Violations = %v, want only event_gap_ms", res.Violations)
	}
	// No dropped/hz observed values recorded.
	if _, ok := res.Values["bluetooth.dropped_events_pct"]; ok {
		t.Errorf("dropped value recorded for missing signal")
	}
	if _, ok := res.Values["bluetooth.movement_update_hz"]; ok {
		t.Errorf("hz value recorded for missing signal")
	}
}

func TestEvaluateInfraFitness_NilControllerEntrySkipped(t *testing.T) {
	th := DefaultBluetoothThresholds()
	infra := window(fp(20), fp(0.01), nil)
	infra.Controllers = map[string]*infracontext.ControllerHealth{
		"AA:BB": nil,                                         // nil entry
		"CC:DD": {Serial: "CC:DD", MovementUpdateHz: nil},    // rate unobserved
		"EE:FF": {Serial: "EE:FF", MovementUpdateHz: fp(15)}, // healthy
	}
	res := EvaluateInfraFitness(infra, th)
	if !res.Passing {
		t.Errorf("Passing = false, want true; violations=%v", res.Violations)
	}
}

func TestViolationsString_Deterministic(t *testing.T) {
	th := DefaultBluetoothThresholds()
	infra := window(fp(120), fp(0.10), nil)
	infra.Controllers = map[string]*infracontext.ControllerHealth{
		"BB:BB": {Serial: "BB:BB", MovementUpdateHz: fp(8.3)},
		"AA:AA": {Serial: "AA:AA", MovementUpdateHz: fp(6.0)},
	}
	// Multiple evaluations must yield the identical string regardless of map order.
	first := EvaluateInfraFitness(infra, th).ViolationsString()
	for i := 0; i < 20; i++ {
		if got := EvaluateInfraFitness(infra, th).ViolationsString(); got != first {
			t.Fatalf("non-deterministic ViolationsString: %q vs %q", got, first)
		}
	}
	want := "dropped_events_pct 0.1>0.02; event_gap_ms 120>50; " +
		"movement_update_hz[AA:AA] 6<10; movement_update_hz[BB:BB] 8.3<10"
	if first != want {
		t.Errorf("ViolationsString = %q, want %q", first, want)
	}
}

func TestDefaultBluetoothThresholds_MatchesFlagdSchema(t *testing.T) {
	th := DefaultBluetoothThresholds()
	want := BluetoothThresholds{MaxEventGapMs: 50, MaxDroppedEventsPct: 0.02, MinMovementUpdateHz: 10}
	if !reflect.DeepEqual(th, want) {
		t.Errorf("DefaultBluetoothThresholds = %+v, want %+v", th, want)
	}
}

func TestLiveBluetoothFitness_SeedsDefaultsAndSet(t *testing.T) {
	src := NewLiveBluetoothFitness()
	if src.BluetoothThresholds() != DefaultBluetoothThresholds() {
		t.Errorf("seeded thresholds = %+v, want defaults", src.BluetoothThresholds())
	}
	custom := BluetoothThresholds{MaxEventGapMs: 25, MaxDroppedEventsPct: 0.01, MinMovementUpdateHz: 20}
	src.Set(custom)
	if src.BluetoothThresholds() != custom {
		t.Errorf("after Set = %+v, want %+v", src.BluetoothThresholds(), custom)
	}
	// Interface conformance.
	var _ BluetoothFitnessSource = src
}

// fakeBluetoothFlags is a mutable bluetoothFitnessEvaluator: each
// BluetoothFitness call returns whatever value is currently stored, so a test
// can flip the thresholds between two reads and assert the source re-reads them.
type fakeBluetoothFlags struct {
	value flags.BluetoothFitness
	calls int
}

func (f *fakeBluetoothFlags) BluetoothFitness(_ context.Context) flags.BluetoothFitness {
	f.calls++
	return f.value
}

// TestFlagBluetoothFitness_ReReadsEachEvaluation is the #735 liveness contract:
// the flag-backed source re-evaluates the fitness.bluetooth.* flags on EVERY
// BluetoothThresholds call, so a threshold flipped between two evaluations takes
// effect on the second — no restart, no cached startup value.
func TestFlagBluetoothFitness_ReReadsEachEvaluation(t *testing.T) {
	fake := &fakeBluetoothFlags{value: flags.BluetoothFitness{
		MaxEventGapMs: 50, MaxDroppedEventsPct: 0.02, MinMovementUpdateHz: 10,
	}}
	src := NewFlagBluetoothFitness(fake)

	// First evaluation sees the initial thresholds.
	first := src.BluetoothThresholds()
	want1 := BluetoothThresholds{MaxEventGapMs: 50, MaxDroppedEventsPct: 0.02, MinMovementUpdateHz: 10}
	if first != want1 {
		t.Fatalf("first eval = %+v, want %+v", first, want1)
	}

	// Operator flips the thresholds on stage (flagd change).
	fake.value = flags.BluetoothFitness{MaxEventGapMs: 25, MaxDroppedEventsPct: 0.05, MinMovementUpdateHz: 20}

	// Second evaluation MUST observe the new values (live, not frozen at startup).
	second := src.BluetoothThresholds()
	want2 := BluetoothThresholds{MaxEventGapMs: 25, MaxDroppedEventsPct: 0.05, MinMovementUpdateHz: 20}
	if second != want2 {
		t.Errorf("second eval = %+v, want %+v (thresholds not live)", second, want2)
	}
	if fake.calls != 2 {
		t.Errorf("flags evaluated %d times, want 2 (once per BluetoothThresholds call)", fake.calls)
	}

	var _ BluetoothFitnessSource = src
}

// TestFlagBluetoothFitness_NilClientServesDefaults pins the fail-safe: a nil
// flags client serves the flagd-schema defaults rather than a zero-value
// (thresholdless) struct, so the infra loop is never thresholdless.
func TestFlagBluetoothFitness_NilClientServesDefaults(t *testing.T) {
	src := NewFlagBluetoothFitness(nil)
	if got := src.BluetoothThresholds(); got != DefaultBluetoothThresholds() {
		t.Errorf("nil-client thresholds = %+v, want defaults %+v", got, DefaultBluetoothThresholds())
	}
}
