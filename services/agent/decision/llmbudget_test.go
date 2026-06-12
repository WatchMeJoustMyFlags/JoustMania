package decision

import (
	"testing"
	"time"
)

// TestLLMBudget_AdmitsUpToCap: the budget admits exactly maxPerMinute requests in
// one window, then denies.
func TestLLMBudget_AdmitsUpToCap(t *testing.T) {
	b := NewLLMBudget()
	now := time.Unix(1000, 0)
	const cap = 6
	for i := 0; i < cap; i++ {
		if !b.allow(now, cap) {
			t.Fatalf("request %d should be admitted within cap %d", i+1, cap)
		}
	}
	if b.allow(now, cap) {
		t.Errorf("request %d admitted, want denied (cap %d exhausted)", cap+1, cap)
	}
}

// TestLLMBudget_ZeroCapDeniesAll: a cap of 0 (or negative) admits nothing — the
// fully-closed safe reading.
func TestLLMBudget_ZeroCapDeniesAll(t *testing.T) {
	b := NewLLMBudget()
	now := time.Unix(1000, 0)
	if b.allow(now, 0) {
		t.Error("cap 0 admitted, want denied")
	}
	if b.allow(now, -1) {
		t.Error("negative cap admitted, want denied")
	}
}

// TestLLMBudget_WindowSlides: admissions older than the one-minute window are
// pruned, so the budget replenishes as time passes.
func TestLLMBudget_WindowSlides(t *testing.T) {
	b := NewLLMBudget()
	t0 := time.Unix(1000, 0)
	const cap = 2
	if !b.allow(t0, cap) || !b.allow(t0, cap) {
		t.Fatal("first two admissions should succeed")
	}
	if b.allow(t0, cap) {
		t.Fatal("third admission at t0 should be denied")
	}
	// Advance just past the window: the two t0 entries fall out, budget reopens.
	later := t0.Add(llmBudgetWindow + time.Second)
	if !b.allow(later, cap) {
		t.Error("admission after window slide should succeed (entries pruned)")
	}
}

// TestLLMBudget_LiveCapTightening: lowering the cap mid-window takes effect on the
// next call against the SAME instance, without losing window history (#847 #5).
func TestLLMBudget_LiveCapTightening(t *testing.T) {
	b := NewLLMBudget()
	now := time.Unix(1000, 0)
	// Admit 3 under a generous cap.
	for i := 0; i < 3; i++ {
		if !b.allow(now, 10) {
			t.Fatalf("admission %d under cap 10 should succeed", i+1)
		}
	}
	// Tighten the cap below the current window count: the next call is denied
	// immediately, even though no time passed.
	if b.allow(now, 3) {
		t.Error("admission under tightened cap 3 (window already holds 3) should be denied")
	}
}
