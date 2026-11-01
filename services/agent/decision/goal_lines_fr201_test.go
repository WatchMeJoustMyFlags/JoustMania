package decision

import (
	"context"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/decide/llmio"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/goal"
	"github.com/joustmania/agent/telemetry/keys"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// UC: UC-042 A12 BR-009
// goal_lines_fr201_test.go: a claude, openai or jev (#2498) request carries the
// goal lines only under decision.goal_in_prompt, never for neutral alone (#2479, FR-201,
// decide-contract.md §14.4). Under v1 with goal_in_prompt off the request, and so
// the prompt bytes, are unchanged.

func testGoalProfile() goal.Profile {
	return goal.FromObjectives(map[string]float64{"endurance": 0.7, "balanced": 0.1}, 180, 60)
}

// goalRun is one remote decide cycle with a goal source wired: the request, the
// agent.decide span and the decisions dispatched to the sink.
type goalRun struct {
	req        decide.Request
	span       sdktrace.ReadOnlySpan
	dispatched []Decision
}

func (r goalRun) attr(key string) (string, bool) {
	v, ok := attrValue(r.span, key)
	return v.Emit(), ok
}

func runGoal(t *testing.T, goalInPrompt bool, profile string, backend string) goalRun {
	t.Helper()
	snap := remoteSnapshot(time.Second)
	snap.Decision.GoalInPrompt = goalInPrompt
	be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.9, "A", 0.5, "shield"))
	if backend != "" {
		snap.Decision.Backend = backend
		be.name = backend
		snap.Decision.Jev = snap.Decision.Claude
		snap.Decision.OpenAI = snap.Decision.Claude
	}
	h := newRemoteHarness(t, snap, be)
	h.loop.SetPromptProfileSource(func(context.Context, string) string { return profile })
	h.loop.SetGoalSource(func(_ context.Context, c gamecontext.GameContext) goal.Profile {
		if c.SessionID != remoteGameID {
			t.Errorf("goal read for %q", c.SessionID)
		}
		return testGoalProfile()
	})
	span := h.run(t)
	return goalRun{req: be.reqs[0], span: span, dispatched: h.sink.decisions()}
}

