package prediction

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel/attribute"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	"go.uber.org/goleak"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/telemetry/keys"
)

// book_uc040_test.go covers the prediction book (#2479, UC-040 A44, BR-034):
// per-template scoring (early decision, game end first, data missing, stale end,
// the horizon grace), exactly-once scoring, caps, dedupe, controls, restart and
// shutdown.

var t0 = time.Unix(10_000, 0)

func fp(v float64) *float64 { return &v }
func bp(v bool) *bool       { return &v }

// clock is a settable, goroutine-safe clock.
type clock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *clock) now() time.Time  { c.mu.Lock(); defer c.mu.Unlock(); return c.t }
func (c *clock) set(t time.Time) { c.mu.Lock(); c.t = t; c.mu.Unlock() }

// fakeTimers is the afterFunc seam: timers fire only on fire().
type fakeTimers struct {
	mu     sync.Mutex
	timers []*fakeTimer
}

type fakeTimer struct {
	d             time.Duration
	fn            func()
	stopped, done bool
}

func (f *fakeTimers) after(d time.Duration, fn func()) func() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	tm := &fakeTimer{d: d, fn: fn}
	f.timers = append(f.timers, tm)
	return func() bool {
		f.mu.Lock()
		defer f.mu.Unlock()
		was := !tm.stopped && !tm.done
		tm.stopped = true
		return was
	}
}

// fire runs every live timer.
func (f *fakeTimers) fire() {
	f.mu.Lock()
	var fns []func()
	for _, tm := range f.timers {
		if !tm.stopped && !tm.done {
			tm.done = true
			fns = append(fns, tm.fn)
		}
	}
	f.mu.Unlock()
	for _, fn := range fns {
		fn()
	}
}

func (f *fakeTimers) durations() []time.Duration {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []time.Duration
	for _, tm := range f.timers {
		out = append(out, tm.d)
	}
	return out
}

type harness struct {
	t      *testing.T
	b      *Book
	clk    *clock
	timers *fakeTimers
	mu     sync.Mutex
	live   *gamecontext.GameContext
	sr     *tracetest.SpanRecorder
	reader *sdkmetric.ManualReader
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	h := &harness{t: t, clk: &clock{t: t0}, timers: &fakeTimers{}, sr: tracetest.NewSpanRecorder(),
		reader: sdkmetric.NewManualReader()}
	h.b = New(context.Background(), func(id string) (gamecontext.GameContext, bool) {
		h.mu.Lock()
		defer h.mu.Unlock()
		if h.live == nil || h.live.SessionID != id {
			return gamecontext.GameContext{}, false
		}
		return *h.live, true
	})
	h.b.SetClock(h.clk.now)
	h.b.SetAfterFunc(h.timers.after)
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(h.sr))
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(h.reader))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()); _ = mp.Shutdown(context.Background()) })
	h.b.SetTelemetry(tp, mp)
	return h
}

func (h *harness) at(d time.Duration) { h.clk.set(t0.Add(d)) }

func (h *harness) setLive(c *gamecontext.GameContext) {
	h.mu.Lock()
	h.live = c
	h.mu.Unlock()
}

// fire fires every timer and waits for the horizon checks to finish.
func (h *harness) fire() {
	h.timers.fire()
	h.b.Await()
}

// end ends the game normally at the current clock and returns its results.
func (h *harness) end(c gamecontext.GameContext) []Result {
	c.EndReason = gamecontext.EndReasonZero
	h.b.OnGameEnd(c)
	return h.b.Results(c.SessionID)
}

// one asserts exactly one result and returns it.
func (h *harness) one(rs []Result) Result {
	h.t.Helper()
	if len(rs) != 1 {
		h.t.Fatalf("got %d results, want 1: %+v", len(rs), rs)
	}
	return rs[0]
}

// game builds a running game with the given alive players.
func game(alive ...string) gamecontext.GameContext {
	c := gamecontext.GameContext{SessionID: "g1", GameKind: "real", Players: map[string]*gamecontext.PlayerSignals{}}
	c.Session.GameActive = bp(true)
	c.Session.DeathThreshold = fp(2) // no duration: no base-rate control is due
	for _, s := range alive {
		c.Players[s] = &gamecontext.PlayerSignals{Serial: s, Active: bp(true)}
	}
	return c
}

