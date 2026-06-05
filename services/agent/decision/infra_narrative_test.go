package decision

import (
	"context"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"

	"github.com/joustmania/agent/infracontext"
)

// This file is PR G's milestone acceptance coverage (#737): the remediation story
// is auditable end-to-end in the trace timeline. Two kinds of assertions:
//
//  1. Schema completeness — every emitted agent.infrastructure.decision span,
//     across ALL decision outcomes, carries the four core attributes (present, not
//     absent), so a future change that drops one on some path is caught here.
//  2. The narrative — the ordered remediation.action sequence of a full incident
//     (expand → expand → rollback → settle → cooldown → re-expand) reconstructs the
//     story exactly, and the violations string is non-empty exactly on the failing
//     cycles.

// requireCoreAttrs asserts the four core attributes are PRESENT on a span and
// returns the action + violations so callers can assert the narrative. A missing
// core attribute is a fatal schema regression.
func requireCoreAttrs(t *testing.T, s sdktrace.ReadOnlySpan, idx int) (action, violations string) {
	t.Helper()
	if _, ok := spanStr(s, AttrRolloutTarget); !ok {
		t.Fatalf("span[%d] missing core attribute %q", idx, AttrRolloutTarget)
	}
	if _, ok := attrValue(s, AttrFitnessPassing); !ok {
		t.Fatalf("span[%d] missing core attribute %q", idx, AttrFitnessPassing)
	}
	// fitness.violations must be PRESENT even when passing (empty string), never
	// absent — a query needs to tell "passing" apart from "attribute missing".
	v, ok := spanStr(s, AttrFitnessViolations)
	if !ok {
		t.Fatalf("span[%d] missing core attribute %q (must be present-but-empty when passing)", idx, AttrFitnessViolations)
	}
	a, ok := spanStr(s, AttrRemediationAction)
	if !ok {
		t.Fatalf("span[%d] missing core attribute %q", idx, AttrRemediationAction)
	}
	// bluetooth.target_backend + bluetooth.rollout_count always accompany the
	// decision (the observed signals that drove it).
	if _, ok := spanStr(s, AttrBluetoothTargetBackend); !ok {
		t.Fatalf("span[%d] missing %q", idx, AttrBluetoothTargetBackend)
	}
	if _, ok := spanInt(s, AttrBluetoothRolloutCount); !ok {
		t.Fatalf("span[%d] missing %q", idx, AttrBluetoothRolloutCount)
	}
	return a, v
}

// TestInfraSchemaCompleteness_AllOutcomes drives the loop through EVERY decision
// outcome (expand, none-while-passing/terminal, rollback, recommended_only,
// none-while-settling, none-during-cooldown) and asserts the four core attributes
// are present on every resulting span. This is the regression net: a future
// emission path that forgets to route through infraDecisionAttributes fails here.
func TestInfraSchemaCompleteness_AllOutcomes(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)
	l.SetRemediationSource(StaticRemediation(true))
	l.SetCooldown(30 * time.Second)

	// 1. expand (passing, stage 0 → 1)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, passingHz))
	// 2. none (passing but terminal stage — nothing above)
	clock = clock.Add(DefaultRolloutDwell)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 99, clock, passingHz))
	// 3. rollback (failing at active stage, allowed)
	clock = clock.Add(time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 3, clock, failingHz))
	// 4. none (settling — rollback in flight, observed count still > 0)
	clock = clock.Add(time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 3, clock, failingHz))
	// 5. none (recovered + in cooldown)
	clock = clock.Add(time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, passingHz))

	// recommended_only needs the gate flipped to not-allowed; use a second loop so
	// the cooldown state above does not mask it.
	clock2 := time.Unix(2000, 0)
	rollout2 := &fakeRollout{}
	l2, sr2 := newInfraLoop(t, rollout2, &clock2)
	l2.SetRemediationSource(StaticRemediation(false))
	// recommended_only (failing at active stage, NOT allowed)
	l2.OnInfraEvaluate(context.Background(), infraSnap("rust", 3, clock2, failingHz))

	spans := append([]sdktrace.ReadOnlySpan{}, infraSpans(sr)...)
	spans = append(spans, infraSpans(sr2)...)
	if len(spans) != 6 {
		t.Fatalf("got %d spans, want 6 (one per outcome)", len(spans))
	}

	// Every span must carry the complete core schema, whatever the outcome.
	seen := map[string]bool{}
	for i, s := range spans {
		action, _ := requireCoreAttrs(t, s, i)
		seen[action] = true
	}

	// And we must actually have EXERCISED every distinct action value, so the
	// completeness assertion above covers the whole decision surface.
	for _, want := range []string{
		RemediationExpand,
		RemediationNone,
		RemediationRollback,
		RemediationRecommendedOnly,
	} {
		if !seen[want] {
			t.Errorf("outcome %q never exercised — schema test does not cover it", want)
		}
	}
}

