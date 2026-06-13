package decision

import (
	"context"
	"sync"
	"testing"

	"github.com/joustmania/agent/gamecontext"
)

// TestAsync_RecordsDispatchedThroughApply covers the in-scope #945 async wiring: the
// async apply path (#917) dispatches re-validated model decisions through the SAME
// runDecision, so the per-game recorder fires there too. A fresh, applied async
// result records a DISPATCHED intervention into the timeline, consistent with the
// synchronous path — no separate async hook needed.
func TestAsync_RecordsDispatchedThroughApply(t *testing.T) {
	be := newBlockingBackend("phi4-mini", validShieldResponse)
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, _, _ := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1", nil)
	rec := &fakeRecorder{}
	l.SetInterventionRecorder(rec.record)

	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
	close(be.release)
	l.AwaitInflight()

	hits := rec.snapshot()
	if len(hits) != 1 {
		t.Fatalf("recorder hits = %d, want 1 (async-applied decision recorded)", len(hits))
	}
	if hits[0].intervention != "grant_shield" || hits[0].blocked {
		t.Errorf("hit = %+v, want dispatched grant_shield via async apply", hits[0])
	}
}

// intervention_record_test.go covers the #945 wiring: the per-game recorder seam
// (SetInterventionRecorder) feeds each decision's dispatched/blocked outcome into
// the #916 rolling narrative timeline. These tests assert the Loop->recorder
// contract directly (recorder is called with the right intervention/serial/blocked
// for both outcomes, is nil-safe, isolates per game, and is race-safe); the
// receiver-side routing into the right partition's Store is covered in
// gamecontext/multiplexer_test.go (TestMux_AppendInterventionEvent*).

// recordedIntervention is one captured recorder call.
type recordedIntervention struct {
	intervention string
	serial       string
	blocked      bool
}

// fakeRecorder captures every recorder call under its own mutex, mirroring the
// Store.AppendInterventionEvent mutex guarantee so the -race tests are honest.
type fakeRecorder struct {
	mu   sync.Mutex
	hits []recordedIntervention
}

func (r *fakeRecorder) record(intervention, serial string, blocked bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.hits = append(r.hits, recordedIntervention{intervention, serial, blocked})
}

func (r *fakeRecorder) snapshot() []recordedIntervention {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]recordedIntervention, len(r.hits))
	copy(out, r.hits)
	return out
}

// TestRunDecision_RecordsDispatched: a decision that passes the permission chain is
// recorded as a DISPATCHED intervention (blocked=false) with its id + target serial.
func TestRunDecision_RecordsDispatched(t *testing.T) {
	l, _, fl := newTestLoop(t)
	fl.snap.InterventionsAllowed = []string{"grant_shield"}
	rec := &fakeRecorder{}
	l.SetInterventionRecorder(rec.record)
	l.Actions = &fakeSink{}
	l.Rules = fakeRules{out: []Decision{{Intervention: "grant_shield", TargetSerial: "AA:11", Reason: "test"}}}

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "game-1"}, testTrigger())

	hits := rec.snapshot()
	if len(hits) != 1 {
		t.Fatalf("recorder hits = %d, want 1", len(hits))
	}
	if hits[0] != (recordedIntervention{"grant_shield", "AA:11", false}) {
		t.Errorf("hit = %+v, want dispatched grant_shield -> AA:11", hits[0])
	}
}

// TestRunDecision_RecordsBlocked: a decision blocked by a permission gate (here the
// allow-list: the intervention is not in interventions_allowed) is recorded as a
// BLOCKED intervention (blocked=true), never silently dropped.
func TestRunDecision_RecordsBlocked(t *testing.T) {
	l, _, fl := newTestLoop(t)
	// Empty allow-list -> ReasonNotAllowed blocks the decision.
	fl.snap.InterventionsAllowed = nil
	rec := &fakeRecorder{}
	l.SetInterventionRecorder(rec.record)
	sink := &fakeSink{}
	l.Actions = sink
	l.Rules = fakeRules{out: []Decision{{Intervention: "grant_shield", TargetSerial: "BB:22", Reason: "test"}}}

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "game-1"}, testTrigger())

	hits := rec.snapshot()
	if len(hits) != 1 {
		t.Fatalf("recorder hits = %d, want 1", len(hits))
	}
	if hits[0] != (recordedIntervention{"grant_shield", "BB:22", true}) {
		t.Errorf("hit = %+v, want blocked grant_shield -> BB:22", hits[0])
	}
	// A blocked decision must NOT have reached the action sink, but MUST be recorded.
	if c := sink.calls.Load(); c != 0 {
		t.Errorf("blocked decision reached sink %d times, want 0", c)
	}
}

