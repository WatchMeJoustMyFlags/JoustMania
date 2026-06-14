// Package inference holds the agent's real LLM inference backend (#1048, per the
// #1046 design): a single OpenAI-compatible HTTP client that satisfies BOTH the
// propose seam (experiment.Backend) and the decision seam (decision.Backend),
// which are the SAME narrow shape — Infer(ctx, llm.Prompt) (string, error). Go's
// structural typing means one implementation feeds both; main.go builds ONE
// OpenAIBackend and hands its Infer to the proposer and the decision chain.
//
// THE SEAM (#1046): a backend is just "take System+User text, return raw model
// text". The caller builds a constrained prompt (buildProposalPrompt / llm.Build)
// and the defensive decoder (decodeProposal / decode.go) rejects anything
// unparseable or out-of-vocab — so a backend never needs tool-calling, streaming,
// or an agent loop, only an HTTP round-trip to an OpenAI-compatible
// /chat/completions endpoint.
//
// GRACEFUL DEGRADATION is the whole contract (#1048): on an unreachable endpoint,
// a non-2xx status, a timeout/cancel, or an unparseable body, Infer returns a
// CLEAN error. The proposer (ProposeNoBackend) and the decision path (rules
// fallback) already treat any Infer error as "degrade safely this cycle" — so this
// client must NEVER panic and never crash the loop; it only ever returns
// (text, nil) or ("", err).
//
// NO NEW DEPS, CGO-FREE: pure stdlib net/http + encoding/json, so the agent stays
// a static alpine/ARM64 binary (the #1046 constraint).
package inference

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/joustmania/agent/llm"
)

// DefaultBaseURL points at the HOST machine's Ollama OpenAI-compatible endpoint
// from inside the agent container (#1046 local no-key path). host.docker.internal
// resolves to the host gateway when the compose service declares
// extra_hosts: ["host.docker.internal:host-gateway"]. Ollama serves the
// OpenAI-compatible API under /v1, so this is the chat/completions parent.
const DefaultBaseURL = "http://host.docker.internal:11434/v1"

// DefaultModel is the local Ollama model the #1046 design names as the free local
// default — small enough for the dev host / a Pi-class box and adequate for the
// one-flag JSON replies the proposer/decision prompts ask for.
const DefaultModel = "phi4-mini"

// defaultTimeout bounds a single Infer round-trip when the caller's ctx carries no
// deadline. A local model on a modest box can take a few seconds for a short
// completion; 60s is generous enough not to truncate a legitimate slow reply while
// still guaranteeing a hung endpoint cannot wedge the (already async, off-hot-path)
// inference goroutine forever. A caller-supplied ctx deadline still wins when it is
// sooner — this only fills in a floor when none is set.
const defaultTimeout = 60 * time.Second

// OpenAIBackend is the OpenAI-compatible inference backend. It POSTs a System+User
// prompt to <BaseURL>/chat/completions and returns choices[0].message.content as
// raw text. The zero value is NOT usable — build it with New so the defaults and
// the http.Client are populated.
type OpenAIBackend struct {
	// baseURL is the OpenAI-compatible API root (e.g. ".../v1"); /chat/completions
	// is appended. Any trailing slash is trimmed at construction so the join is
	// exactly one slash.
	baseURL string
	// apiKey, when non-empty, is sent as "Authorization: Bearer <apiKey>". Empty (the
	// local Ollama path) sends NO Authorization header — Ollama ignores auth, and a
	// gateway/cloud target sets the key via config.
	apiKey string
	// model is the model name put in the request body (e.g. "phi4-mini").
	model string
	// httpClient performs the request. Injected (defaulted in New) so tests can
	// point it at a mock server / a failing transport without real I/O.
	httpClient *http.Client
}

// Option customizes an OpenAIBackend at construction (functional options keep New's
// signature stable as knobs are added — tests use WithHTTPClient).
type Option func(*OpenAIBackend)

// WithHTTPClient overrides the http.Client (tests inject a mock-server client or a
// failing transport). Production relies on New's default timeout client.
func WithHTTPClient(c *http.Client) Option {
	return func(b *OpenAIBackend) {
		if c != nil {
			b.httpClient = c
		}
	}
}

// New builds an OpenAIBackend. An empty baseURL falls back to DefaultBaseURL and an
// empty model to DefaultModel, so a partially-configured deployment still targets
// the host Ollama rather than a broken empty URL. apiKey may be empty (the local,
// no-key path). The default http.Client carries defaultTimeout as a backstop; a
// per-call ctx deadline still applies on top of it.
func New(baseURL, apiKey, model string, opts ...Option) *OpenAIBackend {
	if strings.TrimSpace(baseURL) == "" {
		baseURL = DefaultBaseURL
	}
	if strings.TrimSpace(model) == "" {
		model = DefaultModel
	}
	b := &OpenAIBackend{
		baseURL:    strings.TrimRight(baseURL, "/"),
		apiKey:     apiKey,
		model:      model,
		httpClient: &http.Client{Timeout: defaultTimeout},
	}
	for _, opt := range opts {
		opt(b)
	}
	return b
}

