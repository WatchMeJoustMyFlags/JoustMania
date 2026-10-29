package telemetry

import (
	"testing"
	"time"

	tracenoop "go.opentelemetry.io/otel/trace/noop"
)

// UC: UC-053 step 8
func TestLinksLookupWaitsForAnInflightWrite(t *testing.T) {
	t.Run("UC-053/A7 a push that runs before Remember still finds the write's link (#2369)", func(t *testing.T) {
		var l Links
		l.Begin()
		go func() {
			time.Sleep(5 * time.Millisecond)
			l.Remember(7, tracenoop.Span{}.SpanContext(), time.Now())
			l.End()
		}()
		if _, ok := l.Lookup(7); !ok {
			t.Fatal("the link of the in-flight write was lost")
		}
	})
	t.Run("UC-053/A7 with no write in flight an unknown version is not waited for", func(t *testing.T) {
		var l Links
		start := time.Now()
		if _, ok := l.Lookup(9); ok {
			t.Fatal("found a link nobody remembered")
		}
		if time.Since(start) > linkWait/2 {
			t.Fatalf("lookup waited %v with nothing in flight", time.Since(start))
		}
	})
}
