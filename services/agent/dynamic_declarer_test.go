package main

import (
	"context"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
)

// dynamic_declarer_test.go is the #995 acceptance suite for the HEURISTIC dynamic
// experiment declarer:
//   - it picks a valid, Gate-passing Intent from game.json flags (existing flag,
//     type-compatible value, experimentable objective);
//   - default-off: with AGENT_EXPERIMENT_DYNAMIC_ENABLED unset, NO dynamic
//     declaration happens (env-seed behavior byte-identical) — the regression guard;
//   - capacity/dedup: it won't declare a second experiment on a flag that already
//     has an active one, nor beyond the concurrent-experiment backstop;
//   - rotation: successive declarations cover DIFFERENT flags (deterministic cursor);
//   - kill-switch off ⇒ the declarer declares nothing.

// gameFileForDeclarer copies the experiment package's game.json fixture to a temp
// file the declarer reads candidate flags from.
func gameFileForDeclarer(t *testing.T) string {
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

// TestDynamicDeclarer_PicksGatePassingIntent asserts the declarer's chosen Intent
// (flag + experimental value) is accepted by the REAL Gate: the flag pre-exists,
// the value is type-compatible (it is one of the flag's own variant values), and
// the objective is experimentable.
func TestDynamicDeclarer_PicksGatePassingIntent(t *testing.T) {
	path := gameFileForDeclarer(t)
	d := &dynamicDeclarer{
		gameFlagPath:     path,
		candidates:       []string{"sensitivity"}, // numeric flag, default medium=2
		defaultObjective: decision.ObjectiveBalanced,
	}

	intent, ok := d.Declare(nil)
	if !ok {
		t.Fatal("declarer returned no intent for a tunable flag")
	}
	if intent.FlagKey != "sensitivity" {
		t.Fatalf("flag = %q, want sensitivity", intent.FlagKey)
	}
	if intent.ExperimentalValue == nil {
		t.Fatal("experimental value is nil")
	}
	if !decision.IsExperimentable(intent.Objective) {
		t.Fatalf("objective %q is not experimentable", intent.Objective)
	}

	// The value must be type-compatible: a number for the numeric sensitivity flag.
	if _, ok := intent.ExperimentalValue.(float64); !ok {
		t.Fatalf("value %v (%T) is not a number for a numeric flag", intent.ExperimentalValue, intent.ExperimentalValue)
	}
	// And it must DIFFER from the default (2): a perturbation, not a no-op.
	if v := intent.ExperimentalValue.(float64); v == 2 {
		t.Fatalf("value %v equals the default (2); not a perturbation", v)
	}

	// The full Intent must pass the REAL Gate when Started: feed the equivalent
	// Proposal through Gate.Review (which writes shadow targeting) and assert accept.
	writer := experiment.NewWriter(path, testLogger())
	gate := experiment.NewGate(writer, nil, testLogger())
	p := experiment.Proposal{
		FlagKey:           intent.FlagKey,
		ExperimentID:      experiment.MintExperimentID(),
		ExperimentalValue: intent.ExperimentalValue,
		Rationale:         intent.Hypothesis,
	}
	if err := gate.Review(context.Background(), p); err != nil {
		t.Fatalf("Gate rejected the declarer's chosen intent: %v", err)
	}
}

// TestDynamicDeclarer_GatePassesObjectAndBoolFlags asserts the chosen value for a
// non-numeric flag (an object-valued flag with pre-existing targeting, and a bool
// flag) is ALSO accepted by the Gate — the perturbation picks an existing variant
// value of the matching JSON kind, so the type guard + invariant hold for every
// kind, not just numbers.
func TestDynamicDeclarer_GatePassesObjectAndBoolFlags(t *testing.T) {
	path := gameFileForDeclarer(t)
	for _, flagKey := range []string{"windows", "random_assignment"} {
		d := &dynamicDeclarer{
			gameFlagPath:     path,
			candidates:       []string{flagKey},
			defaultObjective: decision.ObjectiveBalanced,
		}
		intent, ok := d.Declare(nil)
		if !ok {
			t.Fatalf("%s: no intent", flagKey)
		}
		writer := experiment.NewWriter(path, testLogger())
		gate := experiment.NewGate(writer, nil, testLogger())
		p := experiment.Proposal{
			FlagKey:           intent.FlagKey,
			ExperimentID:      experiment.MintExperimentID(),
			ExperimentalValue: intent.ExperimentalValue,
			Rationale:         intent.Hypothesis,
		}
		if err := gate.Review(context.Background(), p); err != nil {
			t.Fatalf("%s: Gate rejected chosen intent: %v", flagKey, err)
		}
	}
}

// TestDynamicDeclarer_DisabledByDefault is the regression guard (#1044 update):
// the declarer is ALWAYS constructed now, but with experiment_dynamic_enabled OFF
// (the default, here via a zero-value ExperimentConfig) declareDynamicIfEnabled is
// a no-op — the env-seed path is byte-identical. It also asserts a nil declarer is
// safe (the test/degraded wiring).
func TestDynamicDeclarer_DisabledByDefault(t *testing.T) {
	t.Setenv(envDynamicEnabled, "") // explicitly unset/empty
	// The declarer is built regardless of the opt-in now (gating moved to the LIVE
	// flag, checked per tick).
	if d := newDynamicDeclarerFromEnv("/etc/flagd/game.json", nil); d == nil {
		t.Fatal("declarer must always be constructed (#1044) — gating is per-tick via the live flag")
	}
	// A nil declarer is a no-op (test/degraded wiring): no panic, no work.
	loop := &experimentLoop{dynamic: nil, log: testLogger()}
	loop.declareDynamicIfEnabled(context.Background(), flags.ExperimentConfig{DynamicEnabled: true})
	// A wired declarer with DynamicEnabled=false (the live flag off) is ALSO a no-op
	// even though the declarer is present — proving the per-tick flag gate.
	loop.dynamic = &dynamicDeclarer{gameFlagPath: "/etc/flagd/game.json"}
	loop.declareDynamicIfEnabled(context.Background(), flags.ExperimentConfig{DynamicEnabled: false})
}

// TestDynamicDeclarer_EnabledFromEnv asserts the opt-in constructs a declarer and
// honors the candidate / objective / target-N overrides.
func TestDynamicDeclarer_EnabledFromEnv(t *testing.T) {
	t.Setenv(envDynamicEnabled, "true")
	t.Setenv(envDynamicCandidates, "sensitivity, num_teams ,")
	t.Setenv(envDynamicObjective, "endurance")
	t.Setenv(envDynamicTargetN, "5")
	d := newDynamicDeclarerFromEnv("/x/game.json", nil)
	if d == nil {
		t.Fatal("declarer nil with opt-in ON")
	}
	if got := d.candidates; len(got) != 2 || got[0] != "sensitivity" || got[1] != "num_teams" {
		t.Fatalf("candidates = %v, want [sensitivity num_teams]", got)
	}
	if d.defaultObjective != decision.ObjectiveEndurance {
		t.Fatalf("objective = %q, want endurance", d.defaultObjective)
	}
	if d.targetN != 5 {
		t.Fatalf("targetN = %d, want 5", d.targetN)
	}
}

// TestDynamicDeclarer_NonExperimentableObjectiveFallsBack: a chaos default
// objective is non-experimentable, so it is replaced with balanced.
func TestDynamicDeclarer_NonExperimentableObjectiveFallsBack(t *testing.T) {
	t.Setenv(envDynamicEnabled, "true")
	t.Setenv(envDynamicObjective, decision.ObjectiveChaos)
	d := newDynamicDeclarerFromEnv("/x/game.json", nil)
	if d.defaultObjective != decision.ObjectiveBalanced {
		t.Fatalf("chaos objective did not fall back to balanced, got %q", d.defaultObjective)
	}
}

// TestDynamicDeclarer_Rotation asserts successive Declares cover DIFFERENT flags
// via the deterministic cursor (no repeat until the candidate ring is exhausted).
func TestDynamicDeclarer_Rotation(t *testing.T) {
	path := gameFileForDeclarer(t)
	d := &dynamicDeclarer{
		gameFlagPath:     path,
		candidates:       []string{"sensitivity", "num_teams", "fight_club.min_rounds"},
		defaultObjective: decision.ObjectiveBalanced,
	}
	seen := map[string]int{}
	for i := 0; i < 3; i++ {
		intent, ok := d.Declare(nil)
		if !ok {
			t.Fatalf("declare %d returned no intent", i)
		}
		seen[intent.FlagKey]++
	}
	if len(seen) != 3 {
		t.Fatalf("rotation covered %d distinct flags in 3 calls, want 3: %v", len(seen), seen)
	}
	// A 4th call wraps back to the first flag (deterministic ring).
	intent, ok := d.Declare(nil)
	if !ok || intent.FlagKey != "sensitivity" {
		t.Fatalf("4th declare did not wrap to sensitivity, got %q ok=%v", intent.FlagKey, ok)
	}
}

// TestDynamicDeclarer_DedupSkipsActiveFlag asserts a flag with an ALREADY-ACTIVE
// experiment is skipped, so the declarer never stacks a second experiment on it.
func TestDynamicDeclarer_DedupSkipsActiveFlag(t *testing.T) {
	path := gameFileForDeclarer(t)
	d := &dynamicDeclarer{
		gameFlagPath:     path,
		candidates:       []string{"sensitivity", "num_teams"},
		defaultObjective: decision.ObjectiveBalanced,
	}
	// sensitivity is already under experiment ⇒ the declarer must pick num_teams.
	active := activeFlags{"sensitivity": {}}
	intent, ok := d.Declare(active)
	if !ok {
		t.Fatal("declare returned no intent despite a free candidate")
	}
	if intent.FlagKey != "num_teams" {
		t.Fatalf("declarer chose %q, want num_teams (sensitivity is active)", intent.FlagKey)
	}

	// With BOTH candidates active, nothing is declarable.
	active2 := activeFlags{"sensitivity": {}, "num_teams": {}}
	if _, ok := d.Declare(active2); ok {
		t.Fatal("declarer declared on an all-active candidate set; want no-op")
	}
}

// TestDynamicDeclarer_SkipsSingleVariantFlag asserts a flag with no alternate
// value (single variant) is never chosen — there is nothing to perturb toward.
func TestDynamicDeclarer_SkipsSingleVariantFlag(t *testing.T) {
	path := gameFileForDeclarer(t)
	// nonstop.scoring has exactly one variant in the fixture.
	d := &dynamicDeclarer{
		gameFlagPath:     path,
		candidates:       []string{"nonstop.scoring"},
		defaultObjective: decision.ObjectiveBalanced,
	}
	if _, ok := d.Declare(nil); ok {
		t.Fatal("declarer chose a single-variant flag; want no-op (nothing to perturb)")
	}
}

// TestDynamicDeclarer_PressureSelectsObjective asserts the highest-pressure
// experimentable objective is targeted when a pressure signal is available, and
// that chaos (non-experimentable) is ignored even at max pressure.
func TestDynamicDeclarer_PressureSelectsObjective(t *testing.T) {
	path := gameFileForDeclarer(t)
	d := &dynamicDeclarer{
		gameFlagPath:     path,
		candidates:       []string{"sensitivity"},
		defaultObjective: decision.ObjectiveBalanced,
		pressure: func() map[string]float64 {
			return map[string]float64{
				decision.ObjectiveChaos:      1.0, // highest, but NOT experimentable — ignored.
				decision.ObjectiveAccelerate: 0.7,
				decision.ObjectiveEndurance:  0.3,
			}
		},
	}
	intent, ok := d.Declare(nil)
	if !ok {
		t.Fatal("declare returned no intent")
	}
	if intent.Objective != decision.ObjectiveAccelerate {
		t.Fatalf("objective = %q, want accelerate (highest experimentable pressure)", intent.Objective)
	}
}

// TestDynamicDeclarer_NoPressureUsesDefaultObjective asserts the configured
// default objective is used when no pressure signal is present.
func TestDynamicDeclarer_NoPressureUsesDefaultObjective(t *testing.T) {
	path := gameFileForDeclarer(t)
	d := &dynamicDeclarer{
		gameFlagPath:     path,
		candidates:       []string{"sensitivity"},
		defaultObjective: decision.ObjectiveEndurance,
		pressure:         func() map[string]float64 { return nil }, // no signal
	}
	intent, _ := d.Declare(nil)
	if intent.Objective != decision.ObjectiveEndurance {
		t.Fatalf("objective = %q, want endurance (default, no pressure)", intent.Objective)
	}
}

// TestDynamicDeclarer_ReadErrorIsNoOp asserts an unreadable game.json fails closed
// (nothing declarable) rather than panicking or inventing a flag.
func TestDynamicDeclarer_ReadErrorIsNoOp(t *testing.T) {
	d := &dynamicDeclarer{
		gameFlagPath:     filepath.Join(t.TempDir(), "does-not-exist.json"),
		defaultObjective: decision.ObjectiveBalanced,
	}
	if _, ok := d.Declare(nil); ok {
		t.Fatal("declarer declared from an unreadable file; want fail-closed no-op")
	}
}

// --- loop-level wiring tests (declareDynamicIfEnabled against a real Registry) ---

// loopWithRealRegistry builds an experimentLoop with a REAL Registry (Gate/Writer
// over a temp game.json, recording spawner) so a test can exercise the actual
// declare→Start→active-flag bookkeeping the dynamic declarer relies on. maxShadow
// bounds the registry capacity (and thus clamps the dynamic concurrent backstop).
func loopWithRealRegistry(t *testing.T, maxShadow int) (*experimentLoop, string) {
	t.Helper()
	log := testLogger()
	gamePath := gameFileForDeclarer(t)
	writer := experiment.NewWriter(gamePath, log)
	gate := experiment.NewGate(writer, nil, log)
	targeting := experiment.NewGateTargetingWriter(gate, writer)
	reg := experiment.NewRegistry(experiment.RegistryConfig{
		Root:           t.TempDir(),
		MaxShadowGames: maxShadow,
		Spawner:        &recordingSpawner{},
		Verdict:        experiment.NewVerdict(3, 0.5, nil),
		Targeting:      targeting,
		Log:            log,
	})
	loop := &experimentLoop{
		registry: reg,
		log:      log,
		// #1044: the tick cadence is now resolved from the live config; tests drive
		// allocation explicitly, so a long bootstrap-default tick is ample. The
		// configOverride keeps the loop's gates ON so allocateIfEnabled-based tests
		// exercise the declare path (per-test overrides may flip it).
		expDefaults: flags.ExperimentDefaults{
			Enabled:                    true,
			DynamicEnabled:             true,
			Tick:                       time.Hour,
			DynamicMaxConcurrent:       defaultDynamicMaxConcurrent,
			ShadowEffectiveConcurrency: 4,
		},
	}
	loop.configOverride = func(context.Context) flags.ExperimentConfig {
		return flags.ExperimentConfig{
			Enabled:                    true,
			DynamicEnabled:             true,
			Tick:                       time.Hour,
			DynamicMaxConcurrent:       defaultDynamicMaxConcurrent,
			ShadowEffectiveConcurrency: 4,
		}
	}
	return loop, gamePath
}

// dynCfg is a test helper: a live ExperimentConfig with the loop + dynamic gates ON
// and the given concurrent-experiment budget (#1044). It replaces the former
// loop.dynamicMaxConcurrent field the declare-path tests set directly.
func dynCfg(maxConcurrent int) flags.ExperimentConfig {
	return flags.ExperimentConfig{
		Enabled:                    true,
		DynamicEnabled:             true,
		DynamicMaxConcurrent:       maxConcurrent,
		ShadowEffectiveConcurrency: 4,
	}
}

// TestDeclareDynamic_DeclaresAndStarts asserts the loop's hook declares a fresh
// experiment AND Starts it (writes shadow targeting → RUNNING), and that a second
// call dedups the now-active flag onto a different one.
func TestDeclareDynamic_DeclaresAndStarts(t *testing.T) {
	loop, path := loopWithRealRegistry(t, 8)
	loop.dynamic = &dynamicDeclarer{
		gameFlagPath:     path,
		candidates:       []string{"sensitivity", "num_teams"},
		defaultObjective: decision.ObjectiveBalanced,
	}
	cfg := dynCfg(5)

	loop.declareDynamicIfEnabled(context.Background(), cfg)
	if got := loop.registry.LiveCount(); got != 1 {
		t.Fatalf("after 1 dynamic declare LiveCount = %d, want 1", got)
	}
	active := loop.registry.ActiveFlags()
	if _, ok := active["sensitivity"]; !ok {
		t.Fatalf("first declare did not make sensitivity active: %v", active)
	}

	// Second call: sensitivity is now active ⇒ the declarer must pick num_teams.
	loop.declareDynamicIfEnabled(context.Background(), cfg)
	if got := loop.registry.LiveCount(); got != 2 {
		t.Fatalf("after 2 dynamic declares LiveCount = %d, want 2", got)
	}
	active = loop.registry.ActiveFlags()
	if _, ok := active["num_teams"]; !ok {
		t.Fatalf("second declare did not make num_teams active: %v", active)
	}
}

// TestDeclareDynamic_RespectsConcurrentBackstop asserts the declarer stops once the
// live set reaches the concurrent-experiment budget.
func TestDeclareDynamic_RespectsConcurrentBackstop(t *testing.T) {
	loop, path := loopWithRealRegistry(t, 8)
	loop.dynamic = &dynamicDeclarer{
		gameFlagPath:     path,
		candidates:       []string{"sensitivity", "num_teams", "fight_club.min_rounds"},
		defaultObjective: decision.ObjectiveBalanced,
	}
	cfg := dynCfg(2) // budget of 2 (well under capacity 8)

	for i := 0; i < 5; i++ {
		loop.declareDynamicIfEnabled(context.Background(), cfg)
	}
	if got := loop.registry.LiveCount(); got != 2 {
		t.Fatalf("declarer exceeded the concurrent backstop: LiveCount = %d, want 2", got)
	}
}

// TestDeclareDynamic_BackstopClampedToCapacity asserts the budget is clamped to the
// shadow capacity — a high budget cannot declare more experiments than there are
// shadow slots for.
func TestDeclareDynamic_BackstopClampedToCapacity(t *testing.T) {
	loop, path := loopWithRealRegistry(t, 1) // capacity 1
	loop.dynamic = &dynamicDeclarer{
		gameFlagPath:     path,
		candidates:       []string{"sensitivity", "num_teams", "fight_club.min_rounds"},
		defaultObjective: decision.ObjectiveBalanced,
	}
	cfg := dynCfg(99) // huge — but capacity clamps it to 1

	for i := 0; i < 5; i++ {
		loop.declareDynamicIfEnabled(context.Background(), cfg)
	}
	if got := loop.registry.LiveCount(); got != 1 {
		t.Fatalf("budget not clamped to capacity: LiveCount = %d, want 1", got)
	}
}

// TestDeclareDynamic_ConcurrentCallersRespectBudget fires declareDynamicIfEnabled
// from many goroutines at once — the REAL shape, since allocateIfEnabled is reached
// concurrently from the tick, per-partition onGameEnd, onGameTerminal drive and the
// grace-timer goroutines. Without the declarer mutex, concurrent callers all pass the
// LiveCount<budget check before any commits its Declare (TOCTOU over-declaration) and
// data-race d.cursor; with the mutex the whole select→budget-check→dedup→declare path
// is atomic, so the live experiment set never exceeds the budget. Run under -race to
// also catch the cursor race. (Reverting the mutex makes this fail: LiveCount > budget.)
func TestDeclareDynamic_ConcurrentCallersRespectBudget(t *testing.T) {
	loop, path := loopWithRealRegistry(t, 16) // ample capacity so the BUDGET is the bound
	loop.dynamic = &dynamicDeclarer{
		gameFlagPath:     path,
		candidates:       []string{"sensitivity", "num_teams", "fight_club.min_rounds"},
		defaultObjective: decision.ObjectiveBalanced,
	}
	const budget = 2
	cfg := dynCfg(budget) // well under capacity 16

	const goroutines = 8
	var wg sync.WaitGroup
	wg.Add(goroutines)
	for i := 0; i < goroutines; i++ {
		go func() {
			defer wg.Done()
			loop.declareDynamicIfEnabled(context.Background(), cfg)
		}()
	}
	wg.Wait()

	got := loop.registry.LiveCount()
	if got > budget {
		t.Fatalf("concurrent declarers over-declared past the budget: LiveCount = %d, want <= %d", got, budget)
	}
	if got < 1 {
		t.Fatalf("expected the concurrent declarers to declare at least one experiment, LiveCount = %d", got)
	}
}

// TestDeclareDynamic_KillSwitchOffDeclaresNothing asserts that with the agent
// kill-switch OFF (disabled), allocateIfEnabled never reaches the dynamic declarer
// — so nothing is declared. The declarer is subordinate to the SAME kill-switch as
// every other declaration (it is only called AFTER the enabled check).
func TestDeclareDynamic_KillSwitchOffDeclaresNothing(t *testing.T) {
	loop, path := loopWithRealRegistry(t, 8)
	loop.dynamic = &dynamicDeclarer{
		gameFlagPath:     path,
		candidates:       []string{"sensitivity"},
		defaultObjective: decision.ObjectiveBalanced,
	}
	loop.enabledOverride = func(context.Context) bool { return false } // kill-switch OFF

	loop.allocateIfEnabled(context.Background())
	if got := loop.registry.LiveCount(); got != 0 {
		t.Fatalf("kill-switch off must declare nothing, LiveCount = %d", got)
	}

	// Flip the kill-switch ON: now the declarer runs and declares.
	loop.enabledOverride = func(context.Context) bool { return true }
	loop.allocateIfEnabled(context.Background())
	if got := loop.registry.LiveCount(); got != 1 {
		t.Fatalf("kill-switch on should let the declarer declare, LiveCount = %d", got)
	}
}
