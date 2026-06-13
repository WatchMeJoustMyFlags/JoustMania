package experiment

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/joustmania/agent/experiment/journal"
)

// --- recording fakes for the four registry seams ---

// fakeSpawner records (experimentID, arm) spawn calls and hands back synthetic
// game ids. It is the ShadowSpawner seam stub the registry drives in tests; the
// real #976 RPC is not built here.
type fakeSpawner struct {
	mu      sync.Mutex
	calls   []spawnCall
	failNow bool // when true, Spawn returns an error (slot must be released)
	seq     int
}

type spawnCall struct {
	experimentID string
	arm          string
	gameID       string
}

func (f *fakeSpawner) Spawn(_ context.Context, experimentID, arm string) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failNow {
		return "", fmt.Errorf("spawn failed (test)")
	}
	f.seq++
	gameID := fmt.Sprintf("game_%012d", f.seq)
	f.calls = append(f.calls, spawnCall{experimentID, arm, gameID})
	return gameID, nil
}

func (f *fakeSpawner) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.calls)
}

// fakeVerdict returns a fixed verdict, optionally only after a minimum total
// sample count is reached (so a test can drive inconclusive→conclude).
type fakeVerdict struct {
	outcome      string
	concludeAtN  int  // total games across arms before returning a conclusive verdict
	alwaysReport bool // if false, returns ok=false until concludeAtN reached
}

func (f fakeVerdict) Evaluate(view journal.CompactView) (journal.Verdict, bool) {
	total := 0
	for _, a := range view.Arms {
		total += a.Count
	}
	if f.concludeAtN > 0 && total < f.concludeAtN {
		if f.alwaysReport {
			return journal.Verdict{Outcome: OutcomeInconclusive, Reason: "under-powered"}, true
		}
		return journal.Verdict{}, false
	}
	return journal.Verdict{Outcome: f.outcome, Significant: f.outcome == CohortOutcomePromote}, true
}

// fakePromoter records Promote calls.
type fakePromoter struct {
	mu     sync.Mutex
	calls  []string // experiment ids (from the view's intent)
	retErr error
}

func (f *fakePromoter) Promote(_ context.Context, view journal.CompactView) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, view.Intent.ExperimentID)
	return f.retErr
}

func (f *fakePromoter) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.calls)
}

// fakeTargeting records Apply/Strip calls (the #977 writer seam).
type fakeTargeting struct {
	mu       sync.Mutex
	applied  []string // experiment ids
	stripped []string // experiment ids
	applyErr error
}

func (f *fakeTargeting) Apply(_ context.Context, p Proposal) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.applyErr != nil {
		return f.applyErr
	}
	f.applied = append(f.applied, p.ExperimentID)
	return nil
}

func (f *fakeTargeting) Strip(_ context.Context, _ string, experimentID string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.stripped = append(f.stripped, experimentID)
	return nil
}

func (f *fakeTargeting) strippedIDs() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.stripped...)
}

// fixedClock returns deterministic, monotonically-increasing timestamps.
func fixedClock() func() time.Time {
	base := time.Date(2026, 6, 13, 10, 0, 0, 0, time.UTC)
	var n int64
	var mu sync.Mutex
	return func() time.Time {
		mu.Lock()
		defer mu.Unlock()
		n++
		return base.Add(time.Duration(n) * time.Second)
	}
}

func newTestRegistry(t *testing.T, cfg RegistryConfig) *Registry {
	t.Helper()
	if cfg.Root == "" {
		cfg.Root = t.TempDir()
	}
	if cfg.Clock == nil {
		cfg.Clock = fixedClock()
	}
	return NewRegistry(cfg)
}

func sampleIntent() Intent {
	return Intent{
		Hypothesis:        "raising grace reduces frustration deaths",
		FlagKey:           "death_grace_period_seconds",
		ExperimentalValue: 0.5,
		Objective:         "engagement_balanced",
		TargetNPerArm:     2,
	}
}

// --- tests ---

// Declare → PROPOSED in the registry + intent persisted in the journal.
func TestDeclarePersistsIntentAndIsProposed(t *testing.T) {
	root := t.TempDir()
	r := newTestRegistry(t, RegistryConfig{Root: root})

	id, err := r.Declare(sampleIntent())
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if id == "" || id[:4] != "exp_" {
		t.Fatalf("experiment id %q is not exp_<hex>", id)
	}

	st, ok := r.Status(id)
	if !ok || st != StatusProposed {
		t.Fatalf("status = (%v,%v), want (proposed,true)", st, ok)
	}

	// Intent persisted in the journal: reloadable from disk.
	jrnl, err := journal.Load(root, id)
	if err != nil {
		t.Fatalf("journal.Load: %v", err)
	}
	in := jrnl.Intent()
	if in.FlagKey != "death_grace_period_seconds" || in.ExperimentID != id {
		t.Fatalf("persisted intent = %+v, want flag/id to match", in)
	}
	if got := in.Arms; len(got) != 2 || got[0] != ArmExperimental || got[1] != ArmControl {
		t.Fatalf("arms = %v, want [experimental control]", got)
	}
}

