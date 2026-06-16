package main

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel/trace"
	"go.uber.org/goleak"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/experiment/journal"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gamerunner"
	"github.com/joustmania/agent/promote"
)

// experiment_loop_test.go is the #991 acceptance suite for the experiment cohort
// loop FINAL ASSEMBLY:
//   - end-to-end: declare → spawn into arms → conclude → verdict → (gated) promotion,
//     asserting the real-default write happens ONLY with the env ON (+ kill-switch off);
//   - disabled-by-default: the opt-in OFF ⇒ no Registry, no goroutines, no surface;
//   - leak-safe shutdown: the loop's goroutine terminates on ctx cancel (goleak).

func testLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// recordingSpawner is a deterministic ShadowSpawner fake: it returns synthetic
// game_ids and records the (experiment_id, arm) bindings so a test can replay them
// as conclusions. The REAL spawner (shadow_spawner.go) is exercised by the
// gamerunner tests; this isolates the loop's lifecycle from the gRPC stack.
type recordingSpawner struct {
	mu    sync.Mutex
	calls []spawnRec
	seq   int
}

type spawnRec struct {
	experimentID, arm, gameID string
	seed                      uint64
}

func (r *recordingSpawner) Spawn(_ context.Context, experimentID, arm string, seed uint64) (string, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.seq++
	id := "game_" + experimentID + "_" + arm + "_" + itoa(r.seq)
	r.calls = append(r.calls, spawnRec{experimentID, arm, id, seed})
	return id, nil
}

func (r *recordingSpawner) drain() []spawnRec {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := append([]spawnRec(nil), r.calls...)
	r.calls = nil
	return out
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b []byte
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	return string(b)
}

// recordingRealDefault records SetRealDefault calls — the SINGLE real-player-
// affecting write. The whole point of the safety assertion is that this is NEVER
// called with the real-default env OFF, and IS called only under autonomous + the
// kill-switch off.
type recordingRealDefault struct {
	mu    sync.Mutex
	calls []rdCall
}

type rdCall struct {
	flagKey string
	value   any
}

func (r *recordingRealDefault) SetRealDefault(_ context.Context, flagKey string, value any) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.calls = append(r.calls, rdCall{flagKey, value})
	return nil
}

func (r *recordingRealDefault) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.calls)
}

// loopWatcher is a non-degrading autonomous safety-net Watcher (#1016): autonomous
// now FAILS CLOSED without a Watcher wired, so these end-to-end loop tests must wire
// one for the real-default write path to be reachable. It reports "not degraded" so
// a promoted change is kept (the gating-and-keep path these tests assert).
type loopWatcher struct{}

func (loopWatcher) Degraded(context.Context, string, float64) (bool, error) { return false, nil }

// recordingGitHub records OpenIssue/OpenPR so a test can assert the safe (issue)
// path was taken instead of a real-default mutation.
type recordingGitHub struct {
	mu     sync.Mutex
	issues int
	prs    int
}

func (r *recordingGitHub) OpenIssue(context.Context, promote.IssueReq) (string, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.issues++
	return "https://example.test/issue/1", nil
}

func (r *recordingGitHub) OpenPR(context.Context, promote.PRReq) (string, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.prs++
	return "https://example.test/pr/1", nil
}

func (r *recordingGitHub) issueCount() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.issues
}

// newGameFileForLoop copies the experiment package's game.json fixture into a temp
// dir so the loop's GateTargetingWriter writes a real (but throwaway) flag file.
func newGameFileForLoop(t *testing.T) string {
	t.Helper()
	src, err := os.ReadFile(filepath.Join("experiment", "testdata", "game.json"))
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "game.json")
	if err := os.WriteFile(path, src, 0o644); err != nil {
		t.Fatalf("write temp fixture: %v", err)
	}
	return path
}

// buildLoopForTest assembles an experimentLoop with the REAL TargetingWriter
// (Gate/Writer over a temp game.json), the REAL #979 Verdict (low min-N so a small
// test cohort can conclude), the REAL #980 ExperimentPromoter over a #961 Promoter
// wired with recording fakes, and a recording ShadowSpawner. resolveConfig supplies
// the live promotion Config (mode/target/kill-switch) per promotion.
func buildLoopForTest(
	t *testing.T,
	spawner experiment.ShadowSpawner,
	resolveConfig promote.ConfigResolver,
	realDef promote.RealDefaultWriter,
	github promote.GitHubClient,
	fitness gameFitnessFunc,
) *experimentLoop {
	t.Helper()
	return buildLoopForTestWithTracer(t, spawner, resolveConfig, realDef, github, fitness, nil)
}

