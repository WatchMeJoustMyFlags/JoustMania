package decision

import (
	"context"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/gamecontext"
)

// game_cycle_parent_test.go covers #1325 (UC-040 BR-013): the agent's in-game spans
// are remote children of the coordinator's game_cycle span, with the fallback order
// game_cycle -> gameplay_phase -> game root -> own root, and every
// agent.intervention.effect span is a direct child of its own agent.decision.

// hubGameCycleSpanID is a valid game_cycle span id in the same game trace as
// hubGameSpanID / hubGameplayPhaseSpanID.
const hubGameCycleSpanID = "1a2b3c4d5e6f7081"

// gameCycleCtx is gameplayPhaseCtx additionally carrying the game_cycle span id.
func gameCycleCtx(gameID string) gamecontext.GameContext {
	c := gameplayPhaseCtx(gameID)
	c.GameCycleSpanID = hubGameCycleSpanID
	return c
}

// UC: UC-040 BR-013
func TestSignalReceived_ParentFallbackOrder(t *testing.T) {
	invalidCycle := gameCycleCtx("game_gc02")
	invalidCycle.GameCycleSpanID = "not-hex"

	cases := []struct {
		name       string
		ctx        gamecontext.GameContext
		wantParent string // "" = own root
	}{
		{"UC-040/BR-013 game_cycle known: child of game_cycle", gameCycleCtx("game_gc01"), hubGameCycleSpanID},
		{"UC-040/BR-013 game_cycle invalid: child of gameplay_phase", invalidCycle, hubGameplayPhaseSpanID},
		{"UC-040/BR-013 no phase ids: child of the game root", gameTraceCtx("game_gc03"), hubGameSpanID},
		{"UC-040/BR-013 no game ids: own root", gamecontext.GameContext{SessionID: "game_gc04"}, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			l, sr, _ := meteredTestLoop(t)
			l.Rules = fakeRules{out: []Decision{{Intervention: "noop", Reason: "x"}}}
			l.Actions = &fakeSink{}

			l.OnEvaluate(context.Background(), tc.ctx,
				EvalTrigger{Signal: "metrics", T0: time.Now(), RPCService: "svc"})

			roots := spansByName(sr.Ended(), SignalReceived)
			if len(roots) != 1 {
				t.Fatalf("signal_received spans = %d, want 1", len(roots))
			}
			parent := roots[0].Parent()
			if tc.wantParent == "" {
				if parent.IsValid() {
					t.Fatalf("want own root, got parent %v", parent)
				}
				return
			}
			wantTID, _ := trace.TraceIDFromHex(hubGameTrace)
			wantSID, _ := trace.SpanIDFromHex(tc.wantParent)
			if parent.SpanID() != wantSID || parent.TraceID() != wantTID || !parent.IsRemote() {
				t.Fatalf("parent = %s/%s (remote=%v), want remote %s/%s",
					parent.TraceID(), parent.SpanID(), parent.IsRemote(), wantTID, wantSID)
			}
			// The decision keeps its Link to the game ROOT whatever the parent is.
			decisions := spansByName(sr.Ended(), SpanDecision)
			if len(decisions) != 1 {
				t.Fatalf("decision spans = %d, want 1", len(decisions))
			}
			rootSID, _ := trace.SpanIDFromHex(hubGameSpanID)
			if links := decisions[0].Links(); len(links) != 1 || links[0].SpanContext.SpanID() != rootSID {
				t.Fatalf("decision links = %+v, want one Link to the game root %s", links, rootSID)
			}
		})
	}
}

