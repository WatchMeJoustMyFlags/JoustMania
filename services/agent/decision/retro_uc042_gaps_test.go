package decision

import (
	"context"
	"errors"
	"fmt"
	"testing"

	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/sdk/metric"

	"github.com/joustmania/agent/llm"
)

// UC: UC-042 A1
func TestRetro_NotEligibleVariants(t *testing.T) {
	run := func(t *testing.T, kinds []string, gameKind string, enabled bool) (map[string]string, map[string]int64, int, int) {
		be := &countingText{}
		rc, sr, budget, now := budgetedRetro(t, 2, be)
		snap := retroFlagSnapshot()
		snap.Enabled = enabled
		snap.LLMGate.EligibleGameKinds = kinds
		snap.LLMGate.MaxRequestsPerMinute = 2
		rc.Flags = fakeFlags{snap: snap}
		reader := metric.NewManualReader()
		rc.llmGated = newLLMGatedCounter(metric.NewMeterProvider(metric.WithReader(reader)))
		c := endedSession()
		c.GameKind = gameKind
		rc.OnGameEnd(c)
		rc.AwaitInflight()
		spans := spansByName(sr.Ended(), SpanLLMRetro)
		if len(spans) != 1 {
			t.Fatalf("agent.llm.retro spans = %d, want 1 (capture-only)", len(spans))
		}
		attrs := map[string]string{}
		for _, k := range []string{AttrInferenceUsed, AttrInferenceFallback} {
			if v, ok := attrValue(spans[0], k); ok {
				attrs[k] = v.AsString()
			}
		}
		return attrs, gatedByReason(t, reader), be.count(), slotsLeft(budget, now, 2)
	}

	t.Run("UC-042/A1 llm.eligible_game_kinds none: even a real game is capture-only", func(t *testing.T) {
		attrs, gated, calls, left := run(t, []string{}, "real", true)
		if attrs[AttrInferenceUsed] != DefaultInference || attrs[AttrInferenceFallback] != FallbackNotEligible {
			t.Fatalf("span attrs = %v, want used=none fallback=llm_not_eligible", attrs)
		}
		if calls != 0 || left != 2 || gated[FallbackNotEligible] != 1 {
			t.Fatalf("calls=%d free slots=%d gated=%v, want 0 calls, 2 free, llm_not_eligible=1", calls, left, gated)
		}
	})
	t.Run("UC-042/A1 an empty game kind is never eligible", func(t *testing.T) {
		attrs, gated, calls, left := run(t, []string{"real", "shadow"}, "", true)
		if attrs[AttrInferenceFallback] != FallbackNotEligible || calls != 0 || left != 2 || gated[FallbackNotEligible] != 1 {
			t.Fatalf("attrs=%v calls=%d free=%d gated=%v, want an ineligible capture-only retro", attrs, calls, left, gated)
		}
	})
	t.Run("UC-042/A1 the kill switch is checked first: agent_disabled and no llm_not_eligible count", func(t *testing.T) {
		attrs, gated, calls, left := run(t, []string{"real"}, "shadow", false)
		if attrs[AttrInferenceFallback] != FallbackAgentDisabled {
			t.Fatalf("fallback = %q, want %q (A8 wins over A1)", attrs[AttrInferenceFallback], FallbackAgentDisabled)
		}
		if gated[FallbackNotEligible] != 0 || calls != 0 || left != 2 {
			t.Fatalf("gated=%v calls=%d free=%d, want no llm_not_eligible count, no call, no charge", gated, calls, left)
		}
	})
}

