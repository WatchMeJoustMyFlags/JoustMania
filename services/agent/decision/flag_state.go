package decision

import (
	"context"
	"fmt"
	"hash/fnv"
	"sync"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"go.opentelemetry.io/otel/trace/noop"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// agent.flag_state (#2059): the effective flag set of one game partition, as a span.
// The decision trace is lazy (#1053): idle cycles emit no span, so a quiet rules game
// showed its flags only in the agent.evaluate log line. One agent.flag_state per game
// partition on its first evaluated cycle and on every change of the effective
// snapshot makes them readable from Jaeger. A repeated idle cycle with the same
// snapshot emits nothing (a digest compare).
const (
	// SpanFlagState is the span name.
	SpanFlagState = "agent.flag_state"
	// AttrFlagStateTrigger says why the span was emitted: FlagStateTriggerGameStart
	// (the partition's first evaluated cycle) or FlagStateTriggerChanged.
	AttrFlagStateTrigger = "agent.flag_state.trigger"
	// AttrGameMode is the game mode the agent ingested for the partition
	// (GameContext.Session.GameMode). Never game.name: the agent's own isGameRootSpan
	// keys on it.
	AttrGameMode = "game.mode"

	FlagStateTriggerGameStart = "game_start"
	FlagStateTriggerChanged   = "changed"
)

// flagStateTracker is the per-Loop (= per game partition) digest of the last emitted
// agent.flag_state. Guarded by its own mutex: OnEvaluate runs concurrently from the
// trace and metrics Export handlers.
type flagStateTracker struct {
	mu      sync.Mutex
	emitted bool
	digest  uint64
}

// claim reports whether a snapshot with this digest must be emitted, and the trigger.
// It records the digest, so of two concurrent cycles with the same new snapshot only
// one emits.
func (t *flagStateTracker) claim(digest uint64) (string, bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if !t.emitted {
		t.emitted, t.digest = true, digest
		return FlagStateTriggerGameStart, true
	}
	if t.digest == digest {
		return "", false
	}
	t.digest = digest
	return FlagStateTriggerChanged, true
}

// flagStateDigest hashes the whole effective snapshot plus the partition's game mode
// and kind. Every Snapshot field is a value, slice or map (fmt prints maps sorted),
// so equal snapshots render equal.
func flagStateDigest(snapshot flags.Snapshot, gameMode, gameKind string) uint64 {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "%+v\x00%s\x00%s", snapshot, gameMode, gameKind)
	return h.Sum64()
}

// evaluateFlags resolves this cycle's snapshot with a context whose span buffers the
// OpenFeature traces hook's feature_flag.evaluation events (the hook adds them to
// trace.SpanFromContext). Whether agent.flag_state is emitted is known only after the
// evaluation, so the events are replayed onto that span when it is. They are also
// forwarded to the caller's span, so a span in ctx sees them as before.
func (l *Loop) evaluateFlags(ctx context.Context) (flags.Snapshot, *flagEventBuffer) {
	buf := &flagEventBuffer{Span: noop.Span{}, parent: trace.SpanFromContext(ctx)}
	return l.Flags.Evaluate(trace.ContextWithSpan(ctx, buf)), buf
}

// emitFlagState emits agent.flag_state when the snapshot is the partition's first or
// differs from the last one emitted. It runs before the kill-switch short-circuit so
// enabled=false is visible. Parent: remote child of the game's game_cycle with the
// inGameRemoteParent fallback (UC-040 BR-013), else its own root.
func (l *Loop) emitFlagState(ctx context.Context, snapshot flags.Snapshot, c gamecontext.GameContext, events *flagEventBuffer) {
	mode := derefStr(c.Session.GameMode)
	trigger, emit := l.flagState.claim(flagStateDigest(snapshot, mode, c.GameKind))
	if !emit {
		return
	}
	if parentCtx, ok := inGameRemoteParent(ctx, c.GameTraceID, c.GameTraceSpanID, c.GameplayPhaseSpanID, c.GameCycleSpanID); ok {
		ctx = parentCtx
	}
	_, span := l.Tracer.Start(ctx, SpanFlagState, trace.WithAttributes(flagStateAttributes(snapshot, c, mode, trigger)...))
	events.replay(span)
	span.End()
}

// flagStateAttributes is the agent.flag_state attribute set: the partition identity
// and the same flag attribution agent.decision / agent.disabled carry.
func flagStateAttributes(snapshot flags.Snapshot, c gamecontext.GameContext, mode, trigger string) []attribute.KeyValue {
	state := newLayerState(snapshot)
	attrs := []attribute.KeyValue{
		attribute.String(AttrGameID, c.SessionID),
		attribute.String(AttrSessionID, c.SessionID),
		attribute.String(AttrGameKind, c.GameKind),
		attribute.String(AttrGameMode, mode),
		attribute.String(AttrFlagStateTrigger, trigger),
	}
	attrs = append(attrs, layerStateAttributes(&state)...)
	return append(attrs,
		attribute.String(AttrDecideBackendRequested, requestedBackend(snapshot)),
		attribute.Float64(AttrDecideActThreshold, snapshot.Decision.ActConfidenceThreshold),
	)
}

// flagEventBuffer is the context span evaluateFlags installs: it records AddEvent
// calls (and forwards them to the caller's span) so they can be replayed. Every other
// Span method is the no-op span's.
type flagEventBuffer struct {
	noop.Span
	parent trace.Span

	mu     sync.Mutex
	events []bufferedEvent
}

type bufferedEvent struct {
	name string
	opts []trace.EventOption
}

// AddEvent records the event and forwards it to the caller's span.
func (b *flagEventBuffer) AddEvent(name string, opts ...trace.EventOption) {
	b.parent.AddEvent(name, opts...)
	cfg := trace.NewEventConfig(opts...)
	// Pin the original timestamp so the replayed event keeps it.
	kept := []trace.EventOption{trace.WithTimestamp(cfg.Timestamp()), trace.WithAttributes(cfg.Attributes()...)}
	b.mu.Lock()
	b.events = append(b.events, bufferedEvent{name: name, opts: kept})
	b.mu.Unlock()
}

// replay adds the recorded events to span. Nil-safe.
func (b *flagEventBuffer) replay(span trace.Span) {
	if b == nil {
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	for _, ev := range b.events {
		span.AddEvent(ev.name, ev.opts...)
	}
}
