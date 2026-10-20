package decision

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync/atomic"
	"testing"

	"github.com/open-feature/go-sdk/openfeature"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// unreadableFlagd answers every evaluation with an error, like a flagd that
// cannot be read.
type unreadableFlagd struct{}

var errFlagdDown = errors.New("flagd unreadable")

func (unreadableFlagd) BooleanValue(_ context.Context, _ string, def bool, _ openfeature.EvaluationContext, _ ...openfeature.Option) (bool, error) {
	return def, errFlagdDown
}
func (unreadableFlagd) StringValue(_ context.Context, _ string, def string, _ openfeature.EvaluationContext, _ ...openfeature.Option) (string, error) {
	return def, errFlagdDown
}
func (unreadableFlagd) IntValue(_ context.Context, _ string, def int64, _ openfeature.EvaluationContext, _ ...openfeature.Option) (int64, error) {
	return def, errFlagdDown
}
func (unreadableFlagd) FloatValue(_ context.Context, _ string, def float64, _ openfeature.EvaluationContext, _ ...openfeature.Option) (float64, error) {
	return def, errFlagdDown
}
func (unreadableFlagd) ObjectValue(_ context.Context, _ string, def any, _ openfeature.EvaluationContext, _ ...openfeature.Option) (any, error) {
	return def, errFlagdDown
}

type countingRules struct{ calls atomic.Int32 }

func (c *countingRules) Evaluate(context.Context, gamecontext.GameContext) []Decision {
	c.calls.Add(1)
	return []Decision{{Intervention: "noop", Reason: "should never run"}}
}

// UC: UC-040 A28
func TestLoop_UnreadableFlagdResolvesToKillSwitchOff(t *testing.T) {
	t.Run("UC-040/A28 flagd unreadable: nothing decided, no backend call, agent.disabled recorded", func(t *testing.T) {
		sr := tracetest.NewSpanRecorder()
		tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
		t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
		rules := &countingRules{}
		sink := &fakeSink{}
		l := NewLoop(flags.New(unreadableFlagd{}, nil), slog.New(slog.NewTextHandler(io.Discard, nil)))
		l.Tracer = tp.Tracer("test")
		l.Rules = rules
		l.Actions = sink

		state := l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "game_0a1b2c3d4e5f"}, testTrigger())

		if len(state.Outcomes) != 0 {
			t.Fatalf("outcomes = %+v, want none", state.Outcomes)
		}
		if n := rules.calls.Load(); n != 0 {
			t.Fatalf("rules/backend calls = %d, want 0", n)
		}
		if n := sink.calls.Load(); n != 0 {
			t.Fatalf("sink applies = %d, want 0", n)
		}
		disabled := spansByName(sr.Ended(), SpanDisabled)
		if len(disabled) != 1 {
			t.Fatalf("agent.disabled spans = %d, want 1", len(disabled))
		}
		if len(disabled[0].Attributes()) == 0 {
			t.Fatal("agent.disabled span must carry the flags in effect")
		}
	})
}
