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

	mu      sync.Mutex
	lastLog time.Time
	now     func() time.Time
}

// NewInfraLoop builds the observe-only stub. log may be nil (slog.Default() is
// used). A non-positive throttle falls back to DefaultThrottleInterval.
func NewInfraLoop(log *slog.Logger, throttle time.Duration) *InfraLoop {
	if log == nil {
		log = slog.Default()
	}
	if throttle <= 0 {
		throttle = DefaultThrottleInterval
	}
	return &InfraLoop{
		log:      log,
		throttle: throttle,
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

	l.log.Debug("infrastructure observe (#733 stub)",
		"active_controllers", snap.Window.ActiveControllers,
		"target_backend", snap.Window.TargetBackend,
		"rollout_count", snap.Window.RolloutCount,
		"controllers", len(snap.Controllers),
	)
}