// eliminate marks serial eliminated at d after t0 (sequence + timeline).
func eliminate(c gamecontext.GameContext, serial string, d time.Duration) gamecontext.GameContext {
	c.Session.EliminationSequence = append(append([]string(nil), c.Session.EliminationSequence...), serial)
	c.Timeline = append(append([]gamecontext.TimelineEvent(nil), c.Timeline...),
		gamecontext.TimelineEvent{At: t0.Add(d), Kind: gamecontext.EventElimination, Serial: serial})
	if p := c.Players[serial]; p != nil {
		cp := *p
		cp.Active = bp(false)
		players := map[string]*gamecontext.PlayerSignals{}
		for k, v := range c.Players {
			players[k] = v
		}
		players[serial] = &cp
		c.Players = players
	}
	return c
}

// withHistory gives serial one sample per second in (t0+from, t0+from+n] at intensity.
func withHistory(c gamecontext.GameContext, serial string, from time.Duration, n int, intensity float64) gamecontext.GameContext {
	p := *c.Players[serial]
	for i := 1; i <= n; i++ {
		p.History = append(append([]gamecontext.MovementSample(nil), p.History...),
			gamecontext.MovementSample{At: t0.Add(from + time.Duration(i)*time.Second), Intensity: intensity})
	}
	players := map[string]*gamecontext.PlayerSignals{}
	for k, v := range c.Players {
		players[k] = v
	}
	players[serial] = &p
	c.Players = players
	return c
}

func pred(template string, h int, target string) decide.Prediction {
	return decide.NewPrediction(template, h, 0.7, decide.SourceRules, "R4", target)
}

func (h *harness) register(p decide.Prediction, c gamecontext.GameContext, acted bool) string {
	h.t.Helper()
	id, merged, ok := h.b.Register(Registration{Backend: "rules", Prediction: p, Acted: acted, Context: c})
	if !ok || merged {
		h.t.Fatalf("Register ok=%v merged=%v", ok, merged)
	}
	return id
}

func want(t *testing.T, r Result, outcome, reason string, early bool) {
	t.Helper()
	if r.Outcome != outcome || r.UnscorableReason != reason || r.DecidedEarly != early {
		t.Fatalf("result = %s/%s early=%v, want %s/%s early=%v", r.Outcome, r.UnscorableReason, r.DecidedEarly,
			outcome, reason, early)
	}
}

