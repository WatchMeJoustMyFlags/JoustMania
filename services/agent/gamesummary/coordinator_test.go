package gamesummary

import (
	"context"
	"io"
	"log/slog"
	"os"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/gamecontext"
)

// recordingCoordinator builds a Coordinator with a recording tracer writing to a
// temp dir, plus the span recorder for assertions.
func recordingCoordinator(t *testing.T) (*Coordinator, *tracetest.SpanRecorder, string) {
	t.Helper()
	dir := t.TempDir()
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	c := NewCoordinator(NewWriter(dir), slog.New(slog.NewTextHandler(io.Discard, nil)))
	c.tracer = tp.Tracer("test")
	c.now = func() time.Time { return at(61) }
	return c, sr, dir
}

func spansByName(spans []sdktrace.ReadOnlySpan, name string) []sdktrace.ReadOnlySpan {
	var out []sdktrace.ReadOnlySpan
	for _, s := range spans {
		if s.Name() == name {
			out = append(out, s)
		}
	}
	return out
}

func spanAttr(s sdktrace.ReadOnlySpan, key string) (attribute.Value, bool) {
	for _, kv := range s.Attributes() {
		if string(kv.Key) == key {
			return kv.Value, true
		}
	}
	return attribute.Value{}, false
}

// TestCoordinator_WritesFileAndSpan: a real game end writes a summary file AND
// emits exactly one agent.game.summary span carrying the key counts.
func TestCoordinator_WritesFileAndSpan(t *testing.T) {
	c, sr, _ := recordingCoordinator(t)
	snap := representativeGame(t)
	c.OnGameEnd(snap)

	if _, err := os.Stat(c.writer.Path(snap.SessionID)); err != nil {
		t.Errorf("summary file not written: %v", err)
	}

	spans := spansByName(sr.Ended(), SpanGameSummary)
	if len(spans) != 1 {
		t.Fatalf("agent.game.summary spans = %d, want 1", len(spans))
	}
	if v, ok := spanAttr(spans[0], attrGameID); !ok || v.AsString() != snap.SessionID {
		t.Errorf("span game.id = %v, want %s", v.AsString(), snap.SessionID)
	}
	if v, ok := spanAttr(spans[0], attrEliminationCount); !ok || v.AsInt64() != 2 {
		t.Errorf("span elimination_count = %v, want 2", v.AsInt64())
	}
	if v, ok := spanAttr(spans[0], attrInterventionCount); !ok || v.AsInt64() != 2 {
		t.Errorf("span intervention_count = %v, want 2", v.AsInt64())
	}
}

// TestCoordinator_LinksToGameTrace: when the game-session trace ids are present on the
// ending GameContext (#1133), the agent.game.summary span Links back to the game-session
// trace (the hub, #1174). nil-safe: with no ids, no Link is added.
func TestCoordinator_LinksToGameTrace(t *testing.T) {
	const (
		gameTrace  = "4bf92f3577b34da6a3ce929d0e0e4736"
		gameSpanID = "051581bf3cb55c13"
	)

	t.Run("present", func(t *testing.T) {
		c, sr, _ := recordingCoordinator(t)
		snap := representativeGame(t)
		snap.GameTraceID = gameTrace
		snap.GameTraceSpanID = gameSpanID
		c.OnGameEnd(snap)

		spans := spansByName(sr.Ended(), SpanGameSummary)
		if len(spans) != 1 {
			t.Fatalf("agent.game.summary spans = %d, want 1", len(spans))
		}
		links := spans[0].Links()
		if len(links) != 1 {
			t.Fatalf("agent.game.summary links = %d, want 1 (the game-trace hub Link)", len(links))
		}
		if got := links[0].SpanContext.TraceID().String(); got != gameTrace {
			t.Errorf("link trace_id = %s, want %s", got, gameTrace)
		}
		if got := links[0].SpanContext.SpanID().String(); got != gameSpanID {
			t.Errorf("link span_id = %s, want %s", got, gameSpanID)
		}
		if !links[0].SpanContext.IsValid() {
			t.Error("game-trace Link must target a valid SpanContext")
		}
	})

	t.Run("absent", func(t *testing.T) {
		c, sr, _ := recordingCoordinator(t)
		c.OnGameEnd(representativeGame(t)) // no trace ids set
		spans := spansByName(sr.Ended(), SpanGameSummary)
		if len(spans) != 1 {
			t.Fatalf("agent.game.summary spans = %d, want 1", len(spans))
		}
		if n := len(spans[0].Links()); n != 0 {
			t.Errorf("agent.game.summary links = %d, want 0 (no ids => no Link)", n)
		}
	})
}

