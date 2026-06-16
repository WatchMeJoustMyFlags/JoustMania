package journal

import (
	"bufio"
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func testIntent() Intent {
	return Intent{
		ExperimentID:      "exp_a1b2c3d4e5f6",
		CreatedAt:         time.Date(2026, 6, 13, 10, 0, 0, 0, time.UTC),
		Hypothesis:        "Raising death_grace_period reduces frustration deaths.",
		FlagKey:           "death_grace_period_seconds",
		ExperimentalValue: 0.5,
		Objective:         "engagement_balanced",
		TargetNPerArm:     20,
		Arms:              []string{"experimental", "control"},
	}
}

func fitnessEvent(arm string, fitness float64, at time.Time) Event {
	f := fitness
	return Event{
		At:      at,
		Kind:    KindGameConcluded,
		GameID:  "game_" + arm,
		Arm:     arm,
		Fitness: &f,
	}
}

func TestCreate_WritesIntentAndInitialSummary(t *testing.T) {
	dir := t.TempDir()
	j, err := Create(dir, testIntent())
	if err != nil {
		t.Fatalf("Create: %v", err)
	}

	expDir := filepath.Join(dir, "exp_a1b2c3d4e5f6")
	for _, name := range []string{intentFile, summaryFile} {
		if _, err := os.Stat(filepath.Join(expDir, name)); err != nil {
			t.Errorf("expected %s on disk: %v", name, err)
		}
	}
	// events.jsonl must NOT exist yet — no events appended.
	if _, err := os.Stat(filepath.Join(expDir, eventsFile)); !os.IsNotExist(err) {
		t.Errorf("events.jsonl should not exist before any append, got err=%v", err)
	}

	sum := j.Summary()
	if sum.Status != statusRunning {
		t.Errorf("initial status = %q, want %q", sum.Status, statusRunning)
	}
	if len(sum.Arms) != 2 || sum.Arms["experimental"].Count != 0 || sum.Arms["control"].Count != 0 {
		t.Errorf("arms not seeded zeroed: %+v", sum.Arms)
	}
}

func TestCreate_RejectsDuplicate(t *testing.T) {
	dir := t.TempDir()
	if _, err := Create(dir, testIntent()); err != nil {
		t.Fatalf("first Create: %v", err)
	}
	if _, err := Create(dir, testIntent()); err == nil {
		t.Fatal("second Create should fail (intent is write-once)")
	}
}

func TestCreate_RequiresExperimentID(t *testing.T) {
	in := testIntent()
	in.ExperimentID = ""
	if _, err := Create(t.TempDir(), in); err == nil {
		t.Fatal("Create with empty ExperimentID should fail")
	}
}

// TestAppendEvent_WelfordFold: per-arm fitness samples fold into the rolling
// summary's Welford accumulator and match a direct computation.
func TestAppendEvent_WelfordFold(t *testing.T) {
	dir := t.TempDir()
	j, err := Create(dir, testIntent())
	if err != nil {
		t.Fatalf("Create: %v", err)
	}

	exp := []float64{0.71, 0.83, 0.69, 0.77}
	ctrl := []float64{0.66, 0.59, 0.61}
	base := testIntent().CreatedAt
	for i, f := range exp {
		if err := j.AppendEvent(fitnessEvent("experimental", f, base.Add(time.Duration(i)*time.Minute))); err != nil {
			t.Fatalf("append exp: %v", err)
		}
	}
	for i, f := range ctrl {
		if err := j.AppendEvent(fitnessEvent("control", f, base.Add(time.Duration(i)*time.Minute))); err != nil {
			t.Fatalf("append ctrl: %v", err)
		}
	}

	sum := j.Summary()
	for arm, samples := range map[string][]float64{"experimental": exp, "control": ctrl} {
		wantN, wantMean, wantVar := directStats(samples)
		got := sum.Arms[arm]
		if got.Count != wantN {
			t.Errorf("%s count = %d, want %d", arm, got.Count, wantN)
		}
		if math.Abs(got.Mean-wantMean) > 1e-9 {
			t.Errorf("%s mean = %v, want %v", arm, got.Mean, wantMean)
		}
		if math.Abs(got.Variance()-wantVar) > 1e-9 {
			t.Errorf("%s variance = %v, want %v", arm, got.Variance(), wantVar)
		}
	}
	if sum.EventCount != len(exp)+len(ctrl) {
		t.Errorf("EventCount = %d, want %d", sum.EventCount, len(exp)+len(ctrl))
	}
}

// TestAppendEvent_AppendOnly: events.jsonl grows by exactly one line per event,
// in order, and earlier lines are never rewritten.
func TestAppendEvent_AppendOnly(t *testing.T) {
	dir := t.TempDir()
	j, err := Create(dir, testIntent())
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	eventsPath := filepath.Join(dir, "exp_a1b2c3d4e5f6", eventsFile)

	var firstLine string
	base := testIntent().CreatedAt
	for i := 1; i <= 5; i++ {
		if err := j.AppendEvent(fitnessEvent("experimental", float64(i)/10, base.Add(time.Duration(i)*time.Minute))); err != nil {
			t.Fatalf("append %d: %v", i, err)
		}
		lines := readLines(t, eventsPath)
		if len(lines) != i {
			t.Fatalf("after %d appends, file has %d lines", i, len(lines))
		}
		// Sequence numbers are monotonic 1..i in file order.
		for k, ln := range lines {
			var e Event
			if err := json.Unmarshal([]byte(ln), &e); err != nil {
				t.Fatalf("line %d not valid JSON: %v", k, err)
			}
			if e.Seq != k+1 {
				t.Errorf("line %d has Seq %d, want %d", k, e.Seq, k+1)
			}
		}
		if i == 1 {
			firstLine = lines[0]
		} else if lines[0] != firstLine {
			t.Errorf("first line was rewritten: %q != %q", lines[0], firstLine)
		}
	}
}

// TestRehydrate_SurvivesRestart: write intent + events, drop the in-memory state,
// Load from disk, and the rebuilt rolling summary equals the pre-drop one.
func TestRehydrate_SurvivesRestart(t *testing.T) {
	dir := t.TempDir()
	j, err := Create(dir, testIntent())
	if err != nil {
		t.Fatalf("Create: %v", err)
	}

	base := testIntent().CreatedAt
	samples := []struct {
		arm string
		f   float64
	}{
		{"experimental", 0.71}, {"control", 0.66}, {"experimental", 0.83},
		{"control", 0.59}, {"experimental", 0.69}, {"control", 0.61},
	}
	for i, s := range samples {
		if err := j.AppendEvent(fitnessEvent(s.arm, s.f, base.Add(time.Duration(i)*time.Minute))); err != nil {
			t.Fatalf("append: %v", err)
		}
	}
	// A verdict + status transition, to prove non-fitness events rehydrate too.
	if err := j.AppendEvent(Event{
		At:      base.Add(time.Hour),
		Kind:    KindInterimVerdict,
		Verdict: &Verdict{Outcome: "inconclusive", Delta: 0.043, Significant: false, Reason: "n_per_arm < target (20)"},
	}); err != nil {
		t.Fatalf("append verdict: %v", err)
	}

	before := j.Summary()

	// Drop the in-memory handle entirely; rebuild only from disk.
	reloaded, err := Load(dir, "exp_a1b2c3d4e5f6")
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	after := reloaded.Summary()

	if !summariesEqual(before, after) {
		t.Errorf("rehydrated summary differs from pre-drop:\nbefore=%+v\nafter =%+v", before, after)
	}
	// Spot-check a derived statistic and the verdict carried across the restart.
	if after.Arms["experimental"].Count != 3 || after.Verdict == nil || after.Verdict.Outcome != "inconclusive" {
		t.Errorf("rehydrated state wrong: arms=%+v verdict=%+v", after.Arms, after.Verdict)
	}
}

// TestSummary_ObjectiveFromIntent: Summary().Objective is a derived view field
// (#992) always sourced from the immutable intent, so it is present on a fresh
// journal AND survives a rehydrate from disk (intent.json is authoritative) — the
// #992 recent-real anchor reads it to key the per-objective baseline.
func TestSummary_ObjectiveFromIntent(t *testing.T) {
	dir := t.TempDir()
	j, err := Create(dir, testIntent())
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	if got := j.Summary().Objective; got != "engagement_balanced" {
		t.Fatalf("fresh Summary().Objective = %q, want %q", got, "engagement_balanced")
	}

	reloaded, err := Load(dir, "exp_a1b2c3d4e5f6")
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got := reloaded.Summary().Objective; got != "engagement_balanced" {
		t.Fatalf("rehydrated Summary().Objective = %q, want %q", got, "engagement_balanced")
	}
}

// TestRehydrate_RecomputesFromLogNotSummaryCache: the event log is the source of
// truth. A corrupted summary.json on disk must NOT influence Load — the rebuilt
// summary comes purely from intent + events.
func TestRehydrate_RecomputesFromLogNotSummaryCache(t *testing.T) {
	dir := t.TempDir()
	j, err := Create(dir, testIntent())
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	base := testIntent().CreatedAt
	if err := j.AppendEvent(fitnessEvent("experimental", 0.9, base)); err != nil {
		t.Fatalf("append: %v", err)
	}
	want := j.Summary()

	// Corrupt summary.json with bogus stats; Load must ignore it.
	summaryPath := filepath.Join(dir, "exp_a1b2c3d4e5f6", summaryFile)
	if err := os.WriteFile(summaryPath, []byte(`{"experiment_id":"exp_a1b2c3d4e5f6","arms":{"experimental":{"count":999,"mean":-5,"m2":12345}}}`), 0o644); err != nil {
		t.Fatalf("corrupt summary: %v", err)
	}

	reloaded, err := Load(dir, "exp_a1b2c3d4e5f6")
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	got := reloaded.Summary()
	if got.Arms["experimental"].Count != want.Arms["experimental"].Count ||
		math.Abs(got.Arms["experimental"].Mean-want.Arms["experimental"].Mean) > 1e-9 {
		t.Errorf("Load trusted the corrupt summary cache: got %+v want %+v",
			got.Arms["experimental"], want.Arms["experimental"])
	}
}

// TestAtomicWrite_NoPartialFiles: after writes, no .tmp leftovers remain and
// summary.json / intent.json are complete, parseable JSON.
func TestAtomicWrite_NoPartialFiles(t *testing.T) {
	dir := t.TempDir()
	j, err := Create(dir, testIntent())
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	if err := j.AppendEvent(fitnessEvent("experimental", 0.5, testIntent().CreatedAt)); err != nil {
		t.Fatalf("append: %v", err)
	}

	expDir := filepath.Join(dir, "exp_a1b2c3d4e5f6")
	entries, err := os.ReadDir(expDir)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".tmp") {
			t.Errorf("temp leftover present: %s", e.Name())
		}
	}
	// Both rewritten files round-trip as JSON (never half-written).
	for _, name := range []string{intentFile, summaryFile} {
		data, err := os.ReadFile(filepath.Join(expDir, name))
		if err != nil {
			t.Fatalf("read %s: %v", name, err)
		}
		var v map[string]any
		if err := json.Unmarshal(data, &v); err != nil {
			t.Errorf("%s is not complete JSON: %v", name, err)
		}
	}
}

