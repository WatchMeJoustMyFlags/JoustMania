package decision

import (
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/flags"
)

// Span attributes for decisions in pattern games (telemetry.md §9).
const (
	AttrPatternTargetSlot   = "pattern.target_slot"
	AttrDecisionChaosDrawIx = "decision.chaos_draw_index"
)

// PatternObserver is the decision loop's view of a pattern game (UC-046). The
// loop is built before the runner registers the game, so every method answers
// for the CURRENT state (Active may turn true later).
type PatternObserver interface {
	// Active reports whether this loop's game is a live pattern game.
	Active() bool
	// MarkNonExact records a non-exact reason (UC-046 BR-008).
	MarkNonExact(reason string)
	// RecordDecision appends a decision and returns the target's slot (-1 = no player).
	RecordDecision(intervention, targetSerial, backend string) (slot int, ok bool)
}

// chaosDrawCounter is implemented by the rules engine (ObjectiveRules).
type chaosDrawCounter interface{ ChaosDraws() uint64 }

// SetPatternObserver wires the pattern-game observer (UC-046). Set at construction.
func (l *Loop) SetPatternObserver(o PatternObserver) { l.pattern = o }

// markPatternRemote marks this game non-exact when a remote backend is selected.
func (l *Loop) markPatternRemote() {
	if l.pattern != nil && l.pattern.Active() {
		l.pattern.MarkNonExact("remote_backend")
	}
}

// recordPatternDecision appends d to the pattern game's decision sequence and
// stamps pattern.target_slot / decision.chaos_draw_index on the decision span.
func (l *Loop) recordPatternDecision(span trace.Span, snapshot flags.Snapshot, d Decision) {
	if l.pattern == nil || !l.pattern.Active() {
		return
	}
	slot, ok := l.pattern.RecordDecision(d.Intervention, d.TargetSerial, requestedBackend(snapshot))
	if !ok {
		return
	}
	if slot >= 0 {
		span.SetAttributes(attribute.Int(AttrPatternTargetSlot, slot))
	}
	if c, isCounter := l.Rules.(chaosDrawCounter); isCounter {
		span.SetAttributes(attribute.Int64(AttrDecisionChaosDrawIx, int64(c.ChaosDraws())))
	}
}
