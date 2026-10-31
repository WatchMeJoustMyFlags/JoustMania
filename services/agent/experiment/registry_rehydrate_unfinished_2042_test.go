package experiment

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/joustmania/agent/experiment/journal"
)

// String returns everything logged so far (syncBuffer: registry_conclude_once_test.go).
func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// crashStage describes the journal an agent left behind when it stopped hard
// between a conclusion and the end of its teardown (#2042).
type crashStage struct {
	verdict   string           // outcome of the interim verdict journaled before the conclusion ("" = none)
	promoting bool             // a promoting decision follows the concluded decision
	promotion *PromotionResult // a promotion event follows (after promoting, or after concluded for a legacy journal)
}

// restartHarness is the registry after the restart, with its seams.
type restartHarness struct {
	r         *Registry
	root      string
	id        string
	promoter  *fakePromoter
	targeting *fakeTargeting
	logs      *syncBuffer
	mu        sync.Mutex
	terminal  []string
}

func (h *restartHarness) terminalCount() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.terminal)
}

func newRestartRegistry(root string, h *restartHarness) *Registry {
	r := NewRegistry(RegistryConfig{
		Root:           root,
		MaxShadowGames: 4,
		Promoter:       h.promoter,
		Targeting:      h.targeting,
		Clock:          fixedClock(),
		Log:            slog.New(slog.NewTextHandler(h.logs, nil)),
	})
	r.SetTerminalHook(func(id string) {
		h.mu.Lock()
		h.terminal = append(h.terminal, id)
		h.mu.Unlock()
	})
	return r
}

// crashAt runs an experiment to the given crash point in a first registry, drops
// that registry (the hard stop) and builds a second one over the same journal root.
// It does not rehydrate yet.
func crashAt(t *testing.T, stage crashStage) *restartHarness {
	t.Helper()
	root := t.TempDir()
	ctx := context.Background()
	r1 := NewRegistry(RegistryConfig{Root: root, MaxShadowGames: 4, Clock: fixedClock(), Targeting: &fakeTargeting{}})
	id, err := r1.Declare(sampleIntent())
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := r1.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}
	r1.mu.Lock()
	jrnl := r1.entries[id].journal
	r1.mu.Unlock()
	appendOrFail := func(e journal.Event) {
		t.Helper()
		if err := jrnl.AppendEvent(e); err != nil {
			t.Fatalf("append %s: %v", e.Kind, err)
		}
	}
	if stage.verdict != "" {
		appendOrFail(journal.Event{Kind: journal.KindInterimVerdict, Verdict: &journal.Verdict{Outcome: stage.verdict}})
	}
	appendOrFail(journal.Event{Kind: journal.KindDecision, Status: string(StatusConcluded), Note: "target N reached / verdict " + stage.verdict})
	if stage.promoting {
		appendOrFail(journal.Event{Kind: journal.KindDecision, Status: string(StatusPromoting), Note: "routing to promoter"})
	}
	if stage.promotion != nil {
		p := *stage.promotion
		appendOrFail(journal.Event{Kind: journal.KindPromotion, Status: p.Outcome, Note: p.Reason, Promotion: &p})
	}

	h := &restartHarness{root: root, id: id, promoter: &fakePromoter{}, targeting: &fakeTargeting{}, logs: &syncBuffer{}}
	h.r = newRestartRegistry(root, h)
	return h
}

func (h *restartHarness) rehydrate(t *testing.T) {
	t.Helper()
	ids, err := journal.List(h.root)
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if err := h.r.Rehydrate(ids); err != nil {
		t.Fatalf("Rehydrate: %v", err)
	}
}

// outcomeNote returns the note of the single outcome event, failing on any other count.
func (h *restartHarness) outcomeNote(t *testing.T) string {
	t.Helper()
	outcomes := eventsOfKind(journalEvents(t, h.r, h.id), journal.KindOutcome)
	if len(outcomes) != 1 {
		t.Fatalf("outcome events = %+v, want exactly one", outcomes)
	}
	return outcomes[0].Note
}

