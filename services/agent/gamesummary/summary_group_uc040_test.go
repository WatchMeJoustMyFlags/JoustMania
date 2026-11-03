package gamesummary

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// UC: UC-040 A47 BR-033
func TestBuildSummary_GroupGrants(t *testing.T) {
	build := func(ds []GroupDecision) Summary {
		return BuildSummary(gamecontext.GameContext{SessionID: "g", Arm: "B", GameKind: "shadow"}, BuildOptions{Now: time.Unix(1, 0), GroupDecisions: ds})
	}
	t.Run("UC-040/A47 counts non-rescue grants and rescue-refused groups", func(t *testing.T) {
		got := build([]GroupDecision{
			{Outcome: "applied", Rescue: false, Rationale: "rally"},
			{Outcome: "applied", Rescue: true, Rationale: "save"},
			{Outcome: "blocked", Rescue: true, Rationale: "save again"},
			{Outcome: "blocked", Rescue: false, Rationale: ""},
			{Outcome: "failed", Rescue: true, Rationale: "x"},
		}).GroupGrants
		if got == nil || got.Applied != 2 || got.Blocked != 2 || got.NonRescue != 1 || got.RescueRefused != 1 {
			t.Fatalf("group_grants = %+v", got)
		}
		if strings.Join(got.Rationales, "|") != "rally|save|save again|x" {
			t.Fatalf("rationales = %v", got.Rationales)
		}
	})
	t.Run("UC-040/A47 a game without a group decision omits the field", func(t *testing.T) {
		s := build(nil)
		raw, _ := json.Marshal(s)
		if s.GroupGrants != nil || strings.Contains(string(raw), "group_grants") {
			t.Fatalf("summary = %s", raw)
		}
	})
	t.Run("UC-040/A47 the rationale export carries no arm or backend label", func(t *testing.T) {
		raw, _ := json.Marshal(build([]GroupDecision{{Outcome: "applied", Rationale: "rally"}}).GroupGrants)
		if string(raw) != `{"applied":1,"blocked":0,"non_rescue":1,"rescue_refused":0,"rationales":["rally"]}` {
			t.Fatalf("group_grants json = %s", raw)
		}
	})
}
