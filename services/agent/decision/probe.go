package decision

import (
	"context"
	"sync"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// ProbeRules is a demo/verification rules engine that emits a synthetic
// "noop" decision at most once per interval. It exists because the agent only
// emits its audit trace when the rules engine returns decisions — with the
// default NoopRules the trace pipeline can never be observed end-to-end until
// the real rules engine (#726) lands. Enable via AGENT_PROBE_DECISIONS=true;
// off by default and never meant for production sessions.
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
		Intervention: "noop",
		Reason:       "probe decision for trace pipeline verification",
	}}
}
