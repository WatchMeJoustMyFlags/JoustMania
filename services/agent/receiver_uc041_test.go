package main

import (
	"context"
	"io"
	"log/slog"
	"reflect"
	"testing"
	"time"

	"go.opentelemetry.io/collector/pdata/pmetric"
	"go.opentelemetry.io/collector/pdata/pmetric/pmetricotlp"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/collector/pdata/ptrace/ptraceotlp"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/infracontext"
)

// liveHarness is a receiver pair over an enabled decision loop whose rules
// record every evaluation, plus the infra store/loop.
type liveHarness struct {
	mux      *gamecontext.Multiplexer
	rules    *recordingRules
	infra    *recordingInfraLoop
	metrics  *metricsReceiver
	traces   *traceReceiver
	exportMD func(t *testing.T, md pmetric.Metrics)
}

func newLiveHarness(t *testing.T) *liveHarness {
	t.Helper()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	clock := time.Unix(5000, 0)
	mux := gamecontext.NewMultiplexer(func(string) *gamecontext.Store {
		return gamecontext.NewStore(time.Hour, time.Hour, func() time.Time { return clock })
	})
	rules := &recordingRules{}
	loops := decision.NewLoopSet(func(string) *decision.Loop {
		l := decision.NewLoop(staticFlags{flags.Snapshot{
			Enabled:              true,
			InterventionsAllowed: []string{"grant_shield"},
			Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
		}}, logger)
		l.Rules = rules
		return l
	})
	infraLoop := &recordingInfraLoop{}
	pipe := newPipeline(mux, loops, time.Hour).withInfra(infracontext.NewStore(time.Hour, func() time.Time { return clock }), infraLoop)
	pipe.now = func() time.Time { return clock }
	h := &liveHarness{mux: mux, rules: rules, infra: infraLoop, metrics: &metricsReceiver{pipe: pipe}, traces: &traceReceiver{pipe: pipe}}
	h.exportMD = func(t *testing.T, md pmetric.Metrics) {
		t.Helper()
		if _, err := h.metrics.Export(context.Background(), pmetricotlp.NewExportRequestFromMetrics(md)); err != nil {
			t.Fatalf("metrics export: %v", err)
		}
	}
	return h
}

// UC: UC-041 A1
func TestReceiver_HealthSpanTouchesNoGameAndTriggersNoEvaluation(t *testing.T) {
	t.Run("UC-041/A1 a health-only batch leaves the live game context unchanged and evaluates nothing", func(t *testing.T) {
		h := newLiveHarness(t)
		h.exportMD(t, liveGameMetrics("game-A", "PA"))
		before, ok := h.mux.Snapshot("game-A")
		if !ok {
			t.Fatal("game-A must be live")
		}
		evalsBefore := len(h.rules.evaluations())
		if evalsBefore != 1 {
			t.Fatalf("setup: evaluations = %d, want 1", evalsBefore)
		}

		td := ptrace.NewTraces()
		healthSpanInto(td, 42, "PA", "python")
		if _, err := h.traces.Export(context.Background(), ptraceotlp.NewExportRequestFromTraces(td)); err != nil {
			t.Fatalf("trace export: %v", err)
		}

		if got := len(h.rules.evaluations()); got != evalsBefore {
			t.Fatalf("evaluations = %d, want %d (a health span triggers no game evaluation)", got, evalsBefore)
		}
		after, _ := h.mux.Snapshot("game-A")
		if !reflect.DeepEqual(before, after) {
			t.Fatalf("game-A context changed by a health span:\nbefore %+v\nafter  %+v", before, after)
		}
		if _, exists := h.mux.Snapshot(gamecontext.FallbackGameID); exists {
			if fb, _ := h.mux.Snapshot(gamecontext.FallbackGameID); len(fb.Players) != 0 {
				t.Fatalf("fallback context gained players %v from a health span", fb.Players)
			}
		}
		if got := h.infra.calls.Load(); got != 1 {
			t.Fatalf("infra loop calls = %d, want 1 (the health span still feeds the rollout loop)", got)
		}
	})
}

// UC: UC-041 Post-S-1
func TestReceiver_ContextHoldsTheLastBatchAndEndedStaysEnded(t *testing.T) {
	h := newLiveHarness(t)
	alive := func(serial string, v float64) pmetric.Metrics {
		md := pmetric.NewMetrics()
		metricGauge(md, "game_player_alive", v, map[string]string{"game_id": "game-A", "serial": serial})
		return md
	}
	h.exportMD(t, liveGameMetrics("game-A", "PA"))
	h.exportMD(t, alive("PB", 1))

	t.Run("UC-041/Post-S-1 the context is that of the last batch received", func(t *testing.T) {
		snap, _ := h.mux.Snapshot("game-A")
		if snap.Session.GameActive == nil || !*snap.Session.GameActive {
			t.Fatalf("GameActive = %v, want true", snap.Session.GameActive)
		}
		if p := snap.Players["PB"]; p == nil || p.Active == nil || !*p.Active {
			t.Fatalf("PB = %+v, want active after batch 2", p)
		}
		h.exportMD(t, alive("PB", 0))
		snap, _ = h.mux.Snapshot("game-A")
		if p := snap.Players["PB"]; p == nil || p.Active == nil || *p.Active {
			t.Fatalf("PB = %+v, want inactive: the last batch wins", p)
		}
		if p := snap.Players["PA"]; p == nil || p.Active == nil || !*p.Active {
			t.Fatalf("PA = %+v, want still active (untouched by the last batch)", p)
		}
	})

	t.Run("UC-041/Post-S-1 a game that has ended stays ended", func(t *testing.T) {
		end := pmetric.NewMetrics()
		metricGauge(end, "game_active", 0, map[string]string{"game_id": "game-A"})
		h.exportMD(t, end)
		h.exportMD(t, liveGameMetrics("game-A", "PA")) // late game_active 1
		snap, _ := h.mux.Snapshot("game-A")
		if snap.Session.GameActive != nil && *snap.Session.GameActive {
			t.Fatal("a later game_active 1 restarted an ended game")
		}
	})
}

// UC: UC-040 Pre-1
func TestReceiver_OneContextPerGameID(t *testing.T) {
	t.Run("UC-040/Pre-1 each game id holds its own context and a batch for one leaves the other unchanged", func(t *testing.T) {
		h := newLiveHarness(t)
		h.exportMD(t, liveGameMetrics("game-A", "PA"))
		h.exportMD(t, liveGameMetrics("game-B", "PB"))
		a, okA := h.mux.Snapshot("game-A")
		b, okB := h.mux.Snapshot("game-B")
		if !okA || !okB {
			t.Fatalf("one context per game id: okA=%v okB=%v", okA, okB)
		}
		if a.Players["PA"] == nil || a.Players["PB"] != nil || b.Players["PB"] == nil || b.Players["PA"] != nil {
			t.Fatalf("contexts overlap: A=%v B=%v", a.Players, b.Players)
		}
		more := pmetric.NewMetrics()
		metricGauge(more, "game_player_alive", 1, map[string]string{"game_id": "game-A", "serial": "PC"})
		h.exportMD(t, more)
		bAfter, _ := h.mux.Snapshot("game-B")
		if !reflect.DeepEqual(b, bAfter) {
			t.Fatalf("a batch for game-A changed game-B's context:\nbefore %+v\nafter  %+v", b, bAfter)
		}
	})
}
