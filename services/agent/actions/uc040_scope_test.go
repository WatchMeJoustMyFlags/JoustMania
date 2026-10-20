package actions

import (
	"context"
	"testing"

	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// UC: UC-040 Pre-2
// UC: UC-040 Post-S-1
func TestDecideLoop_WriteIsScopedToTheDecidingGame(t *testing.T) {
	snap := flags.Snapshot{
		Enabled:              true,
		InterventionsAllowed: []string{decision.InterventionGrantShield},
		Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
	}
	evalGame := func(t *testing.T, gameID string) (*fakeService, *tracetest.SpanRecorder) {
		l, svc, _, _ := meteredLoop(t, snap,
			oneDecision{Intervention: decision.InterventionGrantShield, TargetSerial: "MOCK0001", Reason: "shield"}, true)
		sr := tracetest.NewSpanRecorder()
		tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
		t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
		l.Tracer = tp.Tracer("test")
		l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: gameID}, testTrigger())
		return svc, sr
	}

	t.Run("UC-040/Pre-2 the registered game's scope is the only scope written", func(t *testing.T) {
		svcA, _ := evalGame(t, "game_aaaaaaaaaaaa")
		svcB, _ := evalGame(t, "game_bbbbbbbbbbbb")
		for _, c := range []struct {
			svc  *fakeService
			game string
		}{{svcA, "game_aaaaaaaaaaaa"}, {svcB, "game_bbbbbbbbbbbb"}} {
			calls := c.svc.all()
			if len(calls) != 1 {
				t.Fatalf("game %s: %d writes, want 1", c.game, len(calls))
			}
			if got := calls[0].write.Scope.GameID; got != c.game {
				t.Fatalf("write scope game = %q, want %q (only the deciding game)", got, c.game)
			}
			if got := calls[0].write.Scope.Serial; got != "MOCK0001" {
				t.Fatalf("write scope serial = %q, want the targeted player", got)
			}
		}
	})

	t.Run("UC-040/Post-S-1 the written decision is recorded as agent.decision and an OK agent.action", func(t *testing.T) {
		svc, sr := evalGame(t, "game_aaaaaaaaaaaa")
		if n := len(svc.all()); n != 1 {
			t.Fatalf("writes = %d, want 1", n)
		}
		var decisions, actions int
		for _, s := range sr.Ended() {
			switch s.Name() {
			case decision.SpanDecision:
				decisions++
			case decision.SpanAction:
				actions++
				if s.Status().Code == codes.Error {
					t.Fatalf("agent.action status = Error, want not an error for a written decision")
				}
			}
		}
		if decisions != 1 || actions != 1 {
			t.Fatalf("agent.decision=%d agent.action=%d, want 1 each", decisions, actions)
		}
	})
}
