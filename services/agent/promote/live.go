package promote

import (
	"context"
	"log/slog"
	"sync/atomic"
	"time"

	"github.com/joustmania/agent/experiment"
)

// live.go holds the PRODUCTION wiring of the autonomous safety net's two live seams
// (#1057/#1065): the RealGameFitness source that reads recent REAL-game fitness from
// the #965 FitnessStore, and the polling Rewatcher that re-observes an
// insufficient-games apply as more real games complete. Both are constructed only in
// main.go behind the autonomous env gate — a nil source/rewatcher keeps the loop
// fail-closed (autonomous refuses to apply, #1016).

// ObjectiveForFlag resolves the fitness objective a promoted flag's experiment serves
// (so the FitnessStore can be queried per-objective, exactly the key the recent-real
// baseline already uses). It is injected because the flag→objective mapping lives in
// the experiment registry, which the promote package does not (and should not)
// import. An empty objective means "any real game" — RecentReal then matches every
// objective, which is still correct (the watcher only needs a recent-real fitness
// trend) but less precise; the resolver should return the experiment's objective when
// known.
type ObjectiveForFlag func(flagKey string) string

// LateObjectiveResolver is a goroutine-safe, settable ObjectiveForFlag. main.go
// builds the FitnessWatcher (and thus the fitness source) BEFORE the experiment
// registry that knows the flag→objective mapping exists; this holder lets the
// registry-backed resolver be SET later (once the loop is built) without rebuilding
// the watcher. Until set, it resolves to "" (any-objective), which is safe — the
// watcher still observes a recent-real fitness trend, just not objective-filtered.
type LateObjectiveResolver struct {
	fn atomic.Pointer[ObjectiveForFlag]
}

// Set installs the resolver (called once the registry exists). A nil fn is ignored.
func (l *LateObjectiveResolver) Set(fn ObjectiveForFlag) {
	if fn != nil {
		l.fn.Store(&fn)
	}
}

// Resolve implements the ObjectiveForFlag shape: it delegates to the installed
// resolver, or returns "" (any-objective) until one is set.
func (l *LateObjectiveResolver) Resolve(flagKey string) string {
	if p := l.fn.Load(); p != nil {
		return (*p)(flagKey)
	}
	return ""
}

// storeRealGameFitness is the production RealGameFitness: it reads the most-recent
// REAL-game fitness samples for the promoted flag's objective from the #965
// FitnessStore — the SAME finalized-per-game real fitness the recent-real baseline
// (#992) and the M7-7 Validator baseline read. It is the real-data source that turns
// the watcher's injected reads into REAL ones; until real games populate the store it
// returns 0 samples, so the watcher reports INSUFFICIENT and the apply stays
// fail-closed-unverified (then re-watched) rather than falsely confirmed.
type storeRealGameFitness struct {
	store     *experiment.FitnessStore
	objective ObjectiveForFlag
}

// NewStoreRealGameFitness builds the production RealGameFitness over the FitnessStore.
// store MUST be non-nil (a nil store could never observe real games — the caller must
// then wire no watcher so autonomous fails closed). objective may be nil → every read
// matches any objective.
func NewStoreRealGameFitness(store *experiment.FitnessStore, objective ObjectiveForFlag) *storeRealGameFitness {
	return &storeRealGameFitness{store: store, objective: objective}
}

// Recent returns up to n most-recent REAL-game fitness scalars for flagKey's
// objective, newest-first (the watcher only averages, so order is immaterial). A nil
// store yields no samples (treated as INSUFFICIENT, never a false healthy verdict).
func (s *storeRealGameFitness) Recent(_ context.Context, flagKey string, n int) ([]float64, error) {
	if s.store == nil {
		return nil, nil
	}
	objective := ""
	if s.objective != nil {
		objective = s.objective(flagKey)
	}
	samples := s.store.RecentReal(objective, n)
	out := make([]float64, 0, len(samples))
	for _, sample := range samples {
		out = append(out, sample.Fitness)
	}
	return out, nil
}

