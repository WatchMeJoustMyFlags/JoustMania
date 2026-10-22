package decision

import (
	"context"
	"sync"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// ProbeRules is a demo/verification rules engine that emits a synthetic
// "noop" decision at most once per interval, so the whole decide → permission →
// act path and its audit trace can be exercised without any game effect
// (UC-040 A23, the `probe` permission level). Enable via
// the decision.probe_decisions flag; off by default and never meant for production
// sessions.
type ProbeRules struct {
	interval time.Duration
	now      func() time.Time

	mu   sync.Mutex
	last time.Time
}

// NewProbeRules builds a probe that fires at most once per interval. now is
// injectable for tests; nil uses time.Now.
func NewProbeRules(interval time.Duration, now func() time.Time) *ProbeRules {
	if now == nil {
		now = time.Now
	}
	return &ProbeRules{interval: interval, now: now}
}

// Evaluate returns one synthetic noop decision when the interval has elapsed,
// otherwise nil.
func (p *ProbeRules) Evaluate(context.Context, gamecontext.GameContext) []Decision {
	p.mu.Lock()
	defer p.mu.Unlock()
	t := p.now()
	if t.Sub(p.last) < p.interval {
		return nil
	}
	p.last = t
	return []Decision{{
		Intervention: InterventionNoop,
		Reason:       "probe decision for trace pipeline verification",
	}}
}