func TestRemote_GoalLines(t *testing.T) {
	t.Run("UC-042/A12 v1 with goal_in_prompt off: no lines, the prompt bytes unchanged", func(t *testing.T) {
		r := runGoal(t, false, "v1", "")
		if _, ok := r.attr(keys.AttrDecideGoalProfile); r.req.Goal != nil || ok {
			t.Fatalf("goal lines %+v, span attribute %v", r.req.Goal, ok)
		}
	})
	t.Run("UC-042/A12 goal_in_prompt on adds the lines under v1, labelled by source", func(t *testing.T) {
		r := runGoal(t, true, "v1", "")
		profile, ok := r.attr(keys.AttrDecideGoalProfile)
		source, _ := r.attr(keys.AttrDecideGoalSource)
		if r.req.Goal == nil || !ok || profile != goal.NameLongRunningGame || source != goal.SourceFromObjectives {
			t.Fatalf("goal lines %+v, span %q/%q (%v)", r.req.Goal, profile, source, ok)
		}
		if r.req.Goal.Goal != "long_running_game duration>=180s activity=0.30..0.60 elimination_pace<=1/min" {
			t.Fatalf("goal line = %q", r.req.Goal.Goal)
		}
		if !strings.HasPrefix(r.req.Goal.Now, "duration=") {
			t.Fatalf("now line = %q", r.req.Goal.Now)
		}
		if p := llmio.Render(r.req); strings.Contains(p.System, "pursue the goal targets") {
			t.Fatal("v1 must keep its system prompt")
		}
	})
	t.Run("UC-042/A12 neutral alone carries no goal lines (a separate factor)", func(t *testing.T) {
		r := runGoal(t, false, "neutral", "")
		if _, ok := r.attr(keys.AttrDecideGoalProfile); r.req.Goal != nil || ok {
			t.Fatalf("neutral without goal_in_prompt: goal lines %+v, span attribute %v", r.req.Goal, ok)
		}
		if p := llmio.Render(r.req); !strings.Contains(p.System, "closer, more dramatic") || strings.Contains(p.User, "\ngoal: ") {
			t.Fatal("neutral without goal lines must keep the #2467 neutral prompt")
		}
	})
	t.Run("UC-042/A12 neutral with goal_in_prompt: the goal line and no prose goal", func(t *testing.T) {
		r := runGoal(t, true, "neutral", "")
		if p := llmio.Render(r.req); strings.Contains(p.System, "closer, more dramatic") || !strings.Contains(p.User, "\ngoal: long_running_game ") {
			t.Fatal("neutral with goal lines must carry the goal line and no prose goal")
		}
	})
	t.Run("UC-042/A12 jev gets the goal lines under goal_in_prompt (#2498), in both profiles", func(t *testing.T) {
		for _, profile := range []string{"v1", "neutral"} {
			r := runGoal(t, true, profile, flags.DecisionBackendJev)
			if got, ok := r.attr(keys.AttrDecideGoalProfile); r.req.Goal == nil || !ok || got != goal.NameLongRunningGame {
				t.Fatalf("%s: jev goal lines %+v, span attribute %q (%v)", profile, r.req.Goal, got, ok)
			}
		}
	})
	t.Run("UC-042/A12 jev with goal_in_prompt off gets no goal lines, neutral included", func(t *testing.T) {
		for _, profile := range []string{"v1", "neutral"} {
			r := runGoal(t, false, profile, flags.DecisionBackendJev)
			if _, ok := r.attr(keys.AttrDecideGoalProfile); r.req.Goal != nil || ok {
				t.Fatalf("%s: jev goal lines %+v, span attribute %v", profile, r.req.Goal, ok)
			}
		}
	})
	t.Run("UC-042/BR-009 the goal lines change neither the request's state and questions nor what lands", func(t *testing.T) {
		with := runGoal(t, true, "v1", "")
		without := runGoal(t, false, "v1", "")
		if with.req.Goal == nil {
			t.Fatal("the comparison is vacuous: no goal lines")
		}
		if with.req.State.Digest != without.req.State.Digest || !reflect.DeepEqual(with.req.Questions, without.req.Questions) {
			t.Fatal("goal lines must only change the words a backend renders")
		}
		for _, key := range []string{AttrDecideOutcome, AttrDecideBlocked, AttrDecideBlockReason, AttrDecideDiscardedReason} {
			w, wok := with.attr(key)
			o, ook := without.attr(key)
			if w != o || wok != ook {
				t.Errorf("%s with goal lines = %q (%v), without = %q (%v)", key, w, wok, o, ook)
			}
		}
		if len(with.dispatched) == 0 {
			t.Fatal("the comparison is vacuous: nothing dispatched")
		}
		if len(with.dispatched) != len(without.dispatched) {
			t.Fatalf("dispatched %d with goal lines, %d without", len(with.dispatched), len(without.dispatched))
		}
		for i := range with.dispatched {
			w, o := with.dispatched[i], without.dispatched[i]
			if w.Intervention != o.Intervention || w.TargetSerial != o.TargetSerial || w.Value != o.Value {
				t.Errorf("dispatched[%d] with goal lines %+v, without %+v", i, w, o)
			}
		}
	})
}

func TestGoalLines_NoTermsNoLines(t *testing.T) {
	l := &Loop{}
	l.SetGoalSource(func(context.Context, gamecontext.GameContext) goal.Profile {
		return goal.FromObjectives(map[string]float64{"endurance": 0.25, "balanced": 0.25}, 120, 60) // mixed
	})
	snap := remoteSnapshot(time.Second)
	snap.Decision.GoalInPrompt = true
	if got := l.goalLines(context.Background(), snap, flags.DecisionBackendClaude, gamecontext.GameContext{}); got != nil {
		t.Fatalf("UC-042/A12 a profile without terms (mixed) must render no lines, got %+v", got)
	}
}

// UC: UC-042 A12
// TestSampleGoal_AfterTheEnabledGate: the decision loop hands a cycle's context to
// the book's goal sampler only past the enabled gate (decide-contract.md §14.4),
// whatever decision.predictions_enabled says.
func TestSampleGoal_AfterTheEnabledGate(t *testing.T) {
	fx := decideFixtures()[2]
	for _, tc := range []struct {
		name             string
		enabled, predOff bool
		want             bool
	}{
		{"UC-042/A12 enabled samples", true, false, true},
		{"UC-042/A12 enabled with predictions off still samples", true, true, true},
		{"UC-042/A12 disabled takes no sample", false, false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			clock := time.Unix(10_000, 0)
			snap := rulesSnapshot()
			snap.Enabled = tc.enabled
			snap.Decision.PredictionsOff = tc.predOff
			l, _ := decideRecordingLoop(t, snap)
			l.Rules = newTestEngine(fx.cfg, &clock)
			l.now = func() time.Time { return clock }
			l.Actions = &capturingSink{}
			b := testBook(t, &clock)
			l.SetPredictionBook(b)
			l.OnEvaluate(context.Background(), fx.ctx, testTrigger())
			acc := b.GoalAccumulator(fx.ctx.SessionID)
			if got := acc.Sampled(); got != tc.want {
				t.Fatalf("sampled = %v, want %v", got, tc.want)
			}
		})
	}
}
