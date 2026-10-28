package main

import (
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/decide/backends/claude"
	"github.com/joustmania/agent/decide/backends/jev"
	"github.com/joustmania/agent/decide/backends/openai"
	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/internal/anthropichttp"
	"github.com/joustmania/agent/keysafe"
)

// decide_backends.go wires the remote decision backends (#1244, epic #1225) into
// the DecideRegistry. There is no gateway: each backend calls its vendor directly
// with a key from the agent's own environment.
//
// The factories run on every eligible decision cycle with that cycle's flag
// snapshot, so they must be cheap and do no I/O:
//   - API keys are read ONCE at startup (decideBackendEnvFromOS) and closed over.
//     They are env secrets, never flags, and never recorded on a span.
//     UC: UC-044 BR-001 (a key set or changed later needs a restart), C-016.
//   - openai and jev share one *http.Client with claude, so TLS connections are
//     pooled across the per-cycle backend values. Decide's ctx deadline (the
//     decision.<backend>.timeout_ms flag) is the real budget; the client timeout
//     is only a backstop.
//   - A backend without its key returns nil (backend_unavailable -> rules)
//     without building anything.
//   - An openai, jev or claude base URL that is not key-safe (keysafe.BaseURL: https, or
//     plain http only to a loopback host) returns nil the same way, so nothing is
//     sent and the key never travels in clear text. It is a url.Parse, no I/O.
//     UC: UC-044 BR-003, C-016 (#1814).
//   - The shared client follows no redirect (the 3xx is returned as the
//     response, which the backends turn into an error -> rules fallback), so an
//     endpoint cannot forward the key to another host or downgrade it to http.
//   - claude.New builds an Anthropic SDK client (~16 allocs / 3.5 KB per call,
//     vs 3 allocs for openai.New / jev.New), so claude backends are cached per
//     claude.Config (model, timeout, capture, base URL; the key and client are
//     fixed). The base URL is the decision.claude.base_url flag (#1900). A flag flip is a new Config, hence a new backend on the next cycle;
//     the cache is bounded so flag churn cannot grow it. openai/jev are cheap
//     enough to build per cycle (openai carries its json_schema downgrade across
//     instances in a package-level map).

// decideBackendEnv holds the remote backends' API keys, read once at startup.
type decideBackendEnv struct {
	AnthropicAPIKey string // ANTHROPIC_API_KEY (claude)
	OpenAIAPIKey    string // DECISION_OPENAI_API_KEY (openai)
	JevAPIKey       string // JEV_API_KEY (jev)
}

// decideBackendEnvFromOS reads the backend keys from the process environment.
func decideBackendEnvFromOS() decideBackendEnv {
	return decideBackendEnv{
		AnthropicAPIKey: strings.TrimSpace(os.Getenv("ANTHROPIC_API_KEY")),
		OpenAIAPIKey:    strings.TrimSpace(os.Getenv("DECISION_OPENAI_API_KEY")),
		JevAPIKey:       strings.TrimSpace(os.Getenv("JEV_API_KEY")),
	}
}

// decideHTTPTimeout is the shared client's backstop timeout (see above).
const decideHTTPTimeout = 30 * time.Second

// newDecideHTTPClient is the one HTTP client every remote backend shares.
// It never follows a redirect: the key-bearing request goes only to the
// key-safe base URL (UC-044 BR-003).
func newDecideHTTPClient() *http.Client {
	return anthropichttp.NewHTTPClient(decideHTTPTimeout)
}

// newDecideRegistry builds the decision-backend registry main.go hands to every
// per-game loop: claude, openai and jev, each falling back to rules
// (backend_unavailable) when its key, model or base URL is missing.
func newDecideRegistry(env decideBackendEnv, client *http.Client) *decision.DecideRegistry {
	reg := decision.NewDecideRegistry()
	claudeBackends := newClaudeCache(claudeCacheSize)

	reg.Register(flags.DecisionBackendClaude, func(d flags.DecisionBackend) decide.Backend {
		if env.AnthropicAPIKey == "" {
			return nil
		}
		// Key-safety BEFORE the cache get: a rejected URL must never build or
		// cache a backend (UC-044 BR-003, C-016).
		if u := strings.TrimSpace(d.Claude.BaseURL); u != "" {
			if _, ok := keysafe.BaseURL(u); !ok {
				return nil
			}
		}
		return claudeBackends.get(claude.Config{
			APIKey:         env.AnthropicAPIKey,
			BaseURL:        d.Claude.BaseURL,
			Model:          d.Claude.Model,
			Timeout:        d.Claude.Timeout,
			HTTPClient:     client,
			CaptureContent: d.CaptureContent,
		})
	})
	reg.Register(flags.DecisionBackendOpenAI, func(d flags.DecisionBackend) decide.Backend {
		if env.OpenAIAPIKey == "" {
			return nil
		}
		if _, ok := keysafe.BaseURL(d.OpenAI.BaseURL); !ok {
			return nil
		}
		return openai.New(openai.Config{
			APIKey:          env.OpenAIAPIKey,
			BaseURL:         d.OpenAI.BaseURL,
			Model:           d.OpenAI.Model,
			Timeout:         d.OpenAI.Timeout,
			HTTPClient:      client,
			CaptureContent:  d.CaptureContent,
			ReasoningEffort: d.OpenAI.ReasoningEffort,
		})
	})
	// Jev needs only JEV_API_KEY: an empty base URL or model falls back to the jev
	// package defaults (UC-044 BR-002); a set base URL must be key-safe (BR-003).
	reg.Register(flags.DecisionBackendJev, func(d flags.DecisionBackend) decide.Backend {
		if env.JevAPIKey == "" {
			return nil
		}
		if u := strings.TrimSpace(d.Jev.BaseURL); u != "" {
			if _, ok := keysafe.BaseURL(u); !ok {
				return nil
			}
		}
		return jev.New(jev.Config{
			APIKey:         env.JevAPIKey,
			BaseURL:        d.Jev.BaseURL,
			Model:          d.Jev.Model,
			Timeout:        d.Jev.Timeout,
			HTTPClient:     client,
			CaptureContent: d.CaptureContent,
		})
	})
	return reg
}

// claudeCacheSize bounds the claude backend cache. In steady state there is one
// entry (one model/timeout/capture combination); a handful covers flipping
// between variants during a demo.
const claudeCacheSize = 8

// claudeCache memoizes claude backends per Config. A Backend is immutable after
// New and safe for concurrent use, so one value serves every game loop. When the
// cache is full a new Config resets it: flag flips are rare and the next
// cycle simply rebuilds.
type claudeCache struct {
	mu       sync.Mutex
	max      int
	backends map[claude.Config]*claude.Backend
}

func newClaudeCache(size int) *claudeCache {
	return &claudeCache{max: size, backends: make(map[claude.Config]*claude.Backend, size)}
}

func (c *claudeCache) get(cfg claude.Config) *claude.Backend {
	c.mu.Lock()
	defer c.mu.Unlock()
	if b, ok := c.backends[cfg]; ok {
		return b
	}
	if len(c.backends) >= c.max {
		clear(c.backends)
	}
	b := claude.New(cfg)
	c.backends[cfg] = b
	return b
}

func (c *claudeCache) size() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.backends)
}
