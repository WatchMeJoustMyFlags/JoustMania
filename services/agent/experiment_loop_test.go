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

	"go.uber.org/goleak"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/experiment/journal"
	"github.com/joustmania/agent/gamecontext"
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
}

func (r *recordingSpawner) Spawn(_ context.Context, experimentID, arm string) (string, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.seq++
	id := "game_" + experimentID + "_" + arm + "_" + itoa(r.seq)
	r.calls = append(r.calls, spawnRec{experimentID, arm, id})
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
	log := testLogger()
	gamePath := newGameFileForLoop(t)
	journalRoot := t.TempDir()

	writer := experiment.NewWriter(gamePath, log)
	gate := experiment.NewGate(writer, nil, log)
	targeting := experiment.NewGateTargetingWriter(gate, writer)

	base := promote.NewPromoter(github, nil, realDef, nil, nil, promote.RepoRef{}, nil, log)
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
	})

	return &experimentLoop{
		registry: reg,
		log:      log,
		tick:     time.Hour, // tests drive allocation explicitly
		fitness:  fitness,
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

// TestBuildExperimentLoop_DisabledByDefault is the disabled-default AC: with the
// opt-in OFF (the default, env unset), buildExperimentLoop returns nil — NO
// Registry, NO seams, NO goroutines. The agent's normal behavior is unchanged.
func TestBuildExperimentLoop_DisabledByDefault(t *testing.T) {
	// Ensure the opt-in env is unset (the default).
	t.Setenv(envExperimentsEnabled, "")
	loop := buildExperimentLoop(nil, nil, "/nonexistent/game.json", testLogger())
	if loop != nil {
		t.Fatalf("buildExperimentLoop must return nil when AGENT_EXPERIMENTS_ENABLED is unset (default-off)")
	}

	// Explicit "false" is also off.
	t.Setenv(envExperimentsEnabled, "false")
	if buildExperimentLoop(nil, nil, "/nonexistent/game.json", testLogger()) != nil {
		t.Fatalf("buildExperimentLoop must return nil when AGENT_EXPERIMENTS_ENABLED=false")
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
	// stops on ctx cancel rather than merely never having started.
	loop.tick = 5 * time.Millisecond
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