// UC: UC-040 A44 BR-034
func TestBook_Survives(t *testing.T) {
	t.Run("UC-040/A44 alive at the horizon is a hit", func(t *testing.T) {
		h := newHarness(t)
		c := game("P1", "P2")
		h.register(pred(decide.TemplateSurvives, 15, "P1"), c, true)
		if d := h.timers.durations(); len(d) != 1 || d[0] != 18*time.Second {
			t.Fatalf("timer = %v, want horizon 15 s + 3 s grace", d)
		}
		h.at(18 * time.Second)
		h.setLive(&c)
		h.fire()
		want(t, h.one(h.end(c)), OutcomeHit, "", false)
	})
	t.Run("UC-040/A44 eliminated inside the horizon is an early miss", func(t *testing.T) {
		h := newHarness(t)
		c := game("P1", "P2")
		h.register(pred(decide.TemplateSurvives, 15, "P1"), c, true)
		h.at(6 * time.Second)
		h.b.Observe(eliminate(c, "P1", 5*time.Second))
		if h.b.Pending() != 0 {
			t.Fatal("an early miss must leave nothing pending")
		}
		want(t, h.one(h.end(eliminate(c, "P1", 5*time.Second))), OutcomeMiss, "", true)
	})
	t.Run("UC-040/A44 horizon grace: eliminated after the horizon but before the check is a hit", func(t *testing.T) {
		h := newHarness(t)
		c := game("P1", "P2")
		h.register(pred(decide.TemplateSurvives, 15, "P1"), c, true)
		h.at(18 * time.Second)
		live := eliminate(c, "P1", 17*time.Second)
		h.setLive(&live)
		h.fire()
		want(t, h.one(h.end(live)), OutcomeHit, "", false)
	})
	t.Run("UC-040/A44 eliminated exactly at the horizon is a miss", func(t *testing.T) {
		h := newHarness(t)
		c := game("P1", "P2")
		h.register(pred(decide.TemplateSurvives, 15, "P1"), c, true)
		h.at(18 * time.Second)
		live := eliminate(c, "P1", 15*time.Second)
		h.setLive(&live)
		h.fire()
		want(t, h.one(h.end(live)), OutcomeMiss, "", false)
	})
	t.Run("UC-040/A44 game ends first with the target alive: unscorable game_ended", func(t *testing.T) {
		h := newHarness(t)
		c := game("P1", "P2")
		h.register(pred(decide.TemplateSurvives, 15, "P1"), c, true)
		h.at(5 * time.Second)
		r := h.one(h.end(c))
		want(t, r, OutcomeUnscorable, ReasonGameEnded, false)
		if r.EndOffsetS == nil || *r.EndOffsetS != 5 {
			t.Fatalf("end offset = %v, want 5", r.EndOffsetS)
		}
	})
	t.Run("UC-040/A44 game ends first after the target died: early miss", func(t *testing.T) {
		h := newHarness(t)
		c := game("P1", "P2")
		h.register(pred(decide.TemplateSurvives, 15, "P1"), c, true)
		h.at(5 * time.Second)
		want(t, h.one(h.end(eliminate(c, "P1", 4*time.Second))), OutcomeMiss, "", true)
	})
	t.Run("UC-040/A44 stale end: unscorable stale_end", func(t *testing.T) {
		h := newHarness(t)
		c := game("P1", "P2")
		h.register(pred(decide.TemplateSurvives, 15, "P1"), c, true)
		h.at(5 * time.Second)
		end := eliminate(c, "P1", 4*time.Second)
		end.EndReason = gamecontext.EndReasonStale
		h.b.OnGameEnd(end)
		want(t, h.one(h.b.Results("g1")), OutcomeUnscorable, ReasonStaleEnd, false)
	})
	t.Run("UC-040/A44 no target and a dead target are unscorable at registration", func(t *testing.T) {
		h := newHarness(t)
		c := eliminate(game("P1", "P2"), "P2", -time.Second)
		noTarget := pred(decide.TemplateSurvives, 15, "")
		h.register(noTarget, c, true)
		h.register(pred(decide.TemplateSurvives, 15, "P2"), c, true)
		if h.b.Pending() != 0 || len(h.timers.durations()) != 0 {
			t.Fatal("predictions scored at registration must not stay pending or start a timer")
		}
		rs := h.end(c)
		if len(rs) != 2 || rs[0].UnscorableReason != ReasonNoTarget || rs[1].UnscorableReason != ReasonTargetNotAlive {
			t.Fatalf("results = %+v", rs)
		}
	})
	t.Run("UC-040/A44 a horizon check with the partition gone waits for game end", func(t *testing.T) {
		h := newHarness(t)
		c := game("P1", "P2")
		h.register(pred(decide.TemplateSurvives, 15, "P1"), c, true)
		h.at(18 * time.Second)
		h.fire() // no live context
		if h.b.Pending() != 1 {
			t.Fatal("the prediction must stay pending")
		}
		h.at(20 * time.Second)
		want(t, h.one(h.end(c)), OutcomeHit, "", false)
	})
}