// assertFinished checks the common A15 / BR-006 end state: terminal status (in
// memory and in the journal), never promoted, one strip, one terminal notice, one
// concluded decision, budget and flag freed, and a second rehydrate is a no-op.
func (h *restartHarness) assertFinished(t *testing.T, want Status) {
	t.Helper()
	if st, _ := h.r.Status(h.id); st != want {
		t.Fatalf("status = %q, want %q", st, want)
	}
	if view, _ := h.r.CompactView(h.id); view.Status != string(want) {
		t.Errorf("journaled status = %q, want %q", view.Status, want)
	}
	if n := h.promoter.count(); n != 0 {
		t.Errorf("promoter called %d times, want 0 (a restored experiment is never promoted)", n)
	}
	if got := h.targeting.strippedIDs(); len(got) != 1 || got[0] != h.id {
		t.Errorf("stripped = %v, want [%s]", got, h.id)
	}
	if n := h.terminalCount(); n != 1 {
		t.Errorf("terminal hook fired %d times, want 1", n)
	}
	evs := journalEvents(t, h.r, h.id)
	concluded := 0
	for _, e := range evs {
		if e.Kind == journal.KindDecision && e.Status == string(StatusConcluded) {
			concluded++
		}
	}
	if concluded != 1 {
		t.Errorf("concluded decisions = %d, want 1 (BR-006)", concluded)
	}
	if n := h.r.LiveCount(); n != 0 {
		t.Errorf("LiveCount = %d, want 0 (the budget slot is freed)", n)
	}
	if _, held := h.r.ActiveFlags()[sampleIntent().FlagKey]; held {
		t.Error("the restored experiment still holds its flag under test")
	}

	// A second rehydrate in the same process changes nothing.
	h.rehydrate(t)
	if n := h.promoter.count(); n != 0 {
		t.Errorf("second rehydrate: promoter called %d times", n)
	}
	if got := h.targeting.strippedIDs(); len(got) != 1 {
		t.Errorf("second rehydrate: stripped = %v, want one strip", got)
	}
	// A third agent start finds the experiment terminal and skips it.
	h2 := &restartHarness{promoter: &fakePromoter{}, targeting: &fakeTargeting{}, logs: &syncBuffer{}}
	r3 := newRestartRegistry(h.root, h2)
	if err := r3.Rehydrate([]string{h.id}); err != nil {
		t.Fatalf("third rehydrate: %v", err)
	}
	if _, known := r3.Status(h.id); known {
		t.Error("third start restored the experiment again: its terminal status was not journaled")
	}
}

