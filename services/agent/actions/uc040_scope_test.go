package actions

import (
	"context"
	"testing"

	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/flagservice"
	"github.com/joustmania/agent/gamecontext"
)

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

	t.Run("UC-040/Post-S-1 the deciding game's scope is the only scope written", func(t *testing.T) {
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

// registryService is a flag service that knows which games the coordinator
// registered (UC-053): a write scoped to any other game is refused with
// 409 game_not_live, as the real flag service does (state/write.go).
type registryService struct {
	fakeService
	registered map[string]bool
}

func (r *registryService) Put(ctx context.Context, set, flag string, w flagservice.Write) (flagservice.Result, error) {
	res, _ := r.fakeService.Put(ctx, set, flag, w)
	if !r.registered[w.Scope.GameID] {
		return flagservice.Result{}, &flagservice.Error{Status: 409, Reason: flagservice.ReasonGameNotLive, Detail: "game is not registered"}
	}
	return res, nil
}

// UC: UC-040 Pre-2
func TestDecideLoop_OnlyARegisteredGameIsWritten(t *testing.T) {
	snap := flags.Snapshot{
		Enabled:              true,
		InterventionsAllowed: []string{decision.InterventionGrantShield},
		Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
	}
	run := func(t *testing.T, gameID string) (*registryService, []sdktrace.ReadOnlySpan) {
		svc := &registryService{registered: map[string]bool{"game_registered01": true}}
		l, _, _, spy := meteredLoop(t, snap,
			oneDecision{Intervention: decision.InterventionGrantShield, TargetSerial: "MOCK0001", Reason: "shield"}, true)
		spy.inner = NewWriter(svc, discardLogger())
		sr := tracetest.NewSpanRecorder()
		tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
		t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
		l.Tracer = tp.Tracer("test")
		l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: gameID}, testTrigger())
		var actions []sdktrace.ReadOnlySpan
		for _, s := range sr.Ended() {
			if s.Name() == decision.SpanAction {
				actions = append(actions, s)
			}
		}
		return svc, actions
	}

	t.Run("UC-040/Pre-2 the game the coordinator registered has a scope: the write succeeds", func(t *testing.T) {
		svc, actions := run(t, "game_registered01")
		if n := len(svc.all()); n != 1 {
			t.Fatalf("writes = %d, want 1", n)
		}
		if len(actions) != 1 || actions[0].Status().Code == codes.Error {
			t.Fatalf("agent.action = %d spans, want one that is not an error", len(actions))
		}
	})

	t.Run("UC-040/Pre-2 a game the coordinator never registered has no scope: the write is refused", func(t *testing.T) {
		svc, actions := run(t, "game_unregistrd1")
		calls := svc.all()
		if len(calls) == 0 || calls[0].write.Scope.GameID != "game_unregistrd1" {
			t.Fatalf("writes = %+v, want the attempt for the unregistered game", calls)
		}
		if len(actions) != 1 || actions[0].Status().Code != codes.Error {
			t.Fatal("agent.action must be an error: without the registration nothing reaches the game")
		}
	})
}