// buildLoopForTestWithTracer is buildLoopForTest with an injectable root-span tracer for
// the registry (#1188): passing the SAME recording tracer the loop uses lets a test
// assert the analysis spans re-parent under the registry's agent.experiment ROOT span. A
// nil tracer leaves the registry on its default (no-op) tracer — the pre-#1188 shape.
func buildLoopForTestWithTracer(
	t *testing.T,
	spawner experiment.ShadowSpawner,
	resolveConfig promote.ConfigResolver,
	realDef promote.RealDefaultWriter,
	github promote.GitHubClient,
	fitness gameFitnessFunc,
	tracer trace.Tracer,
) *experimentLoop {
	t.Helper()
	log := testLogger()
	gamePath := newGameFileForLoop(t)
	journalRoot := t.TempDir()

	writer := experiment.NewWriter(gamePath, log)
	gate := experiment.NewGate(writer, nil, log)
	targeting := experiment.NewGateTargetingWriter(gate, writer)

	// #1016: autonomous fails closed without a Watcher, so wire a non-degrading one
	// (the safety net) for the real-default write path to be reachable in these tests.
	base := promote.NewPromoter(github, nil, realDef, loopWatcher{}, nil, promote.RepoRef{}, nil, log)
	promo := promote.NewExperimentPromoter(base, resolveConfig)

	// Low min-N so a handful of conclusions reaches a conclusive verdict; effect
	// threshold default. A large mean gap in the test makes the effect clearly
	// significant.
	verdict := experiment.NewVerdict(3, 0.5, nil)

	reg := experiment.NewRegistry(experiment.RegistryConfig{
		Root:           journalRoot,
		MaxShadowGames: 8,
		Spawner:        spawner,
		Verdict:        verdict,
		Promoter:       promo,
		Targeting:      targeting,
		Log:            log,
		Tracer:         tracer,
	})

	return &experimentLoop{
		registry: reg,
		log:      log,
		fitness:  fitness,
		// #1044: the loop self-gates each tick on the live ExperimentConfig. With a nil
		// flags client these tests drive allocation explicitly, so prime the bootstrap
		// defaults ENABLED (the loop's gate ON) and a long tick — preserving the
		// pre-#1044 "the loop runs and allocates" behavior the conclusion/grace tests
		// assert. config(ctx) with a nil flags client returns these defaults verbatim.
		expDefaults: flags.ExperimentDefaults{
			Enabled:              true,
			Tick:                 time.Hour, // tests drive allocation explicitly
			DynamicMaxConcurrent: defaultDynamicMaxConcurrent,
			// ShadowEffectiveConcurrency LEFT 0 on purpose: Reconfigure ignores a
			// non-positive concurrency, so the loop's allocateIfEnabled does NOT clobber
			// the registry's own EffectiveConcurrency (some tests pin concurrency=1 by
			// swapping in their own registry). Production resolves a positive env/flag value.
		},
	}
}

// gcFor builds a game-end GameContext for an experiment arm with a known SessionID.
func gcFor(gameID, experimentID, arm string) gamecontext.GameContext {
	dur := 120.0
	return gamecontext.GameContext{
		SessionID:    gameID,
		GameKind:     "shadow",
		ExperimentID: experimentID,
		Arm:          arm,
		Session:      gamecontext.SessionSignals{DurationSeconds: &dur},
	}
}

// armFitness returns a fitness func that scores the experimental arm high and the
// control arm low — a clear, significant separation so the #979 verdict promotes.
// A small per-game jitter (derived from the game id) gives each arm a NON-ZERO
// variance, so the effect-size gate (Cohen's d) is defined: identical samples
// would yield zero pooled variance and an undefined (inconclusive) effect.
func armFitness() gameFitnessFunc {
	return func(gc gamecontext.GameContext, _ string) float64 {
		// Deterministic per-game spread from the trailing seq digit in the id
		// ("..._<seq>"), so each arm has non-zero variance.
		last := byte('0')
		if n := len(gc.SessionID); n > 0 {
			last = gc.SessionID[n-1]
		}
		jitter := float64(int(last-'0')%5) * 0.02
		if gc.Arm == experiment.ArmExperimental {
			return 0.80 + jitter
		}
		return 0.10 + jitter
	}
}

// concludeCohort spawns + concludes enough games per arm to reach a verdict. It
// allocates via the registry (using the spawner's recorded bindings) and replays
// each as a game-end through onGameEnd, so the WHOLE pipeline (journal fold →
// verdict → conclude → promote → teardown) runs exactly as in production.
func concludeCohort(t *testing.T, loop *experimentLoop, spawner *recordingSpawner, gamesPerArm int) {
	t.Helper()
	ctx := context.Background()
	for i := 0; i < gamesPerArm*2; i++ {
		// Allocate one game (round-robin fills experimental then control).
		n := loop.registry.AllocateAndSpawn(ctx)
		if n == 0 {
			// Cap reached for this round; conclude what we have to free slots.
		}
		for _, c := range spawner.drain() {
			loop.onGameEnd(gcFor(c.gameID, c.experimentID, c.arm))
		}
	}
}

