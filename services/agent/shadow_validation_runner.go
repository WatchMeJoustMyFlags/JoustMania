package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/joustmania/agent/experiment"
)

// shadow_validation_runner.go is the REAL SyntheticRunner seam (#965) the M7-7
// Validator (experiment/validate.go) injects — the production replacement for the
// validate_test.go fakeRunner.
//
// WHAT IT DOES (the #965 live wiring): given a validated Proposal whose experiment
// is ALREADY written shadow-scoped (the Gate accepted it — the Validator's
// precondition), it drives `games` REAL shadow games on the live stack through the
// agent's existing shadow-game machinery (the #778 gamerunner via the #976
// shadowSpawner), bound to the proposal's experiment_id + the EXPERIMENTAL arm so
// each game resolves the candidate variant (the targeting #977 routes it). It then
// AWAITS each game's finalized fitness in the #965 FitnessStore and returns a Run
// whose ID carries the spawned game_ids — which the StoreMeasurer reads back.
//
// HOW IT REUSES THE SESSION'S SHADOW MACHINERY:
//   - shadowSpawner.Spawn starts ONE experiment-bound shadow game and returns its
//     coordinator game_id promptly; the game then plays to completion in a
//     background goroutine and its fitness flows through telemetry → onGameEnd, the
//     SAME path every cohort game uses. The runner does NOT re-implement game
//     driving — it reuses the spawner verbatim.
//   - onGameEnd Records the finalized per-game fitness into the FitnessStore keyed
//     by game_id (the #1017 instrumentation). The runner waits for those records to
//     appear, so "synthetic run + measure" is the spawner + the store, end to end.
//
// SCOPED vs DEFERRED (#965): the runner spawns + awaits REAL shadow games and reads
// REAL computed fitness — the substrate the issue asks for. It is invoked ONLY from
// the autonomous-promotion path, which is itself gated and currently FAIL-CLOSED
// (#1016), so wiring it adds no new privilege. Driving real PRIMARY games / the live
// LLM proposer is #992 / the live loop's territory, NOT here.

// DefaultValidationGameTimeout bounds how long the runner waits for ALL spawned
// shadow games to finalize their fitness in the store before giving up on the
// stragglers. Shadow games are minutes-long and run sequentially under the
// single-game coordinator, so the budget must comfortably exceed games ×
// per-game-duration; it is generous but bounded so a stuck game can never wedge the
// validation cycle. Tunable via AGENT_VALIDATION_GAME_TIMEOUT_SECONDS.
const DefaultValidationGameTimeout = 10 * time.Minute

// DefaultValidationPollInterval is how often the runner re-checks the store for a
// spawned game's finalized fitness while awaiting completion.
const DefaultValidationPollInterval = 500 * time.Millisecond

// shadowValidationRunner is the SyntheticRunner backed by the real shadow spawner +
// the finalized-per-game fitness store. It depends on the experiment.ShadowSpawner
// INTERFACE (which the production shadowSpawner satisfies) so it is unit-testable
// with a recording fake spawner.
type shadowValidationRunner struct {
	spawner experiment.ShadowSpawner
	store   *experiment.FitnessStore
	log     *slog.Logger

	// gameTimeout bounds the await for all spawned games' fitness; pollInterval is
	// the store re-check cadence. Injected so tests drive completion without real
	// sleeps.
	gameTimeout  time.Duration
	pollInterval time.Duration

	// now/sleep are injected for deterministic tests; production uses the wall clock.
	now   func() time.Time
	sleep func(context.Context, time.Duration) error
}

// newShadowValidationRunner builds the runner over the shared shadow spawner and
// fitness store. log nil ⇒ slog.Default().
func newShadowValidationRunner(spawner experiment.ShadowSpawner, store *experiment.FitnessStore, gameTimeout time.Duration, log *slog.Logger) *shadowValidationRunner {
	if log == nil {
		log = slog.Default()
	}
	if gameTimeout <= 0 {
		gameTimeout = DefaultValidationGameTimeout
	}
	return &shadowValidationRunner{
		spawner:      spawner,
		store:        store,
		log:          log,
		gameTimeout:  gameTimeout,
		pollInterval: DefaultValidationPollInterval,
		now:          func() time.Time { return time.Now() },
		sleep:        sleepCtx,
	}
}

