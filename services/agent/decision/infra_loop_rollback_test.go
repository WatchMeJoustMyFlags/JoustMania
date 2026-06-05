package decision

import (
	"context"
	"errors"
	"testing"
	"time"
)

// failingHz is below the default movement-update floor (10), so infraSnap with
// this rate produces a movement_update_hz violation → failing fitness.
const failingHz = 5

// passingHz is comfortably above the floor → passing fitness.
const passingHz = 30

// TestInfraRollback_AllowedAtActiveStage: failing fitness + remediation allowed
// + stage>0 → one SetControllerCount("none"), span "rollback".
func TestInfraRollback_AllowedAtActiveStage(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)
	l.SetRemediationSource(StaticRemediation(true))

	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 3, clock, failingHz))

	if got := rollout.calls(); len(got) != 1 || got[0] != "none" {
		t.Fatalf("writes = %v, want [none] (rollback resets to stable default)", got)
	}
	spans := infraSpans(sr)
	if len(spans) != 1 {
		t.Fatalf("got %d spans, want 1", len(spans))
	}
	if v, _ := spanStr(spans[0], AttrRemediationAction); v != RemediationRollback {
		t.Errorf("remediation.action = %q, want %q", v, RemediationRollback)
	}
	if v, ok := spanStr(spans[0], AttrFitnessViolations); !ok || v == "" {
		t.Errorf("fitness.violations = %q, want non-empty on rollback", v)
	}
}

// TestInfraRollback_NotAllowedRecommendsOnly: failing fitness + NOT allowed →
// no write, span "recommended_only" with violations.
func TestInfraRollback_NotAllowedRecommendsOnly(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)
	l.SetRemediationSource(StaticRemediation(false))

	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 3, clock, failingHz))

	if got := rollout.calls(); len(got) != 0 {
		t.Fatalf("writes = %v, want none (remediation not allowed)", got)
	}
	spans := infraSpans(sr)
	if len(spans) != 1 {
		t.Fatalf("got %d spans, want 1", len(spans))
	}
	if v, _ := spanStr(spans[0], AttrRemediationAction); v != RemediationRecommendedOnly {
		t.Errorf("remediation.action = %q, want %q", v, RemediationRecommendedOnly)
	}
	if v, ok := spanStr(spans[0], AttrFitnessViolations); !ok || v == "" {
		t.Errorf("fitness.violations = %q, want non-empty", v)
	}
}

// TestInfraRollback_NothingToRollBackAtStageNone: failing + allowed + stage==0
// → nothing to roll back, "none", no write.
func TestInfraRollback_NothingToRollBackAtStageNone(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)
	l.SetRemediationSource(StaticRemediation(true))

	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, failingHz))

	if got := rollout.calls(); len(got) != 0 {
		t.Fatalf("writes = %v, want none (already at stage none)", got)
	}
	spans := infraSpans(sr)
	if len(spans) != 1 {
		t.Fatalf("got %d spans, want 1", len(spans))
	}
	if v, _ := spanStr(spans[0], AttrRemediationAction); v != RemediationNone {
		t.Errorf("remediation.action = %q, want %q (nothing to roll back)", v, RemediationNone)
	}
}

// TestInfraRollback_SettlingWindowNoSecondWrite: after a rollback write, while
// the observed count is still >0 (controller-manager catching up), subsequent
// failing cycles record "none" and do NOT write again.
func TestInfraRollback_SettlingWindowNoSecondWrite(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)
	l.SetRemediationSource(StaticRemediation(true))

	// First failing cycle at stage 3 → rollback written.
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 3, clock, failingHz))
	// Settling: count not yet back to 0, still failing → no second write.
	clock = clock.Add(time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 3, clock, failingHz))
	clock = clock.Add(time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 1, clock, failingHz))

	if got := rollout.calls(); len(got) != 1 {
		t.Fatalf("writes = %v, want exactly one rollback during settling window", got)
	}
	spans := infraSpans(sr)
	if len(spans) != 3 {
		t.Fatalf("got %d spans, want 3", len(spans))
	}
	if v, _ := spanStr(spans[0], AttrRemediationAction); v != RemediationRollback {
		t.Errorf("span[0] action = %q, want %q", v, RemediationRollback)
	}
	for i := 1; i < 3; i++ {
		if v, _ := spanStr(spans[i], AttrRemediationAction); v != RemediationNone {
			t.Errorf("settling span[%d] action = %q, want %q (rollback in flight)", i, v, RemediationNone)
		}
	}
}

// TestInfraRollback_CooldownThenExpand: after a rollback, observed count returns
// to 0 and fitness recovers → expansion is HELD for the cooldown ("none"), then
// expands once the cooldown elapses ("expand").
func TestInfraRollback_CooldownThenExpand(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)
	l.SetRemediationSource(StaticRemediation(true))
	l.SetCooldown(30 * time.Second)

	// Rollback at stage 3.
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 3, clock, failingHz))
	// Count back to 0, fitness recovered, but within cooldown → no expand.
	clock = clock.Add(5 * time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, passingHz))
	if got := rollout.calls(); len(got) != 1 {
		t.Fatalf("writes = %v, want only the rollback (expansion held by cooldown)", got)
	}
	if spans := infraSpans(sr); len(spans) != 2 {
		t.Fatalf("got %d spans, want 2", len(spans))
	} else if v, _ := spanStr(spans[1], AttrRemediationAction); v != RemediationNone {
		t.Errorf("in-cooldown action = %q, want %q", v, RemediationNone)
	}

	// Advance past the cooldown → expansion resumes (none→one).
	clock = clock.Add(30 * time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, passingHz))
	if got := rollout.calls(); len(got) != 2 || got[1] != "one" {
		t.Fatalf("writes = %v, want rollback then expand-to-one after cooldown", got)
	}
	spans := infraSpans(sr)
	if v, _ := spanStr(spans[len(spans)-1], AttrRemediationAction); v != RemediationExpand {
		t.Errorf("post-cooldown action = %q, want %q", v, RemediationExpand)
	}
}