// TestCompactView_BoundedInN: the compact view's size stays constant as N grows —
// arms count is fixed and the recent tail is capped, regardless of total events.
func TestCompactView_BoundedInN(t *testing.T) {
	dir := t.TempDir()
	j, err := Create(dir, testIntent(), WithTailSize(5))
	if err != nil {
		t.Fatalf("Create: %v", err)
	}

	base := testIntent().CreatedAt
	sizeAt := func() (int, int) {
		v := j.CompactView()
		return len(v.Arms), len(v.RecentTail)
	}

	for i := 1; i <= 200; i++ {
		arm := "experimental"
		if i%2 == 0 {
			arm = "control"
		}
		if err := j.AppendEvent(fitnessEvent(arm, float64(i%10)/10, base.Add(time.Duration(i)*time.Second))); err != nil {
			t.Fatalf("append %d: %v", i, err)
		}
		if i == 10 || i == 100 || i == 200 {
			arms, tail := sizeAt()
			if arms != 2 {
				t.Errorf("at N=%d, arms=%d, want 2 (O(1))", i, arms)
			}
			if tail != 5 {
				t.Errorf("at N=%d, tail=%d, want capped at 5 (O(1))", i, tail)
			}
		}
	}

	// The full event count is still reported (for context) without carrying the log.
	v := j.CompactView()
	if v.EventCount != 200 {
		t.Errorf("EventCount = %d, want 200", v.EventCount)
	}
	// Tail is oldest-first and holds the LAST 5 events.
	if v.RecentTail[len(v.RecentTail)-1].Seq != 200 {
		t.Errorf("tail does not end at the newest event: last seq %d", v.RecentTail[len(v.RecentTail)-1].Seq)
	}
	if v.RecentTail[0].Seq != 196 {
		t.Errorf("tail does not start at seq 196: got %d", v.RecentTail[0].Seq)
	}
}