// UC: UC-040 A44 BR-034
func TestBook_Eliminations(t *testing.T) {
	t.Run("UC-040/A44 no_elimination: none in the horizon is a hit", func(t *testing.T) {
		h := newHarness(t)
		c := game("P1", "P2", "P3")
		h.register(pred(decide.TemplateNoElimination, 15, ""), c, true)
		h.at(18 * time.Second)
		live := eliminate(c, "P3", 16*time.Second) // after the horizon
		h.setLive(&live)
		h.fire()
		r := h.one(h.end(live))
		want(t, r, OutcomeHit, "", false)
		if r.Eliminations == nil || *r.Eliminations != 0 {
			t.Fatalf("eliminations = %v, want 0", r.Eliminations)
		}
	})
	t.Run("UC-040/A44 no_elimination: an elimination is an early miss", func(t *testing.T) {
		h := newHarness(t)
		c := game("P1", "P2", "P3")
		h.register(pred(decide.TemplateNoElimination, 15, ""), c, true)
		h.at(4 * time.Second)
		h.b.Observe(eliminate(c, "P3", 3*time.Second))
		want(t, h.one(h.end(c)), OutcomeMiss, "", true)
	})
	t.Run("UC-040/A44 no_elimination: game ends first without one is unscorable", func(t *testing.T) {
		h := newHarness(t)
		c := game("P1", "P2", "P3")
		h.register(pred(decide.TemplateNoElimination, 15, ""), c, true)
		h.at(5 * time.Second)
		want(t, h.one(h.end(c)), OutcomeUnscorable, ReasonGameEnded, false)
	})
	t.Run("UC-040/A44 elimination: one before the game ends is an early hit", func(t *testing.T) {
		h := newHarness(t)
		c := game("P1", "P2", "P3")
		h.register(pred(decide.TemplateElimination, 30, ""), c, true)
		h.at(5 * time.Second)
		want(t, h.one(h.end(eliminate(c, "P3", 4*time.Second))), OutcomeHit, "", true)
	})
	t.Run("UC-040/A44 elimination: none at the horizon is a miss", func(t *testing.T) {
		h := newHarness(t)
		c := game("P1", "P2", "P3")
		h.register(pred(decide.TemplateElimination, 30, ""), c, true)
		h.at(33 * time.Second)
		h.setLive(&c)
		h.fire()
		want(t, h.one(h.end(c)), OutcomeMiss, "", false)
	})
	t.Run("UC-040/A44 a lost timeline falls back to the sequence, flagged approx", func(t *testing.T) {
		h := newHarness(t)
		c := game("P1", "P2", "P3")
		h.register(pred(decide.TemplateElimination, 30, ""), c, true)
		h.at(5 * time.Second)
		live := c
		live.Session.EliminationSequence = []string{"P3"} // timeline empty: evicted
		h.b.Observe(live)
		r := h.one(h.end(live))
		want(t, r, OutcomeHit, "", true)
		if !r.Approx {
			t.Fatal("want approx timing")
		}
	})
}

// UC: UC-040 A44 BR-034
func TestBook_GameEnds(t *testing.T) {
	t.Run("UC-040/A44 game ends inside the horizon: early hit", func(t *testing.T) {
		h := newHarness(t)
		c := game("P1", "P2", "P3", "P4")
		h.register(pred(decide.TemplateGameEnds, 60, ""), c, true)
		h.at(20 * time.Second)
		want(t, h.one(h.end(c)), OutcomeHit, "", true)
	})
	t.Run("UC-040/A44 game still running at the horizon: miss", func(t *testing.T) {
		h := newHarness(t)
		c := game("P1", "P2", "P3", "P4")
		h.register(pred(decide.TemplateGameEnds, 60, ""), c, true)
		h.at(63 * time.Second)
		h.setLive(&c)
		h.fire()
		want(t, h.one(h.end(c)), OutcomeMiss, "", false)
	})
}