// UC: UC-043 A15 BR-006 BR-007
func TestRehydrateFinishesUnfinishedConclusion_2042(t *testing.T) {
	t.Run("UC-043/A15 a concluded experiment with a discard verdict is discarded", func(t *testing.T) {
		h := crashAt(t, crashStage{verdict: CohortOutcomeDiscard})
		h.rehydrate(t)
		h.assertFinished(t, StatusDiscarded)
		if note := h.outcomeNote(t); note != "concluded before restart: discard; not promoted" {
			t.Errorf("outcome note = %q", note)
		}
	})

	t.Run("UC-043/A15 a concluded experiment with a promote verdict is discarded, not promoted", func(t *testing.T) {
		h := crashAt(t, crashStage{verdict: CohortOutcomePromote})
		h.rehydrate(t)
		h.assertFinished(t, StatusDiscarded)
		if note := h.outcomeNote(t); note != "concluded before restart: promote; not promoted" {
			t.Errorf("outcome note = %q", note)
		}
	})

	t.Run("UC-043/A15 a concluded experiment without a journaled verdict is discarded as unknown", func(t *testing.T) {
		h := crashAt(t, crashStage{})
		h.rehydrate(t)
		h.assertFinished(t, StatusDiscarded)
		if note := h.outcomeNote(t); note != "concluded before restart: unknown; not promoted" {
			t.Errorf("outcome note = %q", note)
		}
	})

	t.Run("UC-043/A15 a promoting experiment with an applied promotion ends done", func(t *testing.T) {
		applied := PromotionResult{Outcome: PromotionApplied, Reference: "https://github.com/o/r/issues/7"}
		h := crashAt(t, crashStage{verdict: CohortOutcomePromote, promoting: true, promotion: &applied})
		h.rehydrate(t)
		h.assertFinished(t, StatusDone)
		if note := h.outcomeNote(t); !strings.Contains(note, "promotion applied") || !strings.Contains(note, "agent restart") {
			t.Errorf("outcome note = %q, want the journaled promotion and the restart", note)
		}
	})

	t.Run("UC-043/A15 a promoting experiment with a reverted promotion ends done", func(t *testing.T) {
		reverted := PromotionResult{Outcome: PromotionReverted, Reason: "real fitness fell"}
		h := crashAt(t, crashStage{verdict: CohortOutcomePromote, promoting: true, promotion: &reverted})
		h.rehydrate(t)
		h.assertFinished(t, StatusDone)
	})

	t.Run("UC-043/A15 a promoting experiment with a discarded promotion ends discarded", func(t *testing.T) {
		discarded := PromotionResult{Outcome: PromotionDiscarded, Reason: "open issue: 502"}
		h := crashAt(t, crashStage{verdict: CohortOutcomePromote, promoting: true, promotion: &discarded})
		h.rehydrate(t)
		h.assertFinished(t, StatusDiscarded)
		if note := h.outcomeNote(t); !strings.Contains(note, "promotion discarded: open issue: 502") {
			t.Errorf("outcome note = %q", note)
		}
	})

	t.Run("UC-043/A15 a legacy concluded journal with a promotion event ends from that event", func(t *testing.T) {
		// Before BR-007 the promoting decision could fail to append while the later
		// promotion event succeeded: the journal reads concluded, yet a promotion acted.
		applied := PromotionResult{Outcome: PromotionApplied, Reference: "local:abc"}
		h := crashAt(t, crashStage{verdict: CohortOutcomePromote, promotion: &applied})
		h.rehydrate(t)
		h.assertFinished(t, StatusDone)
	})

	t.Run("UC-043/A15 a promoting experiment without a promotion event is aborted promotion_interrupted", func(t *testing.T) {
		h := crashAt(t, crashStage{verdict: CohortOutcomePromote, promoting: true})
		h.rehydrate(t)
		h.assertFinished(t, StatusAborted)
		if note := h.outcomeNote(t); note != AbortReasonPromotionInterrupted {
			t.Errorf("outcome note = %q, want %q", note, AbortReasonPromotionInterrupted)
		}
		if n := len(eventsOfKind(journalEvents(t, h.r, h.id), journal.KindPromotion)); n != 0 {
			t.Errorf("promotion events = %d, want 0 (the outcome is unknown, none is invented)", n)
		}
		logs := h.logs.String()
		if !strings.Contains(logs, "level=WARN") || !strings.Contains(logs, "experiment.promotion_interrupted") ||
			!strings.Contains(logs, "flag_key="+sampleIntent().FlagKey) {
			t.Errorf("want one WARN experiment.promotion_interrupted with the flag key, logs:\n%s", logs)
		}
	})
}

// A journal that cannot be loaded does not stop the restore of the others
// (UC-043 A15 step 5): each failure is returned, and every later id is restored.
// UC: UC-043 A15
func TestRehydrateContinuesPastABadJournal_2042(t *testing.T) {
	t.Run("UC-043/A15 a bad id is reported and the next experiment is still finished", func(t *testing.T) {
		h := crashAt(t, crashStage{verdict: CohortOutcomePromote})
		bad := "exp_00000000-0000-0000-0000-000000000000"
		if err := os.MkdirAll(filepath.Join(h.root, bad), 0o755); err != nil {
			t.Fatal(err)
		}
		err := h.r.Rehydrate([]string{bad, h.id})
		if err == nil || !strings.Contains(err.Error(), bad) {
			t.Fatalf("Rehydrate error = %v, want one naming %s", err, bad)
		}
		if st, _ := h.r.Status(h.id); st != StatusDiscarded {
			t.Fatalf("status of the id after the bad one = %q, want discarded", st)
		}
		if !strings.Contains(h.logs.String(), bad) {
			t.Errorf("the bad id is not logged:\n%s", h.logs.String())
		}
	})
}

