package promote

import (
	"context"
	"fmt"
	"log/slog"
)

// watcher.go is the autonomous-promotion SAFETY NET (#1016): after promoteAutonomous
// applies a validated change to the REAL default, the fitnessWatcher observes
// real-game fitness for a window and reports DEGRADATION vs the pre-promotion
// baseline, so the Promoter can auto-revert (the OutcomeRevert path) before a
// regression rides on real players for long.
//
// WHY THIS IS THE CRITICAL GATE (#1016): promoting to real players with no
// auto-revert is the single most dangerous gap in the loop. Today it is masked only
// because autonomous is default-off, env-gated, kill-switched, and a human merges —
// none of which survive going lights-out. This watcher is what makes an autonomous
// apply self-correcting: it watches the SAME real-game fitness signal the agent
// already aggregates (#731 / gamewindow), compares a recent window to the baseline
// the promotion was justified against, and trips the revert when fitness drops below
// baseline by a margin over a minimum number of real games (so one unlucky game
// never reverts a good change).
//
// SEAM SPLIT (mirrors the Validator / RealDefaultWriter): the watcher owns ONLY the
// degradation-decision logic. Reading real-game fitness is the injected
// RealGameFitness seam — a fake in tests (no real games exist there), the #731
// evaluator / gamewindow in production (a documented follow-up to wire; see
// RealGameFitness). The promote package stays free of an OpenFeature / coordinator
// dependency.

// WatchPolicy is the degradation policy the fitnessWatcher judges by. All thresholds
// are configurable; DefaultWatchPolicy supplies sane defaults so a fully-wired
// autonomous path has a safety net without per-call tuning.
type WatchPolicy struct {
	// MinGames is the minimum number of real-game fitness samples that must be
	// observed before any degradation verdict is rendered. Below this, the watcher
	// has too little evidence to revert on — it must NOT revert a good change on one
	// unlucky game. Default DefaultMinGames. Clamped to >= 1 by the watcher.
	MinGames int
	// DegradationMargin is how far recent real-game fitness must fall BELOW the
	// pre-promotion baseline (baseline - recentMean > margin) to count as a
	// degradation. A small positive margin absorbs ordinary game-to-game noise so the
	// watcher reverts on a genuine regression, not on jitter at the baseline. Default
	// DefaultDegradationMargin.
	DegradationMargin float64
	// WindowGames is how many of the MOST RECENT real-game fitness samples the recent
	// mean is computed over (the observation window). The watcher asks the
	// RealGameFitness seam for up to this many samples. Default DefaultWindowGames.
	// Clamped to >= MinGames by the watcher (the window can never be smaller than the
	// evidence floor).
	WindowGames int
}

// Degradation policy defaults (#1016). Conservative: require several real games and
// a clear margin below baseline before reverting, so the safety net trips on a real
// regression rather than on noise, while still catching a degradation within a small
// number of real games rather than letting it ride.
const (
	// DefaultMinGames is the default minimum real games before a verdict: enough that
	// one unlucky game cannot trigger a revert.
	DefaultMinGames = 3
	// DefaultDegradationMargin is the default fitness drop (in the 0..1 fitness space
	// the #731 evaluator produces) below baseline that counts as degradation. 0.05 is
	// a clear, beyond-noise regression while staying sensitive.
	DefaultDegradationMargin = 0.05
	// DefaultWindowGames is the default observation window: the most recent real games
	// the recent mean is averaged over.
	DefaultWindowGames = 5
)

// normalize fills zero/invalid fields with the defaults and enforces the invariants
// (MinGames >= 1, WindowGames >= MinGames, margin >= 0). Returns a sanitized copy so
// the watcher always judges by a coherent policy even from a partially-set struct.
func (p WatchPolicy) normalize() WatchPolicy {
	if p.MinGames <= 0 {
		p.MinGames = DefaultMinGames
	}
	if p.DegradationMargin < 0 {
		p.DegradationMargin = DefaultDegradationMargin
	}
	if p.DegradationMargin == 0 {
		// A zero margin is almost certainly an unset field, not a deliberate
		// "revert on any drop below baseline"; use the noise-absorbing default.
		p.DegradationMargin = DefaultDegradationMargin
	}
	if p.WindowGames < p.MinGames {
		p.WindowGames = p.MinGames
	}
	if p.WindowGames < DefaultWindowGames {
		// Keep the window at least the default unless explicitly larger via MinGames.
		if DefaultWindowGames >= p.MinGames {
			p.WindowGames = DefaultWindowGames
		}
	}
	return p
}

