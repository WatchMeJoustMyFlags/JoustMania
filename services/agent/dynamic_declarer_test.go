package main

import (
	"context"
	"errors"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/flagservice"
	"github.com/joustmania/agent/flagservice/fake"
)

// dynamic_declarer_test.go is the #995 acceptance suite for the HEURISTIC dynamic
// experiment declarer:
//   - it picks a valid, Gate-passing Intent from game.json flags (existing flag,
//     type-compatible value, experimentable objective);
//   - default-off: with experiment_dynamic_enabled off or unresolved, NO dynamic
//     declaration happens (seed-only behavior byte-identical) — the regression guard;
//   - capacity/dedup: it won't declare a second experiment on a flag that already
//     has an active one, nor beyond the concurrent-experiment backstop;
//   - rotation: successive declarations cover DIFFERENT flags (deterministic cursor);
//   - kill-switch off ⇒ the declarer declares nothing.

// gameServiceForDeclarer serves the experiment package's game.json fixture from
// a fake flag service with the real registry's game policies (the experiment
// catalog the declarer reads candidate flags from).
func gameServiceForDeclarer(t *testing.T) *fake.Service {
	t.Helper()
	return gameServiceFrom(t, filepath.Join("experiment", "testdata", "game.json"))
}

func gameServiceFrom(t *testing.T, baseline string) *fake.Service {
	t.Helper()
	policies, err := fake.RegistryPolicies(filepath.Join("..", "flag-service", "registry.yaml"), experiment.GameFlagSet)
	if err != nil {
		t.Fatalf("read registry: %v", err)
	}
	svc := fake.New()
	if err := svc.LoadSetWithPolicies(experiment.GameFlagSet, baseline, policies); err != nil {
		t.Fatalf("load %s: %v", baseline, err)
	}
	return svc
}

