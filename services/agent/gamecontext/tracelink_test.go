package gamecontext

import (
	"context"
	"testing"

	"go.opentelemetry.io/otel/trace"
)

const (
	validTrace  = "4bf92f3577b34da6a3ce929d0e0e4736" // valid 16-byte hex trace id
	validSpanID = "051581bf3cb55c13"                 // valid 8-byte hex span id
)

// TestTraceLink covers the single game-trace Link primitive (#1174): a valid
// (trace,span) pair yields exactly one option; any missing/invalid id yields nil
// (graceful no-Link fallback, no panic).
func TestTraceLink(t *testing.T) {
	cases := []struct {
		name      string
		trace     string
		span      string
		wantLinks int
	}{
		{"valid", validTrace, validSpanID, 1},
		{"empty trace", "", validSpanID, 0},
		{"empty span", validTrace, "", 0},
		{"both empty", "", "", 0},
		{"invalid trace", "zzzz", validSpanID, 0},
		{"invalid span", validTrace, "zz", 0},
		{"all-zero trace", "00000000000000000000000000000000", validSpanID, 0},
		{"all-zero span", validTrace, "0000000000000000", 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := TraceLink(tc.trace, tc.span, "game.trace_id", "game.trace_span_id")
			if len(got) != tc.wantLinks {
				t.Errorf("TraceLink(%q,%q) = %d options, want %d", tc.trace, tc.span, len(got), tc.wantLinks)
			}
		})
	}
}

// TestRemoteParent covers the #1178 sibling primitive: a valid (trace,span) pair returns a
// context carrying the game span as a remote parent (ok=true), reusing the EXACT
// SpanContext construction TraceLink uses; any missing/invalid id returns the unchanged
// ctx + ok=false (graceful own-root fallback, no panic).
func TestRemoteParent(t *testing.T) {
	cases := []struct {
		name   string
		trace  string
		span   string
		wantOK bool
	}{
		{"valid", validTrace, validSpanID, true},
		{"empty trace", "", validSpanID, false},
		{"empty span", validTrace, "", false},
		{"both empty", "", "", false},
		{"invalid trace", "zzzz", validSpanID, false},
		{"invalid span", validTrace, "zz", false},
		{"all-zero trace", "00000000000000000000000000000000", validSpanID, false},
		{"all-zero span", validTrace, "0000000000000000", false},
	}
	base := context.Background()
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ctx, ok := RemoteParent(base, tc.trace, tc.span)
			if ok != tc.wantOK {
				t.Fatalf("RemoteParent(%q,%q) ok = %v, want %v", tc.trace, tc.span, ok, tc.wantOK)
			}
			if !tc.wantOK {
				if ctx != base {
					t.Error("invalid ids must return the unchanged ctx")
				}
				return
			}
			sc := trace.SpanContextFromContext(ctx)
			wantTID, _ := trace.TraceIDFromHex(tc.trace)
			wantSID, _ := trace.SpanIDFromHex(tc.span)
			if sc.TraceID() != wantTID || sc.SpanID() != wantSID {
				t.Errorf("remote parent = %s/%s, want %s/%s", sc.TraceID(), sc.SpanID(), wantTID, wantSID)
			}
			if !sc.IsRemote() {
				t.Error("remote parent SpanContext must be marked Remote")
			}
			if !sc.IsSampled() {
				t.Error("remote parent SpanContext must be Sampled so the child inherits the game's sampling")
			}
		})
	}
}

// TestRemoteParentFromSpanContext covers the #1188 SpanContext-typed sibling: a VALID
// SpanContext is installed as the remote parent (ok=true); an invalid (zero)
// SpanContext returns the unchanged ctx + ok=false (graceful own-root fallback).
func TestRemoteParentFromSpanContext(t *testing.T) {
	base := context.Background()

	// Invalid (zero) SpanContext => no re-parent, unchanged ctx.
	if ctx, ok := RemoteParentFromSpanContext(base, trace.SpanContext{}); ok || ctx != base {
		t.Errorf("zero SpanContext: ok=%v ctx-changed=%v, want ok=false, unchanged", ok, ctx != base)
	}

	tid, _ := trace.TraceIDFromHex(validTrace)
	sid, _ := trace.SpanIDFromHex(validSpanID)
	sc := trace.NewSpanContext(trace.SpanContextConfig{
		TraceID: tid, SpanID: sid, TraceFlags: trace.FlagsSampled,
	})
	ctx, ok := RemoteParentFromSpanContext(base, sc)
	if !ok {
		t.Fatal("valid SpanContext must re-parent (ok=true)")
	}
	got := trace.SpanContextFromContext(ctx)
	if got.TraceID() != tid || got.SpanID() != sid {
		t.Errorf("remote parent = %s/%s, want %s/%s", got.TraceID(), got.SpanID(), tid, sid)
	}
	if !got.IsRemote() {
		t.Error("installed parent SpanContext must be marked Remote")
	}
}