// Capacity: with a cap of N and K experiments, allocation is equal-share /
// round-robin and never exceeds the cap.
func TestAllocationEqualShareNeverExceedsCap(t *testing.T) {
	const cap = 6
	spawner := &fakeSpawner{}
	r := newTestRegistry(t, RegistryConfig{
		MaxShadowGames: cap,
		Spawner:        spawner,
		Verdict:        fakeVerdict{concludeAtN: 1000}, // never conclude during this test
	})
	ctx := context.Background()

	const k = 3
	ids := make([]string, k)
	for i := range ids {
		id, err := r.Declare(sampleIntent())
		if err != nil {
			t.Fatalf("Declare: %v", err)
		}
		if err := r.Start(ctx, id); err != nil {
			t.Fatalf("Start: %v", err)
		}
		ids[i] = id
	}

	got := r.AllocateAndSpawn(ctx)
	if got != cap {
		t.Fatalf("spawned %d, want cap %d", got, cap)
	}
	if total := r.TotalInFlight(); total != cap {
		t.Fatalf("total in-flight %d, want %d", total, cap)
	}
	if total := r.TotalInFlight(); total > cap {
		t.Fatalf("exceeded cap: %d > %d", total, cap)
	}
	// Equal share: cap 6 over 3 experiments → 2 each.
	for _, id := range ids {
		if n := r.InFlight(id); n != 2 {
			t.Fatalf("experiment %s in-flight %d, want 2 (equal share)", id, n)
		}
	}
	// A second allocate spawns nothing (cap full).
	if again := r.AllocateAndSpawn(ctx); again != 0 {
		t.Fatalf("second allocate spawned %d, want 0 (cap full)", again)
	}
}

// Arm assignment → game_assigned journal event recorded with the right
// (experiment_id, arm); the ShadowSpawner seam is called.
func TestArmAssignmentRecordsJournalEventAndCallsSpawner(t *testing.T) {
	root := t.TempDir()
	spawner := &fakeSpawner{}
	r := newTestRegistry(t, RegistryConfig{
		Root:           root,
		MaxShadowGames: 2,
		Spawner:        spawner,
		Verdict:        fakeVerdict{concludeAtN: 1000},
	})
	ctx := context.Background()

	id, err := r.Declare(sampleIntent())
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := r.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}

	n := r.AllocateAndSpawn(ctx)
	if n != 2 {
		t.Fatalf("spawned %d, want 2", n)
	}
	if spawner.count() != 2 {
		t.Fatalf("spawner called %d times, want 2", spawner.count())
	}

	// Both arms were assigned (round-robin within the experiment).
	arms := map[string]bool{}
	for _, c := range spawner.calls {
		if c.experimentID != id {
			t.Fatalf("spawn for %q, want %q", c.experimentID, id)
		}
		arms[c.arm] = true
	}
	if !arms[ArmExperimental] || !arms[ArmControl] {
		t.Fatalf("arms assigned = %v, want both experimental + control", arms)
	}

	// game_assigned events recorded in the journal with the right (game_id, arm).
	jrnl, err := journal.Load(root, id)
	if err != nil {
		t.Fatalf("journal.Load: %v", err)
	}
	view := jrnl.CompactView()
	assigned := map[string]string{}
	for _, e := range view.RecentTail {
		if e.Kind == journal.KindGameAssigned {
			assigned[e.GameID] = e.Arm
		}
	}
	if len(assigned) != 2 {
		t.Fatalf("game_assigned events = %d, want 2 (tail=%+v)", len(assigned), view.RecentTail)
	}
	for _, c := range spawner.calls {
		if assigned[c.gameID] != c.arm {
			t.Fatalf("journal arm for %s = %q, want %q", c.gameID, assigned[c.gameID], c.arm)
		}
	}
}

