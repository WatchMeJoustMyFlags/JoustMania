package experiment

import (
	"sort"
	"sync"
	"time"
)

// fitness_store.go is the FINALIZED-PER-GAME fitness store (#965, #1017): the
// missing instrumentation seam that turns the M7-7 Validator's INJECTED fitness
// reads (Baseline / FitnessMeasurer) into REAL ones.
//
// THE #1017 GAP, precisely: shadow and real games already emit the SAME fitness
// inputs through the SAME code path, and decision.EvaluateFitness is
// substrate-agnostic — so fitness is COMPUTABLE today; it is simply never STORED
// for the validation path. The experiment loop already computes a per-game
// fitness scalar at conclusion (defaultGameFitness → EvaluateFitness) but folds
// it ONLY into the cohort journal for experiment-bound games and DROPS it for
// everything else. This store is the rolling, in-memory, game_id-keyed sink that
// captures that already-computed sample for BOTH:
//
//   - a SHADOW validation game (the SyntheticRunner spawns it; the FitnessMeasurer
//     reads its finalized fitness back by game_id — the StoreMeasurer here), and
//   - a REAL primary game (the StoreBaseline reads the mean of the last N real
//     games as the validation baseline — the #965 Baseline.Fitness seam).
//
// FINALIZED, NOT A LIVE SCRAPE (#958 / #1017): Record is called from onGameEnd
// AFTER the game has concluded and its duration has been recovered
// (withEffectiveDuration), so a reader sees the SETTLED per-game fitness — it
// never races the ~10s OTel span flush. This is the explicit requirement the
// validate.go Baseline doc and the #958 strategy doc both call out.
//
// NO-PLAYER-IDENTITY (#23): a sample is keyed by game_id and carries only
// per-game aggregates (the objective's fitness scalar, the resolved flag config,
// the duration). It holds NOTHING per-person and is evicted by a bounded ring, so
// it composes cleanly with controllers≠persons — no cross-session profile is ever
// assembled here.

// DefaultFitnessStoreCapacity bounds the store's ring. A few hundred finalized
// samples is ample: the Baseline only ever reads the last N real games (N is a
// handful), and a validation batch reads a handful of just-spawned shadow
// game_ids, so the working set is tiny. The ring keeps the store O(1) in memory
// regardless of how long the agent runs.
const DefaultFitnessStoreCapacity = 512

// GameKindReal (the game_kind label a live, player-facing game carries —
// game_session.py:52-53) is defined in targeting.go. The Baseline reads only
// samples with this kind so a shadow validation game can never pollute the
// recent-real baseline.

// FitnessSample is one finalized per-game fitness datapoint (#965). It is the unit
// the store holds and both validation seams read.
type FitnessSample struct {
	// GameID is the coordinator-assigned game id this sample finalized for. It is
	// the store key and the join key the FitnessMeasurer uses against a Run's ids.
	GameID string
	// Fitness is the game's finalized objective fitness scalar in [0,1] (higher =
	// better) — the SAME defaultGameFitness → EvaluateFitness value the cohort
	// journal folds, captured here for the validation path.
	Fitness float64
	// Objective is the fitness objective the Fitness scalar scored (endurance /
	// balanced / accelerate). The Baseline filters recent-real samples by it so a
	// baseline is per-objective (the #958 study is per-objective: only
	// endurance/accelerate carry real signal today, #1015).
	Objective string
	// GameKind is "real" for a primary game or "shadow" for a validation/cohort
	// game. The Baseline reads only GameKindReal; the FitnessMeasurer keys purely
	// by game_id (a Run only ever holds shadow ids).
	GameKind string
	// DurationS is the game's recovered effective duration in seconds (#997), kept
	// alongside the fitness for the audit trail / drift monitor (#1017 §4).
	DurationS float64
	// Config is the resolved per-game flag snapshot in effect for this game (the
	// value(s) under experiment), so a paired (config → fitness) study can assemble
	// observations (#1017 §3 item 3). Optional — nil when the loop has no resolved
	// snapshot to attach. Copied on Record so a caller cannot mutate stored state.
	Config map[string]any
	// At is when the sample finalized (Record time), used for recency ordering in
	// RecentReal.
	At time.Time
}

