package main

import (
	"context"
	"log/slog"

	otelmetric "go.opentelemetry.io/otel/metric"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamejournal"
)

// newGameJournal opens the calibration journal under the experiments root and
// starts its writer goroutine on ctx (UC-047 step 1, BR-009, #1055). It never
// fails the agent: an unreadable journal file is logged and counted (reason
// load), and the recorder then counts every refused write instead of replacing
// the file. agentFlags may be nil (the test-session mark is then unknown).
func newGameJournal(ctx context.Context, root string, agentFlags *flags.Flags, mp otelmetric.MeterProvider, log *slog.Logger) *gamejournal.Recorder {
	j, rep, err := gamejournal.Open(root, gamejournal.DefaultMaxGames)
	var testSession func(context.Context) (bool, bool)
	if agentFlags != nil {
		testSession = agentFlags.CalibrationTestSession
	}
	rec := gamejournal.NewRecorder(j, testSession, mp, log)
	switch {
	case err != nil:
		rec.CountFailure(gamejournal.ReasonLoad)
		log.Warn("agent.calibration.journal_load_failed", "path", j.Path(), "error", err)
	case rep.Skipped > 0:
		rec.CountFailure(gamejournal.ReasonLoad)
		log.Warn("agent.calibration.journal_load_failed", "path", j.Path(), "records", rep.Records, "skipped_lines", rep.Skipped)
	default:
		log.Info("Calibration game journal enabled (UC-047, #1055)", "path", j.Path(), "records", rep.Records, "max_games", gamejournal.DefaultMaxGames)
	}
	go rec.Run(ctx)
	return rec
}
