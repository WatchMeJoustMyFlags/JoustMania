package actions

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

// newTestRolloutWriter copies the real rollout.json fixture into a temp file and
// returns a RolloutWriter over it plus the temp path.
func newTestRolloutWriter(t *testing.T) (*RolloutWriter, string) {
	t.Helper()
	src, err := os.ReadFile(filepath.Join("testdata", "rollout.json"))
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "rollout.json")
	if err := os.WriteFile(path, src, 0o644); err != nil {
		t.Fatalf("write temp fixture: %v", err)
	}
	return NewRolloutWriter(path, discardLogger()), path
}

func defaultVariantOf(t *testing.T, path, flagKey string) string {
	t.Helper()
	flag := readFlag(t, path, flagKey)
	dv, _ := flag["defaultVariant"].(string)
	return dv
}

func TestSetControllerCount_FlipsDefaultVariant(t *testing.T) {
	w, path := newTestRolloutWriter(t)

	if got := defaultVariantOf(t, path, flagCurrentControllerCount); got != "none" {
		t.Fatalf("fixture defaultVariant = %q, want none", got)
	}
	for _, variant := range []string{"one", "three", "six", "all"} {
		if err := w.SetControllerCount(variant); err != nil {
			t.Fatalf("SetControllerCount(%q): %v", variant, err)
		}
		if got := defaultVariantOf(t, path, flagCurrentControllerCount); got != variant {
			t.Fatalf("after SetControllerCount(%q): defaultVariant = %q", variant, got)
		}
	}
}

// TestSetControllerCount_UntouchedFlagsByteStable asserts the order-preserving
// RMW leaves every flag other than current_controller_count byte-for-byte intact.
func TestSetControllerCount_UntouchedFlagsByteStable(t *testing.T) {
	w, path := newTestRolloutWriter(t)

	rawBefore := flagBytes(t, path)
	if err := w.SetControllerCount("six"); err != nil {
		t.Fatalf("SetControllerCount: %v", err)
	}
	rawAfter := flagBytes(t, path)

	for _, key := range []string{"target_backend", "strategy", "remediation_allowed"} {
		if string(rawBefore[key]) != string(rawAfter[key]) {
			t.Errorf("flag %q changed:\n before: %s\n  after: %s", key, rawBefore[key], rawAfter[key])
		}
	}
	// And the touched flag's variants map is untouched (only defaultVariant moved).
	flag := readFlag(t, path, flagCurrentControllerCount)
	variants, _ := flag["variants"].(map[string]any)
	for _, name := range []string{"none", "one", "three", "six", "all"} {
		if _, ok := variants[name]; !ok {
			t.Errorf("variant %q lost after flip", name)
		}
	}
}

// flagBytes returns the raw JSON bytes of each flag in the document.
func flagBytes(t *testing.T, path string) map[string]json.RawMessage {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read file: %v", err)
	}
	var doc struct {
		Flags map[string]json.RawMessage `json:"flags"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("unmarshal doc: %v", err)
	}
	return doc.Flags
}

func TestSetControllerCount_UnknownVariantErrors(t *testing.T) {
	w, path := newTestRolloutWriter(t)

	if err := w.SetControllerCount("seven"); err == nil {
		t.Fatal("SetControllerCount(unknown) returned nil error, want error")
	}
	// File must be left untouched on error.
	if got := defaultVariantOf(t, path, flagCurrentControllerCount); got != "none" {
		t.Errorf("defaultVariant changed on failed write: %q", got)
	}
}

func TestNextStage_Ladder(t *testing.T) {
	cases := []struct {
		current     int
		wantVariant string
		wantValue   int
		wantOK      bool
	}{
		{0, "one", 1, true},
		{1, "three", 3, true},
		{3, "six", 6, true},
		{6, "all", 99, true},
		{99, "", 0, false}, // terminal stage
		{2, "", 0, false},  // unknown value: do not advance
		{-1, "", 0, false},
		{100, "", 0, false},
	}
	for _, tc := range cases {
		gotVar, gotVal, gotOK := NextStage(tc.current)
		if gotVar != tc.wantVariant || gotVal != tc.wantValue || gotOK != tc.wantOK {
			t.Errorf("NextStage(%d) = (%q,%d,%v), want (%q,%d,%v)",
				tc.current, gotVar, gotVal, gotOK, tc.wantVariant, tc.wantValue, tc.wantOK)
		}
	}
}

func TestStageVariantForValue(t *testing.T) {
	cases := []struct {
		value  int
		want   string
		wantOK bool
	}{
		{0, "none", true},
		{1, "one", true},
		{3, "three", true},
		{6, "six", true},
		{99, "all", true},
		{2, "", false},
		{-5, "", false},
	}
	for _, tc := range cases {
		got, ok := StageVariantForValue(tc.value)
		if got != tc.want || ok != tc.wantOK {
			t.Errorf("StageVariantForValue(%d) = (%q,%v), want (%q,%v)", tc.value, got, ok, tc.want, tc.wantOK)
		}
	}
}

// TestNextStage_FullLadderWalk walks none→all and confirms it terminates.
func TestNextStage_FullLadderWalk(t *testing.T) {
	current := 0
	wantValues := []int{1, 3, 6, 99}
	for i, want := range wantValues {
		variant, value, ok := NextStage(current)
		if !ok {
			t.Fatalf("step %d: NextStage(%d) ok=false, want advance", i, current)
		}
		if value != want {
			t.Fatalf("step %d: NextStage(%d) value=%d, want %d", i, current, value, want)
		}
		if v, vok := StageVariantForValue(value); !vok || v != variant {
			t.Fatalf("step %d: StageVariantForValue(%d) = (%q,%v), NextStage variant=%q", i, value, v, vok, variant)
		}
		current = value
	}
	if _, _, ok := NextStage(current); ok {
		t.Fatalf("NextStage(%d) ok=true, want terminal", current)
	}
}

// TestSetControllerCount_ConcurrentRace drives concurrent flips to exercise the
// per-writer mutex under -race. Final state must be one of the valid variants.
func TestSetControllerCount_ConcurrentRace(t *testing.T) {
	w, path := newTestRolloutWriter(t)
	variants := []string{"one", "three", "six", "all", "none"}

	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func(n int) {
			defer wg.Done()
			if err := w.SetControllerCount(variants[n%len(variants)]); err != nil {
				t.Errorf("SetControllerCount: %v", err)
			}
		}(i)
	}
	wg.Wait()

	got := defaultVariantOf(t, path, flagCurrentControllerCount)
	valid := map[string]bool{"one": true, "three": true, "six": true, "all": true, "none": true}
	if !valid[got] {
		t.Fatalf("final defaultVariant = %q, want one of the valid variants", got)
	}
}

func TestDryRunRolloutWriter_NeverWrites(t *testing.T) {
	w := NewDryRunRolloutWriter(discardLogger())
	if err := w.SetControllerCount("all"); err != nil {
		t.Fatalf("dry-run SetControllerCount: %v", err)
	}
	// Ladder helpers must match the real writer's.
	if v, _, ok := w.NextStage(0); !ok || v != "one" {
		t.Errorf("dry-run NextStage(0) = (%q,%v)", v, ok)
	}
	if v, ok := w.StageVariantForValue(6); !ok || v != "six" {
		t.Errorf("dry-run StageVariantForValue(6) = (%q,%v)", v, ok)
	}
}