// UC: UC-040 BR-013
// UC: UC-035 main step 5
func TestInGameRemoteParent_GameCycleFirst(t *testing.T) {
	parentOf := func(ctx context.Context) string {
		return trace.SpanContextFromContext(ctx).SpanID().String()
	}
	t.Run("UC-040/BR-013 game_cycle wins over gameplay_phase and root", func(t *testing.T) {
		ctx, ok := inGameRemoteParent(context.Background(), hubGameTrace, hubGameSpanID, hubGameplayPhaseSpanID, hubGameCycleSpanID)
		if !ok || parentOf(ctx) != hubGameCycleSpanID {
			t.Fatalf("parent = %s (ok=%v), want game_cycle %s", parentOf(ctx), ok, hubGameCycleSpanID)
		}
	})
	t.Run("UC-040/BR-013 game_cycle without gameplay_phase still wins", func(t *testing.T) {
		ctx, ok := inGameRemoteParent(context.Background(), hubGameTrace, hubGameSpanID, "", hubGameCycleSpanID)
		if !ok || parentOf(ctx) != hubGameCycleSpanID {
			t.Fatalf("parent = %s (ok=%v), want game_cycle %s", parentOf(ctx), ok, hubGameCycleSpanID)
		}
	})
	t.Run("UC-040/BR-013 empty game_cycle falls back to gameplay_phase", func(t *testing.T) {
		ctx, ok := inGameRemoteParent(context.Background(), hubGameTrace, hubGameSpanID, hubGameplayPhaseSpanID, "")
		if !ok || parentOf(ctx) != hubGameplayPhaseSpanID {
			t.Fatalf("parent = %s (ok=%v), want gameplay_phase %s", parentOf(ctx), ok, hubGameplayPhaseSpanID)
		}
	})
	t.Run("UC-040/BR-013 no trace id: own root even with a game_cycle id", func(t *testing.T) {
		ctx, ok := inGameRemoteParent(context.Background(), "", hubGameSpanID, hubGameplayPhaseSpanID, hubGameCycleSpanID)
		if ok || trace.SpanContextFromContext(ctx).IsValid() {
			t.Fatalf("want own root, got ok=%v parent=%s", ok, parentOf(ctx))
		}
	})
}

// UC: UC-040 main BR-013
// UC: UC-035 main step 5
func TestRemote_DecideLandsUnderGameCycle(t *testing.T) {
	t.Run("UC-040/BR-013 async agent.decide is a remote child of game_cycle", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "shield"))
		h := newRemoteHarness(t, remoteSnapshot(time.Second), be)
		c := remoteContext()
		c.GameTraceID, c.GameTraceSpanID = hubGameTrace, hubGameSpanID
		c.GameplayPhaseSpanID, c.GameCycleSpanID = hubGameplayPhaseSpanID, hubGameCycleSpanID
		h.loop.OnEvaluate(context.Background(), c, testTrigger())
		h.loop.AwaitInflight()

		spans := h.sr.Ended()
		ds := spansByName(spans, SpanDecide)
		anchors := spansByName(spans, SignalReceived)
		if len(ds) != 1 || len(anchors) != 1 {
			t.Fatalf("decide=%d anchors=%d, want 1 each", len(ds), len(anchors))
		}
		if p := ds[0].Parent(); p.TraceID().String() != hubGameTrace || p.SpanID().String() != hubGameCycleSpanID || !p.IsRemote() {
			t.Fatalf("agent.decide parent = %v, want remote game_cycle %s", p, hubGameCycleSpanID)
		}
		// The fire-cycle Link stays intact.
		if links := ds[0].Links(); len(links) != 1 || links[0].SpanContext.SpanID() != anchors[0].SpanContext().SpanID() {
			t.Fatalf("links = %+v, want one Link to the fire-cycle agent.signal_received", links)
		}
		// The landing's decision nests under the agent.decide span.
		decisions := spansByName(spans, SpanDecision)
		if len(decisions) != 1 || decisions[0].Parent().SpanID() != ds[0].SpanContext().SpanID() {
			t.Fatalf("agent.decision must be a child of the async agent.decide")
		}
	})
}

// effectsByIntervention groups the ended effect spans by intervention id, failing on
// a span without one.
func effectsByIntervention(t *testing.T, spans []sdktrace.ReadOnlySpan) map[string][]sdktrace.ReadOnlySpan {
	t.Helper()
	out := map[string][]sdktrace.ReadOnlySpan{}
	for _, s := range spans {
		if s.Name() != SpanInterventionEffect {
			continue
		}
		v, ok := attrValue(s, AttrDecisionInterventionID)
		if !ok {
			t.Fatalf("effect span without %s", AttrDecisionInterventionID)
		}
		out[v.AsString()] = append(out[v.AsString()], s)
	}
	return out
}

