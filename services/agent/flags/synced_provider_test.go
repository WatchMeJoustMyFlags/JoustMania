package flags

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	isync "github.com/open-feature/flagd/core/pkg/sync"
	flagd "github.com/open-feature/go-sdk-contrib/providers/flagd/pkg"
	"github.com/open-feature/go-sdk/openfeature"
)

// fakeFlagdSource stands in for flagd's sync port: a flag configuration that is
// unreachable until up, then pushed to every open sync stream on each change.
type fakeFlagdSource struct {
	mu      sync.Mutex
	up      bool
	enabled string // variant of `enabled`
	changed chan struct{}
}

const fakeSourceURI = "fake://flagd"

func newFakeFlagdSource(enabled string) *fakeFlagdSource {
	return &fakeFlagdSource{enabled: enabled, changed: make(chan struct{})}
}

func (s *fakeFlagdSource) set(up bool, enabled string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.up, s.enabled = up, enabled
	close(s.changed)
	s.changed = make(chan struct{})
}

func (s *fakeFlagdSource) snapshot() (bool, string, chan struct{}) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.up, s.enabled, s.changed
}

func (s *fakeFlagdSource) config(enabled string) string {
	doc := map[string]any{"flags": map[string]any{
		keyEnabled: map[string]any{
			"state": "ENABLED", "defaultVariant": enabled,
			"variants": map[string]any{"on": true, "off": false},
		},
		keyPlayerTTLSeconds: map[string]any{
			"state": "ENABLED", "defaultVariant": "short",
			"variants": map[string]any{"short": 3, "default": 5},
		},
		"remediation_allowed": map[string]any{
			"state": "ENABLED", "defaultVariant": "on",
			"variants": map[string]any{"on": true, "off": false},
		},
	}}
	raw, _ := json.Marshal(doc)
	return string(raw)
}

// fakeSync is one provider's sync stream (isync.ISync) on the fake source.
type fakeSync struct{ src *fakeFlagdSource }

func (f *fakeSync) Init(context.Context) error { return nil }
func (f *fakeSync) IsReady() bool              { return true }
func (f *fakeSync) ReSync(context.Context, chan<- isync.DataSync) error {
	return nil
}