// TestCoordinator_RunsForShadowGames: the builder runs for synthetic (shadow)
// games too (#928 — both real AND shadow).
func TestCoordinator_RunsForShadowGames(t *testing.T) {
	c, sr, _ := recordingCoordinator(t)
	c.OnGameEnd(gamecontext.GameContext{
		SessionID: "game_shadow1",
		GameKind:  "shadow",
		Session:   gamecontext.SessionSignals{GameActive: bptr(false)},
		Players:   map[string]*gamecontext.PlayerSignals{"AA:11": {Serial: "AA:11"}},
	})
	if _, err := os.Stat(c.writer.Path("game_shadow1")); err != nil {
		t.Errorf("shadow-game summary not written: %v", err)
	}
	if len(spansByName(sr.Ended(), SpanGameSummary)) != 1 {
		t.Error("shadow game must emit an agent.game.summary span")
	}
}

// TestCoordinator_OncePerGame: a repeated game-end for the same id summarizes
// exactly once (exactly-once dedupe), so the file is not rewritten and only one
// span is emitted.
func TestCoordinator_OncePerGame(t *testing.T) {
	c, sr, _ := recordingCoordinator(t)
	snap := gamecontext.GameContext{
		SessionID: "game_dup",
		GameKind:  "real",
		Session:   gamecontext.SessionSignals{GameActive: bptr(false)},
	}
	c.OnGameEnd(snap)
	c.OnGameEnd(snap)
	c.OnGameEnd(snap)
	if n := len(spansByName(sr.Ended(), SpanGameSummary)); n != 1 {
		t.Errorf("spans = %d, want 1 (exactly-once per game)", n)
	}
}

// TestCoordinator_SkipsNonEnd: an active-game or empty-id snapshot is not a game
// end and must not write or span.
func TestCoordinator_SkipsNonEnd(t *testing.T) {
	c, sr, dir := recordingCoordinator(t)

	c.OnGameEnd(gamecontext.GameContext{SessionID: "active", Session: gamecontext.SessionSignals{GameActive: bptr(true)}})
	c.OnGameEnd(gamecontext.GameContext{SessionID: "", Session: gamecontext.SessionSignals{GameActive: bptr(false)}})

	if len(spansByName(sr.Ended(), SpanGameSummary)) != 0 {
		t.Error("non-end snapshots must not emit a span")
	}
	entries, _ := os.ReadDir(dir)
	if len(entries) != 0 {
		t.Errorf("non-end snapshots must not write files, found %d", len(entries))
	}
}

// TestCoordinator_TwoConcurrentGames: two independent game ids produce two
// independent summary files — the #845 per-game guarantee.
func TestCoordinator_TwoConcurrentGames(t *testing.T) {
	c, _, dir := recordingCoordinator(t)
	var wg sync.WaitGroup
	for _, id := range []string{"game_p1", "game_p2"} {
		wg.Add(1)
		go func(id string) {
			defer wg.Done()
			c.OnGameEnd(gamecontext.GameContext{
				SessionID: id,
				GameKind:  "real",
				Session:   gamecontext.SessionSignals{GameActive: bptr(false)},
			})
		}(id)
	}
	wg.Wait()
	entries, _ := os.ReadDir(dir)
	if len(entries) != 2 {
		t.Errorf("want 2 independent summary files, got %d", len(entries))
	}
}

