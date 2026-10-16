package decision

import (
	"context"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	"github.com/joustmania/agent/flags"
)

// llm_admission_test.go covers the shared LLM call gate the post-game proposer
// uses (#1270): eligibility, the shared budget, the env override, and the
// agent_llm_gated_total{reason} counter.

func admissionUnderTest(t *testing.T, kinds []string, maxPerMinute, override int, budget *llmBudget) (*LLMAdmission, *metric.ManualReader) {
	t.Helper()
	src := fakeFlags{snap: flags.Snapshot{LLMGate: flags.LLMGate{
		EligibleGameKinds:    kinds,
		MaxRequestsPerMinute: maxPerMinute,
	}}}
	a := NewLLMAdmission(src, budget, override, slog.New(slog.NewTextHandler(io.Discard, nil)))
	reader := metric.NewManualReader()
	a.llmGated = newLLMGatedCounter(metric.NewMeterProvider(metric.WithReader(reader)))
	now := time.Unix(1000, 0)
	a.now = func() time.Time { return now }
	return a, reader
}

// gatedByReason collects agent_llm_gated_total into a reason -> count map.
func gatedByReason(t *testing.T, reader *metric.ManualReader) map[string]int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatalf("collect metrics: %v", err)
	}
	out := map[string]int64{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "agent_llm_gated_total" {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			if !ok {
				t.Fatalf("expected Sum[int64], got %T", m.Data)
			}
			for _, dp := range sum.DataPoints {
				reason, _ := dp.Attributes.Value("reason")
				out[reason.AsString()] = dp.Value
			}
		}
	}
	return out
}

func TestLLMAdmission_AdmitsAndChargesSharedBudget(t *testing.T) {
	budget := NewLLMBudget()
	a, reader := admissionUnderTest(t, []string{"real"}, 2, 0, budget)

	if ok, reason := a.Admit(context.Background(), "real"); !ok {
		t.Fatalf("first call gated (%q), want admitted", reason)
	}
	// The slot was taken from the SHARED budget: only one of the cap's two remains.
	if left := slotsLeft(budget, a.now(), 2); left != 1 {
		t.Fatalf("slots left = %d, want 1 (admission must charge the shared budget)", left)
	}
	if got := gatedByReason(t, reader); len(got) != 0 {
		t.Fatalf("gated counter = %v, want none", got)
	}
}

func TestLLMAdmission_BudgetExhaustedGates(t *testing.T) {
	budget := NewLLMBudget()
	a, reader := admissionUnderTest(t, []string{"real"}, 1, 0, budget)
	// A remote decide call already spent this minute's only slot.
	if !budget.allow(a.now(), 1) {
		t.Fatal("pre-spend should succeed")
	}

	ok, reason := a.Admit(context.Background(), "real")
	if ok || reason != FallbackBudgetExhausted {
		t.Fatalf("Admit = (%v, %q), want (false, %q)", ok, reason, FallbackBudgetExhausted)
	}
	if got := gatedByReason(t, reader)[FallbackBudgetExhausted]; got != 1 {
		t.Fatalf("gated{llm_budget_exhausted} = %d, want 1", got)
	}
}

func TestLLMAdmission_NotEligibleGatesWithoutCharging(t *testing.T) {
	budget := NewLLMBudget()
	a, reader := admissionUnderTest(t, []string{"real"}, 5, 0, budget)

	ok, reason := a.Admit(context.Background(), "shadow")
	if ok || reason != FallbackNotEligible {
		t.Fatalf("Admit = (%v, %q), want (false, %q)", ok, reason, FallbackNotEligible)
	}
	if left := slotsLeft(budget, a.now(), 5); left != 5 {
		t.Fatalf("slots left = %d, want 5 (an ineligible call must not charge)", left)
	}
	if got := gatedByReason(t, reader)[FallbackNotEligible]; got != 1 {
		t.Fatalf("gated{llm_not_eligible} = %d, want 1", got)
	}
}

// The AGENT_LLM_MAX_REQUESTS_PER_MINUTE override replaces the flag cap, as on the
// decide path and the retro.
func TestLLMAdmission_EnvOverrideReplacesFlagCap(t *testing.T) {
	budget := NewLLMBudget()
	a, _ := admissionUnderTest(t, []string{"real"}, 1, 3, budget)
	for i := 0; i < 3; i++ {
		if ok, reason := a.Admit(context.Background(), "real"); !ok {
			t.Fatalf("call %d gated (%q), want admitted under the override cap of 3", i+1, reason)
		}
	}
	if ok, _ := a.Admit(context.Background(), "real"); ok {
		t.Fatal("4th call admitted, want gated at the override cap")
	}
}

func TestLLMAdmission_ConcurrentAdmitsRespectCap(t *testing.T) {
	budget := NewLLMBudget()
	a, _ := admissionUnderTest(t, []string{"real"}, 4, 0, budget)
	var wg sync.WaitGroup
	var mu sync.Mutex
	admitted := 0
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if ok, _ := a.Admit(context.Background(), "real"); ok {
				mu.Lock()
				admitted++
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	if admitted != 4 {
		t.Fatalf("admitted = %d, want exactly the cap of 4", admitted)
	}
}
