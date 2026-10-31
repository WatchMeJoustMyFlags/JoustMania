package jev

import (
	"encoding/json"
	"flag"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/joustmania/agent/decide"
)

// UC: UC-040 BR-036
// criteria_profile_uc040_test.go: jev's criteria under decision.prompt_profile
// (#2467 PR 5, decide-contract.md §3). v1 is pinned by the existing request
// fixtures (contract_test.go); neutral by testdata/neutral_*.json.
//
//	go test ./decide/backends/jev -run TestNeutralProfile_Fixtures -update-neutral-fixtures

var updateNeutralFixtures = flag.Bool("update-neutral-fixtures", false, "rewrite testdata/neutral_*.json")

var bannedVerdicts = []string{
	"unfairly", "wasted", "matters most", "lopsided", "to build tension", "to keep a lopsided game close",
	"would help", "would even the game", "keeps the game close", "pile on", "little to balance",
	"often right", "right when",
}

func neutralRequest() decide.Request {
	req := testRequest()
	req.PromptProfile = decide.PromptProfileNeutral
	return req
}

func checkNoVerdict(t *testing.T, where, text string) {
	t.Helper()
	lower := strings.ToLower(text)
	for _, b := range bannedVerdicts {
		if strings.Contains(lower, b) {
			t.Errorf("%s contains %q: %q", where, b, text)
		}
	}
}

func TestNeutralProfile_Fixtures(t *testing.T) {
	for _, tc := range []struct {
		name, fixture, chosen string
		req                   func() decide.Request
	}{
		{"stage 1", "neutral_request_intervention.json", "", func() decide.Request { return splitStages(neutralRequest()).first }},
		{"shield details", "neutral_shield_request_details.json", "grant_shield", func() decide.Request {
			return detailsRequest(neutralRequest(), "grant_shield").ask
		}},
	} {
		t.Run("UC-040/BR-036 neutral "+tc.name+" request is pinned", func(t *testing.T) {
			sub := tc.req()
			if sub.PromptProfile != decide.PromptProfileNeutral {
				t.Fatalf("the %s request lost the profile: %q", tc.name, sub.PromptProfile)
			}
			out, err := encodeRequest(fixtureModel, sub, tc.chosen)
			if err != nil {
				t.Fatal(err)
			}
			if *updateNeutralFixtures {
				var v any
				if err := json.Unmarshal(out.Body, &v); err != nil {
					t.Fatal(err)
				}
				pretty, _ := json.MarshalIndent(v, "", "  ")
				if err := os.WriteFile(filepath.Join("testdata", tc.fixture), append(pretty, '\n'), 0o644); err != nil {
					t.Fatal(err)
				}
				return
			}
			jsonEqual(t, out.Body, tc.fixture)
			// The lever, target and magnitude questions carry no verdict; the game
			// description and the gate criteria are the objective, unchanged in
			// both profiles until the goal lines (#2479 PR E).
			var wire jevRequest
			if err := json.Unmarshal(out.Body, &wire); err != nil {
				t.Fatal(err)
			}
			for id, q := range wire.Questions {
				if id == GateQuestionID {
					continue
				}
				raw, _ := json.Marshal(q)
				checkNoVerdict(t, tc.fixture+" "+id, string(raw))
			}
			v1Sub := tc.req()
			v1Sub.PromptProfile = ""
			v1Out, _ := encodeRequest(fixtureModel, v1Sub, tc.chosen)
			if string(v1Out.Body) == string(out.Body) {
				t.Error("the neutral request must differ from v1")
			}
		})
	}
}

func TestNeutralProfile_Descriptions(t *testing.T) {
	t.Run("UC-040/BR-036 every lever and noop has a neutral description without a verdict", func(t *testing.T) {
		for o, d := range interventionDescriptionsNeutral {
			if d == "" {
				t.Errorf("%s has no neutral description", o)
			}
			checkNoVerdict(t, o, d)
		}
		if len(interventionDescriptionsNeutral) != len(interventionDescriptions) {
			t.Errorf("neutral covers %d options, v1 %d", len(interventionDescriptionsNeutral), len(interventionDescriptions))
		}
	})
	t.Run("UC-040/BR-036 unknown and empty profiles use v1", func(t *testing.T) {
		for _, p := range []string{"", "v1", "bogus"} {
			if descriptionsFor(p)[decide.OptionNoop] != interventionDescriptions[decide.OptionNoop] {
				t.Errorf("profile %q did not give v1", p)
			}
		}
		if !strings.Contains(targetInstructions("grant_shield", "neutral"), "immune to elimination for a few seconds") ||
			strings.Contains(targetInstructions("grant_shield", "neutral"), "unfairly") {
			t.Errorf("neutral target instructions = %q", targetInstructions("grant_shield", "neutral"))
		}
	})
}

func TestNeutralProfile_Suitability(t *testing.T) {
	st := testRequest().State
	dangers := []string{decide.DangerNearThreshold, decide.DangerElevated, decide.DangerSafe, ""}
	ivs := []string{"grant_shield", "toughness_boost", "adjust_player_sensitivity", "set_player_handicap", "soft_penalty", "send_controller_effect", "eliminate_player"}
	t.Run("UC-040/BR-036 suitability states the situation, never a verdict", func(t *testing.T) {
		for _, iv := range ivs {
			for _, danger := range dangers {
				for _, shielded := range []bool{false, true} {
					p := decide.PlayerState{Serial: "X", Alive: true, Danger: danger, Shielded: shielded}
					for _, role := range []string{"", "calmest", "struggling"} {
						s := st
						switch role {
						case "calmest":
							s.MostHeadroom = "X"
						case "struggling":
							s.Struggling = "X"
						}
						checkNoVerdict(t, iv+"/"+danger+"/"+role, suitabilityNeutral(iv, p, s))
					}
				}
			}
		}
		if got := suitabilityNeutral("grant_shield", decide.PlayerState{Danger: decide.DangerSafe}, st); got != "In no danger right now." {
			t.Errorf("safe player = %q", got)
		}
		if got := suitabilityNeutral("eliminate_player", decide.PlayerState{Serial: "X"}, decide.State{MostHeadroom: "X"}); got != "" {
			t.Errorf("eliminate_player must get no suitability: %q", got)
		}
	})
	t.Run("UC-040/BR-036 refusals stay facts in both profiles", func(t *testing.T) {
		p := decide.PlayerState{Serial: "X", Alive: true, Danger: decide.DangerNearThreshold, ShieldCharged: true}
		if suitabilityNeutral("grant_shield", p, st) != suitability("grant_shield", p, st) {
			t.Error("a charged player's refusal must read the same in both profiles")
		}
	})
	t.Run("UC-040/BR-036 v1 candidates keep their verdicts", func(t *testing.T) {
		for serial, d := range describeCandidatesFor(st, "grant_shield", "neutral") {
			checkNoVerdict(t, serial, d)
		}
		v1 := describeCandidates(st, "grant_shield")
		if same := describeCandidatesFor(st, "grant_shield", "bogus"); len(same) != len(v1) {
			t.Fatal("an unknown profile must describe as v1")
		}
		for serial, d := range describeCandidatesFor(st, "grant_shield", "bogus") {
			if d != v1[serial] {
				t.Errorf("%s: unknown profile %q != v1 %q", serial, d, v1[serial])
			}
		}
	})
}