// TestInfraSchemaCompleteness_PassingSpanHasEmptyViolations pins the
// present-but-empty contract for fitness.violations on a passing cycle.
func TestInfraSchemaCompleteness_PassingSpanHasEmptyViolations(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)

	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, passingHz))

	spans := infraSpans(sr)
	if len(spans) != 1 {
		t.Fatalf("got %d spans, want 1", len(spans))
	}
	v, ok := spanStr(spans[0], AttrFitnessViolations)
	if !ok {
		t.Fatalf("fitness.violations absent on a passing span; want present-but-empty")
	}
	if v != "" {
		t.Errorf("fitness.violations = %q on a passing span, want empty", v)
	}
}

// TestInfraNarrative_FullIncident is the milestone acceptance test: it drives a
// complete rollout → instability → rollback → recovery → re-expand incident and
// asserts the recorded remediation.action timeline tells exactly that story, with
// the four attributes present on every span and the violations string non-empty
// on exactly the failing cycles.
func TestInfraNarrative_FullIncident(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)
	l.SetRemediationSource(StaticRemediation(true))
	l.SetCooldown(30 * time.Second)

	// (1) rollout active, fitness passing → expand 0→1.
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, passingHz))
	// (2) dwell elapses, still passing → expand 1→3.
	clock = clock.Add(DefaultRolloutDwell)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 1, clock, passingHz))
	// (3) unstable degradation arrives (violating window signal) → rollback (allowed).
	clock = clock.Add(time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 3, clock, failingHz))
	// (4) settling: observed count still 3, still failing → none (reset in flight).
	clock = clock.Add(time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 3, clock, failingHz))
	// (5) observed count 0, fitness recovered, still in cooldown → none.
	clock = clock.Add(time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, passingHz))
	// (6) cooldown elapses, still passing → expand 0→1 again.
	clock = clock.Add(30 * time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, passingHz))

	// The story, told purely by the remediation.action timeline.
	wantActions := []string{
		RemediationExpand,
		RemediationExpand,
		RemediationRollback,
		RemediationNone,
		RemediationNone,
		RemediationExpand,
	}
	// violations non-empty exactly on the two failing cycles (rollback + settling).
	wantViolating := []bool{false, false, true, true, false, false}

	spans := infraSpans(sr)
	if len(spans) != len(wantActions) {
		t.Fatalf("got %d spans, want %d", len(spans), len(wantActions))
	}
	for i := range wantActions {
		action, violations := requireCoreAttrs(t, spans[i], i)
		if action != wantActions[i] {
			t.Errorf("narrative span[%d] action = %q, want %q", i, action, wantActions[i])
		}
		if wantViolating[i] && violations == "" {
			t.Errorf("narrative span[%d] violations empty, want non-empty (failing cycle)", i)
		}
		if !wantViolating[i] && violations != "" {
			t.Errorf("narrative span[%d] violations = %q, want empty (passing cycle)", i, violations)
		}
	}

	// The actuator writes mirror the story: two expands, one rollback, one re-expand.
	if got := rollout.calls(); len(got) != 4 ||
		got[0] != "one" || got[1] != "three" || got[2] != "none" || got[3] != "one" {
		t.Fatalf("writes = %v, want [one three none one]", got)
	}
}

