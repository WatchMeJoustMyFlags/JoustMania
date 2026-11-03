package gamesummary

import (
	"bytes"
	"encoding/json"
	"log/slog"
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
			{Outcome: "applied", Rescue: true, Rationale: ""},
			{Outcome: "blocked", Rescue: true, Rationale: "save again"},
			{Outcome: "blocked", Rescue: true, Rationale: ""},
			{Outcome: "blocked", Rescue: false, Rationale: ""},
			{Outcome: "failed", Rescue: true, Rationale: "x"},
		}).GroupGrants
		if got == nil || got.Applied != 3 || got.Blocked != 3 || got.NonRescue != 1 || got.RescueRefused != 2 {
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

// UC: UC-040 A47 BR-033
func TestCoordinator_ExportsGroupRationalesBlind(t *testing.T) {
	c, sr, _ := recordingCoordinator(t)
	var logs bytes.Buffer
	c.log = slog.New(slog.NewTextHandler(&logs, nil))
	c.SetGroupLookup(func(string) []GroupDecision {
		return []GroupDecision{
			{Outcome: "applied", Rescue: false, Rationale: "rally the field"},
			{Outcome: "blocked", Rescue: true, Rationale: "save A"},
		}
	})
	c.OnGameEnd(representativeGame(t))
	spans := spansByName(sr.Ended(), SpanGameSummary)
	if len(spans) != 1 {
		t.Fatalf("spans = %d", len(spans))
	}
	if v, ok := spanAttr(spans[0], attrGroupBlocked); !ok || v.AsInt64() != 1 {
		t.Errorf("summary.group.blocked = %v", v)
	}
	var reasons []string
	for _, ev := range spans[0].Events() {
		if ev.Name != eventGroupRationale {
			continue
		}
		if len(ev.Attributes) != 1 || string(ev.Attributes[0].Key) != attrGroupReason {
			t.Fatalf("rationale event attrs = %v, want only reason", ev.Attributes)
		}
		reasons = append(reasons, ev.Attributes[0].Value.AsString())
	}
	if strings.Join(reasons, "|") != "rally the field|save A" {
		t.Fatalf("rationale events = %v", reasons)
	}
	if out := logs.String(); !strings.Contains(out, "group_applied=1") || !strings.Contains(out, "group_blocked=1") || !strings.Contains(out, "group_rescue_refused=1") {
		t.Fatalf("summary_built log lacks group counts: %s", out)
	}
}