func TestCompactView_RehydratedTailIsBounded(t *testing.T) {
	dir := t.TempDir()
	j, err := Create(dir, testIntent(), WithTailSize(3))
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	base := testIntent().CreatedAt
	for i := 1; i <= 50; i++ {
		if err := j.AppendEvent(fitnessEvent("experimental", 0.5, base.Add(time.Duration(i)*time.Second))); err != nil {
			t.Fatalf("append: %v", err)
		}
	}
	reloaded, err := Load(dir, "exp_a1b2c3d4e5f6", WithTailSize(3))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got := len(reloaded.CompactView().RecentTail); got != 3 {
		t.Errorf("rehydrated tail = %d, want capped at 3", got)
	}
}

// TestAppendEvent_UnknownArmLoggedButNotFolded: an event for an arm not in the
// intent is still recorded in the log (complete audit trail) but folds into no
// accumulator.
func TestAppendEvent_UnknownArmLoggedButNotFolded(t *testing.T) {
	dir := t.TempDir()
	j, err := Create(dir, testIntent())
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	if err := j.AppendEvent(fitnessEvent("bogus", 0.9, testIntent().CreatedAt)); err != nil {
		t.Fatalf("append: %v", err)
	}
	sum := j.Summary()
	if _, ok := sum.Arms["bogus"]; ok {
		t.Error("unknown arm should not appear in summary arms")
	}
	if sum.EventCount != 1 {
		t.Errorf("event should still be logged: EventCount=%d", sum.EventCount)
	}
}

