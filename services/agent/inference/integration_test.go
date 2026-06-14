package inference

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/llm"
)

// integration_test.go proves the #1046 "one implementation feeds both seams"
// claim end-to-end with the REAL openaiBackend pointed at a mock OpenAI-compatible
// server (no live model): the SAME backend output drives (1) the experiment
// Proposer to a written shadow Proposal, and (2) the decision-shaped consumer to
// usable raw text. It uses only the public experiment API + the experiment package's
// testdata fixture, so it exercises the actual decode/Gate pipeline.

// proposalCompletion is a mock /chat/completions reply whose assistant content is a
// valid one-flag proposal over the experiment testdata vocabulary.
const proposalCompletion = `{
  "choices": [
    {"message": {"role": "assistant",
      "content": "{\"flag\":\"death_grace_period_seconds\",\"value\":0.75,\"rationale\":\"longer grace may extend sessions\"}"}}
  ]
}`

// tempGameFile copies the experiment testdata game.json into a temp file so the
// Writer/Gate can mutate it without touching the fixture.
func tempGameFile(t *testing.T) string {
	t.Helper()
	src, err := os.ReadFile(filepath.Join("..", "experiment", "testdata", "game.json"))
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	path := filepath.Join(t.TempDir(), "game.json")
	if err := os.WriteFile(path, src, 0o644); err != nil {
		t.Fatalf("write temp fixture: %v", err)
	}
	return path
}

func gameVocab(t *testing.T, path string) func() map[string]struct{} {
	t.Helper()
	return func() map[string]struct{} {
		raw, err := os.ReadFile(path)
		if err != nil {
			return nil
		}
		var doc struct {
			Flags map[string]any `json:"flags"`
		}
		if err := json.Unmarshal(raw, &doc); err != nil {
			return nil
		}
		set := make(map[string]struct{}, len(doc.Flags))
		for k := range doc.Flags {
			set[k] = struct{}{}
		}
		return set
	}
}

// TestOpenAIBackend_FeedsProposerSeam runs the real openaiBackend (against a mock
// server) through the experiment Proposer and asserts a shadow experiment is
// proposed + written — the propose-side seam.
func TestOpenAIBackend_FeedsProposerSeam(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, proposalCompletion)
	}))
	defer srv.Close()

	backend := New(srv.URL+"/v1", "", "phi4-mini", WithHTTPClient(srv.Client()))

	path := tempGameFile(t)
	writer := experiment.NewWriter(path, nil)
	gate := experiment.NewGate(writer, nil, nil)
	proposer := experiment.NewProposer(backend, gate, gameVocab(t, path), nil, nil)

	res := proposer.ProposeOnce(context.Background(), experiment.Narrative{GameMode: "joust"}, experiment.ProposerConfig{Engine: "openai"})
	if res.Outcome != experiment.ProposeProposed {
		t.Fatalf("outcome = %q, want proposed (err=%v)", res.Outcome, res.Err)
	}
	if res.Proposal == nil || res.Proposal.FlagKey != "death_grace_period_seconds" {
		t.Fatalf("proposal = %+v, want death_grace_period_seconds", res.Proposal)
	}
}

// TestOpenAIBackend_FeedsDecisionSeam proves the SAME backend type satisfies the
// decision seam shape and returns usable raw text — what llm_decide parses.
// decisionConsumer mirrors decision.Backend's Infer signature; assigning the
// backend to it is a compile-time proof of structural satisfaction.
func TestOpenAIBackend_FeedsDecisionSeam(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, proposalCompletion)
	}))
	defer srv.Close()

	backend := New(srv.URL+"/v1", "", "phi4-mini", WithHTTPClient(srv.Client()))

	// The decision seam is exactly Infer(ctx, llm.Prompt) (string, error). A var of
	// that func type assigned from backend.Infer is a structural-satisfaction proof.
	var consumer func(ctx context.Context, prompt llm.Prompt) (string, error) = backend.Infer
	raw, err := consumer(context.Background(), llm.Prompt{System: "sys", User: "usr"})
	if err != nil {
		t.Fatalf("decision-seam Infer error: %v", err)
	}
	if raw == "" {
		t.Fatal("decision-seam Infer returned empty text; want usable model content")
	}
}