// TestInfraNarrative_RecommendedOnlyStalls is the recommended_only variant: with
// remediation NOT allowed, the same instability produces a recommendation that
// stalls — the rollout is never actually rolled back, so the story does not
// progress to recovery (no write ever happens, the stage stays put).
func TestInfraNarrative_RecommendedOnlyStalls(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)
	l.SetRemediationSource(StaticRemediation(false)) // rollback NOT allowed

	// (1) passing → expand 0→1.
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, passingHz))
	// (2) dwell elapses, passing → expand 1→3.
	clock = clock.Add(DefaultRolloutDwell)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 1, clock, passingHz))
	// (3) instability arrives, NOT allowed → recommended_only (no write).
	clock = clock.Add(time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 3, clock, failingHz))
	// (4) still failing, still not allowed, stage unchanged → recommended_only again.
	clock = clock.Add(time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 3, clock, failingHz))

	wantActions := []string{
		RemediationExpand,
		RemediationExpand,
		RemediationRecommendedOnly,
		RemediationRecommendedOnly,
	}
	wantViolating := []bool{false, false, true, true}

	spans := infraSpans(sr)
	if len(spans) != len(wantActions) {
		t.Fatalf("got %d spans, want %d", len(spans), len(wantActions))
	}
	for i := range wantActions {
		action, violations := requireCoreAttrs(t, spans[i], i)
		if action != wantActions[i] {
			t.Errorf("recommended-only span[%d] action = %q, want %q", i, action, wantActions[i])
		}
		if wantViolating[i] != (violations != "") {
			t.Errorf("recommended-only span[%d] violations=%q, wantViolating=%v", i, violations, wantViolating[i])
		}
	}

	// The story STALLS at recommendation: only the two expansions ever wrote; no
	// rollback was applied because remediation was not allowed.
	if got := rollout.calls(); len(got) != 2 || got[0] != "one" || got[1] != "three" {
		t.Fatalf("writes = %v, want [one three] only (recommendation never writes)", got)
	}
}

// activeSnapWithSignals builds an active-rollout snapshot that populates ALL the
// optional window signals, so the schema test can also assert the optional
// bluetooth.* attributes land when present. Unused by the action-sequence tests;
// kept here to document the full signal set on the decision span.
func activeSnapWithSignals(now time.Time) infracontext.InfraContext {
	gap := 20.0
	dropped := 0.01
	hz := 30.0
	active := 4
	return infracontext.InfraContext{
		Window: infracontext.WindowSignals{
			EventGapMs:        &gap,
			DroppedEventsPct:  &dropped,
			MovementUpdateHz:  &hz,
			ActiveControllers: &active,
			TargetBackend:     "rust",
			RolloutCount:      0,
		},
		Controllers: map[string]*infracontext.ControllerHealth{
			"AA:BB": {Serial: "AA:BB", LastUpdate: now},
		},
	}
}

// TestInfraDecisionSpan_CarriesOptionalSignals asserts the optional bluetooth.*
// signals are lifted onto the span when present in the window.
func TestInfraDecisionSpan_CarriesOptionalSignals(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)

	l.OnInfraEvaluate(context.Background(), activeSnapWithSignals(clock))

	spans := infraSpans(sr)
	if len(spans) != 1 {
		t.Fatalf("got %d spans, want 1", len(spans))
	}
	for _, key := range []string{
		AttrBluetoothEventGapMs,
		AttrBluetoothDroppedEventsPct,
		AttrBluetoothMovementUpdateHz,
	} {
		if _, ok := attrValue(spans[0], key); !ok {
			t.Errorf("optional signal %q missing from span", key)
		}
	}
	if v, ok := spanInt(spans[0], AttrBluetoothActiveControllers); !ok || v != 4 {
		t.Errorf("bluetooth.active_controllers = %d (ok=%v), want 4", v, ok)
	}
}
