package llmio

import (
	"slices"
	"strings"
	"testing"

	"github.com/joustmania/agent/decide"
)

// group_uc040_test.go: claude and openai both parse through Parse, so a group
// answer is valid exactly when the request offered the group option (#2467 PR 4;
// UC-040 A47; decide-contract §2, §4.1).

func groupRequest() decide.Request {
	req := testRequest()
	req.Questions = decide.FFAQuestions(req.State, []string{"grant_shield", "grant_group_shield"})
	return req
}

const groupAnswer = `{
  "answers": [
    {"id": "ffa.intervention", "choice": "grant_group_shield", "score": 0,
     "probabilities": [{"option": "grant_group_shield", "p": 0.7}, {"option": "noop", "p": 0.3}],
     "confidence": 0.85},
    {"id": "ffa.target", "choice": "group:all", "score": 0,
     "probabilities": [{"option": "group:all", "p": 0.9}, {"option": "MOCK0001", "p": 0.1}],
     "confidence": 0.8},
    {"id": "ffa.magnitude", "choice": "", "score": 0, "probabilities": [], "confidence": 0.6}
  ],
  "rationale": "everyone may move at once"
}`

// UC: UC-040 A47 BR-033
func TestParse_GroupAnswer(t *testing.T) {
	t.Run("UC-040/A47 a group option the request offered parses", func(t *testing.T) {
		resp, err := Parse(groupRequest(), []byte(groupAnswer), "claude")
		if err != nil {
			t.Fatalf("Parse: %v", err)
		}
		if a, _ := resp.Find(decide.QuestionFFATarget); a.Choice != decide.TargetGroupAll {
			t.Fatalf("target = %q, want group:all", a.Choice)
		}
	})
	t.Run("UC-040/A48 a group option the request did not offer is invalid", func(t *testing.T) {
		if _, err := Parse(testRequest(), []byte(groupAnswer), "openai"); err == nil {
			t.Fatal("a group answer to a request without the lever must be rejected")
		}
	})
	t.Run("UC-040/A47 the user message lists the group options and their meaning", func(t *testing.T) {
		p := Render(groupRequest())
		if !strings.Contains(p.User, "group:all") || !strings.Contains(p.User, "every alive player except that one") {
			t.Fatalf("user message lacks the group options:\n%s", p.User)
		}
		if p.System != Render(testRequest()).System {
			t.Fatal("offering the group lever must not change the v1 system prompt")
		}
	})
	t.Run("UC-040/A47 the neutral Levers block describes the lever by its effect", func(t *testing.T) {
		req := groupRequest()
		req.PromptProfile = decide.PromptProfileNeutral
		if !slices.ContainsFunc(strings.Split(Render(req).System, "\n"), func(l string) bool {
			return strings.HasPrefix(l, "- grant_group_shield: ")
		}) {
			t.Fatal("neutral Levers block lacks grant_group_shield")
		}
	})
}
