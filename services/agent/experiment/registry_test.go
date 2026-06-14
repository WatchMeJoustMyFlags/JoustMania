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
	mu           sync.Mutex
	calls        []spawnCall
	failNow      bool // when true, Spawn returns a genuine error (slot must be released)
	backpressNow bool // when true, Spawn returns ErrSpawnBackpressure (#998 backoff)
	attempts     int  // total Spawn calls (including backpressure/failed ones)
	seq          int
}

type spawnCall struct {
	experimentID string
	arm          string
	gameID       string
}

func (f *fakeSpawner) Spawn(_ context.Context, experimentID, arm string) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.attempts++
	if f.backpressNow {
		// Mimic the real shadowSpawner wrapping the coordinator's single-game cap
		// rejection: the error wraps ErrSpawnBackpressure (errors.Is matches).
		return "", fmt.Errorf("shadow spawn for %s/%s: %w: coordinator rejected start", experimentID, arm, ErrSpawnBackpressure)
	}
	if f.failNow {
		return "", fmt.Errorf("spawn failed (test)")
	}
	f.seq++
	gameID := fmt.Sprintf("game_%012d", f.seq)
	f.calls = append(f.calls, spawnCall{experimentID, arm, gameID})
	return gameID, nil
}

func (f *fakeSpawner) attemptCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.attempts
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

