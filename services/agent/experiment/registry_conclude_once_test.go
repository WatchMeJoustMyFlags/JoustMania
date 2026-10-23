package experiment

import (
	"bytes"
	"context"
	"log/slog"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/joustmania/agent/experiment/journal"
	"github.com/joustmania/agent/patterngame"
)

// raceWait bounds every wait in these tests so a regression hangs for seconds, not forever.
const raceWait = 5 * time.Second

// rendezvousVerdict returns a fixed verdict. Once armed, it numbers its calls:
// call 1 (the game-end path, past its registry lock) closes first, and call
// blockAt waits until call blockAt+1 arrives, so the game-end path and the
// allocation pass are both past their "still running?" check before either
// concludes (#2034).
type rendezvousVerdict struct {
	outcome string
	blockAt int
	armed   atomic.Bool
	calls   atomic.Int32
	first   chan struct{}
	release chan struct{}
}

func newRendezvousVerdict(outcome string, blockAt int) *rendezvousVerdict {
	return &rendezvousVerdict{outcome: outcome, blockAt: blockAt, first: make(chan struct{}), release: make(chan struct{})}
}

func (v *rendezvousVerdict) Evaluate(journal.Summary) (journal.Verdict, bool) {
	if !v.armed.Load() {
		return journal.Verdict{Outcome: OutcomeInconclusive}, true
	}
	n := int(v.calls.Add(1))
	if n == 1 {
		close(v.first)
	}
	switch n {
	case v.blockAt:
		select {
		case <-v.release:
		case <-time.After(raceWait):
		}
	case v.blockAt + 1:
		close(v.release)
	}
	return journal.Verdict{Outcome: v.outcome, Significant: v.outcome == CohortOutcomePromote}, true
}

// blockingTargeting holds its first Strip until release is closed.
type blockingTargeting struct {
	fakeTargeting
	calls   atomic.Int32
	release chan struct{}
}

func (b *blockingTargeting) Strip(ctx context.Context, flagKey, experimentID string) error {
	if b.calls.Add(1) == 1 { // only the first waits (a sync.Once would block the second caller too)
		select {
		case <-b.release:
		case <-time.After(raceWait):
		}
	}
	return b.fakeTargeting.Strip(ctx, flagKey, experimentID)
}

// syncBuffer is a log sink safe for concurrent writers.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.buf.Write(p)
}

func (s *syncBuffer) count(msg string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return strings.Count(s.buf.String(), "msg="+msg+" ")
}

// concludeRace drives a one-archetype pattern experiment to its last game, then
// ends that game (ConcludeGame, the game-end handler) while the allocation pass
// (AllocateAndSpawn → concludeExhaustedPatterns, UC-046 A6) concludes the same
// experiment. The verdict rendezvous puts both paths past their running check
// before either concludes; holdFirst is wired by the caller into the seam the
// first conclusion blocks in, and is released when the other path has returned.
func concludeRace(t *testing.T, cfg RegistryConfig, verdict *rendezvousVerdict, holdFirst chan struct{}, terminal *atomic.Int32) (*Registry, string) {
	t.Helper()
	spawner := &patternSpawner{}
	cfg.MaxShadowGames, cfg.EffectiveConcurrency, cfg.MaxGamesPerExperiment = 4, 4, -1
	cfg.Spawner, cfg.Verdict = spawner, verdict
	cfg.Patterns = &queuePinner{pins: []patterngame.Pin{testPin(t, "only")}}
	r := newTestRegistry(t, cfg)
	r.SetTerminalHook(func(string) { terminal.Add(1) })
	id := startExperiment(t, r)
	ctx := context.Background()

	lastGame := func(game int) string {
		spawner.fakeSpawner.mu.Lock()
		defer spawner.fakeSpawner.mu.Unlock()
		return spawner.fakeSpawner.calls[game].gameID
	}
	if r.AllocateAndSpawn(ctx) != 1 {
		t.Fatal("first pair game not spawned")
	}
	if _, err := r.ConcludeGame(ctx, lastGame(0), 0.5, 30); err != nil {
		t.Fatal(err)
	}
	if r.AllocateAndSpawn(ctx) != 1 {
		t.Fatal("second pair game not spawned")
	}
	if st, _ := r.Status(id); st != StatusRunning {
		t.Fatalf("status before the last game ends = %s, want running", st)
	}

	verdict.armed.Store(true)
	var wg sync.WaitGroup
	var otherDone sync.Once
	finished := func() { otherDone.Do(func() { close(holdFirst) }) }
	wg.Add(2)
	go func() { // Path A: the last game ends.
		defer wg.Done()
		defer finished()
		if _, err := r.ConcludeGame(ctx, lastGame(1), 0.5, 30); err != nil {
			t.Error(err)
		}
	}()
	select {
	case <-verdict.first: // A has ended the game and is past its lock.
	case <-time.After(raceWait):
		t.Fatal("game-end path never reached the verdict")
	}
	go func() { // Path B: the allocation pass sees the exhausted pattern.
		defer wg.Done()
		defer finished()
		r.AllocateAndSpawn(ctx)
	}()
	wg.Wait()
	return r, id
}

