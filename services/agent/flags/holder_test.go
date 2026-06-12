package flags

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/open-feature/go-sdk/openfeature"
)

// settableEvaluator is a mutable stubEvaluator: tests mutate its float values
// between Reload calls to simulate a flagd config-change WITHOUT a live flagd.
// It satisfies Evaluator like the hand-rolled stub in flags_test.go but lets a
// test change a flag mid-flight under a mutex (Reload runs from the "event"
// goroutine in the race test).
type settableEvaluator struct {
	mu     sync.Mutex
	floats map[string]float64
}

func newSettableEvaluator() *settableEvaluator {
	return &settableEvaluator{floats: map[string]float64{}}
}

func (s *settableEvaluator) set(key string, v float64) {
	s.mu.Lock()
	s.floats[key] = v
	s.mu.Unlock()
}

func (s *settableEvaluator) FloatValue(_ context.Context, flag string, def float64, _ openfeature.EvaluationContext, _ ...openfeature.Option) (float64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if v, ok := s.floats[flag]; ok {
		return v, nil
	}
	return def, nil
}

// The remaining Evaluator methods are unused by Lifecycle (it reads only floats)
// but are required to satisfy the interface.
func (s *settableEvaluator) BooleanValue(_ context.Context, _ string, def bool, _ openfeature.EvaluationContext, _ ...openfeature.Option) (bool, error) {
	return def, nil
}
func (s *settableEvaluator) StringValue(_ context.Context, _ string, def string, _ openfeature.EvaluationContext, _ ...openfeature.Option) (string, error) {
	return def, nil
}
func (s *settableEvaluator) IntValue(_ context.Context, _ string, def int64, _ openfeature.EvaluationContext, _ ...openfeature.Option) (int64, error) {
	return def, nil
}
func (s *settableEvaluator) ObjectValue(_ context.Context, _ string, def any, _ openfeature.EvaluationContext, _ ...openfeature.Option) (any, error) {
	return def, nil
}

// TestLifecycleHolder_PrimedWithInitialValues verifies the holder primes once at
// construction (before any config-change event) with the current flag values, so
// consumers read correct values from the first cycle (#927).
func TestLifecycleHolder_PrimedWithInitialValues(t *testing.T) {
	ev := newSettableEvaluator()
	ev.set(keyDecisionThrottleSecs, 3)
	ev.set(keyPlayerTTLSeconds, 7)
	ev.set(keyEvictIntervalSeconds, 2)
	ev.set(keySessionGraceSeconds, 20)

	h := NewLifecycleHolder(context.Background(), New(ev, nil), nil)

	got := h.Load()
	if got.DecisionThrottle != 3*time.Second {
		t.Errorf("primed DecisionThrottle = %v, want 3s", got.DecisionThrottle)
	}
	if got.PlayerTTL != 7*time.Second {
		t.Errorf("primed PlayerTTL = %v, want 7s", got.PlayerTTL)
	}
	if got.EvictInterval != 2*time.Second {
		t.Errorf("primed EvictInterval = %v, want 2s", got.EvictInterval)
	}
	if got.SessionGrace != 20*time.Second {
		t.Errorf("primed SessionGrace = %v, want 20s", got.SessionGrace)
	}
}

// TestLifecycleHolder_DefaultsWhenUnset verifies an empty flag source primes the
// holder with the flagd-schema safe defaults — the down-control-plane behavior
// the read-at-startup path had, preserved by #927.
func TestLifecycleHolder_DefaultsWhenUnset(t *testing.T) {
	h := NewLifecycleHolder(context.Background(), New(newSettableEvaluator(), nil), nil)
	got := h.Load()
	if got.DecisionThrottle != time.Duration(DefaultDecisionThrottleSeconds*float64(time.Second)) {
		t.Errorf("default DecisionThrottle = %v, want %vs", got.DecisionThrottle, DefaultDecisionThrottleSeconds)
	}
	if got.PlayerTTL != time.Duration(DefaultPlayerTTLSeconds*float64(time.Second)) {
		t.Errorf("default PlayerTTL = %v, want %vs", got.PlayerTTL, DefaultPlayerTTLSeconds)
	}
}

