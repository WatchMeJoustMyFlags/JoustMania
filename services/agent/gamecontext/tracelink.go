package gamecontext

import (
	"context"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
)

// TraceLink builds the game-session-trace correlation span Link (#1133/#1157): when
// BOTH gameTraceID and gameTraceSpanID are valid hex ids it returns a single
// trace.WithLinks option whose Link targets a remote SpanContext reconstructed from
// BOTH ids — so the linking span references the game's ROOT span (Jaeger's uiFind
// highlights the actual game-start span, #1157) rather than an all-zero span id under
// the trace. The SpanContext is marked Remote + Sampled. Both ids are stamped as Link
// attributes under traceIDAttrKey / spanIDAttrKey (the caller passes its own canonical
// keys so the on-wire attribute names are unchanged across packages).
//
// An empty/unparseable trace_id OR span_id yields NO option, so the span is created
// exactly as before: graceful fallback, no Link, no error. Pre-#1157 emitters (or an
// unsampled game span) carry an empty span_id and therefore produce no Link — the same
// as a missing trace_id.
//
// This is THE single implementation of the game-trace Link primitive (#1174). It lives
// in gamecontext because both the decision package and the cycle-independent game-end
// emitters (gamesummary) need it, and gamecontext is the one package both already
// import with no dependency cycle. decision.GameTraceLink delegates here, preserving
// the existing exported entry point (#1165).
func TraceLink(gameTraceID, gameTraceSpanID, traceIDAttrKey, spanIDAttrKey string) []trace.SpanStartOption {
	sc, ok := gameSpanContext(gameTraceID, gameTraceSpanID)
	if !ok {
		return nil
	}
	return []trace.SpanStartOption{
		trace.WithLinks(trace.Link{
			SpanContext: sc,
			Attributes: []attribute.KeyValue{
				attribute.String(traceIDAttrKey, gameTraceID),
				attribute.String(spanIDAttrKey, gameTraceSpanID),
			},
		}),
	}
}

// RemoteParent is the sibling of TraceLink for the #1178 in-game re-parenting: instead
// of LINKING a span to the game trace it makes the span a remote CHILD of the game span.
// When BOTH gameTraceID and gameTraceSpanID are valid hex ids it reconstructs the SAME
// remote game SpanContext TraceLink builds (gameSpanContext) and returns a context with
// that SpanContext installed as the remote parent (trace.ContextWithRemoteSpanContext on
// the passed parent ctx), plus ok=true. A span started from that ctx joins the game
// trace as a child of the game span — regardless of the game span's liveness, since the
// "can't child an ended span" rule is local-only — and inherits the game trace's sampling.
//
// When either id is empty/unparseable/all-zero it returns the UNCHANGED parent ctx and
// ok=false, so the caller starts the span exactly as before (own root, no parent):
// graceful fallback, never breaking span emission. This mirrors TraceLink's nil contract
// (an empty span_id from a pre-#1157 / unsampled game span yields no parent, same as a
// missing trace_id).
func RemoteParent(ctx context.Context, gameTraceID, gameTraceSpanID string) (context.Context, bool) {
	sc, ok := gameSpanContext(gameTraceID, gameTraceSpanID)
	if !ok {
		return ctx, false
	}
	return trace.ContextWithRemoteSpanContext(ctx, sc), true
}

// gameSpanContext reconstructs the remote SpanContext of the coordinator's game span from
// its hex trace_id + span_id. It is the single source of the #1133/#1157 SpanContext
// construction, shared by TraceLink (Link target) and RemoteParent (remote parent). The
// returned SpanContext is marked Remote + Sampled. ok=false when either id is empty,
// unparseable, or all-zero (invalid) — the graceful-fallback signal for both callers.
func gameSpanContext(gameTraceID, gameTraceSpanID string) (trace.SpanContext, bool) {
	if gameTraceID == "" || gameTraceSpanID == "" {
		return trace.SpanContext{}, false
	}
	tid, err := trace.TraceIDFromHex(gameTraceID)
	if err != nil || !tid.IsValid() {
		return trace.SpanContext{}, false
	}
	sid, err := trace.SpanIDFromHex(gameTraceSpanID)
	if err != nil || !sid.IsValid() {
		return trace.SpanContext{}, false
	}
	return trace.NewSpanContext(trace.SpanContextConfig{
		TraceID:    tid,
		SpanID:     sid,
		TraceFlags: trace.FlagsSampled,
		Remote:     true,
	}), true
}