// UC: UC-040 main BR-013
// UC: UC-035 main step 5
func TestEffectSpans_EachChildOfItsOwnDecision(t *testing.T) {
	t.Run("UC-040/BR-013 consecutive effects never nest", func(t *testing.T) {
		provider := newFakeContextProvider()
		const gameID = "game_fx01"
		provider.set(gameID, durationContext(gameID, 90))
		l, sr, _, sched := effectLoop(t, provider, gameID)

		// Two consecutive dispatched decisions, each measured in turn.
		decisionOf := map[string]trace.SpanContext{}
		for i := 0; i < 2; i++ {
			dCtx, dSpan := l.Tracer.Start(context.Background(), SpanDecision)
			id := l.scheduleEffectSample(dCtx, gameCycleCtx(gameID), shieldDecision(), enduranceBaseline(), defaultFit())
			if id == "" {
				t.Fatal("expected a scheduled sample")
			}
			decisionOf[id] = dSpan.SpanContext()
			dSpan.End()
			sched.fire()
			l.AwaitEffects()
		}

		effects := effectsByIntervention(t, sr.Ended())
		if len(effects) != 2 {
			t.Fatalf("effects for %d interventions, want 2", len(effects))
		}
		effectIDs := map[trace.SpanID]bool{}
		for _, spans := range effects {
			for _, s := range spans {
				effectIDs[s.SpanContext().SpanID()] = true
			}
		}
		for id, spans := range effects {
			// One span per baseline signal, no extra wrapper span.
			if len(spans) != len(enduranceBaseline()) {
				t.Fatalf("intervention %s: %d effect spans, want %d (one per signal)", id, len(spans), len(enduranceBaseline()))
			}
			want := decisionOf[id]
			for _, s := range spans {
				if effectIDs[s.Parent().SpanID()] {
					t.Fatalf("effect span %s is a child of another effect span", s.SpanContext().SpanID())
				}
				if s.Parent().SpanID() != want.SpanID() || s.Parent().TraceID() != want.TraceID() {
					t.Fatalf("effect parent = %s, want its own decision %s", s.Parent().SpanID(), want.SpanID())
				}
				if _, ok := attrValue(s, AttrEffectSignal); !ok {
					t.Fatal("every effect span names its signal")
				}
			}
		}
	})

	t.Run("UC-040/BR-013 aborted effect is a child of its decision", func(t *testing.T) {
		provider := newFakeContextProvider() // no context for the game: follow-up aborts
		const gameID = "game_fx02"
		l, sr, _, sched := effectLoop(t, provider, gameID)

		dCtx, dSpan := l.Tracer.Start(context.Background(), SpanDecision)
		if l.scheduleEffectSample(dCtx, gameCycleCtx(gameID), shieldDecision(), enduranceBaseline(), defaultFit()) == "" {
			t.Fatal("expected a scheduled sample")
		}
		dSpan.End()
		sched.fire()
		l.AwaitEffects()

		spans := spansByName(sr.Ended(), SpanInterventionEffect)
		if len(spans) != 1 {
			t.Fatalf("aborted effect spans = %d, want 1", len(spans))
		}
		if spans[0].Parent().SpanID() != dSpan.SpanContext().SpanID() {
			t.Fatalf("aborted effect parent = %s, want decision %s", spans[0].Parent().SpanID(), dSpan.SpanContext().SpanID())
		}
		if v, ok := attrValue(spans[0], AttrEffectAborted); !ok || !v.AsBool() {
			t.Fatal("aborted effect must carry intervention.effect_aborted=true")
		}
	})

	t.Run("UC-040/BR-013 effect spans are backdated to dispatch", func(t *testing.T) {
		provider := newFakeContextProvider()
		const gameID = "game_fx03"
		provider.set(gameID, durationContext(gameID, 90))
		l, sr, _, sched := effectLoop(t, provider, gameID)
		dispatched := time.Unix(20_000, 0)
		l.now = func() time.Time { return dispatched }

		dCtx, dSpan := l.Tracer.Start(context.Background(), SpanDecision)
		l.scheduleEffectSample(dCtx, gameCycleCtx(gameID), shieldDecision(), enduranceBaseline(), defaultFit())
		dSpan.End()
		sched.fire()
		l.AwaitEffects()

		for _, s := range spansByName(sr.Ended(), SpanInterventionEffect) {
			if !s.StartTime().Equal(dispatched) {
				t.Fatalf("effect start = %v, want dispatch time %v", s.StartTime(), dispatched)
			}
		}
	})
}
