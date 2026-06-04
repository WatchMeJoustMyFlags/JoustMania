// Package decision holds the agent's decision loop: a gated, throttled hook that
// turns a GameContext snapshot into interventions via a pluggable rules engine
// and action sink. The concrete rules and actions are stubbed in this scaffold.
package decision

import (
	"log/slog"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// Decision is a single intervention the rules engine wants applied.
type Decision struct {
	// Intervention is an identifier from interventions.allowed
	// (docs/research/722-intervention-surface.md §6), e.g. "grant_shield".
	Intervention string
	// TargetSerial scopes the intervention to one player; empty = session-scoped.
	TargetSerial string
	// Reason is a human-readable explanation for logging/observability.
	Reason string
}

// RulesEngine turns a context snapshot into zero or more Decisions.
type RulesEngine interface {
	Evaluate(gamecontext.GameContext) []Decision
}

// ActionSink applies decisions to the outside world.
type ActionSink interface {
	Apply([]Decision) error
}

// throttleInterval bounds how often the evaluate log line is emitted.
const throttleInterval = time.Second

// Loop wires the rules engine to the action sink and is invoked once per gated
// signal update.
type Loop struct {
	Rules   RulesEngine
	Actions ActionSink
	Log     *slog.Logger

	lastLog time.Time
	now     func() time.Time
}

// NewLoop builds a Loop with the no-op rules/actions stubs. log may be nil, in
// which case slog.Default() is used.
func NewLoop(log *slog.Logger) *Loop {
	if log == nil {
		log = slog.Default()
	}
	return &Loop{
		Rules:   NoopRules{},
		Actions: NoopActions{},
		Log:     log,
		now:     time.Now,
	}
}

// OnEvaluate runs one evaluation pass: emit a throttled (max 1/second) info log,
// run the rules, and apply any resulting decisions.
func (l *Loop) OnEvaluate(c gamecontext.GameContext) {
	now := l.now
	if now == nil {
		now = time.Now
	}
	if t := now(); t.Sub(l.lastLog) >= throttleInterval {
		l.lastLog = t
		l.Log.Info("agent.evaluate",
			"session_id", c.SessionID,
			"player_count", len(c.Players),
			"game_mode", derefStr(c.Session.GameMode),
			"duration", derefFloat(c.Session.DurationSeconds),
		)
	}

	decisions := l.Rules.Evaluate(c)
	if len(decisions) == 0 {
		return
	}
	if err := l.Actions.Apply(decisions); err != nil {
		l.Log.Error("agent.apply_failed", "error", err, "decisions", len(decisions))
	}
}

func derefStr(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

func derefFloat(p *float64) float64 {
	if p == nil {
		return 0
	}
	return *p
}