// UC: UC-040 A44 BR-034
func TestBook_ActivityAndCloseness(t *testing.T) {
	t.Run("UC-040/A44 activity_rises: after above before x 1.05 is a hit", func(t *testing.T) {
		h := newHarness(t)
		c := withHistory(game("P1", "P2"), "P1", -10*time.Second, 10, 0.5)
		h.register(pred(decide.TemplateActivityRises, 10, "P1"), c, true)
		h.at(13 * time.Second)
		live := withHistory(c, "P1", 0, 10, 1.0)
		h.setLive(&live)
		h.fire()
		r := h.one(h.end(live))
		want(t, r, OutcomeHit, "", false)
		if *r.Before != 0.25 || *r.After != 0.5 {
			t.Fatalf("before/after = %v/%v, want 0.25/0.5", *r.Before, *r.After)
		}
	})
	t.Run("UC-040/A44 activity_rises: no rise is a miss", func(t *testing.T) {
		h := newHarness(t)
		c := withHistory(game("P1", "P2"), "P1", -10*time.Second, 10, 0.5)
		h.register(pred(decide.TemplateActivityRises, 10, "P1"), c, true)
		h.at(13 * time.Second)
		live := withHistory(c, "P1", 0, 10, 0.51)
		h.setLive(&live)
		h.fire()
		want(t, h.one(h.end(live)), OutcomeMiss, "", false)
	})
	t.Run("UC-040/A44 activity_rises: thin data is data_missing", func(t *testing.T) {
		h := newHarness(t)
		c := withHistory(game("P1", "P2"), "P1", -10*time.Second, 10, 0.5)
		h.register(pred(decide.TemplateActivityRises, 10, "P1"), c, true)
		h.at(13 * time.Second)
		live := withHistory(c, "P1", 0, 3, 1.0)
		h.setLive(&live)
		h.fire()
		want(t, h.one(h.end(live)), OutcomeUnscorable, ReasonDataMissing, false)
	})
	t.Run("UC-040/A44 activity_rises: a target that died is target_not_alive", func(t *testing.T) {
		h := newHarness(t)
		c := withHistory(game("P1", "P2"), "P1", -10*time.Second, 10, 0.5)
		h.register(pred(decide.TemplateActivityRises, 10, "P1"), c, true)
		h.at(13 * time.Second)
		live := eliminate(withHistory(c, "P1", 0, 10, 1.0), "P1", 8*time.Second)
		h.setLive(&live)
		h.fire()
		want(t, h.one(h.end(live)), OutcomeUnscorable, ReasonTargetNotAlive, false)
	})
	t.Run("UC-040/A44 closeness_rises: a smaller spread is a hit", func(t *testing.T) {
		h := newHarness(t)
		c := game("P1", "P2")
		c = withHistory(withHistory(c, "P1", -15*time.Second, 15, 0.4), "P2", -15*time.Second, 15, 1.6)
		h.register(pred(decide.TemplateClosenessRises, 15, ""), c, true)
		h.at(18 * time.Second)
		live := withHistory(withHistory(c, "P1", 0, 15, 0.9), "P2", 0, 15, 1.1)
		h.setLive(&live)
		h.fire()
		want(t, h.one(h.end(live)), OutcomeHit, "", false)
	})
}

// UC: UC-040 A44 BR-034
// TestBook_ExactlyOnce: a horizon timer racing the game end scores the
// prediction exactly once (one result, one span, one outcome count).
func TestBook_ExactlyOnce(t *testing.T) {
	for i := 0; i < 200; i++ {
		h := newHarness(t)
		c := game("P1", "P2")
		id := h.register(pred(decide.TemplateSurvives, 10, "P1"), c, true)
		h.at(13 * time.Second)
		h.setLive(&c)
		var wg sync.WaitGroup
		wg.Add(2)
		go func() { defer wg.Done(); h.timers.fire() }()
		go func() {
			defer wg.Done()
			end := c
			end.EndReason = gamecontext.EndReasonZero
			h.b.OnGameEnd(end)
		}()
		wg.Wait()
		h.b.Await()
		if rs := h.b.Results("g1"); len(rs) > 1 {
			t.Fatalf("iteration %d: %d results", i, len(rs))
		}
		spans := 0
		for _, s := range h.sr.Ended() {
			if s.Name() == keys.SpanPredictionOutcome {
				spans++
			}
		}
		if spans != 1 {
			t.Fatalf("iteration %d: %d outcome spans for %s, want 1", i, spans, id)
		}
		if n := counterSum(t, h.reader, MetricOutcomes); n != 1 {
			t.Fatalf("iteration %d: outcomes counted %d, want 1", i, n)
		}
	}
}

func counterSum(t *testing.T, r *sdkmetric.ManualReader, name string) int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := r.Collect(context.Background(), &rm); err != nil {
		t.Fatal(err)
	}
	var n int64
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != name {
				continue
			}
			if s, ok := m.Data.(metricdata.Sum[int64]); ok {
				for _, dp := range s.DataPoints {
					n += dp.Value
				}
			}
		}
	}
	return n
}