// TestAppendEvent_ConcurrentSafe: concurrent appends never reuse a Seq nor lose a
// fold; the final count equals the number of appends. Run with -race.
func TestAppendEvent_ConcurrentSafe(t *testing.T) {
	dir := t.TempDir()
	j, err := Create(dir, testIntent())
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	const n = 50
	var wg sync.WaitGroup
	wg.Add(n)
	for i := 0; i < n; i++ {
		go func() {
			defer wg.Done()
			_ = j.AppendEvent(fitnessEvent("experimental", 1.0, time.Now()))
		}()
	}
	wg.Wait()

	sum := j.Summary()
	if sum.EventCount != n {
		t.Errorf("EventCount = %d, want %d", sum.EventCount, n)
	}
	if sum.Arms["experimental"].Count != n {
		t.Errorf("fold count = %d, want %d", sum.Arms["experimental"].Count, n)
	}
	// Seq numbers in the log are a contiguous 1..n permutation (none lost/dup).
	lines := readLines(t, filepath.Join(dir, "exp_a1b2c3d4e5f6", eventsFile))
	seen := make(map[int]bool, n)
	for _, ln := range lines {
		var e Event
		if err := json.Unmarshal([]byte(ln), &e); err != nil {
			t.Fatalf("bad line: %v", err)
		}
		if seen[e.Seq] {
			t.Errorf("duplicate Seq %d", e.Seq)
		}
		seen[e.Seq] = true
	}
	for s := 1; s <= n; s++ {
		if !seen[s] {
			t.Errorf("missing Seq %d", s)
		}
	}
}

func TestLoad_MissingExperiment(t *testing.T) {
	if _, err := Load(t.TempDir(), "exp_nope"); err == nil {
		t.Fatal("Load of a non-existent experiment should fail")
	}
}

