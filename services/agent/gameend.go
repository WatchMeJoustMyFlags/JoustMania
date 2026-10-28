package main

import (
	"context"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	otelmetric "go.opentelemetry.io/otel/metric"
	metricnoop "go.opentelemetry.io/otel/metric/noop"

	"github.com/joustmania/agent/gamecontext"
)

// gameend.go holds the game-end chain's handling of a stale end (UC-041 A4, #1494):
// the agent ended a game because its game_active series went silent, which means
// the game's telemetry broke and the end snapshot is truncated.

// gameEndMeterName scopes the game-end instruments (module path, the main package).
const gameEndMeterName = "github.com/joustmania/agent"

const metricGameEndStale = "agent_game_end_stale_total"

// newGameEndStaleCounter builds agent_game_end_stale_total, incremented once per
// game the agent ended on the stale game_active backstop, labeled game_kind
// (primary / shadow, gamecontext.TelemetryGameKind; an empty kind counts as shadow). An instrument-creation error yields a no-op counter, as in
// decision/metrics.go.
func newGameEndStaleCounter(mp otelmetric.MeterProvider) otelmetric.Int64Counter {
	c, err := mp.Meter(gameEndMeterName).Int64Counter(
		metricGameEndStale,
		otelmetric.WithDescription("Total games the agent ended because their game_active series went silent (telemetry lost, UC-041 A4, #1494)"),
	)
	if err != nil {
		c, _ = metricnoop.NewMeterProvider().Meter(gameEndMeterName).Int64Counter(metricGameEndStale)
	}
	return c
}

// countStaleGameEnd returns a game-end hook that increments counter for a
// stale-ended game and ignores every other end.
func countStaleGameEnd(counter otelmetric.Int64Counter) func(gamecontext.GameContext) {
	return func(c gamecontext.GameContext) {
		if c.EndReason != gamecontext.EndReasonStale {
			return
		}
		counter.Add(context.Background(), 1,
			otelmetric.WithAttributes(attribute.String("game_kind", gamecontext.TelemetryGameKind(c.GameKind))))
	}
}

// skipStaleGameEnd wraps a game-end consumer so a stale-ended game never reaches
// it. Used for the retro, the game summary and the proposer: a stale end's
// snapshot stops wherever the telemetry stopped (missing eliminations, a short
// duration), so an LLM retro of it would assess a game that did not happen, its
// summary would enter the cross-game window the decide path and the proposer read,
// and the proposer would propose from it. Skipping (rather than flagging) keeps
// every consumer's input honest without each one learning a new field; the stale
// end stays visible through agent_game_end_stale_total and the agent log.
func skipStaleGameEnd(fn func(gamecontext.GameContext)) func(gamecontext.GameContext) {
	if fn == nil {
		return nil
	}
	return func(c gamecontext.GameContext) {
		if c.EndReason == gamecontext.EndReasonStale {
			return
		}
		fn(c)
	}
}

// buildGameEndChain composes the per-partition game-end chain (#928, #1100,
// #1494): the stale counter, then the experiment conclusion hook (first, so it
// records fitness before the retro reads it back), then the retro, the game
// summary and the proposer. A stale end (UC-041 A4) is counted and reaches the
// experiment hook, which releases its slot itself, but never the retro, the
// summary or the proposer. nil consumers are skipped.
func buildGameEndChain(
	staleEnds otelmetric.Int64Counter,
	experimentOnGameEnd, retroOnGameEnd, summaryOnGameEnd, proposeOnGameEnd func(gamecontext.GameContext),
) func(gamecontext.GameContext) {
	return chainGameEnd(
		countStaleGameEnd(staleEnds),
		experimentOnGameEnd,
		skipStaleGameEnd(retroOnGameEnd),
		skipStaleGameEnd(summaryOnGameEnd),
		skipStaleGameEnd(proposeOnGameEnd),
	)
}

// globalMeterProvider is the process meter provider (otel.go installs it at startup).
func globalMeterProvider() otelmetric.MeterProvider { return otel.GetMeterProvider() }