// promote acts only on a concluded experiment (UC-043 BR-006, BR-007): one already
// promoting is never promoted a second time.
// UC: UC-043 BR-006 BR-007
func TestPromoteRequiresConcluded_2042(t *testing.T) {
	t.Run("UC-043/BR-007 promote on a promoting experiment does not call the promoter", func(t *testing.T) {
		promoter := &fakePromoter{}
		r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 4, Promoter: promoter, Targeting: &fakeTargeting{}})
		id := startExperiment(t, r)
		r.mu.Lock()
		e := r.entries[id]
		e.status = StatusPromoting
		jrnl := e.journal
		r.mu.Unlock()

		res := r.promote(context.Background(), id, jrnl)
		if n := promoter.count(); n != 0 {
			t.Fatalf("promoter called %d times on a promoting experiment, want 0", n)
		}
		if res.Outcome != PromotionDiscarded || !strings.Contains(res.Reason, "not concluded") {
			t.Errorf("result = %+v, want discarded, not concluded", res)
		}
	})
}

// replaceWithDir swaps a journal file for a directory, so the next write to it
// fails (EISDIR) even when the test runs as root.
func replaceWithDir(t *testing.T, path string) {
	t.Helper()
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(path, 0o755); err != nil {
		t.Fatal(err)
	}
}

