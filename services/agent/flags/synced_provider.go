package flags

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/open-feature/go-sdk/openfeature"
)

// innerProvider is what syncedProvider wraps: a flagd provider (or a test double) with
// the OpenFeature state and event interfaces.
type innerProvider interface {
	openfeature.FeatureProvider
	openfeature.StateHandler
	openfeature.EventHandler
}

// innerFactory builds a fresh inner provider whose Init waits at most initDeadline for
// the first flag sync.
type innerFactory func(initDeadline time.Duration) (innerProvider, error)

// syncTiming paces the init attempts (flags.md §1.1, #1713).
type syncTiming struct {
	initDeadline time.Duration // inner Init deadline: effectively none (see syncedProvider)
	firstPause   time.Duration // pause after the first failed attempt; doubles per attempt
	maxPause     time.Duration // cap for the pause
	waitLog      time.Duration // how often a pending Init logs that it still waits
}

// noInitDeadline is longer than any outage the agent outlives, so the inner Init ends
// only on a first sync or on an error event, never on its timer.
const noInitDeadline = 365 * 24 * time.Hour

// defaultSyncTiming: no Init deadline, attempts 250 ms apart doubling to 10 s.
var defaultSyncTiming = syncTiming{initDeadline: noInitDeadline, firstPause: 250 * time.Millisecond, maxPause: 10 * time.Second, waitLog: 10 * time.Second}

var errProviderShutDown = errors.New("flagd provider shut down before its first sync")

// syncedProvider keeps a flagd in-process provider syncing however late flagd answers
// (#1713). go-sdk-contrib flagd v0.6.0 gives Init a single deadline (500 ms by default);
// when it passes before the first sync the provider never starts its event loop, the
// service's event buffer (5) fills after a few flag changes and the sync stops applying
// data: later flips are lost, the in-process form of #1709. Init also fails at once on the
// first STALE / ERROR event (flagd refusing the stream), with the same result. So:
//   - the inner Init gets no effective deadline (noInitDeadline): it waits for the first
//     sync, logging every waitLog that it still waits;
//   - an Init that fails on an error event is shut down and a FRESH provider is tried
//     after a pause (firstPause doubling to maxPause), logged at WARN, with no overall
//     limit. (Shutting a provider down after its timer fired would race its still-running
//     service Init; after an error event it is ordered, so the timer is never used.)
//
// Reads fail closed while the domain is not READY: before the first sync (the SDK keeps
// the domain NOT_READY while Init runs) and while the sync stream is lost (STALE / ERROR
// until flagd sends again), every evaluation returns the caller's default with
// PROVIDER_NOT_READY, as on the RPC resolver when flagd was unreachable (UC-045 A3).
type syncedProvider struct {
	name    string
	factory innerFactory
	timing  syncTiming
	log     *slog.Logger

	events   chan openfeature.Event
	stop     chan struct{}
	stopOnce sync.Once

	mu      sync.RWMutex
	inner   innerProvider // set once the first sync arrived
	serving bool          // READY: the inner provider has synced and its stream is not lost
}

var (
	_ openfeature.FeatureProvider = (*syncedProvider)(nil)
	_ openfeature.StateHandler    = (*syncedProvider)(nil)
	_ openfeature.EventHandler    = (*syncedProvider)(nil)
)

func newSyncedProvider(name string, factory innerFactory, timing syncTiming, log *slog.Logger) *syncedProvider {
	if log == nil {
		log = slog.Default()
	}
	return &syncedProvider{
		name:    name,
		factory: factory,
		timing:  timing,
		log:     log,
		events:  make(chan openfeature.Event, 16),
		stop:    make(chan struct{}),
	}
}

// Metadata names the provider "flagd", like the wrapped one.
func (p *syncedProvider) Metadata() openfeature.Metadata {
	return openfeature.Metadata{Name: "flagd"}
}

// Hooks: none (the flagd provider has none either).
func (p *syncedProvider) Hooks() []openfeature.Hook { return nil }

// EventChannel carries the inner provider's events after the first sync.
func (p *syncedProvider) EventChannel() <-chan openfeature.Event { return p.events }

// Init returns once a fresh inner provider has its first sync, or with an error when the
// provider is shut down first. SetNamedProvider runs it in the background, so a late
// flagd never blocks the agent's start.
func (p *syncedProvider) Init(evalCtx openfeature.EvaluationContext) error {
	started := time.Now()
	pause := p.timing.firstPause
	for attempt := 1; ; attempt++ {
		inner, err := p.factory(p.timing.initDeadline)
		if err == nil {
			err = p.initInner(inner, evalCtx)
			if err == nil {
				p.mu.Lock()
				p.inner, p.serving = inner, true
				p.mu.Unlock()
				go p.forward(inner)
				p.log.Info("flagd in-process provider synced", "domain", p.name, "attempts", attempt,
					"waited", time.Since(started).Round(time.Millisecond))
				return nil
			}
			go inner.Shutdown()
		}
		if errors.Is(err, errProviderShutDown) {
			return err
		}
		p.log.Warn("flagd in-process provider has no first sync yet; reads fail closed, retrying",
			"domain", p.name, "attempt", attempt, "retry_in", pause, "error", err)
		select {
		case <-p.stop:
			return errProviderShutDown
		case <-time.After(pause):
		}
		pause = min(2*pause, p.timing.maxPause)
	}
}

