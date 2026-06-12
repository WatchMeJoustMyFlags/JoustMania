package decision

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
)

// fakeBackend is the test Backend (#741): a named tier whose availability is a
// flippable atomic flag and whose probe count is recorded, so tests can assert
// both the resolved tier AND that the probe is NOT called per decision (acceptance
// #2). It never touches the network. Safe for concurrent use.
type fakeBackend struct {
	name   string
	up     atomic.Bool
	probes atomic.Int64 // how many times Available was called (probe count)
}

func newFakeBackend(name string, up bool) *fakeBackend {
	b := &fakeBackend{name: name}
	b.up.Store(up)
	return b
}

func (b *fakeBackend) Name() string { return b.name }

func (b *fakeBackend) Available(context.Context) bool {
	b.probes.Add(1)
	return b.up.Load()
}

func (b *fakeBackend) set(up bool)       { b.up.Store(up) }
func (b *fakeBackend) probeCount() int64 { return b.probes.Load() }

// chainBackends builds a fresh test chain (cloud -> gemma3:4b -> phi4-mini) with
// the given up/down states and returns the resolver plus the individual backends
// so a test can flip them. The resolver is NOT started (no goroutine); tests call
// Refresh explicitly for deterministic, single-threaded cache updates.
func chainBackends(cloud, gemma, phi bool) (*Resolver, *fakeBackend, *fakeBackend, *fakeBackend) {
	c := newFakeBackend(TierCloud, cloud)
	g := newFakeBackend(TierGemma, gemma)
	p := newFakeBackend(TierPhi, phi)
	r := NewResolver([]Backend{c, g, p}, 0) // 0 -> DefaultProbeInterval (unused; no Start)
	return r, c, g, p
}

// --- Acceptance #1: resolution follows the chain order from the configured model ---

// TestResolve_ChainOrderFromConfiguredModel: from each configured model the
// resolver starts at the right rung and returns the first reachable tier at/below
// it. A higher-but-unreachable tier is skipped to the next reachable one.
func TestResolve_ChainOrderFromConfiguredModel(t *testing.T) {
	tests := []struct {
		name         string
		model        string
		cloud        bool
		gemma        bool
		phi          bool
		wantUsed     string
		wantFallback string
	}{
		{"cloud configured, cloud up", "claude", true, true, true, TierCloud, ""},
		{"copilot maps to cloud", "copilot", true, false, false, TierCloud, ""},
		{"cloud down -> gemma", "claude", false, true, true, TierGemma, FallbackEndpointUnreachable},
		{"cloud+gemma down -> phi", "claude", false, false, true, TierPhi, FallbackEndpointUnreachable},
		{"gemma configured starts at gemma", "gemma3:4b", true, true, true, TierGemma, ""},
		{"gemma configured, gemma down -> phi", "gemma3:4b", true, false, true, TierPhi, FallbackEndpointUnreachable},
		{"gemma configured never climbs to cloud", "gemma3:4b", true, false, false, InferenceRules, FallbackNoBackend},
		{"phi configured, phi up", "phi4-mini", true, true, true, TierPhi, ""},
		{"phi configured, phi down -> rules", "phi4-mini", true, true, false, InferenceRules, FallbackNoBackend},
		{"unknown model starts at top", "mystery-model", true, false, false, TierCloud, ""},
		{"whole chain down -> rules", "claude", false, false, false, InferenceRules, FallbackNoBackend},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			r, _, _, _ := chainBackends(tc.cloud, tc.gemma, tc.phi)
			r.Refresh(context.Background())
			res := r.resolve(tc.model)
			if res.Used != tc.wantUsed {
				t.Errorf("inference.used = %q, want %q", res.Used, tc.wantUsed)
			}
			if res.FallbackReason != tc.wantFallback {
				t.Errorf("fallback_reason = %q, want %q", res.FallbackReason, tc.wantFallback)
			}
		})
	}
}

// TestResolve_EmptyCacheBottomsOutAtRules: before any Refresh the cache is empty,
// so every tier reads as unreachable and resolve honestly returns rules. This is
// the fail-safe startup state (don't claim a tier is up before probing it).
func TestResolve_EmptyCacheBottomsOutAtRules(t *testing.T) {
	r, _, _, _ := chainBackends(true, true, true) // backends "up" but never probed
	res := r.resolve("claude")
	if res.Used != InferenceRules || res.FallbackReason != FallbackNoBackend {
		t.Errorf("pre-refresh resolve = %+v, want rules/%s", res, FallbackNoBackend)
	}
}

// --- Acceptance #2: availability cached between checks, NOT probed per decision ---

// TestResolve_CachedNotProbedPerDecision: one Refresh probes each tier exactly
// once; a thousand resolves after it add ZERO probes — the hot path reads the
// cache only. This is the core "periodic + cached" guarantee.
func TestResolve_CachedNotProbedPerDecision(t *testing.T) {
	r, c, g, p := chainBackends(true, true, true)
	r.Refresh(context.Background())

	if c.probeCount() != 1 || g.probeCount() != 1 || p.probeCount() != 1 {
		t.Fatalf("after one Refresh, probe counts = %d/%d/%d, want 1/1/1",
			c.probeCount(), g.probeCount(), p.probeCount())
	}
	for i := 0; i < 1000; i++ {
		r.resolve("claude")
	}
	if c.probeCount() != 1 || g.probeCount() != 1 || p.probeCount() != 1 {
		t.Errorf("after 1000 resolves, probe counts = %d/%d/%d, want still 1/1/1 (cache read, no probe)",
			c.probeCount(), g.probeCount(), p.probeCount())
	}
	// A second Refresh probes again (periodic re-check), proving probing is the
	// Refresh's job, not the resolve's.
	r.Refresh(context.Background())
	if c.probeCount() != 2 {
		t.Errorf("cloud probe count after 2nd Refresh = %d, want 2", c.probeCount())
	}
}