// UC: UC-040 A44 BR-034
func TestBook_CapsAndDedupe(t *testing.T) {
	t.Run("UC-040/A44 an identical pending prediction is merged", func(t *testing.T) {
		h := newHarness(t)
		c := game("P1", "P2")
		id := h.register(pred(decide.TemplateSurvives, 15, "P1"), c, true)
		id2, merged, ok := h.b.Register(Registration{Backend: "rules", Prediction: pred(decide.TemplateSurvives, 15, "P1"), Acted: true, Context: c})
		if !ok || !merged || id2 != id || h.b.Pending() != 1 {
			t.Fatalf("second Register = %q merged=%v ok=%v pending=%d", id2, merged, ok, h.b.Pending())
		}
		if n := counterSum(t, h.reader, MetricMerged); n != 1 {
			t.Fatalf("merged counted %d, want 1", n)
		}
		// A counterfactual of the same prediction is not merged into the acted one.
		if _, merged, _ := h.b.Register(Registration{Backend: "rules", Prediction: pred(decide.TemplateSurvives, 15, "P1"), Context: c}); merged {
			t.Fatal("acted and counterfactual must not merge")
		}
	})
	t.Run("UC-040/A44 over the per-game cap the oldest is overflow", func(t *testing.T) {
		h := newHarness(t)
		c := game()
		for i := 0; i <= MaxPendingPerGame; i++ {
			c.Players[fmt.Sprintf("P%02d", i)] = &gamecontext.PlayerSignals{Active: bp(true)}
		}
		var first string
		for i := 0; i <= MaxPendingPerGame; i++ {
			h.at(time.Duration(i) * time.Millisecond)
			id := h.register(pred(decide.TemplateSurvives, 15, fmt.Sprintf("P%02d", i)), c, true)
			if i == 0 {
				first = id
			}
		}
		if h.b.Pending() != MaxPendingPerGame {
			t.Fatalf("pending = %d, want %d", h.b.Pending(), MaxPendingPerGame)
		}
		rs := h.end(c)
		if rs[0].ID != first || rs[0].UnscorableReason != ReasonOverflow {
			t.Fatalf("first result = %+v, want the oldest as overflow", rs[0])
		}
	})
	t.Run("UC-040/A44 over the global cap the globally oldest is overflow", func(t *testing.T) {
		h := newHarness(t)
		games := MaxPendingTotal/MaxPendingPerGame + 1
		for g := 0; g < games; g++ {
			c := game()
			c.SessionID = fmt.Sprintf("g%02d", g)
			for i := 0; i < MaxPendingPerGame; i++ {
				c.Players[fmt.Sprintf("P%02d", i)] = &gamecontext.PlayerSignals{Active: bp(true)}
			}
			for i := 0; i < MaxPendingPerGame && h.b.Pending() < MaxPendingTotal+1; i++ {
				h.at(time.Duration(g*100+i) * time.Millisecond)
				h.b.Register(Registration{Backend: "rules", Prediction: pred(decide.TemplateSurvives, 15, fmt.Sprintf("P%02d", i)), Context: c})
				if h.b.Pending() > MaxPendingTotal {
					t.Fatalf("pending %d above the global cap", h.b.Pending())
				}
			}
		}
		end := game()
		end.SessionID = "g00"
		h.b.OnGameEnd(end)
		rs := h.b.Results("g00")
		if len(rs) == 0 || rs[0].UnscorableReason != ReasonOverflow {
			t.Fatalf("g00's oldest must be overflow, got %+v", rs)
		}
	})
}

// UC: UC-040 A44 BR-034
func TestBook_Controls(t *testing.T) {
	h := newHarness(t)
	c := game("P1", "P2", "P3")
	for _, d := range []float64{5, 15, 16, 30} {
		c.Session.DurationSeconds = fp(d)
		h.b.Observe(c)
	}
	if h.b.Pending() != 2 {
		t.Fatalf("pending = %d, want 2 controls (at 15 s and 30 s)", h.b.Pending())
	}
	for d := 45.0; d <= 600; d += 15 {
		// Score the pending ones first, so no control merges into a pending twin.
		h.at(time.Duration(d) * time.Second)
		h.setLive(&c)
		h.fire()
		c.Session.DurationSeconds = fp(d)
		h.b.Observe(c)
	}
	rs := h.end(c)
	if len(rs) != maxControlsPerGame {
		t.Fatalf("controls = %d, want the cap %d", len(rs), maxControlsPerGame)
	}
	seen := map[string]int{}
	for _, r := range rs {
		if r.Prediction.Source != decide.SourceControl || r.Backend != BackendNone || r.Acted {
			t.Fatalf("control result %+v", r)
		}
		seen[r.Prediction.Template]++
	}
	if len(seen) != len(decide.Templates()) {
		t.Fatalf("control templates = %v, want every template", seen)
	}
}