// TestDynamicDeclarer_PicksGatePassingIntent asserts the declarer's chosen Intent
// (flag + experimental value) is accepted by the REAL Gate: the flag pre-exists,
// the value is type-compatible (it is one of the flag's own variant values), and
// the objective is experimentable.
func TestDynamicDeclarer_PicksGatePassingIntent(t *testing.T) {
	svc := gameServiceForDeclarer(t)
	d := &dynamicDeclarer{
		gameFlags:        svc,
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
	writer := experiment.NewWriter(svc, testLogger())
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

// TestDynamicDeclarer_GatePassesObjectFlag asserts the chosen value for an
// object-valued catalog flag with pre-existing targeting is ALSO accepted by the
// Gate, and that a flag outside the experiment catalog (the bool
// random_assignment) is never offered.
func TestDynamicDeclarer_GatePassesObjectFlag(t *testing.T) {
	svc := gameServiceForDeclarer(t)
	outside := &dynamicDeclarer{gameFlags: svc, candidates: []string{"random_assignment"}, defaultObjective: decision.ObjectiveBalanced}
	if intent, ok := outside.Declare(nil); ok {
		t.Fatalf("declarer offered a flag outside the catalog: %+v", intent)
	}
	for _, flagKey := range []string{"windows"} {
		d := &dynamicDeclarer{
			gameFlags:        svc,
			candidates:       []string{flagKey},
			defaultObjective: decision.ObjectiveBalanced,
		}
		intent, ok := d.Declare(nil)
		if !ok {
			t.Fatalf("%s: no intent", flagKey)
		}
		writer := experiment.NewWriter(svc, testLogger())
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
// a no-op — the seed-only path is byte-identical. It also asserts a nil declarer is
// safe (the test/degraded wiring).
func TestDynamicDeclarer_DisabledByDefault(t *testing.T) {
	// The declarer is built regardless of the opt-in now (gating moved to the LIVE
	// flag, checked per tick).
	if d := newDynamicDeclarer(nil, nil); d == nil {
		t.Fatal("declarer must always be constructed (#1044) — gating is per-tick via the live flag")
	}
	// A nil declarer is a no-op (test/degraded wiring): no panic, no work.
	loop := &experimentLoop{dynamic: nil, log: testLogger()}
	loop.declareDynamicIfEnabled(context.Background(), flags.ExperimentConfig{DynamicEnabled: true})
	// A wired declarer with DynamicEnabled=false (the live flag off) is ALSO a no-op
	// even though the declarer is present — proving the per-tick flag gate.
	loop.dynamic = &dynamicDeclarer{gameFlags: gameServiceForDeclarer(t)}
	loop.declareDynamicIfEnabled(context.Background(), flags.ExperimentConfig{DynamicEnabled: false})
}

// TestDynamicDeclarer_ConfigureFromFlags asserts configure applies the per-tick
// experiment_dynamic_* settings (#1907): the candidate list, the default objective
// (a non-experimentable one falls back to balanced) and the target N (negative = 0).
func TestDynamicDeclarer_ConfigureFromFlags(t *testing.T) {
	d := newDynamicDeclarer(nil, nil)
	d.configure(flags.ExperimentConfig{
		DynamicCandidates: "sensitivity, num_teams ,",
		DynamicObjective:  " endurance ",
		DynamicTargetN:    5,
	})
	if got := d.candidates; len(got) != 2 || got[0] != "sensitivity" || got[1] != "num_teams" {
		t.Fatalf("candidates = %v, want [sensitivity num_teams]", got)
	}
	if d.defaultObjective != decision.ObjectiveEndurance {
		t.Fatalf("objective = %q, want endurance", d.defaultObjective)
	}
	if d.targetN != 5 {
		t.Fatalf("targetN = %d, want 5", d.targetN)
	}
	for _, objective := range []string{decision.ObjectiveChaos, "", "bogus"} {
		d.configure(flags.ExperimentConfig{DynamicObjective: objective, DynamicTargetN: -3})
		if d.defaultObjective != decision.ObjectiveBalanced || d.targetN != 0 {
			t.Fatalf("objective %q / target -3 gave %q / %d, want balanced / 0", objective, d.defaultObjective, d.targetN)
		}
	}
	// A later tick with the flags back at their defaults clears the earlier settings.
	if d.candidates != nil {
		t.Fatalf("candidates = %v after an empty flag, want nil (every tunable flag)", d.candidates)
	}
}

// TestDynamicDeclarer_Rotation asserts successive Declares cover DIFFERENT flags
// via the deterministic cursor (no repeat until the candidate ring is exhausted).
func TestDynamicDeclarer_Rotation(t *testing.T) {
	svc := gameServiceForDeclarer(t)
	d := &dynamicDeclarer{
		gameFlags:        svc,
		candidates:       []string{"sensitivity", "nonstop.respawn_seconds", "fight_club.round_seconds"},
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
	svc := gameServiceForDeclarer(t)
	d := &dynamicDeclarer{
		gameFlags:        svc,
		candidates:       []string{"sensitivity", "nonstop.respawn_seconds"},
		defaultObjective: decision.ObjectiveBalanced,
	}
	// sensitivity is already under experiment ⇒ the declarer must pick nonstop.respawn_seconds.
	active := activeFlags{"sensitivity": {}}
	intent, ok := d.Declare(active)
	if !ok {
		t.Fatal("declare returned no intent despite a free candidate")
	}
	if intent.FlagKey != "nonstop.respawn_seconds" {
		t.Fatalf("declarer chose %q, want nonstop.respawn_seconds (sensitivity is active)", intent.FlagKey)
	}

	// With BOTH candidates active, nothing is declarable.
	active2 := activeFlags{"sensitivity": {}, "nonstop.respawn_seconds": {}}
	if _, ok := d.Declare(active2); ok {
		t.Fatal("declarer declared on an all-active candidate set; want no-op")
	}
}

// TestDynamicDeclarer_SkipsSingleVariantFlag asserts a flag with no alternate
// value (single variant) is never chosen — there is nothing to perturb toward.
func TestDynamicDeclarer_SkipsSingleVariantFlag(t *testing.T) {
	svc := gameServiceForDeclarer(t)
	// nonstop.scoring has exactly one variant in the fixture.
	d := &dynamicDeclarer{
		gameFlags:        svc,
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
	svc := gameServiceForDeclarer(t)
	d := &dynamicDeclarer{
		gameFlags:        svc,
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
	svc := gameServiceForDeclarer(t)
	d := &dynamicDeclarer{
		gameFlags:        svc,
		candidates:       []string{"sensitivity"},
		defaultObjective: decision.ObjectiveEndurance,
		pressure:         func() map[string]float64 { return nil }, // no signal
	}
	intent, _ := d.Declare(nil)
	if intent.Objective != decision.ObjectiveEndurance {
		t.Fatalf("objective = %q, want endurance (default, no pressure)", intent.Objective)
	}
}

// failingGameFlags is a flag service that does not answer.
type failingGameFlags struct{}

func (failingGameFlags) GetSet(context.Context, string) (flagservice.SetView, error) {
	return flagservice.SetView{}, errors.New("flag service unreachable")
}

// TestDynamicDeclarer_ReadErrorIsNoOp asserts an unreachable flag service fails closed
// (nothing declarable) rather than panicking or inventing a flag.
func TestDynamicDeclarer_ReadErrorIsNoOp(t *testing.T) {
	d := &dynamicDeclarer{
		gameFlags:        failingGameFlags{},
		defaultObjective: decision.ObjectiveBalanced,
	}
	if _, ok := d.Declare(nil); ok {
		t.Fatal("declarer declared without the flag service; want fail-closed no-op")
	}
}

// --- loop-level wiring tests (declareDynamicIfEnabled against a real Registry) ---

// loopWithRealRegistry builds an experimentLoop with a REAL Registry (Gate/Writer
// over a temp game.json, recording spawner) so a test can exercise the actual
// declare→Start→active-flag bookkeeping the dynamic declarer relies on. maxShadow
// bounds the registry capacity (and thus clamps the dynamic concurrent backstop).
func loopWithRealRegistry(t *testing.T, maxShadow int) (*experimentLoop, *fake.Service) {
	t.Helper()
	log := testLogger()
	svc := gameServiceForDeclarer(t)
	writer := experiment.NewWriter(svc, log)
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
	return loop, svc
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
		// The proposer gate's flag half (#1810); the env half is set per test.
		ProposerEnabled: true,
	}
}

// withSeed makes the loop's live config carry the resolved experiment_seed
// object (#1907); nil = the flag could not be resolved.
func withSeed(loop *experimentLoop, seed any) {
	base := loop.configOverride
	loop.configOverride = func(ctx context.Context) flags.ExperimentConfig {
		cfg := base(ctx)
		cfg.Seed = seed
		return cfg
	}
}

// testSeed is a valid experiment_seed object as flagd resolves it (numbers as
// float64, objective balanced).
func testSeed(flagKey string, value any, targetN int) map[string]any {
	return map[string]any{
		"flag": flagKey, "value": value, "objective": decision.ObjectiveBalanced,
		"target_n": float64(targetN), "hypothesis": "h",
	}
}

// withCandidates makes the loop's live config (the configOverride seam) carry the
// experiment_dynamic_candidates value keys (#1907: the declarer takes its settings
// from the per-tick config, never from its struct fields).
func withCandidates(loop *experimentLoop, keys string) {
	base := loop.configOverride
	loop.configOverride = func(ctx context.Context) flags.ExperimentConfig {
		cfg := base(ctx)
		cfg.DynamicCandidates = keys
		return cfg
	}
}

// TestDeclareDynamic_DeclaresAndStarts asserts the loop's hook declares a fresh
// experiment AND Starts it (writes shadow targeting → RUNNING), and that a second
// call dedups the now-active flag onto a different one.
func TestDeclareDynamic_DeclaresAndStarts(t *testing.T) {
	loop, svc := loopWithRealRegistry(t, 8)
	loop.dynamic = newDynamicDeclarer(svc, nil)
	cfg := dynCfg(5)
	cfg.DynamicCandidates = "sensitivity,nonstop.respawn_seconds"

	loop.declareDynamicIfEnabled(context.Background(), cfg)
	if got := loop.registry.LiveCount(); got != 1 {
		t.Fatalf("after 1 dynamic declare LiveCount = %d, want 1", got)
	}
	active := loop.registry.ActiveFlags()
	if _, ok := active["sensitivity"]; !ok {
		t.Fatalf("first declare did not make sensitivity active: %v", active)
	}

	// Second call: sensitivity is now active ⇒ the declarer must pick nonstop.respawn_seconds.
	loop.declareDynamicIfEnabled(context.Background(), cfg)
	if got := loop.registry.LiveCount(); got != 2 {
		t.Fatalf("after 2 dynamic declares LiveCount = %d, want 2", got)
	}
	active = loop.registry.ActiveFlags()
	if _, ok := active["nonstop.respawn_seconds"]; !ok {
		t.Fatalf("second declare did not make nonstop.respawn_seconds active: %v", active)
	}
}

// TestDeclareDynamic_RespectsConcurrentBackstop asserts the declarer stops once the
// live set reaches the concurrent-experiment budget.
func TestDeclareDynamic_RespectsConcurrentBackstop(t *testing.T) {
	loop, svc := loopWithRealRegistry(t, 8)
	loop.dynamic = newDynamicDeclarer(svc, nil)
	cfg := dynCfg(2) // budget of 2 (well under capacity 8)
	cfg.DynamicCandidates = "sensitivity,nonstop.respawn_seconds,fight_club.round_seconds"

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
	loop, svc := loopWithRealRegistry(t, 1) // capacity 1
	loop.dynamic = newDynamicDeclarer(svc, nil)
	cfg := dynCfg(99) // huge — but capacity clamps it to 1
	cfg.DynamicCandidates = "sensitivity,nonstop.respawn_seconds,fight_club.round_seconds"

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
	loop, svc := loopWithRealRegistry(t, 16) // ample capacity so the BUDGET is the bound
	loop.dynamic = newDynamicDeclarer(svc, nil)
	const budget = 2
	cfg := dynCfg(budget) // well under capacity 16
	cfg.DynamicCandidates = "sensitivity,nonstop.respawn_seconds,fight_club.round_seconds"

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
	loop, svc := loopWithRealRegistry(t, 8)
	loop.dynamic = newDynamicDeclarer(svc, nil)
	withCandidates(loop, "sensitivity")
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

// TestDeclareDynamic_TalkFallbackKeepsAGameInProgress: in the talk's screen-only
// fallback a game must always be in progress. The dynamic declarer keeps declaring
// after the kill-switch beat or a `fallback off` aborted every experiment, while
// the experiment seed (the experiment_seed flag) left set is not declared again
// (the latch keeps the last value until `off` is read).
// UC: UC-070 A9 BR-007
func TestDeclareDynamic_TalkFallbackKeepsAGameInProgress(t *testing.T) {
	ctx := context.Background()
	declarer := func(svc *fake.Service) *dynamicDeclarer { return newDynamicDeclarer(svc, nil) }

	t.Run("UC-070/BR-007 the declarer re-declares after the kill switch aborted the experiments", func(t *testing.T) {
		loop, svc := loopWithRealRegistry(t, 8)
		loop.dynamic = declarer(svc)
		withCandidates(loop, "sensitivity")
		enabled := true
		loop.enabledOverride = func(context.Context) bool { return enabled }

		loop.allocateIfEnabled(ctx)
		if got := loop.registry.LiveCount(); got != 1 {
			t.Fatalf("first tick LiveCount = %d, want 1", got)
		}
		enabled = false // the kill-switch beat
		loop.allocateIfEnabled(ctx)
		if got := loop.registry.LiveCount(); got != 0 {
			t.Fatalf("kill switch off LiveCount = %d, want 0 (aborted)", got)
		}
		enabled = true // the presenter switches the agent back on
		loop.allocateIfEnabled(ctx)
		if got := loop.registry.LiveCount(); got != 1 {
			t.Fatalf("after switching back on LiveCount = %d, want 1 (re-declared)", got)
		}
		if _, ok := loop.registry.ActiveFlags()["sensitivity"]; !ok {
			t.Fatalf("re-declared experiment not on sensitivity: %v", loop.registry.ActiveFlags())
		}
	})

	t.Run("UC-070/BR-007 the declarer re-declares after fallback off and on", func(t *testing.T) {
		loop, svc := loopWithRealRegistry(t, 8)
		loop.dynamic = declarer(svc)
		on := dynCfg(defaultDynamicMaxConcurrent)
		on.DynamicCandidates = "sensitivity"
		cfg := on
		loop.configOverride = func(context.Context) flags.ExperimentConfig { return cfg }

		loop.allocateIfEnabled(ctx)
		cfg = flags.ExperimentConfig{Enabled: false} // fallback off: experiments_enabled off
		loop.allocateIfEnabled(ctx)
		if got := loop.registry.LiveCount(); got != 0 {
			t.Fatalf("fallback off LiveCount = %d, want 0 (aborted)", got)
		}
		cfg = on // fallback on again
		loop.allocateIfEnabled(ctx)
		if got := loop.registry.LiveCount(); got != 1 {
			t.Fatalf("fallback on again LiveCount = %d, want 1 (re-declared)", got)
		}
	})

	t.Run("UC-070/BR-007 the experiment seed is declared once per agent process", func(t *testing.T) {
		loop, svc := loopWithRealRegistry(t, 8)
		loop.dynamic = declarer(svc)
		withCandidates(loop, "sensitivity")
		withSeed(loop, testSeed("nonstop.respawn_seconds", 6.0, 50))
		enabled := true
		loop.enabledOverride = func(context.Context) bool { return enabled }

		loop.allocateIfEnabled(ctx)
		active := loop.registry.ActiveFlags()
		if _, ok := active["nonstop.respawn_seconds"]; !ok {
			t.Fatalf("first tick did not declare the experiment seed: %v", active)
		}
		if _, ok := active["sensitivity"]; !ok {
			t.Fatalf("first tick did not declare the dynamic experiment: %v", active)
		}
		enabled = false
		loop.allocateIfEnabled(ctx)
		enabled = true
		loop.allocateIfEnabled(ctx)
		active = loop.registry.ActiveFlags()
		if _, ok := active["nonstop.respawn_seconds"]; ok {
			t.Fatalf("the experiment seed was declared again after the kill switch: %v", active)
		}
		if _, ok := active["sensitivity"]; !ok {
			t.Fatalf("the dynamic declarer did not re-declare: %v", active)
		}
	})
}

// TestDynamicDeclarer_DefaultRotationSkipsPlumbingFlags asserts the default
// rotation over the SHIPPED game.json files never offers a plumbing, lobby/admin
// or ceremony-timing flag (#1316), while game-tuning flags stay candidates.
// UC: UC-070 A9 BR-008
func TestDynamicDeclarer_DefaultRotationSkipsPlumbingFlags(t *testing.T) {
	plumbing := []string{
		"shadow_policy", "max_concurrent_games", "max_shadow_games", "tempo_schedule_mode",
		"force_all_start", "force_all_start_seconds", "countdown_phase_duration_ms",
		"winner_rainbow_duration_ms",
	}
	for _, shipped := range []string{"../flagd/ci/game.json", "../flagd/game.json"} {
		t.Run("UC-070/BR-008 "+shipped, func(t *testing.T) {
			d := &dynamicDeclarer{gameFlags: gameServiceFrom(t, shipped), defaultObjective: decision.ObjectiveBalanced}
			flagsRead := d.readCandidateFlags()
			if len(flagsRead) == 0 {
				t.Fatalf("no candidate flags read from %s", shipped)
			}
			order := map[string]bool{}
			for _, k := range d.candidateOrder(flagsRead) {
				order[k] = true
			}
			for _, k := range plumbing {
				if order[k] {
					t.Errorf("plumbing flag %q is a dynamic candidate", k)
				}
			}
			if !order["sensitivity"] {
				t.Errorf("game-tuning flag sensitivity missing from the rotation: %v", order)
			}
		})
	}
}
