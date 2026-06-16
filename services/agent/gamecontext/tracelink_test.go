package gamecontext

import "testing"

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