// UC: UC-040 A44 BR-034
func TestBook_RestartAndLateEnd(t *testing.T) {
	h := newHarness(t)
	c := game("P1", "P2")
	h.b.OnGameEnd(c) // a game the book never saw: no-op, no crash
	if h.b.Results("g1") != nil {
		t.Fatal("unknown game must have no results")
	}
	h.register(pred(decide.TemplateSurvives, 15, "P1"), c, true)
	h.end(c)
	if _, _, ok := h.b.Register(Registration{Backend: "rules", Prediction: pred(decide.TemplateSurvives, 15, "P2"), Context: c}); ok {
		t.Fatal("a game that ended must refuse new predictions")
	}
	h.b.OnGameEnd(c) // a second end of the same game: no-op
	if len(h.b.Results("g1")) != 1 {
		t.Fatal("a second end must not score again")
	}
}

// UC: UC-040 A44 BR-034
func TestBook_OutcomeSpan(t *testing.T) {
	h := newHarness(t)
	c := game("P1", "P2")
	c.GameTraceID = "0102030405060708090a0b0c0d0e0f10"
	c.GameCycleSpanID = "1112131415161718"
	decideSC := trace.NewSpanContext(trace.SpanContextConfig{TraceID: trace.TraceID{9}, SpanID: trace.SpanID{9}, TraceFlags: trace.FlagsSampled})
	id, _, _ := h.b.Register(Registration{Backend: "jev", Prediction: pred(decide.TemplateSurvives, 15, "P1"), Acted: true, DecideSpan: decideSC, Context: c})
	h.at(18 * time.Second)
	h.setLive(&c)
	h.fire()
	var span sdktrace.ReadOnlySpan
	for _, s := range h.sr.Ended() {
		if s.Name() == keys.SpanPredictionOutcome {
			span = s
		}
	}
	if span == nil {
		t.Fatal("no outcome span")
	}
	if span.Parent().SpanID().String() != c.GameCycleSpanID || span.Parent().TraceID().String() != c.GameTraceID {
		t.Fatalf("parent = %v, want the game_cycle span", span.Parent())
	}
	if len(span.Links()) != 1 || span.Links()[0].SpanContext.SpanID() != decideSC.SpanID() {
		t.Fatalf("links = %v, want the decide span", span.Links())
	}
	if !span.StartTime().Equal(t0) || !span.EndTime().Equal(t0.Add(18*time.Second)) {
		t.Fatalf("span covers %v..%v", span.StartTime(), span.EndTime())
	}
	attrs := map[attribute.Key]attribute.Value{}
	for _, a := range span.Attributes() {
		attrs[a.Key] = a.Value
	}
	for k, v := range map[string]string{
		keys.AttrPredictionID: id, keys.AttrPredictionOutcome: OutcomeHit, keys.AttrPredictionTemplate: decide.TemplateSurvives,
		keys.AttrPredictionBackend: "jev", keys.AttrPredictionRule: "R4", keys.AttrPredictionTarget: "P1",
	} {
		if attrs[attribute.Key(k)].AsString() != v {
			t.Errorf("%s = %q, want %q", k, attrs[attribute.Key(k)].AsString(), v)
		}
	}
	if !attrs[keys.AttrPredictionActed].AsBool() {
		t.Error("prediction.acted must be true")
	}
}

// UC: UC-040 A44 BR-034
// TestBook_ShutdownLeavesNoGoroutine: cancelling the root context stops every
// timer without scoring and Await joins the goroutines (production timers).
func TestBook_ShutdownLeavesNoGoroutine(t *testing.T) {
	defer goleak.VerifyNone(t, goleak.IgnoreCurrent())
	ctx, cancel := context.WithCancel(context.Background())
	b := New(ctx, nil)
	c := game("P1", "P2")
	for i, tpl := range []string{decide.TemplateSurvives, decide.TemplateGameEnds} {
		target := ""
		if i == 0 {
			target = "P1"
		}
		if _, _, ok := b.Register(Registration{Backend: "rules", Prediction: pred(tpl, map[int]int{0: 30, 1: 120}[i], target), Context: c}); !ok {
			t.Fatal("register failed")
		}
	}
	cancel()
	b.Await()
	if b.Pending() != 2 {
		t.Fatalf("shutdown must score nothing, pending = %d", b.Pending())
	}
}

