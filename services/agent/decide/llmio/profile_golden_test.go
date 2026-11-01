package llmio

import (
	"flag"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/internal/anthropichttp"
)

// UC: UC-040 BR-036
// profile_golden_test.go pins the prompt profiles of decision.prompt_profile
// (#2467 PR 5, decide-contract.md §3). Under v1 (the flag's default, an empty,
// unknown or unreadable value) Render's system prompt and user message are
// byte-identical to testdata/profile_v1_*.golden, written from main before #2467
// PR 5: a diff there is a change of the talk's prompt, never regenerate it to make a
// change pass. The neutral system prompt is pinned too, so a wording change is a
// reviewed diff.
//
//	go test ./decide/llmio -run TestPromptProfileGolden -update-profile-golden

var updateProfileGolden = flag.Bool("update-profile-golden", false, "rewrite decide/llmio/testdata/profile_*.golden")

// v1SystemSHA is the fingerprint (llm.prompt.system_sha256) of the v1 system prompt
// on main before #2467 PR 5.
const v1SystemSHA = "5946fe381206612c"

// bannedNeutralPhrases are purpose and verdict phrases the neutral lever text must
// not carry (UC-040 BR-036).
var bannedNeutralPhrases = []string{
	"unfairly", "wasted", "matters most", "lopsided", "to build tension",
	"to keep a lopsided game close", "often the right answer",
}

func checkProfileGolden(t *testing.T, name, got string) {
	t.Helper()
	path := filepath.Join("testdata", name)
	if *updateProfileGolden {
		if err := os.WriteFile(path, []byte(got), 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read golden: %v", err)
	}
	if got != string(want) {
		t.Fatalf("%s changed\n--- got ---\n%s\n--- want ---\n%s", name, got, want)
	}
}

func TestPromptProfileGolden(t *testing.T) {
	for _, profile := range []string{"", "v1", "V1", "bogus", " "} {
		t.Run("UC-040/BR-036 v1 is byte-identical for profile "+strconvQuote(profile), func(t *testing.T) {
			req := testRequest()
			req.PromptProfile = profile
			p := Render(req)
			checkProfileGolden(t, "profile_v1_system.golden", p.System)
			checkProfileGolden(t, "profile_v1_user.golden", p.User)
		})
	}
	t.Run("UC-040/BR-036 neutral system prompt is pinned and the user message is v1's", func(t *testing.T) {
		req := testRequest()
		req.PromptProfile = decide.PromptProfileNeutral
		p := Render(req)
		checkProfileGolden(t, "profile_neutral_system.golden", p.System)
		if p.User != Render(testRequest()).User {
			t.Fatal("the neutral profile must not change the user message")
		}
	})
}

func strconvQuote(s string) string { return `"` + s + `"` }

func TestPromptProfile_NeutralLeverLines(t *testing.T) {
	all := decide.Names(nil)
	st := testRequest().State
	req := decide.Request{State: st, Questions: decide.FFAQuestions(st, all), PromptProfile: decide.PromptProfileNeutral}
	neutral := Render(req).System
	v1 := Render(decide.Request{State: st, Questions: req.Questions}).System

	t.Run("UC-040/BR-036 every offered lever has its effect line", func(t *testing.T) {
		var offered []string
		for _, q := range req.Questions {
			if q.ID == decide.QuestionFFAIntervention {
				offered = q.Options
			}
		}
		if len(offered) != len(all)+1 {
			t.Fatalf("offered = %v, want every catalog lever and noop", offered)
		}
		for _, o := range offered {
			want := "- " + o + ": " + decide.NoopDescriptionNeutral
			if iv, ok := decide.ByName(o); ok {
				want = "- " + o + ": " + iv.JevDescriptionNeutral
			}
			if !strings.Contains(neutral, want+"\n") {
				t.Errorf("neutral prompt has no line %q", want)
			}
		}
		if !strings.Contains(neutral, "\nLevers:\n") {
			t.Error("neutral prompt has no Levers block")
		}
	})
	t.Run("UC-040/BR-036 neutral has none of the purpose or verdict phrases", func(t *testing.T) {
		lower := strings.ToLower(neutral)
		for _, banned := range bannedNeutralPhrases {
			if strings.Contains(lower, banned) {
				t.Errorf("neutral prompt contains %q", banned)
			}
		}
		if !strings.Contains(neutral, "only intervene when the game state clearly calls for it") {
			t.Error("neutral must keep the restraint clause: it changes the lever wording, not the willingness to act")
		}
	})
	t.Run("UC-040/BR-036 noop stays always valid, often right only in v1", func(t *testing.T) {
		if !strings.Contains(neutral, `"noop" (do nothing) is always a valid answer; only intervene when`) {
			t.Error("neutral prompt lost the noop-is-valid sentence")
		}
		if !strings.Contains(v1, "often the right answer") || strings.Contains(v1, "Levers:") {
			t.Error("v1 must keep its noop sentence and have no Levers block")
		}
	})
	t.Run("UC-040/BR-036 the system prompt is static per profile", func(t *testing.T) {
		few := Render(decide.Request{State: st, Questions: decide.FFAQuestions(st, []string{"grant_shield"}), PromptProfile: "neutral"}).System
		if few != neutral {
			t.Error("the neutral system prompt must not depend on the offered levers")
		}
	})
}

func TestPromptProfile_Fingerprint(t *testing.T) {
	t.Run("UC-040/BR-036 v1 fingerprint unchanged, neutral differs", func(t *testing.T) {
		v1 := anthropichttp.SystemSHA(SystemPrompt(decide.PromptProfileV1))
		if v1 != v1SystemSHA {
			t.Fatalf("v1 system fingerprint = %s, want %s (the v1 prompt changed)", v1, v1SystemSHA)
		}
		if n := anthropichttp.SystemSHA(SystemPrompt(decide.PromptProfileNeutral)); n == v1 {
			t.Fatal("the neutral system prompt must have its own fingerprint")
		}
	})
}