// TestInfraRollback_EndToEndNarrative: the mini PR-G narrative —
// failing(rollback) → settling(none) → recovered+cooldown(none) → expand.
func TestInfraRollback_EndToEndNarrative(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)
	l.SetRemediationSource(StaticRemediation(true))
	l.SetCooldown(30 * time.Second)

	// 1. Failing at stage 6 → rollback.
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 6, clock, failingHz))
	// 2. Settling: still observed >0, still failing → none.
	clock = clock.Add(time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 6, clock, failingHz))
	// 3. Count back to 0, fitness recovered, within cooldown → none.
	clock = clock.Add(time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, passingHz))
	// 4. Cooldown elapsed, still passing → expand.
	clock = clock.Add(30 * time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, passingHz))

	want := []string{
		RemediationRollback,
		RemediationNone,
		RemediationNone,
		RemediationExpand,
	}
	spans := infraSpans(sr)
	if len(spans) != len(want) {
		t.Fatalf("got %d spans, want %d", len(spans), len(want))
	}
	for i, w := range want {
		if v, _ := spanStr(spans[i], AttrRemediationAction); v != w {
			t.Errorf("narrative span[%d] action = %q, want %q", i, v, w)
		}
	}
	if got := rollout.calls(); len(got) != 2 || got[0] != "none" || got[1] != "one" {
		t.Fatalf("writes = %v, want [none one] (rollback then expand)", got)
	}
}

// TestInfraRollback_SourceErrorTreatedAsFalse: a RemediationSource that reports
// not-allowed (the fail-closed projection of any error) → recommend-only.
func TestInfraRollback_SourceErrorTreatedAsFalse(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)
	// erroringRemediation models an error path: the live reader returns false on
	// any error, so the loop sees not-allowed.
	l.SetRemediationSource(errorRemediation{})

	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 3, clock, failingHz))

	if got := rollout.calls(); len(got) != 0 {
		t.Fatalf("writes = %v, want none (source error → not allowed)", got)
	}
	if v, _ := spanStr(infraSpans(sr)[0], AttrRemediationAction); v != RemediationRecommendedOnly {
		t.Errorf("remediation.action = %q, want %q", v, RemediationRecommendedOnly)
	}
}

// errorRemediation is a RemediationSource whose underlying resolution failed; it
// fails closed to not-allowed, exactly like actions.RemediationReader does.
type errorRemediation struct{}

func (errorRemediation) RemediationAllowed() bool { return false }

// dryRunRollout is a RolloutActuator that records nothing (a write seam that
// would be a no-op in the disabled rollout). It models actions.DryRunRolloutWriter
// for the rollback path.
type dryRunRollout struct{ fakeRollout }

func (d *dryRunRollout) SetControllerCount(variant string) error {
	// no-op: dry-run never writes, mirrors DryRunRolloutWriter.
	return nil
}

// TestInfraRollback_DryRunDecidedNotApplied: with a dry-run actuator, a failing
// allowed cycle still records "rollback" (decided) but performs no real write.
func TestInfraRollback_DryRunDecidedNotApplied(t *testing.T) {
	clock := time.Unix(1000, 0)
	dry := &dryRunRollout{}
	l, sr := newInfraLoop(t, dry, &clock)
	l.SetRemediationSource(StaticRemediation(true))

	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 3, clock, failingHz))

	// dry-run records nothing in the embedded fakeRollout's slice.
	if got := dry.calls(); len(got) != 0 {
		t.Fatalf("dry-run recorded writes = %v, want none", got)
	}
	if v, _ := spanStr(infraSpans(sr)[0], AttrRemediationAction); v != RemediationRollback {
		t.Errorf("dry-run action = %q, want %q (decided but not applied)", v, RemediationRollback)
	}
}

var _ RolloutActuator = (*dryRunRollout)(nil)

// TestInfraRollback_WriteErrorRecordedOnSpan: a failing rollback write sets the
// span status to Error and does NOT mark the episode in flight (so a retry can
// happen), mirroring the expansion error path.
func TestInfraRollback_WriteErrorRecordedOnSpan(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{err: errors.New("disk full")}
	l, sr := newInfraLoop(t, rollout, &clock)
	l.SetRemediationSource(StaticRemediation(true))

	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 3, clock, failingHz))

	spans := infraSpans(sr)
	if len(spans) != 1 {
		t.Fatalf("got %d spans, want 1", len(spans))
	}
	if status := spans[0].Status(); status.Code.String() != "Error" {
		t.Errorf("span status = %v, want Error", status.Code)
	}
	if v, _ := spanStr(spans[0], AttrRemediationAction); v != RemediationRollback {
		t.Errorf("action = %q, want %q even on write error", v, RemediationRollback)
	}

	// Episode NOT in flight after failed write: a subsequent failing cycle retries.
	rollout.mu.Lock()
	rollout.err = nil
	rollout.mu.Unlock()
	clock = clock.Add(time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 3, clock, failingHz))
	if got := rollout.calls(); len(got) != 1 || got[0] != "none" {
		t.Fatalf("after failed write then success, writes = %v, want one retried rollback", got)
	}
}