func (f fakeVerdict) Evaluate(s journal.Summary) (journal.Verdict, bool) {
	total := 0
	for _, a := range s.Arms {
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
	// The fairness / capacity tests predate the #998 effective-concurrency cap and
	// assert the registry fills up to MaxShadowGames in a single tick. Default the
	// effective cap to MaxShadowGames here so those tests keep exercising the
	// capacity-bookkeeping / round-robin logic. Tests for the effective cap + the
	// backpressure path set EffectiveConcurrency explicitly.
	if cfg.EffectiveConcurrency == 0 && cfg.MaxShadowGames > 0 {
		cfg.EffectiveConcurrency = cfg.MaxShadowGames
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

// Concurrency: many goroutines hammering AllocateAndSpawn (optionally racing
// ConcludeGame) must NEVER exceed the cap, must invoke the spawner exactly cap
// times when no slot is freed, and must leak no reservation. The cap is race-safe
// by design (the slot is reserved under r.mu in nextAllocation before the
// unlocked Spawn); this test locks that invariant in so a future change cannot
// silently break it. Run under -race.
func TestConcurrentAllocateNeverExceedsCap(t *testing.T) {
	const cap = 8
	spawner := &fakeSpawner{}
	r := newTestRegistry(t, RegistryConfig{
		MaxShadowGames: cap,
		Spawner:        spawner,
		Verdict:        fakeVerdict{concludeAtN: 1_000_000}, // never conclude
	})
	ctx := context.Background()

	// A handful of live experiments to share the cap across.
	const k = 4
	for i := 0; i < k; i++ {
		id, err := r.Declare(sampleIntent())
		if err != nil {
			t.Fatalf("Declare: %v", err)
		}
		if err := r.Start(ctx, id); err != nil {
			t.Fatalf("Start: %v", err)
		}
	}

	// N goroutines each call AllocateAndSpawn repeatedly. While they run, a
	// watchdog goroutine continuously asserts the cap is never observed exceeded.
	const goroutines = 16
	const callsPer = 50
	var maxObserved int64
	stop := make(chan struct{})
	var wgWatch sync.WaitGroup
	wgWatch.Add(1)
	go func() {
		defer wgWatch.Done()
		for {
			select {
			case <-stop:
				return
			default:
				if n := int64(r.TotalInFlight()); n > maxObserved {
					maxObserved = n
				}
			}
		}
	}()

	var wg sync.WaitGroup
	wg.Add(goroutines)
	for g := 0; g < goroutines; g++ {
		go func() {
			defer wg.Done()
			for c := 0; c < callsPer; c++ {
				r.AllocateAndSpawn(ctx)
			}
		}()
	}
	wg.Wait()
	close(stop)
	wgWatch.Wait()

	// Invariant 1: the cap was never exceeded (live observation by the watchdog).
	if maxObserved > cap {
		t.Fatalf("observed in-flight %d exceeded cap %d during concurrent allocate", maxObserved, cap)
	}
	// Invariant 2: settled exactly at the cap (no slot was freed, so it fills full).
	if total := r.TotalInFlight(); total != cap {
		t.Fatalf("settled total in-flight %d, want exactly cap %d", total, cap)
	}
	// Invariant 3: the spawner was invoked EXACTLY cap times — no over-spawn from a
	// lost reservation, no under-spawn from a leaked one.
	if got := spawner.count(); got != cap {
		t.Fatalf("spawner invoked %d times, want exactly cap %d (no over/under-spawn)", got, cap)
	}
}

// Concurrency: AllocateAndSpawn racing ConcludeGame frees and refills slots
// without ever exceeding the cap and without leaking reservations. After the
// dust settles, in-flight + concluded accounts for every spawn (no orphaned
// reservation placeholder lingering against the cap). Run under -race.
func TestConcurrentAllocateAndConcludeNoLeak(t *testing.T) {
	const cap = 8
	spawner := &fakeSpawner{}
	r := newTestRegistry(t, RegistryConfig{
		MaxShadowGames: cap,
		Spawner:        spawner,
		Verdict:        fakeVerdict{concludeAtN: 1_000_000}, // never conclude the experiment itself
	})
	ctx := context.Background()

	const k = 4
	for i := 0; i < k; i++ {
		id, err := r.Declare(sampleIntent())
		if err != nil {
			t.Fatalf("Declare: %v", err)
		}
		if err := r.Start(ctx, id); err != nil {
			t.Fatalf("Start: %v", err)
		}
	}

	var maxObserved int64
	stop := make(chan struct{})
	var wgWatch sync.WaitGroup
	wgWatch.Add(1)
	go func() {
		defer wgWatch.Done()
		for {
			select {
			case <-stop:
				return
			default:
				if n := int64(r.TotalInFlight()); n > maxObserved {
					maxObserved = n
				}
			}
		}
	}()

	// Allocators keep filling capacity.
	const allocators = 12
	var wg sync.WaitGroup
	wg.Add(allocators)
	for g := 0; g < allocators; g++ {
		go func() {
			defer wg.Done()
			for c := 0; c < 100; c++ {
				r.AllocateAndSpawn(ctx)
			}
		}()
	}

	// Concluders drain bound games as they appear, freeing slots to refill.
	const concluders = 6
	wg.Add(concluders)
	for g := 0; g < concluders; g++ {
		go func() {
			defer wg.Done()
			for c := 0; c < 100; c++ {
				// Grab a snapshot of bound (real) game ids and conclude one.
				spawner.mu.Lock()
				var gid string
				if len(spawner.calls) > 0 {
					gid = spawner.calls[len(spawner.calls)-1].gameID
				}
				spawner.mu.Unlock()
				if gid == "" {
					continue
				}
				// A double-conclude of the same gid is a harmless no-op (unknown id).
				if _, err := r.ConcludeGame(ctx, gid, 0.5, 60); err != nil {
					t.Errorf("ConcludeGame: %v", err)
					return
				}
			}
		}()
	}

	wg.Wait()
	close(stop)
	wgWatch.Wait()

	// The cap was never exceeded despite the alloc/conclude race.
	if maxObserved > cap {
		t.Fatalf("observed in-flight %d exceeded cap %d under concurrent alloc/conclude", maxObserved, cap)
	}
	// No reservation leak: a final settling allocate refills to exactly the cap.
	// (A leaked reservation would permanently shrink capacity below cap.)
	r.AllocateAndSpawn(ctx)
	if total := r.TotalInFlight(); total != cap {
		t.Fatalf("after settling allocate, in-flight %d != cap %d (reservation leak?)", total, cap)
	}
}

// --- #1001 termination guard tests ---

// alwaysInconclusive is a CohortVerdict that NEVER concludes: it always returns
// an inconclusive verdict (as the real verdict does for all-zero / within-noise /
// degenerate cohorts). It isolates the registry's termination guard from the
// verdict so a test proves the registry itself bounds the experiment.
type alwaysInconclusive struct{}

func (alwaysInconclusive) Evaluate(_ journal.Summary) (journal.Verdict, bool) {
	return journal.Verdict{Outcome: OutcomeInconclusive, Reason: "always inconclusive (test)"}, true
}

// drainToTerminal repeatedly refills shadow capacity and concludes every spawned
// game with the same fitness, until the experiment reaches a terminal status or
// safetyMax games have been concluded (a safety net so a REGRESSED guard fails
// the test instead of looping forever). It returns the terminal status and the
// number of games concluded. The spawner is the source of truth for the spawned
// game ids (robust regardless of journal tail size).
func drainToTerminal(t *testing.T, r *Registry, spawner *fakeSpawner, id string, fitness float64, safetyMax int) (Status, int) {
	t.Helper()
	ctx := context.Background()
	done := map[string]bool{}
	concluded := 0
	for concluded < safetyMax {
		r.AllocateAndSpawn(ctx)

		// Conclude every spawned-but-not-yet-concluded game for this experiment.
		spawner.mu.Lock()
		pending := make([]string, 0, len(spawner.calls))
		for _, c := range spawner.calls {
			if c.experimentID == id && !done[c.gameID] {
				pending = append(pending, c.gameID)
			}
		}
		spawner.mu.Unlock()

		if len(pending) == 0 {
			st, _ := r.Status(id)
			t.Fatalf("no games to conclude but experiment still %s (stuck)", st)
		}

		for _, gid := range pending {
			st, err := r.ConcludeGame(ctx, gid, fitness, 60)
			if err != nil {
				t.Fatalf("ConcludeGame(%s): %v", gid, err)
			}
			done[gid] = true
			concluded++
			if st.IsTerminal() {
				return st, concluded
			}
		}
	}
	st, _ := r.Status(id)
	return st, concluded
}

// (a) GAMES BUDGET: with a verdict that never concludes, the experiment MUST stop
// at the configured max-games budget instead of spawning forever. This is the
// direct #1001 regression: the dry run hit 235 games with no bound.
func TestTerminationGamesBudget(t *testing.T) {
	root := t.TempDir()
	spawner := &fakeSpawner{}
	targeting := &fakeTargeting{}
	r := newTestRegistry(t, RegistryConfig{
		Root:                  root,
		MaxShadowGames:        2,
		MaxGamesPerExperiment: 6,   // small budget for the test
		VerdictMinN:           100, // huge so the inconclusive-past-target guard never fires
		Spawner:               spawner,
		Verdict:               alwaysInconclusive{},
		Targeting:             targeting,
	})
	ctx := context.Background()

	id, err := r.Declare(sampleIntent())
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := r.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}

	st, concluded := drainToTerminal(t, r, spawner, id, 0.5, 50)
	if !st.IsTerminal() {
		t.Fatalf("experiment did not terminate (status=%s, concluded=%d)", st, concluded)
	}
	if st != StatusDiscarded {
		t.Fatalf("terminal status = %s, want discarded (inconclusive teardown)", st)
	}
	// It must have stopped near the budget, not run unbounded.
	if concluded > 6+2 { // budget + one in-flight capacity pass of slack
		t.Fatalf("concluded %d games, want ~6 (budget); guard did not bound the experiment", concluded)
	}
	if got := targeting.strippedIDs(); len(got) != 1 || got[0] != id {
		t.Fatalf("stripped = %v, want [%s] (teardown freed targeting)", got, id)
	}
}

// (c) DEGENERATE VARIANCE / all-zero fitness: using the REAL verdict, all games
// score fitness=0 ⇒ both arms zero-variance ⇒ undefined effect ⇒ permanent
// inconclusive. The experiment MUST terminate via the degenerate-variance guard
// once both arms pass target N — not loop forever (the exact #997/#1001 case).
func TestTerminationAllZeroFitnessDegenerate(t *testing.T) {
	root := t.TempDir()
	spawner := &fakeSpawner{}
	targeting := &fakeTargeting{}
	r := newTestRegistry(t, RegistryConfig{
		Root:                  root,
		MaxShadowGames:        2,
		MaxGamesPerExperiment: 1000, // do NOT let the budget be what stops it
		VerdictMinN:           3,
		Spawner:               spawner,
		Verdict:               NewVerdict(3, 0.5, nil), // the real min-N + Cohen's d gate
		Targeting:             targeting,
	})
	ctx := context.Background()

	in := sampleIntent()
	in.TargetNPerArm = 3
	id, err := r.Declare(in)
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := r.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}

	st, concluded := drainToTerminal(t, r, spawner, id, 0.0, 100)
	if !st.IsTerminal() {
		t.Fatalf("all-zero experiment did not terminate (status=%s, concluded=%d)", st, concluded)
	}
	if st != StatusDiscarded {
		t.Fatalf("terminal status = %s, want discarded", st)
	}
	// Should stop shortly after both arms reach target N=3 (≈6 games), nowhere near
	// the 1000 budget.
	if concluded > 12 {
		t.Fatalf("concluded %d games, want ~6 (degenerate guard at target N)", concluded)
	}
}

// (target_n < min-N) must NOT cause an infinite under-powered loop. Declare
// reconciles target_n up to min-N, and the guard then concludes once both arms
// reach that floor. Uses the real verdict with constant non-zero fitness (still
// inconclusive because zero within-arm variance).
func TestTerminationTargetBelowMinNDoesNotLoop(t *testing.T) {
	root := t.TempDir()
	spawner := &fakeSpawner{}
	targeting := &fakeTargeting{}
	r := newTestRegistry(t, RegistryConfig{
		Root:                  root,
		MaxShadowGames:        2,
		MaxGamesPerExperiment: 1000, // budget must not be the stopper
		VerdictMinN:           8,    // min-N well above target_n
		Spawner:               spawner,
		Verdict:               NewVerdict(8, 0.5, nil),
		Targeting:             targeting,
	})
	ctx := context.Background()

	in := sampleIntent()
	in.TargetNPerArm = 2 // < min-N=8: would loop forever without reconciliation
	id, err := r.Declare(in)
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}

	// Declare must have reconciled the persisted target_n up to min-N.
	jrnl, err := journal.Load(root, id)
	if err != nil {
		t.Fatalf("journal.Load: %v", err)
	}
	if got := jrnl.Intent().TargetNPerArm; got != 8 {
		t.Fatalf("persisted target_n = %d, want 8 (reconciled up to min-N)", got)
	}

	if err := r.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}

	st, concluded := drainToTerminal(t, r, spawner, id, 0.42, 200)
	if !st.IsTerminal() {
		t.Fatalf("target_n<min-N experiment did not terminate (status=%s, concluded=%d)", st, concluded)
	}
	// Terminates around both arms reaching N=8 (≈16 games), not unbounded.
	if concluded > 24 {
		t.Fatalf("concluded %d games, want ~16 (reconciled target N=8)", concluded)
	}
}