// TestLifecycleHolder_ReloadAppliesNewValues is the #927 ACCEPTANCE test driver
// at the holder level: changing decision.throttle_seconds and re-running the
// holder's update path (the function the config-change handler calls) makes the
// new value visible with NO restart — the live source funcs the consumers hold
// reflect it on the very next read.
func TestLifecycleHolder_ReloadAppliesNewValues(t *testing.T) {
	ev := newSettableEvaluator()
	ev.set(keyDecisionThrottleSecs, 1)
	h := NewLifecycleHolder(context.Background(), New(ev, nil), nil)

	if got := h.DecisionThrottle(); got != 1*time.Second {
		t.Fatalf("initial DecisionThrottle source = %v, want 1s", got)
	}

	// Simulate a flagd config-change: the flag's value moves, then the event
	// handler's Reload runs. No live flagd, no sleep, no network.
	ev.set(keyDecisionThrottleSecs, 5)
	h.Reload(context.Background())

	if got := h.DecisionThrottle(); got != 5*time.Second {
		t.Fatalf("after Reload DecisionThrottle source = %v, want 5s (mid-session change, no restart)", got)
	}
	// The other live sources also reflect the reload (they share the same Load()).
	if got := h.PlayerTTL(); got != time.Duration(DefaultPlayerTTLSeconds*float64(time.Second)) {
		t.Errorf("PlayerTTL source = %v, want default (unchanged flag)", got)
	}
}

// fakeEventClient captures the registered config-change handler so a test can
// invoke it with a synthetic event — standing in for flagd emitting a
// ProviderConfigChange.
type fakeEventClient struct {
	eventType openfeature.EventType
	callback  openfeature.EventCallback
}

func (f *fakeEventClient) AddHandler(eventType openfeature.EventType, callback openfeature.EventCallback) {
	f.eventType = eventType
	f.callback = callback
}

// TestLifecycleHolder_ConfigChangeEventReloads verifies the wired OpenFeature
// configuration-change handler (the maintainer's requested mechanism) re-reads
// the flags when the SDK fires the event: we register against a fake client,
// then invoke the captured callback with a synthetic EventDetails and assert the
// holder picked up the new throttle value (#927).
func TestLifecycleHolder_ConfigChangeEventReloads(t *testing.T) {
	ev := newSettableEvaluator()
	ev.set(keyDecisionThrottleSecs, 1)
	h := NewLifecycleHolder(context.Background(), New(ev, nil), nil)

	fc := &fakeEventClient{}
	h.RegisterConfigChangeHandler(context.Background(), fc)

	if fc.eventType != openfeature.ProviderConfigChange {
		t.Fatalf("registered for %q, want ProviderConfigChange", fc.eventType)
	}
	if fc.callback == nil {
		t.Fatal("no config-change callback was registered")
	}

	// flagd's config changes, then the SDK fires the event -> invoke the callback.
	ev.set(keyDecisionThrottleSecs, 8)
	(*fc.callback)(openfeature.EventDetails{ProviderName: "flagd"})

	if got := h.DecisionThrottle(); got != 8*time.Second {
		t.Fatalf("after config-change event DecisionThrottle = %v, want 8s", got)
	}
}

// TestLifecycleHolder_ConcurrentReloadAndRead runs Reload (the event goroutine)
// concurrently with the consumer read paths under -race, guarding the holder's
// atomic value (#927 concurrency requirement: the handler writes from the SDK's
// event goroutine while the Loop/ticker/Store read from their own).
func TestLifecycleHolder_ConcurrentReloadAndRead(t *testing.T) {
	ev := newSettableEvaluator()
	ev.set(keyDecisionThrottleSecs, 1)
	h := NewLifecycleHolder(context.Background(), New(ev, nil), nil)

	var wg sync.WaitGroup
	stop := make(chan struct{})

	// Writer: simulates the event goroutine reloading on rapid config-changes.
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; ; i++ {
			select {
			case <-stop:
				return
			default:
				ev.set(keyDecisionThrottleSecs, float64(i%5)+1)
				h.Reload(context.Background())
			}
		}
	}()

	// Readers: the three consumer hot-path source funcs.
	for r := 0; r < 3; r++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
					_ = h.DecisionThrottle()
					_ = h.PlayerTTL()
					_ = h.SessionGrace()
					_ = h.EvictInterval()
				}
			}
		}()
	}

	time.Sleep(20 * time.Millisecond)
	close(stop)
	wg.Wait()
}