// chatMessage is one role/content pair in the OpenAI chat schema.
type chatMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// chatRequest is the minimal OpenAI-compatible /chat/completions request body: a
// model and the system+user messages. No streaming, no tools — the prompt already
// constrains the model to a single JSON object, and the defensive decoder validates
// it, so nothing richer is needed (#1046).
type chatRequest struct {
	Model    string        `json:"model"`
	Messages []chatMessage `json:"messages"`
	// Stream is explicitly false: we read the full body in one shot. Sent so a server
	// that defaults to streaming gives us a single JSON object instead.
	Stream bool `json:"stream"`
}

// chatResponse is the slice of the OpenAI-compatible /chat/completions response we
// consume: the first choice's message content. Extra fields in the real payload are
// ignored by encoding/json.
type chatResponse struct {
	Choices []struct {
		Message struct {
			Content string `json:"content"`
		} `json:"message"`
	} `json:"choices"`
	// Error mirrors the OpenAI error envelope some servers return WITH a 200 status;
	// surfaced as a clean error so it degrades like any transport failure.
	Error *struct {
		Message string `json:"message"`
	} `json:"error"`
}

// errEmptyCompletion is returned when the endpoint replied 2xx with a well-formed
// body that carried no usable assistant text (no choices, or empty content). The
// caller degrades exactly as it does for any other Infer error — better an honest
// error than handing the decoder an empty string to "parse".
var errEmptyCompletion = errors.New("inference: empty completion (no choices/content)")

// Infer sends prompt.System and prompt.User as the system+user messages to
// <baseURL>/chat/completions and returns choices[0].message.content as RAW text for
// the caller's defensive decoder. It is ctx-aware (cancel/deadline propagate to the
// HTTP request); when ctx has no deadline the client's defaultTimeout backstops it.
//
// EVERY failure mode returns a clean error and never panics, so the proposer
// (no-proposal) and decision (rules) fallbacks engage:
//   - request build failure (should not happen with stdlib marshaling),
//   - transport error (endpoint unreachable / DNS / connection refused / timeout),
//   - non-2xx status (the body is read, truncated, and included for diagnosis),
//   - unparseable JSON body,
//   - an OpenAI-style error envelope returned with a 2xx,
//   - a 2xx with no usable completion text.
func (b *OpenAIBackend) Infer(ctx context.Context, prompt llm.Prompt) (string, error) {
	body, err := json.Marshal(chatRequest{
		Model: b.model,
		Messages: []chatMessage{
			{Role: "system", Content: prompt.System},
			{Role: "user", Content: prompt.User},
		},
		Stream: false,
	})
	if err != nil {
		return "", fmt.Errorf("inference: marshal request: %w", err)
	}

	url := b.baseURL + "/chat/completions"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return "", fmt.Errorf("inference: build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	// Authorization ONLY when a key is configured (the local Ollama path sends none).
	if b.apiKey != "" {
		req.Header.Set("Authorization", "Bearer "+b.apiKey)
	}

	resp, err := b.httpClient.Do(req)
	if err != nil {
		// Unreachable / DNS / refused / timeout / ctx-cancel: a clean error, degrade.
		return "", fmt.Errorf("inference: request to %s failed: %w", url, err)
	}
	defer func() { _ = resp.Body.Close() }()

	// Cap the read so a misbehaving/huge response cannot exhaust memory. A legitimate
	// one-JSON-object completion is far under this.
	const maxBody = 1 << 20 // 1 MiB
	raw, err := io.ReadAll(io.LimitReader(resp.Body, maxBody))
	if err != nil {
		return "", fmt.Errorf("inference: read response body: %w", err)
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", fmt.Errorf("inference: endpoint %s returned status %d: %s",
			url, resp.StatusCode, truncate(string(raw), 256))
	}

	var parsed chatResponse
	if err := json.Unmarshal(raw, &parsed); err != nil {
		return "", fmt.Errorf("inference: unparseable response body: %w", err)
	}
	if parsed.Error != nil && parsed.Error.Message != "" {
		return "", fmt.Errorf("inference: endpoint returned error: %s", parsed.Error.Message)
	}
	if len(parsed.Choices) == 0 || parsed.Choices[0].Message.Content == "" {
		return "", errEmptyCompletion
	}
	return parsed.Choices[0].Message.Content, nil
}

// truncate bounds a string for inclusion in an error message (so a large error body
// does not bloat the log/span). It cuts on bytes, appending an ellipsis marker.
func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "...(truncated)"
}