// Config/env semantics: MaxGamesPerExperimentFromEnv honors an explicit disable,
// and NewRegistry's zero/negative handling matches the documented rule.
func TestMaxGamesPerExperimentConfig(t *testing.T) {
	t.Setenv(maxGamesPerExperimentEnv, "")
	if got := MaxGamesPerExperimentFromEnv(); got != DefaultMaxGamesPerExperiment {
		t.Fatalf("unset env = %d, want default %d", got, DefaultMaxGamesPerExperiment)
	}
	t.Setenv(maxGamesPerExperimentEnv, "0")
	if got := MaxGamesPerExperimentFromEnv(); got != 0 {
		t.Fatalf("env=0 = %d, want 0 (disabled)", got)
	}
	t.Setenv(maxGamesPerExperimentEnv, "garbage")
	if got := MaxGamesPerExperimentFromEnv(); got != DefaultMaxGamesPerExperiment {
		t.Fatalf("env=garbage = %d, want default", got)
	}

	// A negative config value disables the budget (registry maxGames clamps to 0).
	r := NewRegistry(RegistryConfig{Root: t.TempDir(), MaxGamesPerExperiment: -1})
	if r.maxGames != 0 {
		t.Fatalf("negative config maxGames = %d, want 0 (disabled)", r.maxGames)
	}
}