// FitnessStore is a bounded, thread-safe, finalized-per-game fitness store keyed
// by game_id (#965). It is the production backing for the StoreBaseline and
// StoreMeasurer validation seams. Construct with NewFitnessStore; the experiment
// loop's onGameEnd Records into it, and the Validator's seams read from it.
type FitnessStore struct {
	mu  sync.Mutex
	cap int
	// byKey is the authoritative finalized sample per (game_id, objective). A REAL
	// game is scored against EVERY experimentable objective its signals support
	// (#1017 §3 item 1) — so one game_id may hold several samples, one per objective.
	// The composite key keeps them distinct (a single game_id key would clobber). A
	// SHADOW game carries one objective, so it has exactly one entry. Last write wins
	// per key (a defensive re-Record updates in place).
	byKey map[string]FitnessSample
	// order is the insertion order of keys for FIFO eviction once cap is exceeded. A
	// key appears at most once (Record removes a stale dup before re-appending), so
	// order and byKey stay in lockstep.
	order []string
}

// NewFitnessStore builds a store with the given ring capacity. A non-positive
// capacity falls back to DefaultFitnessStoreCapacity (a zero ring would evict
// every sample immediately, defeating the store).
func NewFitnessStore(capacity int) *FitnessStore {
	if capacity <= 0 {
		capacity = DefaultFitnessStoreCapacity
	}
	return &FitnessStore{
		cap:   capacity,
		byKey: make(map[string]FitnessSample, capacity),
	}
}

// sampleKey composes the (game_id, objective) store key. A shadow game's objective
// may be empty (the measurer reads it back by game_id regardless), which is fine —
// the empty-objective key is still distinct per game_id.
func sampleKey(gameID, objective string) string {
	return gameID + "\x00" + objective
}

// Record finalizes one per-game fitness sample, keyed by sample.GameID. It is
// called from onGameEnd after the game concluded and its duration was recovered,
// so the stored value is SETTLED (no span-flush race). A sample with an empty
// GameID is dropped (nothing can read it back). The ring evicts the oldest
// sample(s) once capacity is exceeded.
func (s *FitnessStore) Record(sample FitnessSample) {
	if sample.GameID == "" {
		return
	}
	if sample.At.IsZero() {
		sample.At = time.Now().UTC()
	}
	// Defensive copy so a later mutation of the caller's map cannot reach in.
	sample.Config = copyConfig(sample.Config)

	key := sampleKey(sample.GameID, sample.Objective)

	s.mu.Lock()
	defer s.mu.Unlock()

	if _, exists := s.byKey[key]; exists {
		// Re-Record (rare): update in place, keep its position in the order ring so a
		// duplicate cannot grow order unbounded.
		s.byKey[key] = sample
		return
	}
	s.byKey[key] = sample
	s.order = append(s.order, key)
	s.evictLocked()
}

// Get returns a finalized sample for a game_id and whether one is present. The
// FitnessMeasurer uses it to read a just-completed SHADOW game's fitness — a shadow
// game holds exactly one (game_id, objective) sample, so Get returns it. For a game
// id with several per-objective samples (a real game) Get returns an arbitrary one;
// callers needing a specific objective use RecentReal, which filters by objective.
func (s *FitnessStore) Get(gameID string) (FitnessSample, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, sample := range s.byKey {
		if sample.GameID == gameID {
			return sample, true
		}
	}
	return FitnessSample{}, false
}

// RecentReal returns the most-recent (newest-first) finalized samples from REAL
// (game_kind="real") games for the given objective, up to n. An empty objective
// matches any objective (the caller filters). It is the read side of the
// StoreBaseline: the validation baseline is the mean over what this returns.
func (s *FitnessStore) RecentReal(objective string, n int) []FitnessSample {
	if n <= 0 {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	// Collect matching real samples, then sort newest-first and take n. The ring is
	// small (cap), so a full scan + sort is cheap and keeps ordering correct even
	// across re-Records (which do not move the order entry).
	matches := make([]FitnessSample, 0, len(s.byKey))
	for _, sample := range s.byKey {
		if sample.GameKind != GameKindReal {
			continue
		}
		if objective != "" && sample.Objective != objective {
			continue
		}
		matches = append(matches, sample)
	}
	sort.Slice(matches, func(i, j int) bool {
		return matches[i].At.After(matches[j].At)
	})
	if len(matches) > n {
		matches = matches[:n]
	}
	return matches
}

// Len returns the number of finalized samples currently held (test/observability).
// A real game scored against multiple objectives counts once PER objective.
func (s *FitnessStore) Len() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.byKey)
}

// evictLocked drops the oldest sample(s) until the ring is within capacity.
// Assumes s.mu is held.
func (s *FitnessStore) evictLocked() {
	for len(s.order) > s.cap {
		oldest := s.order[0]
		s.order = s.order[1:]
		delete(s.byKey, oldest)
	}
}

// copyConfig deep-enough copies a resolved-config snapshot (a flat map of scalar
// flag values) so the store owns its own copy. nil/empty maps return nil.
func copyConfig(m map[string]any) map[string]any {
	if len(m) == 0 {
		return nil
	}
	out := make(map[string]any, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}