// TestExperimentLoop_EndToEnd_SafeDefault_NoRealWrite is the headline AC for the
// SAFE default: a promote verdict under the default Config (issue/local,
// kill-switch state irrelevant for issue mode) routes through the #961 Promoter and
// opens an ISSUE — it NEVER performs a real-default write. With the autonomous path
// (the only real-player-affecting one) OFF, recordingRealDefault stays at zero.
func TestExperimentLoop_EndToEnd_SafeDefault_NoRealWrite(t *testing.T) {
	spawner := &recordingSpawner{}
	realDef := &recordingRealDefault{}
	github := &recordingGitHub{}

	// SAFE DEFAULT resolver: mode=issue, target=local, enabled=false — exactly the
	// flagd defaults (#936). Read fresh per promotion.
	resolve := func(context.Context) promote.Config {
		return promote.Config{Mode: promote.ModeIssue, Target: promote.TargetLocal, Enabled: false}
	}

	loop := buildLoopForTest(t, spawner, resolve, realDef, github, armFitness())

	id, err := loop.registry.Declare(experiment.Intent{
		FlagKey: "invincibility_seconds", ExperimentalValue: 6.0, Objective: "balanced", TargetNPerArm: 3,
	})
	if err != nil {
		t.Fatalf("declare: %v", err)
	}
	if err := loop.registry.Start(context.Background(), id); err != nil {
		t.Fatalf("start: %v", err)
	}

	concludeCohort(t, loop, spawner, 4)

	// The experiment must have concluded (terminal) — promote routed, then torn down.
	st, _ := loop.registry.Status(id)
	if st != experiment.StatusDone {
		t.Fatalf("experiment status = %q, want done (promote routed)", st)
	}
	// The promoter ran in the SAFE mode: an issue was opened, NOT a real-default write.
	if github.issueCount() == 0 {
		t.Fatalf("expected the safe-default promotion to open an issue, got none")
	}
	if got := realDef.count(); got != 0 {
		t.Fatalf("SAFETY VIOLATION: real-default write happened %d time(s) with autonomous OFF", got)
	}
}

// TestExperimentLoop_EndToEnd_Autonomous_RealWriteGated is the counterpart: with the
// autonomous mode AND the kill-switch off (the only configuration the #961 safety
// rail permits a real-player write), a promote verdict DOES route a real-default
// write through the #961 Promoter. This proves the gate opens only when every
// condition holds — and that the loop wires the real Promoter, not a stub.
func TestExperimentLoop_EndToEnd_Autonomous_RealWriteGated(t *testing.T) {
	spawner := &recordingSpawner{}
	realDef := &recordingRealDefault{}
	github := &recordingGitHub{}

	// Autonomous + kill-switch OFF (Enabled true) ⇒ the real-default path is reachable.
	resolve := func(context.Context) promote.Config {
		return promote.Config{Mode: promote.ModeAutonomous, Target: promote.TargetLocal, Enabled: true}
	}

	loop := buildLoopForTest(t, spawner, resolve, realDef, github, armFitness())

	id, err := loop.registry.Declare(experiment.Intent{
		FlagKey: "invincibility_seconds", ExperimentalValue: 6.0, Objective: "balanced", TargetNPerArm: 3,
	})
	if err != nil {
		t.Fatalf("declare: %v", err)
	}
	if err := loop.registry.Start(context.Background(), id); err != nil {
		t.Fatalf("start: %v", err)
	}

	concludeCohort(t, loop, spawner, 4)

	if st, _ := loop.registry.Status(id); st != experiment.StatusDone {
		t.Fatalf("experiment status = %q, want done", st)
	}
	if got := realDef.count(); got != 1 {
		t.Fatalf("autonomous (kill-switch off) should perform exactly 1 real-default write, got %d", got)
	}
	if got := realDef.calls[0]; got.flagKey != "invincibility_seconds" || got.value != 6.0 {
		t.Fatalf("real-default write = %+v, want invincibility_seconds=6.0", got)
	}
}

// TestExperimentLoop_EndToEnd_Autonomous_KillSwitchBlocksRealWrite confirms the
// kill-switch (Config.Enabled false) blocks the real-default write EVEN in
// autonomous mode — a promote verdict still concludes, but the real-player write
// degrades to a recorded no-op.
func TestExperimentLoop_EndToEnd_Autonomous_KillSwitchBlocksRealWrite(t *testing.T) {
	spawner := &recordingSpawner{}
	realDef := &recordingRealDefault{}
	github := &recordingGitHub{}

	resolve := func(context.Context) promote.Config {
		return promote.Config{Mode: promote.ModeAutonomous, Target: promote.TargetLocal, Enabled: false}
	}

	loop := buildLoopForTest(t, spawner, resolve, realDef, github, armFitness())

	id, _ := loop.registry.Declare(experiment.Intent{
		FlagKey: "invincibility_seconds", ExperimentalValue: 6.0, Objective: "balanced", TargetNPerArm: 3,
	})
	if err := loop.registry.Start(context.Background(), id); err != nil {
		t.Fatalf("start: %v", err)
	}

	concludeCohort(t, loop, spawner, 4)

	if got := realDef.count(); got != 0 {
		t.Fatalf("kill-switch on (Enabled=false) must block the real-default write, got %d", got)
	}
}

