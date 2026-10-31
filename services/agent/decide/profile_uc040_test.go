package decide

import (
	"encoding/json"
	"strings"
	"testing"
)

// UC: UC-040 BR-036
// profile_uc040_test.go: decision.prompt_profile values and the neutral lever text
// of the catalog (#2467 PR 5, decide-contract.md §3).

func TestNormalizePromptProfile(t *testing.T) {
	for in, want := range map[string]string{
		"": PromptProfileV1, "v1": PromptProfileV1, "V1": PromptProfileV1, "bogus": PromptProfileV1,
		"neutral": PromptProfileNeutral, " Neutral ": PromptProfileNeutral,
	} {
		t.Run("UC-040/BR-036 "+strings.TrimSpace(in)+" is "+want, func(t *testing.T) {
			if got := NormalizePromptProfile(in); got != want {
				t.Fatalf("NormalizePromptProfile(%q) = %q, want %q", in, got, want)
			}
		})
	}
}

func TestCatalog_NeutralDescriptions(t *testing.T) {
	banned := []string{"unfairly", "wasted", "matters most", "lopsided", "to build tension", "to keep a lopsided game close", "saves a player", "to balance"}
	for _, iv := range Catalog {
		t.Run("UC-040/BR-036 "+iv.Name+" describes an effect", func(t *testing.T) {
			if iv.JevDescriptionNeutral == "" {
				t.Fatal("no neutral description")
			}
			lower := strings.ToLower(iv.JevDescriptionNeutral)
			for _, b := range banned {
				if strings.Contains(lower, b) {
					t.Errorf("neutral description %q contains %q", iv.JevDescriptionNeutral, b)
				}
			}
			if iv.Description(PromptProfileNeutral) != iv.JevDescriptionNeutral || iv.Description("") != iv.JevDescription {
				t.Error("Description must pick the profile's text")
			}
		})
	}
}

func TestRequest_PromptProfileJSON(t *testing.T) {
	t.Run("UC-040/BR-036 the profile rides on the request outside State, omitted when empty", func(t *testing.T) {
		v1, err := json.Marshal(Request{})
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(v1), "prompt_profile") {
			t.Fatalf("an empty profile must be omitted: %s", v1)
		}
		n, _ := json.Marshal(Request{PromptProfile: PromptProfileNeutral})
		if !strings.Contains(string(n), `"prompt_profile":"neutral"`) {
			t.Fatalf("profile JSON = %s", n)
		}
		if !(Request{PromptProfile: " neutral"}).IsNeutral() || (Request{}).IsNeutral() {
			t.Fatal("IsNeutral must follow NormalizePromptProfile")
		}
	})
}
