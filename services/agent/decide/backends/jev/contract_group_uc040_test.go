package jev

import (
	"context"
	"testing"

	"github.com/joustmania/agent/decide"
)

// contract_group_uc040_test.go: the group lever's wire contract (#2467 PR 6; UC-040 A47,
// BR-033). group_request_details.json is encodeRequest of the details stage after
// grant_group_shield (three alive players); group_response_details.json is a jev-shaped
// choice answer over the group options. The response is derived from the recorded
// shield details body (same envelope, a choice answer), not recorded live: re-record
// both with the live tests once the group wording is fixed by the experiment.

// UC: UC-040 A47 BR-033
func TestContract_GroupLever(t *testing.T) {
	t.Run("UC-040/A47 the group details request matches the fixture", func(t *testing.T) {
		out, err := encodeRequest(fixtureModel, detailsRequest(groupRequestJev(3), decide.IntvGrantGroupShield).ask, decide.IntvGrantGroupShield)
		if err != nil {
			t.Fatal(err)
		}
		jsonEqual(t, out.Body, "group_request_details.json")
	})
	t.Run("UC-040/A47 the group choice replays through the two-stage round", func(t *testing.T) {
		api := newScriptedAPI(t, ok(actingIntervention(t, decide.IntvGrantGroupShield)), ok(loadFixture(t, "group_response_details.json")))
		req := groupRequestJev(3)
		resp, err := api.backend(fixtureModel, false).Decide(context.Background(), req)
		if err != nil {
			t.Fatal(err)
		}
		if err := decide.Validate(req, resp); err != nil {
			t.Fatalf("Validate: %v", err)
		}
		iv, _ := resp.Find(decide.QuestionFFAIntervention)
		if iv.Choice != decide.IntvGrantGroupShield {
			t.Errorf("intervention = %q", iv.Choice)
		}
		tg, _ := resp.Find(decide.QuestionFFATarget)
		if tg.Choice != "group:all_except:MOCK0002" {
			t.Errorf("target = %q, want group:all_except:MOCK0002", tg.Choice)
		}
		if n := api.calls.Load(); n != 2 {
			t.Errorf("%d calls, want 2 (the magnitude is answered locally)", n)
		}
		api.mu.Lock()
		second := api.bodies[1]
		api.mu.Unlock()
		jsonEqual(t, second, "group_request_details.json")
	})
}