// DefaultRewatchInterval / DefaultRewatchTimeout bound the background re-watch
// (#1065 item 4): poll the watcher every interval until it renders a definitive
// verdict (healthy/degraded/error) or the timeout elapses. Conservative defaults —
// real games complete on the order of minutes, so a minute-scale poll over a
// generous window catches a slowly-filling window without busy-spinning.
const (
	DefaultRewatchInterval = 1 * time.Minute
	DefaultRewatchTimeout  = 60 * time.Minute
)

// pollingRewatcher is the production Rewatcher: on an insufficient-games apply it
// spawns ONE background goroutine that re-observes the flag on an interval until the
// watcher can judge it, then reverts on a confirmed degradation (or on a telemetry
// error, fail-safe). It never blocks the promotion call. Bound to a root context so
// agent shutdown cancels in-flight re-watches.
type pollingRewatcher struct {
	rootCtx  context.Context
	interval time.Duration
	timeout  time.Duration
	now      func() time.Time
	log      *slog.Logger
}

// NewPollingRewatcher builds the background re-watch scheduler. rootCtx binds every
// spawned re-watch to the agent lifecycle (cancelled on shutdown). A non-positive
// interval/timeout falls back to the defaults. log nil uses slog.Default().
func NewPollingRewatcher(rootCtx context.Context, interval, timeout time.Duration, log *slog.Logger) *pollingRewatcher {
	if rootCtx == nil {
		rootCtx = context.Background()
	}
	if interval <= 0 {
		interval = DefaultRewatchInterval
	}
	if timeout <= 0 {
		timeout = DefaultRewatchTimeout
	}
	if log == nil {
		log = slog.Default()
	}
	return &pollingRewatcher{rootCtx: rootCtx, interval: interval, timeout: timeout, now: time.Now, log: log}
}

// Schedule starts the background re-watch (non-blocking). It re-observes via the live
// watcher on each tick:
//   - VerdictDegraded or a telemetry error → revert (fail-safe) and stop.
//   - VerdictHealthy → confirmed keep, stop.
//   - VerdictInsufficient → keep polling until the timeout, then stop (apply stands;
//     too few real games ever arrived to judge — never revert a change on no evidence).
func (r *pollingRewatcher) Schedule(_ context.Context, flagKey string, baseline float64, watch Watcher, revert func(ctx context.Context)) {
	obs, ok := watch.(Observer)
	if !ok {
		// A boolean-only watcher cannot report insufficiency, so it can never be the
		// source of a re-watch; nothing to schedule.
		return
	}
	go func() {
		deadline := r.now().Add(r.timeout)
		ticker := time.NewTicker(r.interval)
		defer ticker.Stop()
		for {
			select {
			case <-r.rootCtx.Done():
				r.log.Info("autonomous re-watch cancelled (shutdown)", "flag", flagKey)
				return
			case <-ticker.C:
				verdict, err := obs.Observe(r.rootCtx, flagKey, baseline)
				switch {
				case err != nil:
					r.log.Warn("autonomous re-watch: telemetry error, reverting fail-safe", "flag", flagKey, "error", err)
					revert(r.rootCtx)
					return
				case verdict == VerdictDegraded:
					r.log.Warn("autonomous re-watch: degradation confirmed, reverting", "flag", flagKey)
					revert(r.rootCtx)
					return
				case verdict == VerdictHealthy:
					r.log.Info("autonomous re-watch: fitness confirmed healthy, keeping apply", "flag", flagKey)
					return
				default: // VerdictInsufficient — keep waiting for more real games.
					if r.now().After(deadline) {
						r.log.Info("autonomous re-watch: timed out with insufficient real games; apply stands unverified", "flag", flagKey)
						return
					}
				}
			}
		}
	}()
}

// ensure the concrete types satisfy the seams at compile time.
var (
	_ RealGameFitness = (*storeRealGameFitness)(nil)
	_ Rewatcher       = (*pollingRewatcher)(nil)
)