// --- Acceptance #4: unplug a tier mid-session -> degrade to the next, then rules ---

// TestResolve_DegradesWhenTierUnplugged: with the cloud configured, flipping the
// reachable cloud down (then gemma, then phi) degrades the resolved tier one rung
// at a time on each Refresh — no error, every resolve returns a usable tier.
func TestResolve_DegradesWhenTierUnplugged(t *testing.T) {
	r, c, g, p := chainBackends(true, true, true)
	r.Refresh(context.Background())
	if got := r.resolve("claude"); got.Used != TierCloud {
		t.Fatalf("initial used = %q, want %q", got.Used, TierCloud)
	}

	// Unplug cloud: next Refresh degrades to gemma with endpoint_unreachable.
	c.set(false)
	r.Refresh(context.Background())
	if got := r.resolve("claude"); got.Used != TierGemma || got.FallbackReason != FallbackEndpointUnreachable {
		t.Errorf("after cloud unplug, resolve = %+v, want gemma/%s", got, FallbackEndpointUnreachable)
	}

	// Unplug gemma too: degrade to phi.
	g.set(false)
	r.Refresh(context.Background())
	if got := r.resolve("claude"); got.Used != TierPhi {
		t.Errorf("after gemma unplug, used = %q, want %q", got.Used, TierPhi)
	}

	// Unplug phi: the whole chain is down -> rules, no_backend_available, no error.
	p.set(false)
	r.Refresh(context.Background())
	if got := r.resolve("claude"); got.Used != InferenceRules || got.FallbackReason != FallbackNoBackend {
		t.Errorf("after full unplug, resolve = %+v, want rules/%s", got, FallbackNoBackend)
	}
}

// --- Acceptance #5: recovery — a recovered higher tier is climbed back to ---

// TestResolve_RecoversToHigherTier: with cloud down the resolver serves gemma;
// when cloud comes back and the cache refreshes, resolve climbs back to cloud.
func TestResolve_RecoversToHigherTier(t *testing.T) {
	r, c, _, _ := chainBackends(false, true, true)
	r.Refresh(context.Background())
	if got := r.resolve("claude"); got.Used != TierGemma {
		t.Fatalf("with cloud down, used = %q, want %q", got.Used, TierGemma)
	}

	// Cloud recovers. BEFORE the next Refresh the cache still says gemma (the
	// recovery is not seen until we re-probe) — proving the cache governs.
	c.set(true)
	if got := r.resolve("claude"); got.Used != TierGemma {
		t.Errorf("before refresh, used = %q, want still %q (cache not yet refreshed)", got.Used, TierGemma)
	}
	// After the periodic re-probe, the higher tier is picked up again.
	r.Refresh(context.Background())
	if got := r.resolve("claude"); got.Used != TierCloud || got.FallbackReason != "" {
		t.Errorf("after recovery refresh, resolve = %+v, want cloud/no-fallback", got)
	}
}

// TestResolve_ConcurrentResolveAndRefresh: resolve() runs from many goroutines
// while Refresh flips availability — the race detector must stay quiet and every
// resolve returns a valid chain tier or rules (never a torn read).
func TestResolve_ConcurrentResolveAndRefresh(t *testing.T) {
	r, c, _, _ := chainBackends(true, true, true)
	r.Refresh(context.Background())

	valid := map[string]bool{TierCloud: true, TierGemma: true, TierPhi: true, InferenceRules: true}
	stop := make(chan struct{})

	// Refresher: flip cloud up/down and re-probe until the resolvers signal stop.
	var refresher sync.WaitGroup
	refresher.Add(1)
	go func() {
		defer refresher.Done()
		for i := 0; ; i++ {
			select {
			case <-stop:
				return
			default:
				c.set(i%2 == 0)
				r.Refresh(context.Background())
			}
		}
	}()

	// Resolvers: hammer resolve concurrently for a fixed number of iterations.
	var resolvers sync.WaitGroup
	for g := 0; g < 8; g++ {
		resolvers.Add(1)
		go func() {
			defer resolvers.Done()
			for i := 0; i < 2000; i++ {
				if used := r.resolve("claude").Used; !valid[used] {
					t.Errorf("resolve returned invalid tier %q", used)
					return
				}
			}
		}()
	}
	resolvers.Wait() // resolvers done -> stop the refresher
	close(stop)
	refresher.Wait()
}

// TestDefaultChain_TierNamesAndOrder: the production chain is cloud -> gemma3:4b
// -> phi4-mini, in that order, with the documented tier names. Empty endpoints
// make a tier permanently unreachable without dialing.
func TestDefaultChain_TierNamesAndOrder(t *testing.T) {
	chain := DefaultChain(Endpoints{}) // all empty -> all unreachable
	wantNames := []string{TierCloud, TierGemma, TierPhi}
	if len(chain) != len(wantNames) {
		t.Fatalf("chain length = %d, want %d", len(chain), len(wantNames))
	}
	for i, b := range chain {
		if b.Name() != wantNames[i] {
			t.Errorf("chain[%d] = %q, want %q", i, b.Name(), wantNames[i])
		}
		if b.Available(context.Background()) {
			t.Errorf("chain[%d] (%q) with empty endpoint must be unreachable", i, b.Name())
		}
	}
}
