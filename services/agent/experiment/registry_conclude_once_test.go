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

// armsVerdict returns a promote verdict once armed. Call 1 waits for call 2, so
// both games are recorded before either concludes; call 2 then waits until the
// first conclusion is parked in the Promoter, so it finds the experiment no
// longer live (#2034).
type armsVerdict struct {
	armed  atomic.Bool
	calls  atomic.Int32
	second chan struct{}
	parked chan struct{}
}

func (v *armsVerdict) Evaluate(journal.Summary) (journal.Verdict, bool) {
	if !v.armed.Load() {
		return journal.Verdict{Outcome: OutcomeInconclusive}, true
	}
	switch v.calls.Add(1) {
	case 1:
		select {
		case <-v.second:
		case <-time.After(raceWait):
		}
	case 2:
		close(v.second)
		select {
		case <-v.parked:
		case <-time.After(raceWait):
		}
	}
	return journal.Verdict{Outcome: CohortOutcomePromote, Significant: true}, true
}

// UC: UC-043 BR-006
func TestRegistryConcludesOnceWhenBothArmsEnd(t *testing.T) {
	t.Run("UC-043/BR-006 the control and experimental games ending together conclude the experiment once", func(t *testing.T) {
		verdict := &armsVerdict{second: make(chan struct{}), parked: make(chan struct{})}
		// The first Promote parks the winner, then holds it on a timer only: no
		// channel from the losing caller, so under -race an unlocked read of the
		// status by the loser is unordered with the winner's later write.
		var promotes atomic.Int32
		promoter := &fakePromoter{during: func() {
			if promotes.Add(1) == 1 {
				close(verdict.parked)
				time.Sleep(100 * time.Millisecond)
			}
		}}
		target := &fakeTargeting{}
		logs := &syncBuffer{}
		spawner := &fakeSpawner{}
		r := newTestRegistry(t, RegistryConfig{
			MaxShadowGames: 2,
			Spawner:        spawner,
			Verdict:        verdict,
			Promoter:       promoter,
			Targeting:      target,
			Log:            slog.New(slog.NewTextHandler(logs, nil)),
		})
		var terminal atomic.Int32
		r.SetTerminalHook(func(string) { terminal.Add(1) })
		ctx := context.Background()
		id := startExperiment(t, r)
		if n := r.AllocateAndSpawn(ctx); n != 2 {
			t.Fatalf("spawned %d games, want one per arm", n)
		}

		verdict.armed.Store(true)
		statuses := make([]Status, 2)
		var wg sync.WaitGroup
		for i := range 2 {
			spawner.mu.Lock()
			gameID := spawner.calls[i].gameID
			spawner.mu.Unlock()
			wg.Add(1)
			go func() {
				defer wg.Done()
				st, err := r.ConcludeGame(ctx, gameID, 0.5, 30)
				if err != nil {
					t.Error(err)
				}
				statuses[i] = st
			}()
		}
		wg.Wait()

		if got := promoter.count(); got != 1 {
			t.Errorf("Promoter called %d times, want 1", got)
		}
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
		if st, _ := r.Status(id); st != StatusDone {
			t.Errorf("status = %s, want done", st)
		}
		// One caller concluded (done); the other got the status current when it
		// looked (promoting, or done if it was slower than the winner's hold).
		for _, st := range statuses {
			if st != StatusDone && st != StatusPromoting {
				t.Errorf("ConcludeGame returned %s, want done or promoting", st)
			}
		}
	})
}
