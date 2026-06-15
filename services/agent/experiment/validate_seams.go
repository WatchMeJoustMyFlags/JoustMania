package experiment

import (
	"context"
	"fmt"
)

// validate_seams.go provides the PRODUCTION implementations of the M7-7 Validator's
// fitness-reading seams (#965), backed by the finalized-per-game FitnessStore
// (fitness_store.go). They replace the validate_test.go fakes for the live wiring:
//
//   - StoreBaseline   → Baseline.Fitness: the mean fitness of the last N REAL
//     (game_kind="real") games for the proposal's objective. This is the #965
//     "baseline established from the last N real games" seam, reading FINALIZED
//     samples (never a live scrape that races the span flush, per #958/#1017).
//   - StoreMeasurer   → FitnessMeasurer.Measure: the mean finalized fitness of the
//     shadow games a Run produced, read back by their game_ids.
//   - WriterReverter  → Reverter.Revert: strips the experiment's shadow targeting
//     branch via the #977 Writer (the inverse of the Gate's Apply).
//
// The SyntheticRunner is the one seam that lives in package main (shadow_validation_runner.go):
// it drives real shadow games via the agent's gamerunner/registry, which package
// experiment cannot import. The runner returns a Run whose ID carries the spawned
// shadow game_ids; the StoreMeasurer joins those ids against this store.

// ObjectiveResolver maps a Proposal to the fitness objective its experiment
// optimizes. A Proposal carries only {flag, value, rationale}; the objective lives
// on the experiment Intent (registry CompactView keyed by ExperimentID). Injecting
// it as a func keeps StoreBaseline free of a registry dependency and unit-testable.
// An empty objective ("") means "any objective" — the baseline then folds whatever
// real-game samples exist (the loop's defaultGameFitness already maps an empty
// objective to balanced, so a real sample always carries a concrete objective).
type ObjectiveResolver func(p Proposal) string

// StoreBaseline implements Baseline over a FitnessStore: the validation baseline is
// the mean finalized fitness of the last N REAL games for the proposal's objective.
type StoreBaseline struct {
	store      *FitnessStore
	objective  ObjectiveResolver
	recentN    int
	minSamples int
}

// NewStoreBaseline builds the store-backed baseline. recentN is how many recent
// real games to average (clamped to >= 1). minSamples is the minimum real samples
// required before a baseline is trustworthy; below it Fitness FAILS CLOSED (returns
// an error) so the Validator never promotes against a baseline of too-few real
// games (epic #982: fitness is observed, never assumed). objective may be nil ⇒ the
// baseline folds any-objective real samples.
func NewStoreBaseline(store *FitnessStore, objective ObjectiveResolver, recentN, minSamples int) *StoreBaseline {
	if recentN < 1 {
		recentN = 1
	}
	if minSamples < 1 {
		minSamples = 1
	}
	return &StoreBaseline{store: store, objective: objective, recentN: recentN, minSamples: minSamples}
}

// Fitness returns the mean finalized fitness of the last recentN real games for
// p's objective. It FAILS CLOSED (error) when the store is unwired or fewer than
// minSamples real samples exist — without a trustworthy baseline there is nothing
// to compare an experiment against, so the cycle must not promote.
func (b *StoreBaseline) Fitness(_ context.Context, p Proposal) (float64, error) {
	if b.store == nil {
		return 0, fmt.Errorf("baseline: fitness store not wired")
	}
	objective := ""
	if b.objective != nil {
		objective = b.objective(p)
	}
	samples := b.store.RecentReal(objective, b.recentN)
	if len(samples) < b.minSamples {
		return 0, fmt.Errorf("baseline: only %d real-game fitness samples for objective %q, need >= %d",
			len(samples), objective, b.minSamples)
	}
	var sum float64
	for _, s := range samples {
		sum += s.Fitness
	}
	return sum / float64(len(samples)), nil
}

// StoreMeasurer implements FitnessMeasurer over a FitnessStore: the synthetic Run's
// fitness is the mean of its shadow games' finalized fitness, read back by game_id.
type StoreMeasurer struct {
	store *FitnessStore
}

// NewStoreMeasurer builds the store-backed measurer.
func NewStoreMeasurer(store *FitnessStore) *StoreMeasurer {
	return &StoreMeasurer{store: store}
}

// Measure returns the mean finalized fitness of the Run's shadow games. The Run's
// ID is the comma-joined shadow game_ids (RunGameIDs decodes them). It FAILS CLOSED
// when the store is unwired or NONE of the run's games has a finalized sample (an
// unmeasurable run must never promote). Games that are missing a sample (e.g. a
// shadow game that errored without folding fitness) are skipped; the mean is over
// the games that did finalize, so a partial batch still measures honestly.
func (m *StoreMeasurer) Measure(_ context.Context, run Run) (float64, error) {
	if m.store == nil {
		return 0, fmt.Errorf("measure: fitness store not wired")
	}
	ids := RunGameIDs(run)
	if len(ids) == 0 {
		return 0, fmt.Errorf("measure: run %q carries no shadow game ids", run.ID)
	}
	var sum float64
	found := 0
	for _, id := range ids {
		if s, ok := m.store.Get(id); ok {
			sum += s.Fitness
			found++
		}
	}
	if found == 0 {
		return 0, fmt.Errorf("measure: none of run %q's %d shadow games has a finalized fitness sample", run.ID, len(ids))
	}
	return sum / float64(found), nil
}

// WriterReverter implements Reverter over the #977 Writer: a degradation rollback
// strips the experiment's shadow targeting branch (the inverse of the Gate's
// Apply). Removing a shadow branch only ever restores a strictly-more-real-
// protective rule, so it needs no invariant gate (design §10) — straight to the
// Writer's Strip, mirroring GateTargetingWriter.Strip.
type WriterReverter struct {
	writer *Writer
}

// NewWriterReverter builds the Writer-backed reverter. writer MUST be the same one
// the experiment's targeting was written through so Strip finds the branch.
func NewWriterReverter(writer *Writer) *WriterReverter {
	return &WriterReverter{writer: writer}
}

// Revert strips p's experiment-scoped shadow targeting branch + reserved variant.
// A missing branch is a no-op (idempotent). It satisfies Reverter.
func (r *WriterReverter) Revert(_ context.Context, p Proposal) error {
	if r.writer == nil {
		return fmt.Errorf("revert: writer not wired")
	}
	return r.writer.Strip(p.FlagKey, p.ExperimentID)
}