// TestExperimentLoop_OnGameEnd_IgnoresNonExperimentGames confirms the conclusion
// hook is a safe no-op for a game NOT bound to any experiment (no ExperimentID) —
// it must never touch the registry, so the existing per-game path is unchanged.
func TestExperimentLoop_OnGameEnd_IgnoresNonExperimentGames(t *testing.T) {
	spawner := &recordingSpawner{}
	resolve := func(context.Context) promote.Config { return promote.Config{Mode: promote.ModeIssue} }
	loop := buildLoopForTest(t, spawner, resolve, &recordingRealDefault{}, &recordingGitHub{}, armFitness())

	// A plain real game with no experiment binding — must be ignored.
	loop.onGameEnd(gcFor("game_real_123", "", ""))
	// A game claiming an experiment the registry does not own — also ignored.
	loop.onGameEnd(gcFor("game_unknown", "exp_does_not_exist", "experimental"))
	// No panic, no state — TotalInFlight stays 0.
	if n := loop.registry.TotalInFlight(); n != 0 {
		t.Fatalf("non-experiment game-ends touched the registry (in-flight=%d)", n)
	}
}

// TestExperimentLoop_OnGameTerminal_ReleasesErroredGame is the #1014 loop-level
// AC: when a spawned game ends in game_error (OutcomeError), the terminal hook
// releases the in-flight slot via the registry — WITHOUT folding a fitness
// sample — so an effective_concurrency=1 loop does not deadlock. A naturally
// COMPLETED game is left for the telemetry onGameEnd path (no release here).
func TestExperimentLoop_OnGameTerminal_ReleasesErroredGame(t *testing.T) {
	spawner := &recordingSpawner{}
	resolve := func(context.Context) promote.Config { return promote.Config{Mode: promote.ModeIssue} }
	loop := buildLoopForTest(t, spawner, resolve, &recordingRealDefault{}, &recordingGitHub{}, armFitness())
	ctx := context.Background()

	id, err := loop.registry.Declare(experiment.Intent{
		Hypothesis: "h", FlagKey: "death_grace_period_seconds",
		ExperimentalValue: 0.5, Objective: "balanced", TargetNPerArm: 3,
	})
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := loop.registry.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}
	if n := loop.registry.AllocateAndSpawn(ctx); n == 0 {
		t.Fatal("expected a spawn")
	}
	recs := spawner.drain()
	if len(recs) == 0 {
		t.Fatal("spawner recorded no game")
	}
	gameID := recs[0].gameID
	if loop.registry.TotalInFlight() == 0 {
		t.Fatal("expected in-flight games after spawn")
	}

	// A naturally completed game does NOT release here (telemetry concludes it):
	// the specific game binding survives so the telemetry onGameEnd path can fold
	// its fitness sample.
	loop.onGameTerminal(gameID, gamerunner.OutcomeCompleted)
	if !gameStillBound(loop, id, gameID) {
		t.Fatal("OutcomeCompleted released the game binding; it must defer to the telemetry conclude path")
	}

	// A game_error releases THAT game's slot (no deadlock) without folding a
	// sample. (The loop may immediately refill the freed slot with a NEW game —
	// that is the correct no-deadlock behavior — so assert on the specific binding
	// being gone, plus that no fitness sample was folded.)
	loop.onGameTerminal(gameID, gamerunner.OutcomeError)
	if gameStillBound(loop, id, gameID) {
		t.Fatal("OutcomeError did not release the errored game's binding (slot leak / deadlock risk)")
	}
	cv, _ := loop.registry.CompactView(id)
	for arm, a := range cv.Arms {
		if a.Count != 0 {
			t.Fatalf("arm %q folded a sample (count=%d) on an errored game; release must be non-counting", arm, a.Count)
		}
	}
}

// gameStillBound reports whether gameID is still an in-flight binding of the
// experiment (a game_concluded/game_released event drops it). It inspects the
// journal tail since the registry exposes no per-game accessor.
func gameStillBound(loop *experimentLoop, expID, gameID string) bool {
	cv, ok := loop.registry.CompactView(expID)
	if !ok {
		return false
	}
	assigned := false
	for _, ev := range cv.RecentTail {
		if ev.GameID != gameID {
			continue
		}
		switch ev.Kind {
		case journal.KindGameAssigned:
			assigned = true
		case journal.KindGameConcluded, journal.KindGameReleased:
			assigned = false
		}
	}
	return assigned
}

// fakeAfter is an injectable afterFunc (#1020) that captures scheduled callbacks
// instead of relying on the wall clock, so the bounded-grace backstop can be fired
// deterministically in a test without sleeping the real grace (60s by default).
type fakeAfter struct {
	mu      sync.Mutex
	pending map[int]func()
	seq     int
}