// fastTerminalSpawner reproduces the bind-vs-fast-terminal race (#1014): inside
// Spawn — i.e. BEFORE Spawn returns and the registry can call bindGame — it fires
// the terminal callback by calling ReleaseGame(gameID) on the registry, exactly as
// a game that errors at admission would (its drive goroutine fires onTerminal →
// ReleaseGame the instant it starts). The real gamerunner spawns its drive
// goroutine inside StartExperimentGame before returning the id, so this ordering is
// real, not contrived; the default fakeSpawner binds synchronously and never
// exercises it.
type fastTerminalSpawner struct {
	mu  sync.Mutex
	reg *Registry // set after construction so Spawn can call back into the registry
	seq int
}

func (f *fastTerminalSpawner) Spawn(_ context.Context, _, _ string) (string, error) {
	f.mu.Lock()
	f.seq++
	seq := f.seq
	gameID := fmt.Sprintf("game_%012d", seq)
	reg := f.reg
	f.mu.Unlock()
	// Only the FIRST game races a pre-bind terminal; otherwise the always-release
	// would make AllocateAndSpawn refill-and-release in a tight loop (the slot keeps
	// freeing). One pre-bind terminal is enough to exercise the race; subsequent
	// spawns bind normally so the pass terminates.
	if seq == 1 {
		// Terminal callback fires BEFORE Spawn returns ⇒ before AllocateAndSpawn binds.
		reg.ReleaseGame(gameID, "game_error at admission (test)")
	}
	return gameID, nil
}