// TestRehydrate_TornTrailingLineDropped: a crash mid-append can leave a partial
// final line in events.jsonl (O_APPEND has no cross-crash atomicity). Load must
// DROP that torn tail and reconverge from the valid prefix — the dropped event was
// never acknowledged to the caller, so the rehydrated summary must equal one built
// from only the valid events. A subsequent AppendEvent must then continue with the
// correct next Seq, and the journal must remain loadable afterwards.
func TestRehydrate_TornTrailingLineDropped(t *testing.T) {
	dir := t.TempDir()
	j, err := Create(dir, testIntent())
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	base := testIntent().CreatedAt
	valid := []struct {
		arm string
		f   float64
	}{
		{"experimental", 0.71}, {"control", 0.66}, {"experimental", 0.83},
	}
	for i, s := range valid {
		if err := j.AppendEvent(fitnessEvent(s.arm, s.f, base.Add(time.Duration(i)*time.Minute))); err != nil {
			t.Fatalf("append: %v", err)
		}
	}
	// Summary built from ONLY the valid events — the expected reconverged state.
	want := j.Summary()

	// Simulate a torn final line: a partial/truncated JSON event with no trailing
	// newline, exactly what a crash mid-write of event N+1 leaves behind.
	eventsPath := filepath.Join(dir, "exp_a1b2c3d4e5f6", eventsFile)
	// No trailing newline — a real torn tail ends mid-write.
	tornLine := `{"seq":4,"kind":"game_concluded","arm":"experimental","fitn`
	appendRawNoNewline(t, eventsPath, tornLine)

	reloaded, err := Load(dir, "exp_a1b2c3d4e5f6")
	if err != nil {
		t.Fatalf("Load must reconverge from a torn tail, got error: %v", err)
	}
	got := reloaded.Summary()
	if !summariesEqual(want, got) {
		t.Errorf("rehydrated summary differs from valid-only:\nwant=%+v\ngot =%+v", want, got)
	}
	if got.EventCount != len(valid) {
		t.Errorf("EventCount = %d, want %d (torn tail must not count)", got.EventCount, len(valid))
	}

	// The torn bytes must be truncated so the file holds only the valid lines.
	if lines := readLines(t, eventsPath); len(lines) != len(valid) {
		t.Errorf("after Load, events.jsonl has %d lines, want %d (torn tail not truncated)", len(lines), len(valid))
	}

	// A subsequent append must continue at the correct next Seq...
	if err := reloaded.AppendEvent(fitnessEvent("control", 0.59, base.Add(time.Hour))); err != nil {
		t.Fatalf("append after recovery: %v", err)
	}
	if got := reloaded.Summary().EventCount; got != len(valid)+1 {
		t.Errorf("EventCount after recovery append = %d, want %d", got, len(valid)+1)
	}
	lastLine := readLines(t, eventsPath)
	var last Event
	if err := json.Unmarshal([]byte(lastLine[len(lastLine)-1]), &last); err != nil {
		t.Fatalf("last line not valid JSON: %v", err)
	}
	if last.Seq != len(valid)+1 {
		t.Errorf("recovered append Seq = %d, want %d", last.Seq, len(valid)+1)
	}

	// ...and the journal must remain loadable (the recovered tail is NOT now an
	// interior torn line that would hard-error the NEXT Load).
	if _, err := Load(dir, "exp_a1b2c3d4e5f6"); err != nil {
		t.Errorf("journal not loadable after recovery + append: %v", err)
	}
}

// TestEvents_ReadOnlyDoesNotTruncateTornTail: the dossier read path (#1183,
// Journal.Events → readEventsReadOnly) drops a torn final line IN-MEMORY but must
// NEVER truncate the file — a read-only consumer opens an independent handle to a
// possibly-live experiment, and truncating could race the live writer and discard a
// just-committed event. The torn bytes must survive so the WRITING handle's next
// Load recovers them.
func TestEvents_ReadOnlyDoesNotTruncateTornTail(t *testing.T) {
	dir := t.TempDir()
	j, err := Create(dir, testIntent())
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	base := testIntent().CreatedAt
	for i := 0; i < 3; i++ {
		if err := j.AppendEvent(fitnessEvent("experimental", 0.7, base.Add(time.Duration(i)*time.Minute))); err != nil {
			t.Fatalf("append: %v", err)
		}
	}

	eventsPath := filepath.Join(dir, "exp_a1b2c3d4e5f6", eventsFile)
	sizeBefore := fileSize(t, eventsPath)
	appendRawNoNewline(t, eventsPath, `{"seq":4,"kind":"game_concluded","fitn`)
	sizeWithTorn := fileSize(t, eventsPath)
	if sizeWithTorn <= sizeBefore {
		t.Fatalf("test setup: torn line did not grow the file (%d <= %d)", sizeWithTorn, sizeBefore)
	}

	// Events() drops the torn tail in-memory (3 valid events) ...
	got, err := j.Events()
	if err != nil {
		t.Fatalf("Events: %v", err)
	}
	if len(got) != 3 {
		t.Errorf("Events returned %d events, want 3 (torn tail dropped in-memory)", len(got))
	}
	// ... but must NOT have touched the file — the torn bytes are still on disk.
	if after := fileSize(t, eventsPath); after != sizeWithTorn {
		t.Errorf("Events truncated the file (%d → %d); the read path must never write", sizeWithTorn, after)
	}

	// The writing path (Load) still recovers the torn tail, proving Events left the
	// recovery to the owning handle.
	if _, err := Load(dir, "exp_a1b2c3d4e5f6"); err != nil {
		t.Fatalf("Load after read-only Events: %v", err)
	}
	if after := fileSize(t, eventsPath); after != sizeBefore {
		t.Errorf("Load did not truncate the torn tail (%d != %d)", after, sizeBefore)
	}
}

