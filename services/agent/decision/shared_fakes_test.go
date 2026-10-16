package decision

import (
	"sync"

	"github.com/joustmania/agent/gamecontext"
)

// shared_fakes_test.go holds the test fakes shared by the remote decide-path and
// effect-sampler tests (moved here when #1234 deleted the legacy async-inference
// tests that used to define them).

func boolPtr(b bool) *bool { return &b }

// fakeContextProvider is the re-validation source. It returns whatever GameContext
// the test has installed for a game_id AT LANDING TIME, so a test can fire a remote
// call against one context and let the answer land against a DIFFERENT one (game
// ended / target eliminated / allow-list tightened).
type fakeContextProvider struct {
	mu      sync.Mutex
	current map[string]gamecontext.GameContext
	missing map[string]bool // game_id -> partition evicted (Current returns ok=false)
}

func newFakeContextProvider() *fakeContextProvider {
	return &fakeContextProvider{
		current: make(map[string]gamecontext.GameContext),
		missing: make(map[string]bool),
	}
}

func (p *fakeContextProvider) set(gameID string, c gamecontext.GameContext) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.current[gameID] = c
	delete(p.missing, gameID)
}

func (p *fakeContextProvider) evict(gameID string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.missing[gameID] = true
}

func (p *fakeContextProvider) Current(gameID string) (gamecontext.GameContext, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.missing[gameID] {
		return gamecontext.GameContext{}, false
	}
	c, ok := p.current[gameID]
	return c, ok
}
