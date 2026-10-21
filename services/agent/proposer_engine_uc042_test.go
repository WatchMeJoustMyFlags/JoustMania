package main

import (
	"context"
	"testing"

	"go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/llm"
)

// code_improvement.engine is informational: it lands on the propose_attempt span
// as given, while whether the model is called follows AGENT_INFERENCE_BACKEND.
// UC: UC-042 BR-003
func TestProposer_EngineIsInformationalBackendFollowsEnv(t *testing.T) {
	cases := []struct {
		name      string
		envBack   string
		engine    string
		wantCalls int
	}{
		{"claude env, engine says stub", "claude", "stub", 1},
		{"stub env, engine says claude", "", "claude", 0},
	}
	for _, tc := range cases {
		t.Run("UC-042/BR-003 "+tc.name, func(t *testing.T) {
			t.Setenv("AGENT_INFERENCE_BACKEND", tc.envBack)
			t.Setenv("ANTHROPIC_API_KEY", "sk-test")
			selected, _ := selectInferenceBackend(quietLogger(), nil)

			calls := 0
			var delegate inferFunc
			if selected != nil { // the env chose a real backend: stand in for its client
				delegate = func(context.Context, llm.Prompt) (string, error) { calls++; return "no proposal", nil }
			}
			rec := tracetest.NewSpanRecorder()
			tp := trace.NewTracerProvider(trace.WithSpanProcessor(rec))
			p := experiment.NewProposer(proposeBackendFor(delegate), nil,
				func() map[string]struct{} { return map[string]struct{}{"sensitivity": {}} },
				tp.Tracer("test"), quietLogger())

			p.ProposeOnce(context.Background(), experiment.Narrative{}, experiment.ProposerConfig{
				Engine: tc.engine,
				Permit: func(context.Context) (bool, string) { return true, "" },
			})

			if calls != tc.wantCalls {
				t.Errorf("model calls = %d, want %d (backend follows AGENT_INFERENCE_BACKEND)", calls, tc.wantCalls)
			}
			var found bool
			for _, s := range rec.Ended() {
				if s.Name() != experiment.SpanProposeAttempt {
					continue
				}
				found = true
				for _, a := range s.Attributes() {
					if string(a.Key) == experiment.AttrEngine && a.Value.AsString() != tc.engine {
						t.Errorf("span engine = %q, want %q", a.Value.AsString(), tc.engine)
					}
				}
			}
			if !found {
				t.Fatal("no propose_attempt span recorded")
			}
		})
	}
}