func newFakeAfter() *fakeAfter { return &fakeAfter{pending: map[int]func(){}} }

// schedule matches the experimentLoop.afterFunc signature: it stores f under a
// fresh id and returns a stop func that removes it (true if it was still pending).
func (f *fakeAfter) schedule(_ time.Duration, fn func()) func() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.seq++
	id := f.seq
	f.pending[id] = fn
	return func() bool {
		f.mu.Lock()
		defer f.mu.Unlock()
		if _, ok := f.pending[id]; ok {
			delete(f.pending, id)
			return true
		}
		return false
	}
}

// fireAll invokes (and removes) every still-pending timer — simulating the grace
// window elapsing. Returns how many fired.
func (f *fakeAfter) fireAll() int {
	f.mu.Lock()
	fns := make([]func(), 0, len(f.pending))
	for id, fn := range f.pending {
		fns = append(fns, fn)
		delete(f.pending, id)
	}
	f.mu.Unlock()
	for _, fn := range fns {
		fn()
	}
	return len(fns)
}

func (f *fakeAfter) pendingCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.pending)
}

// TestExperimentLoop_CompletedGrace_ReleasesOnDroppedDatapoint is the headline
// #1020 AC: a COMPLETED shadow game whose telemetry game_active=0 datapoint never
// arrives (onGameEnd → ConcludeGame never runs) has its in-flight slot released by
// the bounded-grace backstop after the grace elapses — so the loop can spawn again
// at effective_concurrency=1 instead of deadlocking on the leaked slot. The grace
// is fired via an injected fake clock so the test does not sleep the real 60s.
func TestExperimentLoop_CompletedGrace_ReleasesOnDroppedDatapoint(t *testing.T) {
	spawner := &recordingSpawner{}
	resolve := func(context.Context) promote.Config { return promote.Config{Mode: promote.ModeIssue} }
	loop := buildLoopForTest(t, spawner, resolve, &recordingRealDefault{}, &recordingGitHub{}, armFitness())
	ctx := context.Background()

	// Pin effective_concurrency=1 (the #998 default that #1014/#1020 protect): a
	// single leaked slot is a full deadlock. A fake clock drives the grace.
	fake := newFakeAfter()
	loop.afterFunc = fake.schedule
	loop.completedGrace = 60 * time.Second
	loop.registry = newRegistryConcurrency1(t, spawner, resolve)

	id, err := loop.registry.Declare(experiment.Intent{
		Hypothesis: "h", FlagKey: "death_grace_period_seconds",
		ExperimentalValue: 0.5, Objective: "endurance", TargetNPerArm: 5,
	})
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := loop.registry.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}

	if n := loop.registry.AllocateAndSpawn(ctx); n == 0 {
		t.Fatal("expected a spawn")
	}
	recs := spawner.drain()
	if len(recs) != 1 {
		t.Fatalf("expected exactly 1 spawn at concurrency=1, got %d", len(recs))
	}
	first := recs[0].gameID

	// The single slot is now occupied — a second allocate cannot spawn (deadlock
	// territory if the slot never frees).
	if n := loop.registry.AllocateAndSpawn(ctx); n != 0 {
		t.Fatalf("concurrency=1 should block a second spawn while a slot is held, got %d", n)
	}

	// The game COMPLETES but its telemetry datapoint is DROPPED: onGameTerminal sees
	// OutcomeCompleted (arming the grace) and onGameEnd → ConcludeGame NEVER fires.
	loop.onGameTerminal(first, gamerunner.OutcomeCompleted)
	if fake.pendingCount() != 1 {
		t.Fatalf("completed game should arm exactly one grace timer, pending=%d", fake.pendingCount())
	}
	if !gameStillBound(loop, id, first) {
		t.Fatal("before the grace fires the completed game must stay bound (a late conclude could still win)")
	}

	// Grace elapses with no conclude in sight ⇒ the backstop releases the slot AND
	// (inside fireCompletedGrace) nudges AllocateAndSpawn to refill it, proving the
	// loop makes progress again rather than deadlocking on the leaked slot.
	if fired := fake.fireAll(); fired != 1 {
		t.Fatalf("expected the grace timer to fire once, fired=%d", fired)
	}
	if gameStillBound(loop, id, first) {
		t.Fatal("grace backstop did not release the leaked completed-game slot (#1020): deadlock at concurrency=1")
	}
	// The release was NON-COUNTING: no fabricated fitness sample folded.
	cv, _ := loop.registry.CompactView(id)
	for arm, a := range cv.Arms {
		if a.Count != 0 {
			t.Fatalf("arm %q folded a sample (count=%d); grace release must be non-counting", arm, a.Count)
		}
	}

	// The freed slot WAS reused: the backstop's refill spawned a fresh game (a
	// different game_id), so the loop is unblocked at concurrency=1.
	refill := spawner.drain()
	if len(refill) != 1 || refill[0].gameID == first {
		t.Fatalf("grace backstop did not refill the freed slot with a new spawn (got %+v); loop still deadlocked", refill)
	}
}