// TestBindVsFastTerminalDoesNotLeakSlot is the #1014 regression: a game that
// terminates (error) BEFORE the registry binds its game_id must NOT leak the
// in-flight slot. Before the fix, ReleaseGame no-op'd on the not-yet-bound id and
// bindGame then recorded the id permanently, pinning the slot forever and
// deadlocking the effective_concurrency=1 loop. The fix tombstones the early
// release and frees the reservation at bind time, so a SUBSEQUENT AllocateAndSpawn
// succeeds.
func TestBindVsFastTerminalDoesNotLeakSlot(t *testing.T) {
	spawner := &fastTerminalSpawner{}
	r := newTestRegistry(t, RegistryConfig{
		MaxShadowGames:       1,
		EffectiveConcurrency: 1, // the exact deadlock-prone shape (#1014 target case)
		Spawner:              spawner,
		Verdict:              fakeVerdict{concludeAtN: 1000},
	})
	spawner.mu.Lock()
	spawner.reg = r
	spawner.mu.Unlock()

	ctx := context.Background()
	id, err := r.Declare(sampleIntent())
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := r.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}

	// One pass: the FIRST spawned game terminates before bind, freeing its slot; the
	// pass then refills it with a healthy game (seq>=2 binds normally). Before the
	// fix the first game's id was bound permanently — TotalInFlight would have
	// settled at 1 holding a DEAD game, and no further game could ever start. With
	// the fix the lone slot holds the healthy second game, proving the dead game's
	// slot was reclaimed.
	r.AllocateAndSpawn(ctx)
	if total := r.TotalInFlight(); total != 1 {
		t.Fatalf("in-flight %d, want 1 (the dead game's slot reclaimed + refilled)", total)
	}

	// The slot must hold the SECOND (healthy) game, not the first (released) one.
	r.mu.Lock()
	_, _, e := r.findGameLocked("game_000000000001")
	firstStillBound := e != nil
	_, _, e2 := r.findGameLocked("game_000000000002")
	secondBound := e2 != nil
	leftoverTombstones := len(r.releaseTombstones)
	r.mu.Unlock()
	if firstStillBound {
		t.Fatalf("released game_000000000001 is still bound — the #1014 slot leak")
	}
	if !secondBound {
		t.Fatalf("healthy game_000000000002 not bound — the freed slot was not reused")
	}
	// No tombstone may linger after bind consumed it (bounded-map invariant).
	if leftoverTombstones != 0 {
		t.Fatalf("releaseTombstones has %d leftover entries, want 0", leftoverTombstones)
	}

	// Conclude the healthy game and confirm the slot frees for a brand-new spawn —
	// end-to-end proof the loop is not deadlocked.
	if _, err := r.ConcludeGame(ctx, "game_000000000002", 0.5, 10); err != nil {
		t.Fatalf("ConcludeGame: %v", err)
	}
	if total := r.TotalInFlight(); total != 0 {
		t.Fatalf("in-flight %d after concluding the healthy game, want 0", total)
	}
	if n := r.AllocateAndSpawn(ctx); n != 1 {
		t.Fatalf("post-conclude spawned %d, want 1 (loop not deadlocked)", n)
	}
}

// TestBindAfterTeardownDropsTombstone covers the bindGame e==nil branch: if the
// experiment is torn down between the early release and the bind, bindGame must not
// resurrect a tombstone (it has no entry to free) — the map must end empty.
func TestBindAfterTeardownDropsTombstone(t *testing.T) {
	r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 1, EffectiveConcurrency: 1})
	// Seed a tombstone for an id whose experiment does not exist, then bind it.
	r.mu.Lock()
	r.releaseTombstones["game_zzz"] = "stale (test)"
	r.mu.Unlock()
	if bound := r.bindGame("exp_missing", "game_zzz", ArmControl); bound {
		t.Fatalf("bindGame on a missing experiment returned bound=true, want false")
	}
	r.mu.Lock()
	leftover := len(r.releaseTombstones)
	r.mu.Unlock()
	if leftover != 0 {
		t.Fatalf("tombstone not dropped on missing-experiment bind: %d left", leftover)
	}
}