// RealGameFitness reads recent REAL-game fitness for the promoted flag's objective —
// the signal the watcher judges degradation against. It is injected so the watcher's
// degradation logic is unit-testable with fixed samples (no real games exist in a
// test).
//
// PRODUCTION FOLLOW-UP (not built here, #1016 notes live-verify as a follow-up): the
// real implementation reads the fitness of recent REAL (game_kind == "real") games
// AFTER the promotion from the #731 fitness evaluator / the gamewindow rolling
// window — the same recent-real-game fitness the agent already aggregates — filtered
// to the objective(s) the promoted flag serves, newest-first or newest-last is fine
// (the watcher only averages). An error fails the watch closed (treated as "cannot
// confirm healthy" → revert).
type RealGameFitness interface {
	// Recent returns up to n most-recent REAL-game fitness samples observed AFTER the
	// promotion for flagKey's objective. Fewer than n is allowed (the window has not
	// filled yet); the watcher applies the MinGames floor. An error fails closed.
	Recent(ctx context.Context, flagKey string, n int) ([]float64, error)
}

// fitnessWatcher is the concrete Watcher: it pulls recent real-game fitness via the
// RealGameFitness seam and decides degradation by the WatchPolicy against the
// pre-promotion baseline.
type fitnessWatcher struct {
	fitness RealGameFitness
	policy  WatchPolicy
	log     *slog.Logger
}

// NewFitnessWatcher builds the autonomous safety-net Watcher over a RealGameFitness
// source and a degradation policy. A zero-value policy yields the defaults
// (DefaultWatchPolicy). fitness MUST be non-nil — a watcher with no fitness source
// could not observe real games, which would defeat the whole point of the fail-closed
// guard; the caller (main.go) builds the real source behind the env gate or wires no
// Watcher at all (then autonomous fails closed and refuses to apply). log nil uses
// slog.Default().
func NewFitnessWatcher(fitness RealGameFitness, policy WatchPolicy, log *slog.Logger) *fitnessWatcher {
	if log == nil {
		log = slog.Default()
	}
	return &fitnessWatcher{fitness: fitness, policy: policy.normalize(), log: log}
}

// DefaultWatchPolicy returns the default degradation policy (#1016 sane defaults).
func DefaultWatchPolicy() WatchPolicy {
	return WatchPolicy{
		MinGames:          DefaultMinGames,
		DegradationMargin: DefaultDegradationMargin,
		WindowGames:       DefaultWindowGames,
	}
}

// Degraded reports whether real-game fitness degraded vs baseline after the
// autonomous apply. The decision:
//
//  1. read up to WindowGames recent real-game fitness samples (seam error → fail
//     closed: treat as degraded so an unconfirmable change is reverted);
//  2. require at least MinGames samples — fewer is INSUFFICIENT EVIDENCE, so we
//     return (false, nil): NOT a revert, but also not a confirmed-healthy keep on
//     too little data. (The Promoter keeps the apply in that case; a real follow-up
//     re-watches as more games complete. We do not revert a change on one unlucky
//     game.);
//  3. compute the recent mean and declare degradation when
//     baseline - recentMean > DegradationMargin.
func (w *fitnessWatcher) Degraded(ctx context.Context, flagKey string, baseline float64) (bool, error) {
	samples, err := w.fitness.Recent(ctx, flagKey, w.policy.WindowGames)
	if err != nil {
		// Cannot read real-game fitness → cannot confirm healthy. Surface the error so
		// the Promoter's fail-safe revert fires (it treats err != nil as a revert).
		return false, fmt.Errorf("read recent real-game fitness for %q: %w", flagKey, err)
	}
	if len(samples) < w.policy.MinGames {
		// Not enough real games yet to judge. Do NOT revert (one/few games is not
		// evidence of degradation), and do NOT error (the read succeeded). The apply
		// stands; degradation can only be CONFIRMED with MinGames of evidence.
		w.log.Info("autonomous watch: insufficient real games to judge degradation",
			"flag", flagKey, "samples", len(samples), "min_games", w.policy.MinGames)
		return false, nil
	}

	recent := mean(samples)
	drop := baseline - recent
	degraded := drop > w.policy.DegradationMargin
	w.log.Info("autonomous watch: real-game fitness judged",
		"flag", flagKey,
		"baseline", baseline,
		"recent_mean", recent,
		"drop", drop,
		"margin", w.policy.DegradationMargin,
		"games", len(samples),
		"degraded", degraded)
	return degraded, nil
}

// mean returns the arithmetic mean of a non-empty slice (callers guard len via
// MinGames, but guard against an empty slice to avoid a divide-by-zero).
func mean(xs []float64) float64 {
	if len(xs) == 0 {
		return 0
	}
	var sum float64
	for _, x := range xs {
		sum += x
	}
	return sum / float64(len(xs))
}