// TestExperimentLoop_CompletedGrace_NoReleaseWhenConcluded is the no-regression AC:
// a normally-concluding completed game folds its fitness sample via the telemetry
// onGameEnd → ConcludeGame path, and when the grace timer later fires it is a no-op
// (ReleaseGame finds nothing — the game already left in-flight). The sample stays
// counted; the backstop never fabricates a non-counting release for a healthy game.
func TestExperimentLoop_CompletedGrace_NoReleaseWhenConcluded(t *testing.T) {
	spawner := &recordingSpawner{}
	resolve := func(context.Context) promote.Config { return promote.Config{Mode: promote.ModeIssue} }
	loop := buildLoopForTest(t, spawner, resolve, &recordingRealDefault{}, &recordingGitHub{}, armFitness())
	ctx := context.Background()

	fake := newFakeAfter()
	loop.afterFunc = fake.schedule
	loop.completedGrace = 60 * time.Second

	id, err := loop.registry.Declare(experiment.Intent{
		Hypothesis: "h", FlagKey: "death_grace_period_seconds",
		ExperimentalValue: 0.5, Objective: "endurance", TargetNPerArm: 5,
	})
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := loop.registry.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}
	if n := loop.registry.AllocateAndSpawn(ctx); n == 0 {
		t.Fatal("expected a spawn")
	}
	rec := spawner.drain()[0]

	// Completed terminal arms the grace, THEN telemetry concludes it normally
	// (folding the experimental arm's high fitness sample) — the healthy path.
	loop.onGameTerminal(rec.gameID, gamerunner.OutcomeCompleted)
	loop.onGameEnd(gcFor(rec.gameID, rec.experimentID, rec.arm))

	if gameStillBound(loop, id, rec.gameID) {
		t.Fatal("ConcludeGame should have folded + freed the game before the grace fires")
	}
	cv, _ := loop.registry.CompactView(id)
	if cv.Arms[rec.arm].Count != 1 {
		t.Fatalf("normal conclude must fold exactly 1 sample into arm %q, got count=%d", rec.arm, cv.Arms[rec.arm].Count)
	}
	folded := cv.Arms[rec.arm].Count

	// The grace timer fires LATE (after the legitimate conclude already won the
	// race). It must be a pure no-op: no extra release, the sample stays counted.
	fake.fireAll()
	cv2, _ := loop.registry.CompactView(id)
	if cv2.Arms[rec.arm].Count != folded {
		t.Fatalf("late grace timer changed the folded sample count (%d → %d): it must be a no-op",
			folded, cv2.Arms[rec.arm].Count)
	}
	if gameStillBound(loop, id, rec.gameID) {
		t.Fatal("late grace timer re-bound or disturbed the concluded game")
	}
}

