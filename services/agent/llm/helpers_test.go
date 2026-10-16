package llm

import (
	"flag"
	"time"
)

// update regenerates the golden files: go test ./llm -run TestRetroGolden -update.
var update = flag.Bool("update", false, "update golden files")

// fixedNow is the injected wall clock used by every test, so captured_at is
// deterministic.
var fixedNow = time.Date(2026, time.June, 5, 12, 30, 0, 0, time.UTC)

func fptr(v float64) *float64 { return &v }
func iptr(v int) *int         { return &v }
func bptr(v bool) *bool       { return &v }
func sptr(v string) *string   { return &v }
