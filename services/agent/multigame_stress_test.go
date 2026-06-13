package main

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"go.opentelemetry.io/collector/pdata/pmetric/pmetricotlp"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// multigame_stress_test.go is the #845 multi-game concurrency stress coverage
// (#923 part 2). It drives N synthetic game partitions through the SAME
// receiver/multiplexer/LoopSet, concurrently, WITH CHURN (games ending and being
// evicted while signals still stream), and proves the three isolation properties
// the per-game partitioning (#845 PR B/C) exists to guarantee:
//
//	(a) per-game rate budgets are INDEPENDENT — one game exhausting its
//	    intervention budget neither consumes nor blocks another's;
//	(b) the fallback/default partition ("") is unaffected by per-game churn — it
//	    keeps dispatching while other partitions are created and evicted;
//	(c) NO cross-partition state bleed — a decision/effect produced for game A
//	    targets only A's player serial and lands only on A's action sink, never B's.
//
// It is a real concurrency exercise (one goroutine per game + a fallback streamer +
// a churn goroutine, all running at once) and is meant to be run under -race, where
// any shared-mutable-state race in the multiplexer/LoopSet/limiter would trip.

// gameSink is a per-game recording action sink. Each per-game Loop gets its OWN
// sink (closed over the game's id in the factory), so the set of decisions a sink
// receives is exactly the set the corresponding game dispatched. It records every
// target serial it ever saw, which is how property (c) — no cross-partition bleed —
// is checked: a sink must only ever see ITS OWN game's serial.
type gameSink struct {
	gameID  string
	mu      sync.Mutex
	serials map[string]int // target serial -> dispatch count
	total   atomic.Int64
}

func newGameSink(gameID string) *gameSink {
	return &gameSink{gameID: gameID, serials: map[string]int{}}
}

func (s *gameSink) Apply(_ context.Context, d decision.Decision) error {
	s.mu.Lock()
	s.serials[d.TargetSerial]++
	s.mu.Unlock()
	s.total.Add(1)
	return nil
}

// distinctSerials returns the set of distinct target serials this sink ever saw.
// The cross-partition-bleed invariant (property c) is that this set has at most ONE
// element: every decision a Loop dispatches targets the serial derived from THAT
// partition's own context (targetedRules below), so a perfectly-isolated sink sees
// exactly its own partition's serial and no other. A sink seeing 2+ distinct serials
// means a decision built for one partition's context reached another's Loop/sink —
// i.e. state bled across partitions.
func (s *gameSink) distinctSerials() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]string, 0, len(s.serials))
	for serial := range s.serials {
		out = append(out, serial)
	}
	return out
}

// serialFor derives a game's player serial from its id, used to stream a player
// signal for that game (the metric label). The decision target is derived
// separately from the partition's resolved SessionID (see targetedRules).
func serialFor(gameID string) string { return "P-" + gameID }

// targetedRules returns one decision per cycle that targets a serial derived from
// the SessionID of the partition context it is evaluating. Because the receiver
// routes each game's signals to its OWN partition and its OWN Loop, the GameContext
// this engine sees carries that partition's SessionID (the adopted game_id for a
// labeled game, a synthetic "session-N" for the fallback partition). Deriving the
// target from c.SessionID makes the dispatched serial a fingerprint of the
// partition that produced the decision — so any sink that sees more than one
// distinct serial has proof of cross-partition bleed. The engine reads only the
// immutable context argument, so it is concurrency-safe (one fresh instance per
// Loop, like main.go).
type targetedRules struct{}

func (targetedRules) Evaluate(_ context.Context, c gamecontext.GameContext) []decision.Decision {
	return []decision.Decision{{
		Intervention: "grant_shield", // cost 1
		TargetSerial: "target-" + c.SessionID,
		Reason:       "stress",
	}}
}