// UC: UC-040 A44 BR-034
// TestBook_FieldSetFixedAtRegistration: closeness and field activity are measured
// on the players alive and covered before the decision; an elimination among them
// makes the after window data_missing instead of a spread that shrank because the
// outlier left.
func TestBook_FieldSetFixedAtRegistration(t *testing.T) {
	h := newHarness(t)
	c := game("P1", "P2", "P3")
	c = withHistory(withHistory(withHistory(c, "P1", -15*time.Second, 15, 0.8), "P2", -15*time.Second, 15, 1.2), "P3", -15*time.Second, 15, 3.0)
	h.register(pred(decide.TemplateClosenessRises, 15, ""), c, true)
	h.at(18 * time.Second)
	live := withHistory(withHistory(c, "P1", 0, 15, 0.8), "P2", 0, 15, 1.2)
	live = eliminate(live, "P3", 5*time.Second)
	h.setLive(&live)
	h.fire()
	want(t, h.one(h.end(live)), OutcomeUnscorable, ReasonDataMissing, false)
}

// UC: UC-040 A44 BR-034
// TestBook_ControlRotationOffset: each game starts its control template rotation
// at a hash of its id, so a template is not always sampled at the same game time.
func TestBook_ControlRotationOffset(t *testing.T) {
	first := map[string]string{}
	for _, id := range []string{"g-a", "g-b", "g-c", "g-d", "g-e", "g-f", "g-g", "g-h"} {
		h := newHarness(t)
		c := game("P1", "P2")
		c.SessionID = id
		c.Session.DurationSeconds = fp(15)
		h.b.Observe(c)
		rs := h.end(c)
		if len(rs) != 1 {
			t.Fatalf("%s: %d controls, want 1", id, len(rs))
		}
		first[rs[0].Prediction.Template] = id
	}
	if len(first) < 2 {
		t.Fatalf("every game started its rotation with the same template: %v", first)
	}
}

// UC: UC-040 A44 BR-034
// TestBook_MetricsSplitActed: outcomes and the Brier sum carry acted, so a
// counterfactual never mixes into the acted calibration.
func TestBook_MetricsSplitActed(t *testing.T) {
	h := newHarness(t)
	c := game("P1", "P2")
	h.register(pred(decide.TemplateSurvives, 15, "P1"), c, true)
	h.register(pred(decide.TemplateSurvives, 15, "P2"), c, false)
	h.at(18 * time.Second)
	h.setLive(&c)
	h.fire()
	var rm metricdata.ResourceMetrics
	if err := h.reader.Collect(context.Background(), &rm); err != nil {
		t.Fatal(err)
	}
	type key struct{ metric, acted string }
	got := map[key]float64{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			add := func(set attribute.Set, v float64) {
				a, _ := set.Value("acted")
				got[key{m.Name, a.AsString()}] += v
			}
			switch d := m.Data.(type) {
			case metricdata.Sum[int64]:
				for _, dp := range d.DataPoints {
					add(dp.Attributes, float64(dp.Value))
				}
			case metricdata.Sum[float64]:
				for _, dp := range d.DataPoints {
					add(dp.Attributes, dp.Value)
				}
			}
		}
	}
	brier := (0.7 - 1) * (0.7 - 1)
	for k, w := range map[key]float64{
		{MetricOutcomes, "true"}: 1, {MetricOutcomes, "false"}: 1,
		{MetricBrierSum, "true"}: brier, {MetricBrierSum, "false"}: brier,
	} {
		if d := got[k] - w; d > 1e-9 || d < -1e-9 {
			t.Errorf("%s{acted=%s} = %v, want %v (all: %v)", k.metric, k.acted, got[k], w, got)
		}
	}
}
