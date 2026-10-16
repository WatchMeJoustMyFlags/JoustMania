package decision

import (
	"strings"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"

	"github.com/joustmania/agent/flags"
)

// Per-backend act-confidence threshold (#1287): Jev's confidence is the calibrated
// gate p_yes, so jev answers are routed against decision.jev.act_confidence_threshold
// (default 0.6) while claude/openai keep the global decision.act_confidence_threshold
// (default 0.8). These tests use the flag defaults unless they say otherwise.
// Spec: decide-contract.md §5 and §12, UC-040 step 9 and A13, FR-047.

// nearThresholdConfidence is the Jev gate's p_yes for a near-threshold state.
const nearThresholdConfidence = 0.72

// defaultThresholdSnapshot is remoteSnapshot with the decision backend set to name
// and both thresholds at their flag defaults.
func defaultThresholdSnapshot(name string, timeout time.Duration) flags.Snapshot {
	s := remoteSnapshot(timeout)
	s.Decision.Backend = name
	s.Decision.ActConfidenceThreshold = flags.DefaultDecisionActConfidenceThreshold
	s.Decision.Jev = flags.RemoteBackendConfig{
		Model: flags.DefaultDecisionJevModel, Timeout: timeout,
		ActConfidenceThreshold:    flags.DefaultDecisionJevActConfidenceThreshold,
		HasActConfidenceThreshold: true,
	}
	return s
}

func jevBackend(conf float64) *fakeDecideBackend {
	be := newFakeDecideBackend(ffaAnswer("grant_shield", conf, "A", 0.5, "near threshold"))
	be.name = flags.DecisionBackendJev
	return be
}

func assertThreshold(t *testing.T, span sdktrace.ReadOnlySpan, want float64, source string) {
	t.Helper()
	if v, _ := attrValue(span, AttrDecideActThreshold); v.AsFloat64() != want {
		t.Errorf("%s = %v, want %v", AttrDecideActThreshold, v.AsFloat64(), want)
	}
	if g := strAttr(t, span, AttrDecideActThresholdSource); g != source {
		t.Errorf("%s = %q, want %q", AttrDecideActThresholdSource, g, source)
	}
	wantFlag := thresholdFlag(source)
	for _, ev := range span.Events() {
		if ev.Name != evaluationEventName {
			continue
		}
		for _, kv := range ev.Attributes {
			if string(kv.Key) == "gen_ai.evaluation.explanation" {
				if !strings.Contains(kv.Value.AsString(), "threshold "+fmtFloat(want)+" ("+wantFlag+")") {
					t.Errorf("explanation = %q, want threshold %s from %s", kv.Value.AsString(), fmtFloat(want), wantFlag)
				}
				return
			}
		}
	}
	t.Error("no gen_ai.evaluation.result explanation")
}

func TestThreshold_JevActsAtGateByDefault(t *testing.T) {
	h := newRemoteHarness(t, defaultThresholdSnapshot(flags.DecisionBackendJev, time.Second), jevBackend(nearThresholdConfidence))
	span := h.run(t)
	if g := strAttr(t, span, AttrDecideOutcome); g != DecideOutcomeAct {
		t.Fatalf("outcome = %q, want act (0.72 >= jev 0.6)", g)
	}
	if len(h.sink.decisions()) != 1 {
		t.Fatalf("applied = %+v, want one decision", h.sink.decisions())
	}
	assertThreshold(t, span, flags.DefaultDecisionJevActConfidenceThreshold, flags.DecisionBackendJev)
	assertEvaluation(t, span, evaluationLabelPass, nearThresholdConfidence)
}

func TestThreshold_ClaudeHeldAtGlobalByDefault(t *testing.T) {
	be := newFakeDecideBackend(ffaAnswer("grant_shield", nearThresholdConfidence, "A", 0.5, "x"))
	h := newRemoteHarness(t, defaultThresholdSnapshot(flags.DecisionBackendClaude, time.Second), be)
	span := h.run(t)
	if g := strAttr(t, span, AttrDecideOutcome); g != DecideOutcomeHeld {
		t.Fatalf("outcome = %q, want held (0.72 < global 0.8; the jev threshold must not apply)", g)
	}
	if len(h.sink.decisions()) != 0 {
		t.Fatal("held answer applied")
	}
	assertThreshold(t, span, flags.DefaultDecisionActConfidenceThreshold, flags.ActThresholdSourceGlobal)
	assertEvaluation(t, span, evaluationLabelFail, nearThresholdConfidence)
}

func TestThreshold_JevInheritUsesGlobal(t *testing.T) {
	snap := defaultThresholdSnapshot(flags.DecisionBackendJev, time.Second)
	snap.Decision.Jev.HasActConfidenceThreshold = false // the "inherit" variant
	h := newRemoteHarness(t, snap, jevBackend(nearThresholdConfidence))
	span := h.run(t)
	if g := strAttr(t, span, AttrDecideOutcome); g != DecideOutcomeHeld {
		t.Fatalf("outcome = %q, want held under the inherited global 0.8", g)
	}
	assertThreshold(t, span, flags.DefaultDecisionActConfidenceThreshold, flags.ActThresholdSourceGlobal)
}

func TestThreshold_JevGlobalFlipDoesNotApplyWithOwnThreshold(t *testing.T) {
	// The presenter lowering the global bar for Claude must not move Jev's, and
	// vice versa: with its own threshold Jev ignores the global entirely.
	snap := defaultThresholdSnapshot(flags.DecisionBackendJev, time.Second)
	snap.Decision.ActConfidenceThreshold = 0.95
	h := newRemoteHarness(t, snap, jevBackend(nearThresholdConfidence))
	span := h.run(t)
	if g := strAttr(t, span, AttrDecideOutcome); g != DecideOutcomeAct {
		t.Fatalf("outcome = %q, want act under jev's own 0.6", g)
	}
	assertThreshold(t, span, flags.DefaultDecisionJevActConfidenceThreshold, flags.DecisionBackendJev)
}

func TestThreshold_JevRaisedMidFlightAppliesAtLanding(t *testing.T) {
	be := jevBackend(nearThresholdConfidence)
	h, span := fireAndMutateSnapshot(t, be, defaultThresholdSnapshot(flags.DecisionBackendJev, time.Hour), time.Hour,
		func(h remoteHarness) {
			h.flags.mutate(func(s *flags.Snapshot) { s.Decision.Jev.ActConfidenceThreshold = 0.95 })
		})
	if g := strAttr(t, span, AttrDecideOutcome); g != DecideOutcomeHeld {
		t.Fatalf("outcome = %q, want held under the live jev strict threshold", g)
	}
	if len(h.sink.decisions()) != 0 {
		t.Fatal("held answer applied")
	}
	assertThreshold(t, span, 0.95, flags.DecisionBackendJev)
}

func TestThreshold_JevInheritFlippedMidFlightAppliesAtLanding(t *testing.T) {
	be := jevBackend(nearThresholdConfidence)
	_, span := fireAndMutateSnapshot(t, be, defaultThresholdSnapshot(flags.DecisionBackendJev, time.Hour), time.Hour,
		func(h remoteHarness) {
			h.flags.mutate(func(s *flags.Snapshot) { s.Decision.Jev.HasActConfidenceThreshold = false })
		})
	if g := strAttr(t, span, AttrDecideOutcome); g != DecideOutcomeHeld {
		t.Fatalf("outcome = %q, want held: jev flipped to inherit the global 0.8 mid-flight", g)
	}
	assertThreshold(t, span, flags.DefaultDecisionActConfidenceThreshold, flags.ActThresholdSourceGlobal)
}