// TestExperimentLoop_CompletedGrace_StopAllRaceGuard is the #1020 concurrency
// regression anchor (review suggestion #3). It exercises the shutdown timer-fire
// race that the synchronous fakeAfter seam cannot: grace timers are armed for
// genuinely in-flight shadow games, then their callbacks fire on real goroutines
// concurrently with stopAllCompletedGrace() from another goroutine. The afterFunc
// seam returns a stop that always reports "too late" (false) — exactly the
// adversarial case where time.AfterFunc(...).Stop() loses to an already-firing
// callback — so the graceShutdown guard is the ONLY thing that can prevent a
// callback firing after shutdown from calling ReleaseGame + allocateIfEnabled and
// spawning a brand-new shadow game during/after teardown. Two assertions, both
// load-bearing because the games are really bound (ReleaseGame would return true):
//   - under -race: no data race / panic across the graceTimers map + guard;
//   - callbacks that fire AFTER stopAll has returned (guard latched) must NOT spawn.
func TestExperimentLoop_CompletedGrace_StopAllRaceGuard(t *testing.T) {
	spawner := &recordingSpawner{}
	resolve := func(context.Context) promote.Config { return promote.Config{Mode: promote.ModeIssue} }
	loop := buildLoopForTest(t, spawner, resolve, &recordingRealDefault{}, &recordingGitHub{}, armFitness())
	loop.completedGrace = 60 * time.Second
	ctx := context.Background()

	// captureAfter records each scheduled callback (instead of firing it on a wall
	// clock) so the test fires them on real goroutines interleaved with stopAll. Its
	// stop ALWAYS returns false, modeling a callback already past the point Stop()
	// can cancel it — forcing the guard (not Stop) to be what blocks a post-shutdown spawn.
	var capMu sync.Mutex
	var callbacks []func()
	captureAfter := func(_ time.Duration, fn func()) func() bool {
		capMu.Lock()
		callbacks = append(callbacks, fn)
		capMu.Unlock()
		return func() bool { return false }
	}
	loop.afterFunc = captureAfter

	// Declare + start an experiment and put a game in-flight (AllocateAndSpawn spawns
	// one per call into the free slot), so the armed grace timer is for a game the
	// registry STILL holds — ReleaseGame would return true, meaning a non-guarded late
	// callback WOULD ReleaseGame + refill-spawn a brand-new game during shutdown.
	id, err := loop.registry.Declare(experiment.Intent{
		Hypothesis: "h", FlagKey: "death_grace_period_seconds",
		ExperimentalValue: 0.5, Objective: "endurance", TargetNPerArm: 50,
	})
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := loop.registry.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}
	if n := loop.registry.AllocateAndSpawn(ctx); n == 0 {
		t.Fatal("expected the registry to spawn into a free in-flight slot")
	}
	inFlight := spawner.drain()
	if len(inFlight) == 0 {
		t.Fatal("expected an in-flight shadow game to arm grace for")
	}
	boundGame := inFlight[0].gameID

	// Phase 1 (race coverage): concurrently arm many grace timers for distinct (not
	// in-flight) ids while stopAllCompletedGrace runs — this is the map-mutation +
	// guard-latch interleaving the -race detector validates. These ids aren't bound,
	// so even if a callback fires unguarded it can't spawn; the point here is purely
	// no-data-race / no-panic on graceTimers + graceShutdown under concurrency.
	const churn = 32
	var armWG sync.WaitGroup
	for i := 0; i < churn; i++ {
		armWG.Add(1)
		go func(n int) {
			defer armWG.Done()
			loop.armCompletedGrace("game_churn_" + itoa(n))
		}(i)
	}
	armWG.Add(1)
	go func() {
		defer armWG.Done()
		loop.stopAllCompletedGrace()
	}()
	armWG.Wait()

	// Phase 2 (load-bearing guard assertion): stopAll has now latched graceShutdown.
	// Arm grace for the STILL-in-flight game (armCompletedGrace records the timer even
	// post-latch — only fireCompletedGrace is guarded), capture its callback, and fire
	// it on goroutines. Each enters fireCompletedGrace with graceShutdown=true, so the
	// guard must make it bail BEFORE ReleaseGame/allocateIfEnabled. Without the guard,
	// it would ReleaseGame(true)+refill and spawn a brand-new game during shutdown.
	capMu.Lock()
	callbacks = nil // drop the phase-1 churn callbacks; we only fire the bound-game one.
	capMu.Unlock()
	loop.armCompletedGrace(boundGame)
	capMu.Lock()
	fired := append([]func(){}, callbacks...)
	capMu.Unlock()
	if len(fired) != 1 {
		t.Fatalf("expected exactly one armed grace timer for the bound game, captured %d", len(fired))
	}

	var postWG sync.WaitGroup
	for i := 0; i < 8; i++ { // fire the same callback repeatedly on goroutines (race + idempotency).
		postWG.Add(1)
		go func() {
			defer postWG.Done()
			fired[0]()
		}()
	}
	postWG.Wait()

	if recs := spawner.drain(); len(recs) != 0 {
		t.Fatalf("grace callback spawned %d game(s) AFTER stopAll shutdown latched; the #1020 guard must prevent any post-shutdown spawn", len(recs))
	}
	if !gameStillBound(loop, id, boundGame) {
		t.Fatal("the guarded callback released the in-flight game during shutdown; it must bail before ReleaseGame")
	}
	loop.graceMu.Lock()
	shuttingDown := loop.graceShutdown
	loop.graceMu.Unlock()
	if !shuttingDown {
		t.Fatal("stopAllCompletedGrace must latch graceShutdown so a later-firing callback bails")
	}
}

// newRegistryConcurrency1 builds a registry pinned to effective_concurrency=1 (one
// in-flight shadow game cap) so the #1020 deadlock condition is exercised exactly
// as the #998 default deploys it. It mirrors buildLoopForTest's registry wiring.
func newRegistryConcurrency1(t *testing.T, spawner experiment.ShadowSpawner, resolve promote.ConfigResolver) *experiment.Registry {
	t.Helper()
	log := testLogger()
	gamePath := newGameFileForLoop(t)
	writer := experiment.NewWriter(gamePath, log)
	gate := experiment.NewGate(writer, nil, log)
	targeting := experiment.NewGateTargetingWriter(gate, writer)
	base := promote.NewPromoter(&recordingGitHub{}, nil, &recordingRealDefault{}, nil, nil, promote.RepoRef{}, nil, log)
	promo := promote.NewExperimentPromoter(base, resolve)
	return experiment.NewRegistry(experiment.RegistryConfig{
		Root:                 t.TempDir(),
		MaxShadowGames:       8,
		EffectiveConcurrency: 1,
		Spawner:              spawner,
		Verdict:              experiment.NewVerdict(3, 0.5, nil),
		Promoter:             promo,
		Targeting:            targeting,
		Log:                  log,
	})
}

