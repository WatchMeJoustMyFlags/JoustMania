package decision

import (
	"sync"
	"time"
)

// llmbudget.go is the GLOBAL per-minute request cap for the LLM call gate (#847,
// the third and last gate layer). Where decision/ratelimit.go's weighted
// rateLimiter is the PER-GAME intervention-dispatch budget (one per Loop, see
// loopset.go), this limiter is a SINGLE shared instance across ALL per-game loops:
// it caps the total number of LLM requests the whole agent issues per minute,
// regardless of how many concurrent games are running (#845). The shared instance
// is constructed once in main.go and closed over by the Loop factory, so 4
// concurrent eligible games draw down ONE budget and can never collectively exceed
// llm.max_requests_per_minute.
//
// It mirrors rateLimiter's sliding-window + mutex pattern, but the cost model is
// simpler: each admitted LLM attempt is ONE request (unit weight), not a weighted
// intervention. The budget is therefore a plain per-minute count, not a weight sum.

// llmBudgetWindow is the trailing window over which the global request cap
// applies, mirroring rateWindow. One minute, matching llm.max_requests_per_minute.
const llmBudgetWindow = time.Minute

// llmBudget is the global, concurrency-safe, sliding-window request counter for
// the LLM gate's budget layer (#847). It admits LLM attempts until the count of
// admissions within the trailing one-minute window reaches maxPerMinute, then
// denies further ones (it does not queue). Older admissions are pruned on each
// call, so the budget replenishes as the window slides. Safe for concurrent use:
// the per-game Export handler goroutines all call allow on the one shared instance.
type llmBudget struct {
	mu      sync.Mutex
	entries []time.Time
}

// NewLLMBudget builds an empty global LLM request budget. The cap is NOT stored on
// the budget — it is passed to allow on every call (read from the live
// llm.max_requests_per_minute flag each cycle), so tightening the flag mid-session
// takes effect on the very next admission with no reconstruction (acceptance #5).
//
// Exported because main.go constructs the ONE shared instance and injects it into
// every per-game Loop via SetLLMBudget, so all loops share a single global cap.
func NewLLMBudget() *llmBudget { return &llmBudget{} }

// allow reports whether an LLM attempt fits within the global budget at time now.
// When it fits, the admission is recorded and true is returned; when the window
// already holds maxPerMinute admissions, nothing is recorded and false is returned
// (the attempt is gated with llm_budget_exhausted). A maxPerMinute <= 0 admits
// nothing (fully closed) — the safe reading of a zero/negative cap.
//
// The cap is a parameter, not state, so the SAME budget instance honors a
// per-cycle flag value: lowering max_requests_per_minute immediately tightens the
// next call without losing the window history.
func (b *llmBudget) allow(now time.Time, maxPerMinute int) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.prune(now)
	if maxPerMinute <= 0 {
		return false
	}
	if len(b.entries) >= maxPerMinute {
		return false
	}
	b.entries = append(b.entries, now)
	return true
}

// prune drops admissions older than the trailing window ending at now. Caller
// holds b.mu. Mirrors rateLimiter.prune.
func (b *llmBudget) prune(now time.Time) {
	cutoff := now.Add(-llmBudgetWindow)
	kept := b.entries[:0]
	for _, at := range b.entries {
		if at.After(cutoff) {
			kept = append(kept, at)
		}
	}
	b.entries = kept
}