func concludedDecisions(t *testing.T, r *Registry, id string) int {
	t.Helper()
	n := 0
	for _, ev := range eventsOfKind(journalEvents(t, r, id), journal.KindDecision) {
		if ev.Status == string(StatusConcluded) {
			n++
		}
	}
	return n
}

// UC: UC-043 BR-006 A4
// UC: UC-046 A6
func TestRegistryConcludesOnce(t *testing.T) {
	t.Run("UC-043/BR-006 the game end and the allocation pass conclude an exhausted pattern experiment once", func(t *testing.T) {
		target := &blockingTargeting{release: make(chan struct{})}
		logs := &syncBuffer{}
		var terminal atomic.Int32
		r, id := concludeRace(t, RegistryConfig{
			Targeting: target,
			Log:       slog.New(slog.NewTextHandler(logs, nil)),
		}, newRendezvousVerdict(OutcomeInconclusive, 2), target.release, &terminal)
		if got := logs.count("experiment.concluded"); got != 1 {
			t.Errorf("experiment.concluded logged %d times, want 1", got)
		}
		if got := concludedDecisions(t, r, id); got != 1 {
			t.Errorf("concluded decisions in the journal = %d, want 1", got)
		}
		if got := len(target.strippedIDs()); got != 1 {
			t.Errorf("Strip called %d times, want 1", got)
		}
		if got := terminal.Load(); got != 1 {
			t.Errorf("terminal hook fired %d times, want 1", got)
		}
		if st, _ := r.Status(id); st != StatusDiscarded {
			t.Errorf("status = %s, want discarded", st)
		}
	})

	t.Run("UC-043/BR-006 a promote verdict reached by both paths is promoted once", func(t *testing.T) {
		hold := make(chan struct{})
		// Only the first Promote waits (a sync.Once would block the second caller too).
		var calls atomic.Int32
		promoter := &fakePromoter{during: func() {
			if calls.Add(1) == 1 {
				select {
				case <-hold:
				case <-time.After(raceWait):
				}
			}
		}}
		target := &fakeTargeting{}
		var terminal atomic.Int32
		r, id := concludeRace(t, RegistryConfig{Targeting: target, Promoter: promoter},
			newRendezvousVerdict(CohortOutcomePromote, 1), hold, &terminal)
		if got := promoter.count(); got != 1 {
			t.Errorf("Promoter called %d times, want 1", got)
		}
		if got := concludedDecisions(t, r, id); got != 1 {
			t.Errorf("concluded decisions in the journal = %d, want 1", got)
		}
		if got := len(target.strippedIDs()); got != 1 {
			t.Errorf("Strip called %d times, want 1", got)
		}
		if got := terminal.Load(); got != 1 {
			t.Errorf("terminal hook fired %d times, want 1", got)
		}
		if st, _ := r.Status(id); st != StatusDone {
			t.Errorf("status = %s, want done", st)
		}
	})
}