// Game conclusion → game_concluded recorded + CohortVerdict seam called; status
// transitions on the (stubbed) verdict.
func TestConcludeGameRecordsAndTransitionsOnVerdict(t *testing.T) {
	root := t.TempDir()
	spawner := &fakeSpawner{}
	promoter := &fakePromoter{}
	targeting := &fakeTargeting{}
	r := newTestRegistry(t, RegistryConfig{
		Root:           root,
		MaxShadowGames: 4,
		Spawner:        spawner,
		// Conclude (promote) once 2 games total have been folded.
		Verdict:   fakeVerdict{outcome: CohortOutcomePromote, concludeAtN: 2, alwaysReport: true},
		Promoter:  promoter,
		Targeting: targeting,
	})
	ctx := context.Background()

	id, err := r.Declare(sampleIntent())
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := r.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}
	n := r.AllocateAndSpawn(ctx)
	if n == 0 {
		t.Fatalf("no games spawned")
	}
	games := append([]spawnCall(nil), spawner.calls...)

	// First conclusion: still inconclusive (1 < 2) → RUNNING.
	st, err := r.ConcludeGame(ctx, games[0].gameID, 0.7, 120)
	if err != nil {
		t.Fatalf("ConcludeGame: %v", err)
	}
	if st != StatusRunning {
		t.Fatalf("after 1 conclusion status = %v, want running", st)
	}

	// Second conclusion: verdict promotes → CONCLUDED → PROMOTING → DONE.
	st, err = r.ConcludeGame(ctx, games[1].gameID, 0.8, 130)
	if err != nil {
		t.Fatalf("ConcludeGame: %v", err)
	}
	if st != StatusDone {
		t.Fatalf("after promote verdict status = %v, want done", st)
	}
	if promoter.count() != 1 {
		t.Fatalf("promoter called %d times, want 1", promoter.count())
	}
	if got := targeting.strippedIDs(); len(got) != 1 || got[0] != id {
		t.Fatalf("stripped = %v, want [%s] (teardown)", got, id)
	}

	// game_concluded events + the verdict are durable in the journal.
	jrnl, err := journal.Load(root, id)
	if err != nil {
		t.Fatalf("journal.Load: %v", err)
	}
	view := jrnl.CompactView()
	concluded := 0
	for _, e := range view.RecentTail {
		if e.Kind == journal.KindGameConcluded {
			concluded++
			if e.Fitness == nil {
				t.Fatalf("game_concluded event missing fitness sample")
			}
		}
	}
	if concluded < 2 {
		t.Fatalf("game_concluded events in tail = %d, want >= 2", concluded)
	}
	// Both arms folded a fitness sample.
	totalSamples := 0
	for _, a := range view.Arms {
		totalSamples += a.Count
	}
	if totalSamples != 2 {
		t.Fatalf("folded samples = %d, want 2", totalSamples)
	}
}

// Rehydrate: drop the in-memory registry, rebuild from the journal → same live
// experiments / status.
func TestRehydrateRebuildsLiveExperiments(t *testing.T) {
	root := t.TempDir()
	clock := fixedClock()
	ctx := context.Background()

	// First registry: declare + start two experiments, conclude none.
	r1 := NewRegistry(RegistryConfig{Root: root, MaxShadowGames: 4, Clock: clock})
	idA, err := r1.Declare(sampleIntent())
	if err != nil {
		t.Fatalf("Declare A: %v", err)
	}
	idB, err := r1.Declare(sampleIntent())
	if err != nil {
		t.Fatalf("Declare B: %v", err)
	}
	if err := r1.Start(ctx, idA); err != nil {
		t.Fatalf("Start A: %v", err)
	}
	// Leave B PROPOSED (never started) to prove status round-trips per-experiment.

	// New registry over the SAME journal root: rehydrate from disk.
	r2 := NewRegistry(RegistryConfig{Root: root, MaxShadowGames: 4, Clock: clock})
	if err := r2.Rehydrate([]string{idA, idB}); err != nil {
		t.Fatalf("Rehydrate: %v", err)
	}

	stA, okA := r2.Status(idA)
	if !okA || stA != StatusRunning {
		t.Fatalf("rehydrated A status = (%v,%v), want (running,true)", stA, okA)
	}
	stB, okB := r2.Status(idB)
	if !okB || stB != StatusProposed {
		t.Fatalf("rehydrated B status = (%v,%v), want (proposed,true)", stB, okB)
	}
	// A is live (running) and spawnable; B is proposed (not yet spawnable).
	live := r2.Live()
	if len(live) != 1 || live[0] != idA {
		t.Fatalf("live = %v, want [%s] (only running A)", live, idA)
	}
	// Intent survived: flag key matches.
	view, ok := r2.CompactView(idA)
	if !ok || view.Intent.FlagKey != "death_grace_period_seconds" {
		t.Fatalf("rehydrated intent = %+v", view.Intent)
	}
}

