package decision

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"

	"github.com/joustmania/agent/flags"
)

// Remote-path effect measurement (#1699, D-A20): a decision that lands on the
// remote path, and the rules fallback that runs at landing, carry the cycle's
// fitness baseline, so a dispatched one gets a decision.intervention_id and an
// agent.intervention.effect sample like a decision of the rules engine's own cycle.

// allScheduler keeps every scheduled follow-up (a fallback cycle can dispatch
// more than one decision) so the test fires all windows; manualScheduler keeps
// only the last one.
type allScheduler struct {
	mu  sync.Mutex
	fns []func()
}

func (a *allScheduler) after(_ time.Duration, fn func()) func() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.fns = append(a.fns, fn)
	return func() bool { return true }
}

func (a *allScheduler) fireAll() {
	a.mu.Lock()
	fns := append([]func(){}, a.fns...)
	a.mu.Unlock()
	for _, fn := range fns {
		fn()
	}
}

// remoteEffectIDs runs one remote cycle with a manual effect timer, checks every
// dispatched agent.decision span carries an intervention id, fires every
// scheduled window and returns the ids and the effect spans.
func remoteEffectIDs(t *testing.T, h remoteHarness) (map[string]bool, []sdktrace.ReadOnlySpan) {
	t.Helper()
	sched := &allScheduler{}
	h.loop.afterFunc = sched.after
	// h.run joins the effect samplers too (AwaitInflight), which wait for the
	// windows fired below; join only the remote call here.
	h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
	h.loop.inferWG.Wait()

	ids := map[string]bool{}
	for _, dec := range spansByName(h.sr.Ended(), SpanDecision) {
		attrs := attrMap(dec.Attributes())
		if attrs[AttrDecisionBlocked] == true {
			continue
		}
		id, _ := attrs[AttrDecisionInterventionID].(string)
		if id == "" {
			t.Fatal("dispatched remote-path decision carries no decision.intervention_id (no effect sample scheduled)")
		}
		ids[id] = true
	}
	if len(ids) == 0 {
		t.Fatal("remote path dispatched no decision")
	}
	sched.fireAll()
	h.loop.AwaitEffects()
	return ids, spansByName(h.sr.Ended(), SpanInterventionEffect)
}

// effectSnapshot is remoteSnapshot with the default endurance threshold
// (fitness.endurance.min_session_seconds), so the landing has a signal to measure.
func effectSnapshot(timeout time.Duration) flags.Snapshot {
	s := remoteSnapshot(timeout)
	s.Fitness.EnduranceMinSessionSeconds = 120
	return s
}

func assertEffectSpansFor(t *testing.T, ids map[string]bool, effects []sdktrace.ReadOnlySpan) {
	t.Helper()
	if len(effects) == 0 {
		t.Fatal("no agent.intervention.effect span for the remote-path decision")
	}
	for _, s := range effects {
		got, _ := attrMap(s.Attributes())[AttrEffectInterventionID].(string)
		if !ids[got] {
			t.Errorf("effect span intervention.id = %q, want one of %v", got, ids)
		}
	}
}

// UC: UC-040 main
func TestRemote_ActSchedulesEffectSample(t *testing.T) {
	t.Run("UC-040/main remote act gets an effect sample", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.9, "A", 0.5, "shield"))
		h := newRemoteHarness(t, effectSnapshot(time.Second), be)
		ids, effects := remoteEffectIDs(t, h)
		assertEffectSpansFor(t, ids, effects)
	})
}

// UC: UC-040 A2
func TestRemote_FallbackSchedulesEffectSample(t *testing.T) {
	t.Run("UC-040/A2 remote-path rules fallback gets an effect sample", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.9, "A", 0.5, ""))
		be.block = true
		h := newRemoteHarness(t, effectSnapshot(20*time.Millisecond), be)
		ids, effects := remoteEffectIDs(t, h)
		assertEffectSpansFor(t, ids, effects)
	})
}

// errSink fails every write (UC-040 A27).
type errSink struct{}

func (errSink) Apply(context.Context, Decision) error { return errors.New("write failed") }

// UC: UC-040 A11 A27
func TestEffectSample_OnlyForWrittenDecisions(t *testing.T) {
	for _, tc := range []struct {
		name string
		sink ActionSink
		want int
	}{
		{"UC-040/A11 gated no-op gets no effect sample", &gatedSink{gateOn: false}, 0},
		{"UC-040/A27 failed write gets no effect sample", errSink{}, 0},
		{"UC-040/main written decision gets one effect sample", &gatedSink{gateOn: true}, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.9, "A", 0.5, "shield"))
			h := newRemoteHarness(t, effectSnapshot(time.Second), be)
			h.loop.Actions = tc.sink
			sched := &allScheduler{}
			h.loop.afterFunc = sched.after
			h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
			h.loop.inferWG.Wait()

			ids := 0
			for _, dec := range spansByName(h.sr.Ended(), SpanDecision) {
				if id, _ := attrMap(dec.Attributes())[AttrDecisionInterventionID].(string); id != "" {
					ids++
				}
			}
			if ids != tc.want {
				t.Errorf("decisions with decision.intervention_id = %d, want %d", ids, tc.want)
			}
			sched.mu.Lock()
			scheduled := len(sched.fns)
			sched.mu.Unlock()
			if scheduled != tc.want {
				t.Errorf("effect samples scheduled = %d, want %d", scheduled, tc.want)
			}
			sched.fireAll()
			h.loop.AwaitEffects()
			if got := len(spansByName(h.sr.Ended(), SpanInterventionEffect)) > 0; got != (tc.want > 0) {
				t.Errorf("agent.intervention.effect spans present = %v, want %v", got, tc.want > 0)
			}
		})
	}
}