// UC: UC-042 A2
func TestRetro_StubIsCaptureOnlyWhateverEnabledAndKindSay(t *testing.T) {
	for _, tc := range []struct {
		name    string
		kind    string
		enabled bool
	}{
		{"shadow game", "shadow", true},
		{"agent disabled", "real", false},
	} {
		t.Run("UC-042/A2 stub backend, "+tc.name+": no_backend_available and no budget slot", func(t *testing.T) {
			rc, sr, budget, now := budgetedRetro(t, 2, nil)
			snap := retroFlagSnapshot()
			snap.Enabled = tc.enabled
			snap.LLMGate.MaxRequestsPerMinute = 2
			rc.Flags = fakeFlags{snap: snap}
			c := endedSession()
			c.GameKind = tc.kind
			rc.OnGameEnd(c)
			spans := spansByName(sr.Ended(), SpanLLMRetro)
			if len(spans) != 1 {
				t.Fatalf("spans = %d, want 1", len(spans))
			}
			if v, ok := attrValue(spans[0], AttrInferenceFallback); !ok || v.AsString() != FallbackNoBackend {
				t.Fatalf("fallback = %q (present=%v), want %q", v.AsString(), ok, FallbackNoBackend)
			}
			if left := slotsLeft(budget, now, 2); left != 2 {
				t.Fatalf("free slots = %d, want 2", left)
			}
		})
	}
}

// UC: UC-042 A9
func TestRetro_FailedCallKeepsSlotAndForwardsNothing(t *testing.T) {
	for _, tc := range []struct {
		name    string
		err     error
		wantErr string
	}{
		{"transport error", errors.New("boom"), "_OTHER"},
		{"timeout", fmt.Errorf("infer: %w", context.DeadlineExceeded), ""},
	} {
		t.Run("UC-042/A9 "+tc.name+": error span, slot stays used, no suggestions", func(t *testing.T) {
			resetRetroResponseRef()
			be := fakeText("claude-test", func(context.Context, llm.Prompt) (string, error) { return "", tc.err })
			rc, sr, budget, now := budgetedRetro(t, 2, be)
			forwarded := 0
			rc.SetSuggestionSink(func(llm.RetroSuggestion) { forwarded++ })

			rc.OnGameEnd(endedSession())
			rc.AwaitInflight()

			spans := spansByName(sr.Ended(), SpanLLMRetro)
			if len(spans) != 1 {
				t.Fatalf("spans = %d, want 1 (still recorded)", len(spans))
			}
			span := spans[0]
			if span.Status().Code != codes.Error {
				t.Fatalf("status = %v, want Error", span.Status().Code)
			}
			if v, ok := attrValue(span, AttrLLMRetroParseOK); !ok || v.AsBool() {
				t.Fatalf("parse_ok = %v (present=%v), want false", v.AsBool(), ok)
			}
			v, ok := attrValue(span, AttrLLMInferError)
			if !ok || v.AsString() == "" || (tc.wantErr != "" && v.AsString() != tc.wantErr) {
				t.Fatalf("llm.infer.error = %q (present=%v), want an error type %q", v.AsString(), ok, tc.wantErr)
			}
			if left := slotsLeft(budget, now, 2); left != 1 {
				t.Fatalf("free slots = %d, want 1 (the failed call keeps its slot)", left)
			}
			if forwarded != 0 {
				t.Fatalf("suggestions forwarded = %d, want 0 (no A3)", forwarded)
			}
		})
	}
}

// UC: UC-042 Post-S-1
func TestRetro_SuccessfulCallCarriesTheAssessment(t *testing.T) {
	t.Run("UC-042/Post-S-1 a successful model call puts its assessment on the retro span", func(t *testing.T) {
		resetRetroResponseRef()
		reply := `{"session_assessment":"close finish","suggestions":[],"session_focus":"endurance"}`
		rc, sr := recordingRetro(t, retroFlagSnapshot())
		rc.SetBackend(fakeText("claude-test", func(context.Context, llm.Prompt) (string, error) { return reply, nil }))
		rc.OnGameEnd(endedSession())
		rc.AwaitInflight()
		spans := spansByName(sr.Ended(), SpanLLMRetro)
		if len(spans) != 1 {
			t.Fatalf("spans = %d, want 1", len(spans))
		}
		if v, ok := attrValue(spans[0], AttrLLMRetroSessionAssessment); !ok || v.AsString() != "close finish" {
			t.Fatalf("session_assessment = %q (present=%v), want the model's assessment", v.AsString(), ok)
		}
		if v, ok := attrValue(spans[0], AttrLLMRetroParseOK); !ok || !v.AsBool() {
			t.Fatalf("parse_ok = %v, want true", v.AsBool())
		}
	})
}