// Rehydrate is faithful to a concluded experiment: a terminal status is skipped
// (its work is done, capacity freed).
func TestRehydrateSkipsTerminalExperiments(t *testing.T) {
	root := t.TempDir()
	clock := fixedClock()
	ctx := context.Background()

	spawner := &fakeSpawner{}
	r1 := NewRegistry(RegistryConfig{
		Root:           root,
		MaxShadowGames: 2,
		Spawner:        spawner,
		Verdict:        fakeVerdict{outcome: CohortOutcomeDiscard, concludeAtN: 1, alwaysReport: true},
		Targeting:      &fakeTargeting{},
		Clock:          clock,
	})
	id, err := r1.Declare(sampleIntent())
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := r1.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}
	r1.AllocateAndSpawn(ctx)
	// Conclude a game → discard verdict → DISCARDED (terminal).
	st, err := r1.ConcludeGame(ctx, spawner.calls[0].gameID, 0.3, 60)
	if err != nil {
		t.Fatalf("ConcludeGame: %v", err)
	}
	if st != StatusDiscarded {
		t.Fatalf("status = %v, want discarded", st)
	}

	r2 := NewRegistry(RegistryConfig{Root: root, MaxShadowGames: 2, Clock: clock})
	if err := r2.Rehydrate([]string{id}); err != nil {
		t.Fatalf("Rehydrate: %v", err)
	}
	if _, ok := r2.Status(id); ok {
		t.Fatalf("terminal experiment %s should be skipped on rehydrate", id)
	}
}

// Kill-switch → experiments ABORTED, capacity freed, targeting torn down.
func TestKillSwitchAbortsAllAndFreesCapacity(t *testing.T) {
	spawner := &fakeSpawner{}
	targeting := &fakeTargeting{}
	r := newTestRegistry(t, RegistryConfig{
		MaxShadowGames: 4,
		Spawner:        spawner,
		Verdict:        fakeVerdict{concludeAtN: 1000},
		Targeting:      targeting,
	})
	ctx := context.Background()

	var ids []string
	for i := 0; i < 2; i++ {
		id, err := r.Declare(sampleIntent())
		if err != nil {
			t.Fatalf("Declare: %v", err)
		}
		if err := r.Start(ctx, id); err != nil {
			t.Fatalf("Start: %v", err)
		}
		ids = append(ids, id)
	}
	r.AllocateAndSpawn(ctx)
	if r.TotalInFlight() == 0 {
		t.Fatalf("expected in-flight games before abort")
	}

	aborted := r.AbortAll(ctx, "agent disabled (kill-switch)")
	if len(aborted) != 2 {
		t.Fatalf("aborted %d, want 2", len(aborted))
	}
	for _, id := range ids {
		st, _ := r.Status(id)
		if st != StatusAborted {
			t.Fatalf("experiment %s status = %v, want aborted", id, st)
		}
		if n := r.InFlight(id); n != 0 {
			t.Fatalf("experiment %s in-flight %d after abort, want 0 (capacity freed)", id, n)
		}
	}
	if total := r.TotalInFlight(); total != 0 {
		t.Fatalf("total in-flight after abort = %d, want 0", total)
	}
	// Targeting torn down for every aborted experiment.
	stripped := map[string]bool{}
	for _, s := range targeting.strippedIDs() {
		stripped[s] = true
	}
	for _, id := range ids {
		if !stripped[id] {
			t.Fatalf("experiment %s targeting not stripped on abort", id)
		}
	}
	// After freeing capacity a new experiment can claim the freed slots.
	if again := r.AllocateAndSpawn(ctx); again != 0 {
		t.Fatalf("no live experiments remain; allocate should spawn 0, got %d", again)
	}
}

// A failing spawner releases its reserved slot so capacity is not permanently
// shrunk (no slot leak).
func TestSpawnFailureReleasesReservation(t *testing.T) {
	spawner := &fakeSpawner{failNow: true}
	r := newTestRegistry(t, RegistryConfig{
		MaxShadowGames: 2,
		Spawner:        spawner,
		Verdict:        fakeVerdict{concludeAtN: 1000},
	})
	ctx := context.Background()
	id, err := r.Declare(sampleIntent())
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := r.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}
	if n := r.AllocateAndSpawn(ctx); n != 0 {
		t.Fatalf("spawned %d on a failing spawner, want 0", n)
	}
	if total := r.TotalInFlight(); total != 0 {
		t.Fatalf("in-flight %d after failed spawn, want 0 (reservation released)", total)
	}
}

