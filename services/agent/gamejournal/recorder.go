package gamejournal

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"go.opentelemetry.io/otel/attribute"
	otelmetric "go.opentelemetry.io/otel/metric"
	metricnoop "go.opentelemetry.io/otel/metric/noop"

	"github.com/joustmania/agent/gamecontext"
)

const meterName = "github.com/joustmania/agent/gamejournal"

// MetricWriteFailures counts journal records that did not reach the disk, by
// reason (telemetry.md §6): write (the durable write failed, or the journal
// refuses writes after an unreadable file or one with a newer schema),
// queue_full (the writer was behind and the record was dropped), shutdown (a
// game ended after Close, or a record was still queued when Close gave up),
// load (an existing file was unreadable, newer, or had invalid lines at startup).
const MetricWriteFailures = "agent_calibration_journal_write_failures_total"

// Failure reasons of MetricWriteFailures.
const (
	ReasonWrite     = "write"
	ReasonQueueFull = "queue_full"
	ReasonShutdown  = "shutdown"
	ReasonLoad      = "load"
)

// queueSize bounds the records waiting for the writer. Games end at most every
// few seconds, so a full queue means the disk is stuck; the record is dropped.
const queueSize = 16

// Recorder is the game-end consumer that journals real games (UC-047 step 1,
// BR-009). OnGameEnd builds the record on the caller's goroutine (a pure
// function of the snapshot plus one flag read) and hands it to a single writer
// goroutine (Run), so the fsync never runs on the telemetry ingest path. Every
// failure is fail-soft: it logs a WARN and counts, and never reaches the game or
// the decide path.
type Recorder struct {
	journal     *Journal
	testSession func(context.Context) (on, known bool)
	failures    otelmetric.Int64Counter
	log         *slog.Logger
	now         func() time.Time
	queue       chan Record

	// mu guards closed and every send on queue, so no send races Close's close.
	mu     sync.Mutex
	closed bool
	// done is closed when Run has written the last queued record.
	done chan struct{}
}

// NewRecorder builds a recorder over j. testSession reads calibration.test_session
// at use time; nil records the mark as unknown. mp may be nil (no-op metrics).
func NewRecorder(j *Journal, testSession func(context.Context) (bool, bool), mp otelmetric.MeterProvider, log *slog.Logger) *Recorder {
	if log == nil {
		log = slog.Default()
	}
	return &Recorder{
		journal:     j,
		testSession: testSession,
		failures:    NewFailureCounter(mp),
		log:         log,
		now:         time.Now,
		queue:       make(chan Record, queueSize),
		done:        make(chan struct{}),
	}
}

// NewFailureCounter builds MetricWriteFailures; an instrument error or a nil
// provider yields a no-op counter.
func NewFailureCounter(mp otelmetric.MeterProvider) otelmetric.Int64Counter {
	if mp == nil {
		mp = metricnoop.NewMeterProvider()
	}
	c, err := mp.Meter(meterName).Int64Counter(MetricWriteFailures,
		otelmetric.WithDescription("Calibration journal records that did not reach the disk, by reason (UC-047 BR-009, #1055)"))
	if err != nil {
		c, _ = metricnoop.NewMeterProvider().Meter(meterName).Int64Counter(MetricWriteFailures)
	}
	return c
}

// CountFailure increments MetricWriteFailures for reason.
func (r *Recorder) CountFailure(reason string) {
	r.failures.Add(context.Background(), 1, otelmetric.WithAttributes(attribute.String("reason", reason)))
}

// OnGameEnd journals a real game end. A shadow game, an unknown kind, a
// snapshot that is still active or has no game id is not journaled. Never blocks.
func (r *Recorder) OnGameEnd(gc gamecontext.GameContext) {
	if gc.GameKind != gamecontext.GameKindReal || gc.SessionID == "" {
		return
	}
	if gc.Session.GameActive != nil && *gc.Session.GameActive {
		return
	}
	var mark *bool
	if r.testSession != nil {
		if on, known := r.testSession(context.Background()); known {
			mark = &on
		}
	}
	rec := BuildRecord(gc, mark, r.now())
	reason := ""
	r.mu.Lock()
	if r.closed {
		reason = ReasonShutdown
	} else {
		select {
		case r.queue <- rec:
		default:
			reason = ReasonQueueFull
		}
	}
	r.mu.Unlock()
	if reason != "" {
		r.CountFailure(reason)
		r.log.Warn("agent.calibration.journal_write_failed", "game_id", rec.GameID, "reason", reason)
	}
}

// Run writes queued records until Close, then returns. Run it once.
func (r *Recorder) Run() {
	defer close(r.done)
	for rec := range r.queue {
		r.write(rec)
	}
}

// Close stops the intake (a later game end counts shutdown) and waits up to
// timeout for Run to write what is queued. It reports whether Run drained; when
// it did not, the records still queued count shutdown. Call it once, after the
// telemetry intake has stopped (server.go shutdown path).
func (r *Recorder) Close(timeout time.Duration) bool {
	if r == nil {
		return true
	}
	r.mu.Lock()
	if !r.closed {
		r.closed = true
		close(r.queue)
	}
	r.mu.Unlock()
	select {
	case <-r.done:
		return true
	case <-time.After(timeout):
		if n := len(r.queue); n > 0 {
			r.failures.Add(context.Background(), int64(n), otelmetric.WithAttributes(attribute.String("reason", ReasonShutdown)))
		}
		r.log.Warn("agent.calibration.journal_write_failed", "reason", ReasonShutdown, "queued", len(r.queue))
		return false
	}
}

func (r *Recorder) write(rec Record) {
	if err := r.journal.Append(rec); err != nil {
		r.CountFailure(ReasonWrite)
		r.log.Warn("agent.calibration.journal_write_failed", "game_id", rec.GameID, "reason", ReasonWrite, "error", err)
	}
}