// TestBuildExperimentLoop_DisabledByDefault is the disabled-default AC (#1044
// startup-gate inversion). The loop is ALWAYS built now (so an off→on flag flip can
// start it at runtime), but with the opt-in OFF (the default, env unset) it
// SELF-GATES to a no-op: experiments_enabled defaults to false, so allocateIfEnabled
// runs no rehydrate, no seed, no allocate — behaviorally inert (the only residual
// work is the no-op tick). The env value is only the BOOTSTRAP DEFAULT.
func TestBuildExperimentLoop_DisabledByDefault(t *testing.T) {
	// Ensure the opt-in env is unset (the default).
	t.Setenv(envExperimentsEnabled, "")
	loop := buildExperimentLoop(nil, nil, nil, "/nonexistent/game.json", testLogger())
	if loop == nil {
		t.Fatalf("buildExperimentLoop must ALWAYS build the loop now (#1044); gating is the live flag, not a nil return")
	}
	if loop.expDefaults.Enabled {
		t.Fatalf("with AGENT_EXPERIMENTS_ENABLED unset the bootstrap default must be disabled, got Enabled=true")
	}
	// The loop self-gates OFF: an allocate tick with the disabled default does no
	// work (no live experiments declared).
	loop.allocateIfEnabled(context.Background())
	if got := loop.registry.LiveCount(); got != 0 {
		t.Fatalf("disabled-by-default loop declared work: LiveCount = %d, want 0", got)
	}
	if loop.startedOnce {
		t.Fatalf("disabled-by-default loop ran its rehydrate+seed bootstrap; it must defer until enabled")
	}

	// Explicit "false" is also off.
	t.Setenv(envExperimentsEnabled, "false")
	loop2 := buildExperimentLoop(nil, nil, nil, "/nonexistent/game.json", testLogger())
	if loop2 == nil || loop2.expDefaults.Enabled {
		t.Fatalf("AGENT_EXPERIMENTS_ENABLED=false must build a loop with the disabled bootstrap default")
	}
}

// TestExperimentLoop_RunShutdownNoLeak is the leak-safe AC: the experiment loop's
// run() goroutine terminates on ctx cancellation (it AbortAll's live experiments
// and returns), leaving no survivor — the #923 contract applied to the experiment
// loop's own goroutine.
func TestExperimentLoop_RunShutdownNoLeak(t *testing.T) {
	// Ignore the process-global singletons this loop does NOT own: the OpenFeature
	// event executor (a package singleton started transitively when the flags/
	// promote packages touch the SDK), the gRPC balancer watcher, and the
	// opencensus stats worker. IgnoreAnyFunction matches the executor regardless of
	// the exact creating-frame symbol (which varies by SDK version), so this stays
	// robust where the static leakIgnores() top-function string can drift.
	// The executor's creating-frame symbol differs between the plain and -race
	// builds (the compiler inlines newEventExecutor differently), so BOTH variants
	// are listed; one always matches.
	defer goleak.VerifyNone(t,
		goleak.IgnoreAnyFunction("github.com/open-feature/go-sdk/openfeature.(*eventExecutor).startEventListener.func1.1"),
		goleak.IgnoreAnyFunction("github.com/open-feature/go-sdk/openfeature.newEventExecutor.(*eventExecutor).startEventListener.func1.1"),
		goleak.IgnoreAnyFunction("google.golang.org/grpc.(*ccBalancerWrapper).watcher"),
		goleak.IgnoreAnyFunction("go.opencensus.io/stats/view.(*worker).start"),
	)

	spawner := &recordingSpawner{}
	resolve := func(context.Context) promote.Config {
		return promote.Config{Mode: promote.ModeIssue, Target: promote.TargetLocal, Enabled: true}
	}
	loop := buildLoopForTest(t, spawner, resolve, &recordingRealDefault{}, &recordingGitHub{}, armFitness())
	// A short tick so the allocate loop actually fires during the test, proving it
	// stops on ctx cancel rather than merely never having started. #1044: the tick is
	// resolved live from expDefaults.Tick (nil flags client), so set it there.
	loop.expDefaults.Tick = 5 * time.Millisecond
	// Seed an experiment so the loop has live work each tick.
	loop.seed = &experiment.Intent{
		FlagKey: "invincibility_seconds", ExperimentalValue: 6.0, Objective: "balanced", TargetNPerArm: 100,
	}
	// The loop reads e.flags.Evaluate for the kill-switch; a nil flags client is
	// treated as "enabled" (test wiring only, see allocateIfEnabled), so the leak
	// test needs no flagd dependency.
	loop.flags = nil

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		loop.run(ctx)
	}()

	time.Sleep(40 * time.Millisecond) // let the ticker fire a few times
	cancel()

	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("experiment loop run() did not return within 5s of ctx cancellation")
	}
	_ = journal.DefaultDir // keep the journal import meaningful for the suite
}