// initInner runs inner.Init but returns at once on Shutdown (the flagd provider holds its
// lock during Init, so its own Shutdown has to wait for Init to end).
func (p *syncedProvider) initInner(inner innerProvider, evalCtx openfeature.EvaluationContext) error {
	started := time.Now()
	done := make(chan error, 1)
	go func() { done <- inner.Init(evalCtx) }()
	waiting := time.NewTicker(p.timing.waitLog)
	defer waiting.Stop()
	for {
		select {
		case err := <-done:
			return err
		case <-waiting.C:
			p.log.Warn("still waiting for flagd's first flag sync; reads fail closed",
				"domain", p.name, "waited", time.Since(started).Round(time.Second))
		case <-p.stop:
			go inner.Shutdown()
			return errProviderShutDown
		}
	}
}

// forward relays the inner provider's events to the SDK and tracks READY: the inner
// provider sends READY / CONFIGURATION_CHANGED on each sync and STALE / ERROR when its
// stream is lost. Draining the inner channel is what keeps its sync applying data.
func (p *syncedProvider) forward(inner innerProvider) {
	in := inner.EventChannel()
	for {
		select {
		case <-p.stop:
			return
		case ev := <-in:
			switch ev.EventType {
			case openfeature.ProviderReady, openfeature.ProviderConfigChange:
				p.setServing(true)
			case openfeature.ProviderStale, openfeature.ProviderError:
				p.setServing(false)
				p.log.Warn("flagd sync stream lost; agent flag reads fail closed until it syncs again",
					"domain", p.name, "event", ev.EventType, "message", ev.Message)
			}
			select {
			case p.events <- ev:
			case <-p.stop:
				return
			}
		}
	}
}

func (p *syncedProvider) setServing(serving bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if serving && !p.serving {
		p.log.Info("flagd sync stream back; agent flag reads served again", "domain", p.name)
	}
	p.serving = serving
}

// Shutdown stops a running Init and the synced inner provider.
func (p *syncedProvider) Shutdown() {
	p.stopOnce.Do(func() { close(p.stop) })
	p.mu.Lock()
	inner := p.inner
	p.inner, p.serving = nil, false
	p.mu.Unlock()
	if inner != nil {
		inner.Shutdown()
	}
}

// current returns the inner provider while it is READY, else nil.
func (p *syncedProvider) current() innerProvider {
	p.mu.RLock()
	defer p.mu.RUnlock()
	if !p.serving {
		return nil
	}
	return p.inner
}

func (p *syncedProvider) notReady() openfeature.ProviderResolutionDetail {
	return openfeature.ProviderResolutionDetail{
		ResolutionError: openfeature.NewProviderNotReadyResolutionError(
			fmt.Sprintf("flagd domain %q has no live flag sync", p.name)),
		Reason: openfeature.ErrorReason,
	}
}

func (p *syncedProvider) BooleanEvaluation(ctx context.Context, flag string, defaultValue bool, evalCtx openfeature.FlattenedContext) openfeature.BoolResolutionDetail {
	if inner := p.current(); inner != nil {
		return inner.BooleanEvaluation(ctx, flag, defaultValue, evalCtx)
	}
	return openfeature.BoolResolutionDetail{Value: defaultValue, ProviderResolutionDetail: p.notReady()}
}

func (p *syncedProvider) StringEvaluation(ctx context.Context, flag string, defaultValue string, evalCtx openfeature.FlattenedContext) openfeature.StringResolutionDetail {
	if inner := p.current(); inner != nil {
		return inner.StringEvaluation(ctx, flag, defaultValue, evalCtx)
	}
	return openfeature.StringResolutionDetail{Value: defaultValue, ProviderResolutionDetail: p.notReady()}
}

func (p *syncedProvider) FloatEvaluation(ctx context.Context, flag string, defaultValue float64, evalCtx openfeature.FlattenedContext) openfeature.FloatResolutionDetail {
	if inner := p.current(); inner != nil {
		return inner.FloatEvaluation(ctx, flag, defaultValue, evalCtx)
	}
	return openfeature.FloatResolutionDetail{Value: defaultValue, ProviderResolutionDetail: p.notReady()}
}

func (p *syncedProvider) IntEvaluation(ctx context.Context, flag string, defaultValue int64, evalCtx openfeature.FlattenedContext) openfeature.IntResolutionDetail {
	if inner := p.current(); inner != nil {
		return inner.IntEvaluation(ctx, flag, defaultValue, evalCtx)
	}
	return openfeature.IntResolutionDetail{Value: defaultValue, ProviderResolutionDetail: p.notReady()}
}

func (p *syncedProvider) ObjectEvaluation(ctx context.Context, flag string, defaultValue any, evalCtx openfeature.FlattenedContext) openfeature.InterfaceResolutionDetail {
	if inner := p.current(); inner != nil {
		return inner.ObjectEvaluation(ctx, flag, defaultValue, evalCtx)
	}
	return openfeature.InterfaceResolutionDetail{Value: defaultValue, ProviderResolutionDetail: p.notReady()}
}