// TestRunDecision_BlockedByRateLimit: the rate-limit gate also records a blocked
// intervention, confirming the recorder fires for every BlockReason (allow-list,
// battery, rate-limit), not just the allow-list.
func TestRunDecision_BlockedByRateLimit(t *testing.T) {
	l, _, fl := newTestLoop(t)
	fl.snap.InterventionsAllowed = []string{"grant_shield"}
	fl.snap.Policy.MaxInterventionsPerMinute = 0 // budget 0 -> every dispatch is rate-limited
	rec := &fakeRecorder{}
	l.SetInterventionRecorder(rec.record)
	l.Actions = &fakeSink{}
	l.Rules = fakeRules{out: []Decision{{Intervention: "grant_shield", Reason: "test"}}}

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "game-1"}, testTrigger())

	hits := rec.snapshot()
	if len(hits) != 1 || !hits[0].blocked {
		t.Fatalf("recorder hits = %+v, want one blocked", hits)
	}
}

// TestRunDecision_NilRecorderUnchanged: a Loop without a recorder (the construction
// default and all pre-#945 tests) records nothing and otherwise behaves exactly as
// before — the dispatch still reaches the action sink.
func TestRunDecision_NilRecorderUnchanged(t *testing.T) {
	l, _, fl := newTestLoop(t)
	fl.snap.InterventionsAllowed = []string{"grant_shield"}
	sink := &fakeSink{}
	l.Actions = sink
	l.Rules = fakeRules{out: []Decision{{Intervention: "grant_shield", Reason: "test"}}}
	// No SetInterventionRecorder call: l.interventionRecorder stays nil.

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "game-1"}, testTrigger())

	if c := sink.calls.Load(); c != 1 {
		t.Errorf("dispatch reached sink %d times, want 1 (nil recorder must not change behavior)", c)
	}
}

// TestRunDecision_PerGameRecorderIsolation: two Loops with DIFFERENT recorders (as
// the per-game factory builds them, each closing over its own partition's Store)
// record to their own sinks with no cross-bleed — the #845 isolation the #917
// ContextProvider wiring also preserves, extended to interventions (#945).
func TestRunDecision_PerGameRecorderIsolation(t *testing.T) {
	makeLoop := func(rec *fakeRecorder) *Loop {
		l, _, fl := newTestLoop(t)
		fl.snap.InterventionsAllowed = []string{"grant_shield"}
		l.SetInterventionRecorder(rec.record)
		l.Actions = &fakeSink{}
		return l
	}
	recA, recB := &fakeRecorder{}, &fakeRecorder{}
	loopA := makeLoop(recA)
	loopB := makeLoop(recB)
	loopA.Rules = fakeRules{out: []Decision{{Intervention: "grant_shield", TargetSerial: "AA:11"}}}
	loopB.Rules = fakeRules{out: []Decision{{Intervention: "grant_shield", TargetSerial: "BB:22"}}}

	loopA.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "game-A"}, testTrigger())
	loopB.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "game-B"}, testTrigger())

	a, b := recA.snapshot(), recB.snapshot()
	if len(a) != 1 || a[0].serial != "AA:11" {
		t.Errorf("recorder A = %+v, want only AA:11 (no bleed from B)", a)
	}
	if len(b) != 1 || b[0].serial != "BB:22" {
		t.Errorf("recorder B = %+v, want only BB:22 (no bleed from A)", b)
	}
}

// TestRunDecision_ConcurrentRecordRaceFree drives one Loop from many goroutines
// (as the concurrent trace + metrics Export handlers do) to confirm the recorder
// path is race-safe under -race. The recorder reaches the mutex-guarded
// Store.AppendInterventionEvent in production; the fakeRecorder mirrors that lock.
func TestRunDecision_ConcurrentRecordRaceFree(t *testing.T) {
	l, _, fl := newTestLoop(t)
	fl.snap.InterventionsAllowed = []string{"grant_shield"}
	rec := &fakeRecorder{}
	l.SetInterventionRecorder(rec.record)
	l.Actions = &fakeSink{}
	l.Rules = fakeRules{out: []Decision{{Intervention: "grant_shield", TargetSerial: "CC:33"}}}

	const goroutines = 16
	var wg sync.WaitGroup
	wg.Add(goroutines)
	for i := 0; i < goroutines; i++ {
		go func() {
			defer wg.Done()
			l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "game-1"}, testTrigger())
		}()
	}
	wg.Wait()

	// Every cycle whose decision was admitted by the (generous) rate limit records
	// exactly once; the count is bounded by goroutines and must be at least 1.
	if n := len(rec.snapshot()); n < 1 || n > goroutines {
		t.Fatalf("recorder hits = %d, want 1..%d", n, goroutines)
	}
}