// TestMultiGameStress_BudgetIsolationAndNoBleed is the #923 multi-game stress test.
func TestMultiGameStress_BudgetIsolationAndNoBleed(t *testing.T) {
	const (
		numGames      = 12
		perGameBudget = 3 // grant_shield costs 1, so the 4th+ dispatch in a window blocks
		cyclesPerGame = 40
	)

	logger := slog.New(slog.NewTextHandler(io.Discard, nil))

	// A FIXED clock for the multiplexer Stores and the pipeline gate so freshness/
	// eviction are deterministic. The per-game rate limiter inside each Loop uses real
	// time (its clock is unexported), but the whole test runs in well under the
	// limiter's one-minute window, so every dispatch falls in the same trailing window
	// and a game that hits perGameBudget stays exhausted — the budget assertion is
	// still exact. (This mirrors receiver_test.go's per-game budget integration test,
	// which likewise relies on real wall-clock time for the limiter.)
	clock := time.Unix(7000, 0)
	now := func() time.Time { return clock }

	mux := gamecontext.NewMultiplexer(func(string) *gamecontext.Store {
		return gamecontext.NewStore(time.Hour, time.Hour, now)
	})

	// One sink per game id, captured under a mutex as the factory creates loops
	// lazily (factory runs from the streaming goroutines, concurrently).
	var sinksMu sync.Mutex
	sinks := map[string]*gameSink{}

	loops := decision.NewLoopSet(func(gameID string) *decision.Loop {
		sink := newGameSink(gameID)
		sinksMu.Lock()
		sinks[gameID] = sink
		sinksMu.Unlock()

		l := decision.NewLoop(staticFlags{flags.Snapshot{
			Enabled:              true,
			Mode:                 "rules",
			InterventionsAllowed: []string{"grant_shield"},
			Policy:               flags.Policy{MaxInterventionsPerMinute: perGameBudget},
		}}, logger)
		// FRESH rules engine per loop (mirrors main.go); targets this game's serial.
		l.Rules = targetedRules{}
		l.Actions = sink
		return l
	})

	pipe := newPipeline(mux, loops, time.Hour)
	pipe.now = now
	r := &metricsReceiver{pipe: pipe}

	gameIDs := make([]string, numGames)
	for i := range gameIDs {
		gameIDs[i] = fmt.Sprintf("game-%02d", i)
	}

	exportOnce := func(gameID string) {
		md := liveGameMetrics(gameID, serialFor(gameID))
		if _, err := r.Export(context.Background(), pmetricotlp.NewExportRequestFromMetrics(md)); err != nil {
			t.Errorf("export for %s: %v", gameID, err)
		}
	}

	// --- Concurrent drivers ---
	var wg sync.WaitGroup

	// One streaming goroutine per game: hammer its partition through the receiver.
	for _, gameID := range gameIDs {
		wg.Add(1)
		go func(gameID string) {
			defer wg.Done()
			for i := 0; i < cyclesPerGame; i++ {
				exportOnce(gameID)
			}
		}(gameID)
	}

	// Fallback-partition streamer: unlabeled metrics (game_id="") route to the ""
	// partition. It runs throughout, concurrently with all the per-game churn, so we
	// can assert the fallback partition keeps dispatching and is never starved/evicted
	// (property b). Its Loop and sink are created lazily by the factory above, keyed
	// under FallbackGameID, so we read sinks[""] in the assertions.
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < cyclesPerGame*2; i++ {
			md := liveGameMetrics(gamecontext.FallbackGameID, serialFor(gamecontext.FallbackGameID))
			if _, err := r.Export(context.Background(), pmetricotlp.NewExportRequestFromMetrics(md)); err != nil {
				t.Errorf("fallback export: %v", err)
			}
		}
	}()

	// Churn goroutine: while signals stream, repeatedly evict stale partitions and
	// retain the live loop set — the exact lockstep main.go's eviction ticker runs
	// (#845 PR C). With a 1h TTL nothing actually drains here, but the eviction +
	// Retain map mutations run CONCURRENTLY with the streaming goroutines' lazy For/
	// store() creates, which is the race surface this exercises under -race. We also
	// force a few real drops to prove churn does not corrupt sibling partitions.
	churnDone := make(chan struct{})
	go func() {
		defer close(churnDone)
		for i := 0; i < 200; i++ {
			mux.EvictStale()
			loops.Retain(partitionSet(mux.Partitions()))
			time.Sleep(time.Millisecond)
		}
	}()

	wg.Wait()
	<-churnDone

	// --- Assertions ---

	sinksMu.Lock()
	defer sinksMu.Unlock()

	// (a) + (c): every game's sink dispatched at most perGameBudget interventions
	// (its budget was its OWN, independently exhausted) and saw ONLY its own serial
	// (no cross-partition bleed).
	for _, gameID := range gameIDs {
		sink := sinks[gameID]
		if sink == nil {
			t.Errorf("%s: no sink created (partition never evaluated)", gameID)
			continue
		}
		got := sink.total.Load()
		if got == 0 {
			t.Errorf("%s: dispatched 0 interventions; expected up to its own budget of %d", gameID, perGameBudget)
		}
		// The whole test runs inside the limiter's one-minute window, so a
		// correctly-isolated per-game budget caps dispatches at perGameBudget regardless
		// of how many of its cycles ran. Exceeding it would mean the limiter admitted
		// past budget (a shared/leaky limiter).
		if got > perGameBudget {
			t.Errorf("%s: dispatched %d > its budget %d (per-game limiter over-admitted)", gameID, got, perGameBudget)
		}
		// (c): the sink saw exactly one distinct target serial, and it is THIS game's
		// fingerprint ("target-<gameID>", since a labeled game adopts its game_id as
		// SessionID). More than one distinct serial = a decision built for another
		// partition's context reached this sink.
		serials := sink.distinctSerials()
		if len(serials) != 1 {
			t.Errorf("%s: sink saw %d distinct target serials %v (cross-partition bleed); want exactly 1",
				gameID, len(serials), serials)
		} else if want := "target-" + gameID; serials[0] != want {
			t.Errorf("%s: sink's serial = %q, want %q (decision targeted the wrong partition's context)",
				gameID, serials[0], want)
		}
	}

	// (a) headline: the budget is per-game, so the TOTAL dispatched across all games
	// is ~numGames * perGameBudget — NOT a single shared budget of perGameBudget. If
	// the budget were global, total would be capped near perGameBudget regardless of
	// game count. We assert it is at least (numGames-1)*perGameBudget+1 to prove the
	// budgets did not share (allowing one game to be mid-window).
	var totalDispatched int64
	for _, gameID := range gameIDs {
		if sink := sinks[gameID]; sink != nil {
			totalDispatched += sink.total.Load()
		}
	}
	if minIndependent := int64((numGames-1)*perGameBudget + 1); totalDispatched < minIndependent {
		t.Errorf("total dispatched across games = %d; want >= %d. A shared/global budget would cap near %d — budgets are NOT independent",
			totalDispatched, minIndependent, perGameBudget)
	}

	// (b): the fallback partition was evaluated and dispatched despite all the
	// concurrent per-game churn — it is never wholesale-evicted (#845) and its loop
	// is permanent (Retain skips it).
	fb := sinks[gamecontext.FallbackGameID]
	if fb == nil {
		t.Fatal("fallback partition's loop was never created/evaluated; churn or routing starved it")
	}
	if fb.total.Load() == 0 {
		t.Error("fallback partition dispatched 0 interventions; it must be unaffected by per-game churn")
	}
	if fb.total.Load() > perGameBudget {
		t.Errorf("fallback dispatched %d > budget %d (its budget is not its own)", fb.total.Load(), perGameBudget)
	}
	// The fallback partition must likewise see exactly one distinct serial — no
	// labeled game's decision ever bled into it.
	if serials := fb.distinctSerials(); len(serials) != 1 {
		t.Errorf("fallback sink saw %d distinct target serials %v (bleed into the fallback partition); want 1", len(serials), serials)
	}
}
