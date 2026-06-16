package gamecontext

import (
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
	if gameTraceID == "" || gameTraceSpanID == "" {
		return nil
	}
	tid, err := trace.TraceIDFromHex(gameTraceID)
	if err != nil || !tid.IsValid() {
		return nil
	}
	sid, err := trace.SpanIDFromHex(gameTraceSpanID)
	if err != nil || !sid.IsValid() {
		return nil
	}
	sc := trace.NewSpanContext(trace.SpanContextConfig{
		TraceID:    tid,
		SpanID:     sid,
		TraceFlags: trace.FlagsSampled,
		Remote:     true,
	})
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