// Start without a TargetingWriter is fine; with one, Apply is called for the
// experiment.
func TestStartWritesTargeting(t *testing.T) {
	targeting := &fakeTargeting{}
	r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 2, Targeting: targeting})
	ctx := context.Background()
	id, err := r.Declare(sampleIntent())
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := r.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}
	targeting.mu.Lock()
	applied := append([]string(nil), targeting.applied...)
	targeting.mu.Unlock()
	if len(applied) != 1 || applied[0] != id {
		t.Fatalf("applied = %v, want [%s]", applied, id)
	}
	st, _ := r.Status(id)
	if st != StatusRunning {
		t.Fatalf("status = %v, want running", st)
	}
}

// A targeting Apply failure keeps the experiment PROPOSED (it never accrues games
// it cannot scope).
func TestStartTargetingFailureStaysProposed(t *testing.T) {
	targeting := &fakeTargeting{applyErr: fmt.Errorf("write failed (test)")}
	r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 2, Targeting: targeting})
	ctx := context.Background()
	id, err := r.Declare(sampleIntent())
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := r.Start(ctx, id); err == nil {
		t.Fatalf("Start should fail when targeting write fails")
	}
	st, _ := r.Status(id)
	if st != StatusProposed {
		t.Fatalf("status = %v, want proposed (write failed)", st)
	}
}

// Uneven cap: cap 5 over 2 experiments splits as ceil(5/2)=3 ceiling per
// experiment, total never exceeds 5, round-robin gives 3+2.
func TestAllocationUnevenCapRoundRobin(t *testing.T) {
	const cap = 5
	spawner := &fakeSpawner{}
	r := newTestRegistry(t, RegistryConfig{
		MaxShadowGames: cap,
		Spawner:        spawner,
		Verdict:        fakeVerdict{concludeAtN: 1000},
	})
	ctx := context.Background()
	var ids []string
	for i := 0; i < 2; i++ {
		id, err := r.Declare(sampleIntent())
		if err != nil {
			t.Fatalf("Declare: %v", err)
		}
		if err := r.Start(ctx, id); err != nil {
			t.Fatalf("Start: %v", err)
		}
		ids = append(ids, id)
	}
	if got := r.AllocateAndSpawn(ctx); got != cap {
		t.Fatalf("spawned %d, want %d", got, cap)
	}
	if total := r.TotalInFlight(); total != cap {
		t.Fatalf("total in-flight %d, want %d (never exceeds cap)", total, cap)
	}
	// 3 + 2 split (each bounded by ceil(5/2)=3; round-robin gives the first a head start).
	a, b := r.InFlight(ids[0]), r.InFlight(ids[1])
	if a+b != cap || a > 3 || b > 3 {
		t.Fatalf("split = %d + %d, want sum %d with each <= 3", a, b, cap)
	}
}

// Status predicates: terminal + live classification.
func TestStatusPredicates(t *testing.T) {
	for _, s := range []Status{StatusDone, StatusDiscarded, StatusAborted} {
		if !s.IsTerminal() {
			t.Fatalf("%s should be terminal", s)
		}
		if s.IsLive() {
			t.Fatalf("%s should not be live", s)
		}
	}
	for _, s := range []Status{StatusProposed, StatusRunning} {
		if s.IsTerminal() {
			t.Fatalf("%s should not be terminal", s)
		}
		if !s.IsLive() {
			t.Fatalf("%s should be live", s)
		}
	}
}

// MaxShadowGamesFromEnv honors the env override and falls back safely.
func TestMaxShadowGamesFromEnv(t *testing.T) {
	t.Setenv(maxShadowGamesEnv, "7")
	if got := MaxShadowGamesFromEnv(); got != 7 {
		t.Fatalf("MaxShadowGamesFromEnv = %d, want 7", got)
	}
	t.Setenv(maxShadowGamesEnv, "0") // non-positive → default
	if got := MaxShadowGamesFromEnv(); got != DefaultMaxShadowGames {
		t.Fatalf("MaxShadowGamesFromEnv(0) = %d, want default %d", got, DefaultMaxShadowGames)
	}
	t.Setenv(maxShadowGamesEnv, "garbage")
	if got := MaxShadowGamesFromEnv(); got != DefaultMaxShadowGames {
		t.Fatalf("MaxShadowGamesFromEnv(garbage) = %d, want default", got)
	}
}
