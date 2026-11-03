package state

import "testing"

// UC: UC-040 BR-036
// decision.prompt_profile takes experiment+arm values (#2530): scripts/agent-group-ab.sh
// writes one per arm, and a real game never resolves them.
func TestPromptProfile_ArmValues(t *testing.T) {
	t.Run("UC-040/BR-036 arm values resolve only in a non-real game of that experiment and arm", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		const exp = "group-ab-1700000000-42"
		f.put(WriteRequest{Set: "agent", Flag: "decision.prompt_profile", Variant: "v1", Writer: "group-ab", Scope: Scope{ExperimentID: exp, Arm: "A"}})
		f.put(WriteRequest{Set: "agent", Flag: "decision.prompt_profile", Variant: "neutral", Writer: "group-ab", Scope: Scope{ExperimentID: exp, Arm: "B"}})
		cases := []struct {
			name string
			ctx  map[string]any
			want string
		}{
			{"arm A", map[string]any{"game_kind": "shadow", "experiment_id": exp, "arm": "A"}, "v1"},
			{"arm B", map[string]any{"game_kind": "shadow", "experiment_id": exp, "arm": "B"}, "neutral"},
			{"real game with the ids", map[string]any{"game_kind": "real", "experiment_id": exp, "arm": "B"}, "v1"},
			{"unbound shadow game", map[string]any{"game_kind": "shadow"}, "v1"},
			{"other experiment", map[string]any{"game_kind": "shadow", "experiment_id": "group-ab-1", "arm": "B"}, "v1"},
		}
		for _, c := range cases {
			if _, v := f.resolve("decision.prompt_profile", c.ctx); v != c.want {
				t.Errorf("%s: resolved %v, want %s", c.name, v, c.want)
			}
		}
		if _, err := f.state.Clear(ClearRequest{Set: "agent", Flag: "decision.prompt_profile", Scope: Scope{ExperimentID: exp, Arm: "B"}}); err != nil {
			t.Fatal(err)
		}
		if _, v := f.resolve("decision.prompt_profile", cases[1].ctx); v != "v1" {
			t.Errorf("the cleared arm B value still resolves: %v", v)
		}
	})
}
