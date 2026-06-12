// Package gamewindow is the M7-2 rolling game context window (#929). It maintains
// a bounded, in-memory ring of the most recent game gamesummary.Summary values —
// the per-game narratives the M7-1 builder (#928) produces on game end — and
// renders the last N of them into one compact NARRATIVE CONTEXT BLOCK that the
// #739 llm decision path injects into EVERY inference prompt. That block is the
// agent's CROSS-GAME memory: without it each llm cycle sees only the current
// game's live snapshot (+ the #916 in-game timeline); with it the model also sees
// "what happened in the last few games", so it can reason across games/sessions.
//
// Boundaries (deliberately narrow, per #929 M7-2):
//   - The window is GLOBAL across games (shared cross-session memory), NOT
//     per-game: two concurrent games (#845) both see the SAME recent-games window.
//     That is intentional — distinct from #916's per-game timeline, which is
//     per-partition. So the store is mutex-guarded: game-end writes (Record) race
//     concurrent llm-path reads (Recent).
//   - It does NOT re-read the on-disk JSON files the M7-1 writer produced — it
//     hooks the SAME game-end production path (the gamesummary coordinator calls
//     Record with the freshly-built Summary), so the window advances the instant a
//     game ends, with no disk round-trip and no staleness window.
//   - HOW MANY summaries to inject (N) is NOT this package's concern: it retains a
//     fixed cap (RetentionCap) and hands back the last N on demand (Recent(n)).
//     N is read live from an OpenFeature flag on the decision path (see
//     flags.Snapshot.ContextGames + decision/llm_decide.go), so retuning N takes
//     effect on the next llm call with no redeploy. Render renders whatever slice
//     it is given.
//
// Out of scope (later M7 issues): WHICH context is injected via flags (#930 M7-3
// toggles the window/timeline/etc. on and off — M7-2 just builds + injects this
// one window); the dashboard pane (#933); deeper per-summary improvement (#931+).
package gamewindow

import (
	"sync"

	"github.com/joustmania/agent/gamesummary"
)

// RetentionCap bounds the ring: the MOST summaries the window ever holds, and thus
// the most N (flags.Snapshot.ContextGames) can ever inject. It is a safety ceiling,
// not the injected count — the decision path clamps N to [0, RetentionCap] and asks
// for Recent(N). 20 comfortably exceeds any sane N (the flag default is 3) while
// keeping the in-memory footprint and the rendered block bounded; a game-end that
// would push past 20 evicts the OLDEST, so the window always reflects the most
// recent games. Pure constant — no clock, no config.
const RetentionCap = 20

// Store is the global rolling window of recent game summaries (#929). It is a
// fixed-capacity ring retaining the last RetentionCap summaries in arrival
// (game-end) order, oldest-first. Safe for concurrent use: Record (game-end
// writes, possibly two concurrent games) and Recent/Len (llm-path reads) are all
// mutex-guarded, because the window is GLOBAL — one Store shared across every
// per-game decision loop, exactly like the shared LLM budget/resolver (#847/#741).
type Store struct {
	mu sync.Mutex
	// summaries holds the retained window, oldest-first, len <= RetentionCap. A plain
	// slice (not a head/tail ring buffer) keeps Recent a trivial tail-slice and the
	// code obvious; RetentionCap is tiny, so the O(cap) shift on eviction is
	// negligible and runs only on game end (not on the hot read path).
	summaries []gamesummary.Summary
}

// NewStore builds an empty rolling window. main.go constructs ONE and shares it
// across all per-game loops (the window is global cross-session memory), wiring
// Record into the gamesummary coordinator's game-end path and the Store itself
// into every Loop as the context-window source.
func NewStore() *Store {
	return &Store{summaries: make([]gamesummary.Summary, 0, RetentionCap)}
}

// Record appends one freshly-built game Summary to the window, evicting the oldest
// when the ring is full so the window always holds the most recent RetentionCap
// games (#929). It is THE seam that advances the window: the gamesummary
// coordinator calls it once per game on game end, for BOTH real and shadow games,
// from the SAME pre-reset-snapshot path that writes the JSON file — so the window
// updates after EVERY completed game with no disk re-read. Concurrency-safe:
// concurrent game-ends (#845) serialize on mu.
func (s *Store) Record(summary gamesummary.Summary) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.summaries = append(s.summaries, summary)
	if len(s.summaries) > RetentionCap {
		// Drop the oldest. Copy-shift into a fresh backing array rather than reslicing
		// s.summaries[1:], so the evicted Summary is not pinned alive by the slice's
		// retained backing array (the Summaries carry slices of their own).
		trimmed := make([]gamesummary.Summary, RetentionCap)
		copy(trimmed, s.summaries[len(s.summaries)-RetentionCap:])
		s.summaries = trimmed
	}
}

// Recent returns a COPY of the last n retained summaries, newest-LAST (oldest of
// the returned window first), for the llm path to render. n is clamped to
// [0, len]: a non-positive n returns an empty (non-nil) slice (the caller renders
// the explicit "(no prior games)" marker), and an n past the retained count
// returns everything held. The returned slice is a fresh copy, so a caller can
// never mutate the window or observe a later Record mid-read. Concurrency-safe.
func (s *Store) Recent(n int) []gamesummary.Summary {
	s.mu.Lock()
	defer s.mu.Unlock()
	if n <= 0 {
		return []gamesummary.Summary{}
	}
	if n > len(s.summaries) {
		n = len(s.summaries)
	}
	out := make([]gamesummary.Summary, n)
	copy(out, s.summaries[len(s.summaries)-n:])
	return out
}

// Len reports how many summaries the window currently holds (0..RetentionCap),
// for tests and at-a-glance sizing. Concurrency-safe.
func (s *Store) Len() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.summaries)
}