// Run spawns `games` experimental-arm shadow games for p's experiment, awaits their
// finalized fitness in the store, and returns a Run carrying the spawned game_ids.
// It satisfies experiment.SyntheticRunner.
//
// FAIL CLOSED: if NO game can be spawned at all, Run errors so the Validator
// discards (never promotes on no evidence). If SOME games spawn but not all
// finalize within the timeout, the Run still returns the ids that DID finalize — a
// smaller-but-valid sample the Validator records as the realized game count; the
// StoreMeasurer averages only the games that produced a sample.
func (r *shadowValidationRunner) Run(ctx context.Context, p experiment.Proposal, games int) (experiment.Run, error) {
	if r.spawner == nil {
		return experiment.Run{}, fmt.Errorf("synthetic run: shadow spawner not wired")
	}
	if r.store == nil {
		return experiment.Run{}, fmt.Errorf("synthetic run: fitness store not wired")
	}
	if p.ExperimentID == "" {
		// Without an experiment id the spawned games carry no experimental-arm binding,
		// so the targeting (#977) would resolve the DEFAULT variant and the "after"
		// fitness would not reflect the candidate at all. Refuse rather than measure a
		// non-experimental run.
		return experiment.Run{}, fmt.Errorf("synthetic run: proposal has no experiment id (cannot bind experimental arm)")
	}
	if games < 1 {
		games = 1
	}

	spawned := make([]string, 0, games)
	for i := 0; i < games; i++ {
		// seed 0 = entropy: the validation batch is not a CRN-paired cohort (it measures
		// the experimental arm against the recent-real baseline, not a within-batch
		// control), so each game gets independent entropy.
		gameID, err := r.spawner.Spawn(ctx, p.ExperimentID, experiment.ArmExperimental, 0)
		if err != nil {
			if errors.Is(err, experiment.ErrSpawnBackpressure) {
				// Coordinator at capacity: not a failure. Wait a beat and retry the SAME
				// slot so the batch still reaches `games` games rather than under-sampling.
				r.log.Debug("synthetic run: spawn backpressure, retrying",
					"experiment_id", p.ExperimentID, "spawned_so_far", len(spawned))
				if werr := r.sleep(ctx, r.pollInterval); werr != nil {
					break // ctx cancelled while waiting on backpressure.
				}
				i--
				continue
			}
			r.log.Warn("synthetic run: shadow game spawn failed",
				"experiment_id", p.ExperimentID, "spawned_so_far", len(spawned), "error", err)
			break
		}
		spawned = append(spawned, gameID)
	}

	if len(spawned) == 0 {
		return experiment.Run{}, fmt.Errorf("synthetic run: no shadow game could be spawned for experiment %s", p.ExperimentID)
	}

	// Await each spawned game's finalized fitness in the store (the #965/#1017
	// settled-sample contract). Games that never finalize within the timeout are
	// dropped from the returned ids so the measurer never blocks on a stuck game.
	finalized := r.awaitFinalized(ctx, spawned)
	if len(finalized) == 0 {
		return experiment.Run{}, fmt.Errorf("synthetic run: none of %d spawned shadow games finalized fitness within %s",
			len(spawned), r.gameTimeout)
	}
	r.log.Info("synthetic run: shadow validation batch complete",
		"experiment_id", p.ExperimentID, "spawned", len(spawned), "finalized", len(finalized))
	return experiment.NewRun(finalized), nil
}

// awaitFinalized polls the store until every spawned game has a finalized fitness
// sample or the game timeout elapses, returning the ids that finalized. It returns
// promptly once all games are in; the timeout only bites on a stuck/errored game.
func (r *shadowValidationRunner) awaitFinalized(ctx context.Context, spawned []string) []string {
	deadline := r.now().Add(r.gameTimeout)
	for {
		done := make([]string, 0, len(spawned))
		for _, id := range spawned {
			if _, ok := r.store.Get(id); ok {
				done = append(done, id)
			}
		}
		if len(done) == len(spawned) {
			return done // all finalized.
		}
		if !r.now().Before(deadline) {
			r.log.Warn("synthetic run: validation timed out awaiting fitness; using games finalized so far",
				"spawned", len(spawned), "finalized", len(done), "timeout", r.gameTimeout)
			return done
		}
		if err := r.sleep(ctx, r.pollInterval); err != nil {
			// ctx cancelled (agent shutdown): return whatever finalized so far.
			return done
		}
	}
}

// sleepCtx sleeps for d or returns early with ctx.Err() if ctx is cancelled first.
func sleepCtx(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}