// tombstoneCount returns the registry's current tombstone-map size (test-only).
func tombstoneCount(r *Registry) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.releaseTombstones)
}

// TestConcludeThenReleaseLeavesNoOrphanTombstone is the bounded-map regression for
// the conclude-then-release ordering: ReleaseGame is documented as safe to call
// alongside ConcludeGame. If telemetry concludes a (bound) game first and the drive
// goroutine's terminal callback then calls ReleaseGame on the same id, the unfound
// release must NOT leave an un-consumable tombstone — because no spawn is in flight,
// it is provably a stale call and must be skipped. Before the spawnsInFlight gate
// this leaked one map entry per occurrence forever.
func TestConcludeThenReleaseLeavesNoOrphanTombstone(t *testing.T) {
	spawner := &fakeSpawner{}
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
	if n := r.AllocateAndSpawn(ctx); n != 2 {
		t.Fatalf("spawned %d, want 2", n)
	}
	gid := spawner.calls[0].gameID

	// Telemetry concludes the bound game first.
	if _, err := r.ConcludeGame(ctx, gid, 0.5, 30); err != nil {
		t.Fatalf("ConcludeGame: %v", err)
	}
	// The drive goroutine's terminal callback now races in on the SAME id (no spawn
	// is in flight — both spawns already bound). It must be a no-op with no tombstone.
	if freed := r.ReleaseGame(gid, "late terminal callback"); freed {
		t.Fatalf("ReleaseGame freed an already-concluded game, want false")
	}
	if n := tombstoneCount(r); n != 0 {
		t.Fatalf("orphan tombstone leaked on conclude-then-release: %d, want 0", n)
	}
}

// TestDoubleReleaseLeavesNoOrphanTombstone is the bounded-map regression for the
// double-release ordering: the first ReleaseGame frees the bound slot; a second
// ReleaseGame on the same id (no spawn in flight) must be an idempotent no-op that
// leaves no tombstone.
func TestDoubleReleaseLeavesNoOrphanTombstone(t *testing.T) {
	spawner := &fakeSpawner{}
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
	if n := r.AllocateAndSpawn(ctx); n != 2 {
		t.Fatalf("spawned %d, want 2", n)
	}
	gid := spawner.calls[0].gameID

	if freed := r.ReleaseGame(gid, "error"); !freed {
		t.Fatalf("first ReleaseGame should free the bound slot")
	}
	if freed := r.ReleaseGame(gid, "error again"); freed {
		t.Fatalf("second ReleaseGame on the same id should be a no-op")
	}
	if n := tombstoneCount(r); n != 0 {
		t.Fatalf("orphan tombstone leaked on double-release: %d, want 0", n)
	}
}

// TestQuiescentRegistryHasNoTombstones is the bounded-map invariant: after a full
// spawn/bind/conclude cycle with no spawn in flight, the tombstone map is empty (the
// map is O(concurrent in-flight spawns) and quiescent-empty, never accumulating).
func TestQuiescentRegistryHasNoTombstones(t *testing.T) {
	spawner := &fakeSpawner{}
	r := newTestRegistry(t, RegistryConfig{
		MaxShadowGames: 2,
		Spawner:        spawner,
		Verdict:        fakeVerdict{concludeAtN: 1000},
	})
	ctx := context.Background()
	id, _ := r.Declare(sampleIntent())
	if err := r.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}
	r.AllocateAndSpawn(ctx)
	for _, c := range spawner.calls {
		if _, err := r.ConcludeGame(ctx, c.gameID, 0.5, 30); err != nil {
			t.Fatalf("ConcludeGame: %v", err)
		}
	}
	r.mu.Lock()
	inFlightSpawns := r.spawnsInFlight
	tombs := len(r.releaseTombstones)
	r.mu.Unlock()
	if inFlightSpawns != 0 {
		t.Fatalf("spawnsInFlight = %d after a full cycle, want 0", inFlightSpawns)
	}
	if tombs != 0 {
		t.Fatalf("releaseTombstones = %d on a quiescent registry, want 0", tombs)
	}
}