// The promoter acts only once the promoting decision is durable (UC-043 BR-007).
// UC: UC-043 BR-007
func TestPromotionWriteAhead_2042(t *testing.T) {
	t.Run("UC-043/BR-007 a promoting decision that cannot be journaled promotes nothing", func(t *testing.T) {
		promoter := &fakePromoter{}
		root := t.TempDir()
		r := newTestRegistry(t, RegistryConfig{Root: root, MaxShadowGames: 4, Promoter: promoter, Targeting: &fakeTargeting{}})
		id := startExperiment(t, r)
		r.mu.Lock()
		e := r.entries[id]
		e.status = StatusConcluded
		jrnl := e.journal
		r.mu.Unlock()
		replaceWithDir(t, filepath.Join(root, id, "events.jsonl"))

		res := r.promote(context.Background(), id, jrnl)
		if n := promoter.count(); n != 0 {
			t.Fatalf("promoter called %d times without a durable promoting decision, want 0", n)
		}
		if res.Outcome != PromotionDiscarded || !strings.HasPrefix(res.Reason, "promotion not journaled: ") {
			t.Errorf("result = %+v, want discarded, promotion not journaled", res)
		}
	})

	t.Run("UC-043/BR-007 a one-shot journal failure journals the refusal once and a restart ends discarded", func(t *testing.T) {
		promoter := &fakePromoter{}
		root := t.TempDir()
		path := ""
		var original []byte
		// Deterministic seam: every journal append stamps its time from the registry clock
		// first. Once armed, the first stamp belongs to the failing promoting append; the
		// second belongs to the refusal append, where the journal is made writable again.
		var armed bool
		var stamps int
		base := fixedClock()
		restore := func() {
			if err := os.Remove(path); err != nil {
				t.Error(err)
				return
			}
			if err := os.WriteFile(path, original, 0o644); err != nil {
				t.Error(err)
			}
		}
		clock := func() time.Time {
			if armed {
				stamps++
				if stamps == 2 {
					restore()
				}
			}
			return base()
		}
		r := newTestRegistry(t, RegistryConfig{
			Root: root, MaxShadowGames: 4, Promoter: promoter, Targeting: &fakeTargeting{},
			Clock: clock,
		})
		id := startExperiment(t, r)
		r.mu.Lock()
		e := r.entries[id]
		e.status = StatusConcluded
		jrnl := e.journal
		r.mu.Unlock()
		if err := jrnl.AppendEvent(journal.Event{Kind: journal.KindDecision, Status: string(StatusConcluded), Note: "target N reached"}); err != nil {
			t.Fatal(err)
		}
		path = filepath.Join(root, id, "events.jsonl")
		var err error
		if original, err = os.ReadFile(path); err != nil {
			t.Fatal(err)
		}
		replaceWithDir(t, path)
		armed = true

		res := r.promote(context.Background(), id, jrnl)
		if stamps != 2 {
			t.Fatalf("journal appends after arming = %d, want 2 (promoting, refusal)", stamps)
		}
		if n := promoter.count(); n != 0 {
			t.Fatalf("promoter called %d times, want 0", n)
		}
		if !strings.HasPrefix(res.Reason, "promotion not journaled: ") {
			t.Fatalf("result = %+v, want promotion not journaled", res)
		}
		promotions := eventsOfKind(journalEvents(t, r, id), journal.KindPromotion)
		if len(promotions) != 1 || promotions[0].Status != PromotionDiscarded {
			t.Fatalf("promotion events = %+v, want exactly one discarded", promotions)
		}

		h := &restartHarness{root: root, id: id, promoter: &fakePromoter{}, targeting: &fakeTargeting{}, logs: &syncBuffer{}}
		h.r = newRestartRegistry(root, h)
		h.rehydrate(t)
		if st, _ := h.r.Status(id); st != StatusDiscarded {
			t.Errorf("status after restart = %q, want discarded", st)
		}
		if note := h.outcomeNote(t); note == AbortReasonPromotionInterrupted {
			t.Errorf("outcome note = %q, want a discarded promotion, not an interrupted one", note)
		}
	})

	t.Run("UC-043/BR-007 a conclusion whose promoting decision fails ends discarded unpromoted", func(t *testing.T) {
		promoter := &fakePromoter{}
		root := t.TempDir()
		spawner := &fakeSpawner{}
		r := newTestRegistry(t, RegistryConfig{
			Root: root, MaxShadowGames: 2, Spawner: spawner, Promoter: promoter, Targeting: &fakeTargeting{},
			Verdict: fakeVerdict{outcome: CohortOutcomePromote},
		})
		id := startExperiment(t, r)
		ctx := context.Background()
		r.AllocateAndSpawn(ctx)
		if spawner.count() == 0 {
			t.Fatal("nothing spawned")
		}
		replaceWithDir(t, filepath.Join(root, id, "events.jsonl"))

		st, err := r.ConcludeGame(ctx, spawner.calls[0].gameID, 0.9, 60)
		if err != nil {
			t.Fatalf("ConcludeGame: %v", err)
		}
		if st != StatusDiscarded {
			t.Errorf("status = %q, want discarded", st)
		}
		if n := promoter.count(); n != 0 {
			t.Errorf("promoter called %d times, want 0", n)
		}
	})

	t.Run("UC-043/BR-007 a summary rewrite failure after a durable decision still promotes", func(t *testing.T) {
		promoter := &fakePromoter{}
		root := t.TempDir()
		r := newTestRegistry(t, RegistryConfig{Root: root, MaxShadowGames: 4, Promoter: promoter, Targeting: &fakeTargeting{}})
		id := startExperiment(t, r)
		r.mu.Lock()
		e := r.entries[id]
		e.status = StatusConcluded
		jrnl := e.journal
		r.mu.Unlock()
		// summary.json as a non-empty directory: the atomic rename onto it fails,
		// after the event line was appended and fsynced.
		summary := filepath.Join(root, id, "summary.json")
		replaceWithDir(t, summary)
		if err := os.WriteFile(filepath.Join(summary, "keep"), nil, 0o644); err != nil {
			t.Fatal(err)
		}

		res := r.promote(context.Background(), id, jrnl)
		if n := promoter.count(); n != 1 {
			t.Fatalf("promoter called %d times, want 1 (the promoting decision is durable)", n)
		}
		if !promotionActed(res) {
			t.Errorf("result = %+v, want the promoter's applied result", res)
		}
		evs, err := jrnl.Events()
		if err != nil {
			t.Fatal(err)
		}
		promoting := 0
		for _, ev := range evs {
			if ev.Kind == journal.KindDecision && ev.Status == string(StatusPromoting) {
				promoting++
			}
		}
		if promoting != 1 {
			t.Errorf("promoting decisions in the log = %d, want 1", promoting)
		}
	})
}