// TestCoordinator_EndToEndFromStore: wiring a real Store's OnGameEnd to the
// coordinator writes a summary on the GameActive true->false transition, proving
// the full game-end path produces a file with no manual snapshot.
func TestCoordinator_EndToEndFromStore(t *testing.T) {
	dir := t.TempDir()
	c := NewCoordinator(NewWriter(dir), slog.New(slog.NewTextHandler(io.Discard, nil)))
	c.now = func() time.Time { return at(100) }

	clock := base
	s := gamecontext.NewStore(time.Hour, time.Hour, func() time.Time { return clock })
	s.OnGameEnd = c.OnGameEnd

	s.SetGameKind("real")
	s.SetGameActive(true)
	s.AdoptSessionID("game_e2e") // adopt after session start (see representativeGame)
	s.SetActivePlayerCount(2)
	clock = at(30)
	s.SetEliminationOrder("AA:11", 1)
	clock = at(40)
	s.SetGameActive(false) // fires OnGameEnd -> summary written

	if _, err := os.Stat(c.writer.Path("game_e2e")); err != nil {
		t.Errorf("end-to-end store summary not written: %v", err)
	}
}

// TestCoordinator_ReparentsUnderExperiment (#1188): when an experiment-parent seam is
// wired and the finished game is experiment-bound with a live root span, the
// agent.game.summary span is a CHILD of the experiment root span (same trace_id, parent
// = the root span_id). A non-experiment game keeps its own-root behavior.
func TestCoordinator_ReparentsUnderExperiment(t *testing.T) {
	c, sr, _ := recordingCoordinator(t)

	// A stand-in experiment ROOT span context the seam returns for "exp_1".
	rootTID, _ := trace.TraceIDFromHex("4bf92f3577b34da6a3ce929d0e0e4736")
	rootSID, _ := trace.SpanIDFromHex("00f067aa0ba902b7")
	rootSC := trace.NewSpanContext(trace.SpanContextConfig{
		TraceID: rootTID, SpanID: rootSID, TraceFlags: trace.FlagsSampled, Remote: true,
	})
	c.SetExperimentParent(func(id string) (trace.SpanContext, bool) {
		if id == "exp_1" {
			return rootSC, true
		}
		return trace.SpanContext{}, false
	})

	// Experiment-bound game => re-parented under the experiment root span.
	c.OnGameEnd(gamecontext.GameContext{
		SessionID: "game_exp", ExperimentID: "exp_1", GameKind: "shadow",
		Session: gamecontext.SessionSignals{GameActive: bptr(false)},
	})
	// Non-experiment game => own-root (the seam returns ok=false).
	c.OnGameEnd(gamecontext.GameContext{
		SessionID: "game_real", GameKind: "real",
		Session: gamecontext.SessionSignals{GameActive: bptr(false)},
	})

	spans := spansByName(sr.Ended(), SpanGameSummary)
	if len(spans) != 2 {
		t.Fatalf("agent.game.summary spans = %d, want 2", len(spans))
	}
	var exp, real sdktrace.ReadOnlySpan
	for _, s := range spans {
		if v, _ := spanAttr(s, attrGameID); v.AsString() == "game_exp" {
			exp = s
		} else {
			real = s
		}
	}
	if exp == nil || real == nil {
		t.Fatal("expected one experiment-bound and one real summary span")
	}
	if got := exp.SpanContext().TraceID(); got != rootTID {
		t.Errorf("experiment summary trace_id = %s, want root %s", got, rootTID)
	}
	if got := exp.Parent().SpanID(); got != rootSID {
		t.Errorf("experiment summary parent = %s, want root span %s", got, rootSID)
	}
	if real.Parent().IsValid() {
		t.Error("non-experiment summary must be own-root (no valid parent)")
	}
}