// TestRehydrate_InteriorCorruptionHardErrors: a malformed line that is NOT the last
// line cannot be a torn tail (a later event was durably committed after it). That
// signals a genuine lost/corrupted write, so Load must HARD-ERROR rather than
// silently skip it.
func TestRehydrate_InteriorCorruptionHardErrors(t *testing.T) {
	dir := t.TempDir()
	j, err := Create(dir, testIntent())
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	base := testIntent().CreatedAt
	if err := j.AppendEvent(fitnessEvent("experimental", 0.71, base)); err != nil {
		t.Fatalf("append: %v", err)
	}

	// Corrupt an INTERIOR line: a garbage line followed by a valid trailing line.
	eventsPath := filepath.Join(dir, "exp_a1b2c3d4e5f6", eventsFile)
	appendRaw(t, eventsPath, "{not valid json") // interior (a valid line follows)
	good := fitnessEvent("control", 0.66, base.Add(time.Minute))
	good.Seq = 3
	line, err := json.Marshal(good)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	appendRaw(t, eventsPath, string(line))

	if _, err := Load(dir, "exp_a1b2c3d4e5f6"); err == nil {
		t.Fatal("Load must hard-error on an interior malformed line, got nil")
	}
}

func TestDirFromEnv(t *testing.T) {
	t.Setenv(experimentDirEnv, "")
	if DirFromEnv() != DefaultDir {
		t.Errorf("empty env should yield DefaultDir, got %q", DirFromEnv())
	}
	t.Setenv(experimentDirEnv, "/custom/experiments")
	if DirFromEnv() != "/custom/experiments" {
		t.Errorf("env override not honored, got %q", DirFromEnv())
	}
}

// --- test helpers ---

// appendRaw appends a raw line (plus newline) directly to an events log, bypassing
// the journal — used to inject torn or corrupt lines a crash would leave behind.
func appendRaw(t *testing.T, path, line string) {
	t.Helper()
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatalf("open %s for raw append: %v", path, err)
	}
	defer func() { _ = f.Close() }()
	if _, err := f.WriteString(line + "\n"); err != nil {
		t.Fatalf("raw append to %s: %v", path, err)
	}
}

// appendRawNoNewline appends a raw line WITHOUT a trailing newline — a faithful
// torn tail left mid-write by a crash.
func appendRawNoNewline(t *testing.T, path, line string) {
	t.Helper()
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatalf("open %s for raw append: %v", path, err)
	}
	defer func() { _ = f.Close() }()
	if _, err := f.WriteString(line); err != nil {
		t.Fatalf("raw append to %s: %v", path, err)
	}
}

func readLines(t *testing.T, path string) []string {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatalf("open %s: %v", path, err)
	}
	defer func() { _ = f.Close() }()
	var lines []string
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if strings.TrimSpace(sc.Text()) != "" {
			lines = append(lines, sc.Text())
		}
	}
	if err := sc.Err(); err != nil {
		t.Fatalf("scan %s: %v", path, err)
	}
	return lines
}

// fileSize returns the byte size of a file, failing the test if it cannot be stat'd.
func fileSize(t *testing.T, path string) int64 {
	t.Helper()
	fi, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat %s: %v", path, err)
	}
	return fi.Size()
}

// summariesEqual compares two rolling summaries for the fields a rehydrate must
// preserve: status, event count, per-arm Welford state, and verdict.
func summariesEqual(a, b Summary) bool {
	if a.Status != b.Status || a.EventCount != b.EventCount || len(a.Arms) != len(b.Arms) {
		return false
	}
	for name, sa := range a.Arms {
		sb, ok := b.Arms[name]
		if !ok || sa.Count != sb.Count ||
			math.Abs(sa.Mean-sb.Mean) > 1e-12 || math.Abs(sa.M2-sb.M2) > 1e-12 {
			return false
		}
	}
	switch {
	case a.Verdict == nil && b.Verdict == nil:
		return true
	case a.Verdict == nil || b.Verdict == nil:
		return false
	default:
		return *a.Verdict == *b.Verdict
	}
}