func (f *fakeSync) Sync(ctx context.Context, out chan<- isync.DataSync) error {
	for {
		up, enabled, changed := f.src.snapshot()
		if up {
			select {
			case out <- isync.DataSync{FlagData: f.src.config(enabled), Source: fakeSourceURI}:
			case <-ctx.Done():
				return ctx.Err()
			}
		}
		select {
		case <-changed:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// fakeFactory builds real go-sdk-contrib flagd in-process providers over the fake source.
func fakeFactory(src *fakeFlagdSource) innerFactory {
	return func(initDeadline time.Duration) (innerProvider, error) {
		return flagd.NewProvider(
			flagd.WithInProcessResolver(),
			flagd.WithCustomSyncProviderAndUri(&fakeSync{src: src}, fakeSourceURI),
			flagd.WithDeadline(int(initDeadline.Milliseconds())),
		)
	}
}

var testSyncTiming = syncTiming{initDeadline: noInitDeadline, firstPause: 20 * time.Millisecond, maxPause: 80 * time.Millisecond, waitLog: time.Second}

func waitBool(t *testing.T, what string, read func() bool, want bool, timeout time.Duration) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for read() != want {
		if time.Now().After(deadline) {
			t.Fatalf("%s: still %v after %s, want %v", what, !want, timeout, want)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// TestSyncedProvider_LateFirstSyncThenEveryFlipIsSeen: flagd answers only after several
// init deadlines. A bare flagd provider gives up after its one deadline, never starts its
// event loop, and its event buffer (5) then wedges the sync after a few flips (#1713).
// The wrapper retries until the first sync, so every later flip of the kill switch and
// the lifecycle holder's flags is seen; until then the kill switch reads off.
//
// UC: UC-045 main A3
func TestSyncedProvider_LateFirstSyncThenEveryFlipIsSeen(t *testing.T) {
	agentSrc, rolloutSrc := newFakeFlagdSource("on"), newFakeFlagdSource("on")
	ctx, cancel := context.WithCancel(context.Background())
	f, holder, shutdown, err := setupFlagd(ctx, fakeFactory(agentSrc), fakeFactory(rolloutSrc), testSyncTiming, nil)
	if err != nil {
		t.Fatalf("setupFlagd: %v", err)
	}
	t.Cleanup(func() {
		cancel()
		// Bounded: a wedged provider (the #1713 bug) blocks its Shutdown for good.
		done := make(chan struct{})
		go func() { shutdown(); close(done) }()
		select {
		case <-done:
		case <-time.After(2 * time.Second):
			t.Error("provider shutdown still blocked after 2s: its sync is wedged")
		}
	})
	read := func() bool { return f.Evaluate(context.Background()).Enabled }

	t.Run("UC-045/A3 kill switch fails closed before the first sync", func(t *testing.T) {
		time.Sleep(700 * time.Millisecond) // longer than the provider's default 500 ms init deadline
		if read() {
			t.Fatal("enabled read on before any flag sync")
		}
		if f.RemediationAllowed(context.Background()) {
			t.Fatal("remediation_allowed read on before any flag sync")
		}
	})

	t.Run("UC-045/main flips after a late first sync", func(t *testing.T) {
		agentSrc.set(true, "on")
		rolloutSrc.set(true, "on")
		waitBool(t, "enabled after the first sync", read, true, 5*time.Second)
		waitBool(t, "remediation_allowed (rollout domain)", func() bool { return f.RemediationAllowed(context.Background()) }, true, 5*time.Second)
		for i := 0; i < 8; i++ {
			agentSrc.set(true, "off")
			waitBool(t, fmt.Sprintf("flip %d enabled off", i), read, false, time.Second)
			agentSrc.set(true, "on")
			waitBool(t, fmt.Sprintf("flip %d enabled on", i), read, true, time.Second)
		}
		// The holder follows the synced value (configuration change or the 1 s re-read).
		deadline := time.Now().Add(3 * time.Second)
		for holder.PlayerTTL() != 3*time.Second {
			if time.Now().After(deadline) {
				t.Fatalf("lifecycle holder player_ttl = %s, want 3s from the sync", holder.PlayerTTL())
			}
			time.Sleep(10 * time.Millisecond)
		}
	})
}

// fakeInner is a scripted inner provider: Init fails initFailures times, then succeeds;
// the test drives its events.
type fakeInner struct {
	initErr  error
	events   chan openfeature.Event
	shutdown chan struct{}
	once     sync.Once
}

func (p *fakeInner) Metadata() openfeature.Metadata           { return openfeature.Metadata{Name: "fake"} }
func (p *fakeInner) Hooks() []openfeature.Hook                { return nil }
func (p *fakeInner) Init(openfeature.EvaluationContext) error { return p.initErr }
func (p *fakeInner) Shutdown()                                { p.once.Do(func() { close(p.shutdown) }) }
func (p *fakeInner) EventChannel() <-chan openfeature.Event   { return p.events }
func (p *fakeInner) BooleanEvaluation(context.Context, string, bool, openfeature.FlattenedContext) openfeature.BoolResolutionDetail {
	return openfeature.BoolResolutionDetail{Value: true}
}
func (p *fakeInner) StringEvaluation(_ context.Context, _ string, d string, _ openfeature.FlattenedContext) openfeature.StringResolutionDetail {
	return openfeature.StringResolutionDetail{Value: d}
}
func (p *fakeInner) FloatEvaluation(_ context.Context, _ string, d float64, _ openfeature.FlattenedContext) openfeature.FloatResolutionDetail {
	return openfeature.FloatResolutionDetail{Value: d}
}
func (p *fakeInner) IntEvaluation(_ context.Context, _ string, d int64, _ openfeature.FlattenedContext) openfeature.IntResolutionDetail {
	return openfeature.IntResolutionDetail{Value: d}
}
func (p *fakeInner) ObjectEvaluation(_ context.Context, _ string, d any, _ openfeature.FlattenedContext) openfeature.InterfaceResolutionDetail {
	return openfeature.InterfaceResolutionDetail{Value: d}
}

type scriptedFactory struct {
	mu           sync.Mutex
	initFailures int
	built        []*fakeInner
}

func (s *scriptedFactory) build(time.Duration) (innerProvider, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p := &fakeInner{events: make(chan openfeature.Event), shutdown: make(chan struct{})}
	if len(s.built) < s.initFailures {
		p.initErr = errors.New("provider initialization deadline exceeded")
	}
	s.built = append(s.built, p)
	return p, nil
}

func (s *scriptedFactory) last() *fakeInner {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.built[len(s.built)-1]
}

// TestSyncedProvider_RetriesThenFailsClosedWhileTheStreamIsLost: failed attempts are shut
// down and retried; once synced, a lost stream (STALE, then ERROR) turns every read to its
// default until flagd syncs again (READY), as on the RPC resolver with flagd unreachable.
//
// UC: UC-045 A3
func TestSyncedProvider_RetriesThenFailsClosedWhileTheStreamIsLost(t *testing.T) {
	factory := &scriptedFactory{initFailures: 3}
	const domain = "synced-provider-test"
	p := newSyncedProvider(domain, factory.build, testSyncTiming, nil)
	if err := openfeature.SetNamedProviderAndWait(domain, p); err != nil {
		t.Fatalf("SetNamedProviderAndWait: %v", err)
	}
	t.Cleanup(p.Shutdown)
	client := openfeature.GetApiInstance().GetNamedClient(domain)
	read := func() bool {
		v, _ := client.BooleanValue(context.Background(), keyEnabled, false, openfeature.EvaluationContext{})
		return v
	}

	t.Run("UC-045/A3 failed attempts are shut down and retried", func(t *testing.T) {
		if got := len(factory.built); got != 4 {
			t.Fatalf("attempts = %d, want 4 (3 failed + 1 synced)", got)
		}
		for i, inner := range factory.built[:3] {
			select {
			case <-inner.shutdown:
			case <-time.After(time.Second):
				t.Fatalf("failed attempt %d was not shut down", i+1)
			}
		}
		if !read() {
			t.Fatal("enabled read off after the first sync")
		}
	})

	t.Run("UC-045/A3 lost stream fails closed until flagd syncs again", func(t *testing.T) {
		inner := factory.last()
		for _, lost := range []openfeature.EventType{openfeature.ProviderStale, openfeature.ProviderError} {
			inner.events <- openfeature.Event{EventType: lost}
			waitBool(t, string(lost), read, false, time.Second)
			inner.events <- openfeature.Event{EventType: openfeature.ProviderReady}
			waitBool(t, string(lost)+" then READY", read, true, time.Second)
		}
	})

	t.Run("UC-045/A3 shutdown ends a pending Init", func(t *testing.T) {
		blocked := &scriptedFactory{initFailures: 1 << 30}
		pending := newSyncedProvider(domain+"-pending", blocked.build, testSyncTiming, nil)
		done := make(chan error, 1)
		go func() { done <- pending.Init(openfeature.EvaluationContext{}) }()
		time.Sleep(50 * time.Millisecond)
		pending.Shutdown()
		select {
		case err := <-done:
			if !errors.Is(err, errProviderShutDown) {
				t.Fatalf("Init = %v, want errProviderShutDown", err)
			}
		case <-time.After(time.Second):
			t.Fatal("Init still running after Shutdown")
		}
	})
}
