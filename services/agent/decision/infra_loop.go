package decision

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"github.com/joustmania/agent/infracontext"
)

// InfraEvaluator is the seam the infrastructure observe path triggers when the
// Bluetooth-health context updates. PR E plugs the real fitness + remediation
// loop in behind this interface; this PR ships only the logging stub below.
//
// OnInfraEvaluate receives the inbound gRPC context (for trace propagation in
// PR E) and an isolated InfraContext snapshot.
type InfraEvaluator interface {
	OnInfraEvaluate(ctx context.Context, snap infracontext.InfraContext)
}

// InfraLoop is the OBSERVE-only infrastructure evaluation stub (#733, M3 PR C).
// It logs the observed Bluetooth-health snapshot at debug level, throttled to at
// most one line per throttle interval so the ~1Hz health-span stream does not
// flood the log. The real fitness/decision/remediation loop lands in PR E,
// plugging in behind the InfraEvaluator seam.
type InfraLoop struct {
	log      *slog.Logger
	throttle time.Duration
	// fitness supplies the live fitness.bluetooth.* thresholds (#735). The real
	// fitness/remediation consumer arrives in PR E; for now the stub only logs the
	// thresholds in effect so the seam is exercised. May be nil (defaults logged).
	fitness BluetoothFitnessSource

	mu      sync.Mutex
	lastLog time.Time
	now     func() time.Time
}

// NewInfraLoop builds the observe-only stub. log may be nil (slog.Default() is
// used). A non-positive throttle falls back to DefaultThrottleInterval. fitness
// may be nil; the per-cycle Bluetooth thresholds it provides (#735) are only
// logged in this PR — PR E plugs the fitness evaluation in behind this seam.
func NewInfraLoop(log *slog.Logger, throttle time.Duration, fitness BluetoothFitnessSource) *InfraLoop {
	if log == nil {
		log = slog.Default()
	}
	if throttle <= 0 {
		throttle = DefaultThrottleInterval
	}
	return &InfraLoop{
		log:      log,
		throttle: throttle,
		fitness:  fitness,
		now:      time.Now,
	}
}

// OnInfraEvaluate logs the current Bluetooth-health snapshot, throttled. It is
// safe for concurrent use: the trace Export handler invokes it from its own
// goroutine.
func (l *InfraLoop) OnInfraEvaluate(_ context.Context, snap infracontext.InfraContext) {
	l.mu.Lock()
	now := l.now()
	if !l.lastLog.IsZero() && now.Sub(l.lastLog) < l.throttle {
		l.mu.Unlock()
		return
	}
	l.lastLog = now
	l.mu.Unlock()

	th := DefaultBluetoothThresholds()
	if l.fitness != nil {
		th = l.fitness.BluetoothThresholds()
	}
	l.log.Debug("infrastructure observe (#733 stub)",
		"active_controllers", snap.Window.ActiveControllers,
		"target_backend", snap.Window.TargetBackend,
		"rollout_count", snap.Window.RolloutCount,
		"controllers", len(snap.Controllers),
		"max_event_gap_ms", th.MaxEventGapMs,
		"max_dropped_events_pct", th.MaxDroppedEventsPct,
		"min_movement_update_hz", th.MinMovementUpdateHz,
	)
}
