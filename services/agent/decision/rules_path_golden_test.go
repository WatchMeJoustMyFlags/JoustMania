package decision

import (
	"context"
	"encoding/json"
	"flag"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/joustmania/agent/flags"
)

// rules_path_golden_test.go pins the in-game rules path (decision.backend=rules, the
// default) to a golden file. The golden was generated on main BEFORE #1234 retired
// the legacy mode/model/resolver path, so this test proves that removal left the
// rules path's decisions and permission outcomes byte-identical.
//
// #2395 changed it on purpose: with R4's catch-up the balanced fixture shields WEAK,
// then MID, and no helped player again while STRONG has none.
//
// Regenerate (only for an intended behavior change): go test ./decision -run
// TestRulesPathGolden -update-rules-golden

var updateRulesGolden = flag.Bool("update-rules-golden", false, "rewrite testdata/rules_path_golden.json")

type rulesGoldenStep struct {
	Step     int               `json:"step"`
	Outcomes []DecisionOutcome `json:"outcomes"`
}

type rulesGoldenFixture struct {
	Name    string            `json:"name"`
	Steps   []rulesGoldenStep `json:"steps"`
	Applied []Decision        `json:"applied"`
}

func TestRulesPathGolden(t *testing.T) {
	var got []rulesGoldenFixture
	for _, fx := range decideFixtures() {
		clock := time.Unix(10_000, 0)
		l, _ := decideRecordingLoop(t, flags.Snapshot{
			Enabled:              true,
			InterventionsAllowed: allInterventions(),
			Policy:               flags.Policy{MaxInterventionsPerMinute: 6},
			Decision: flags.DecisionBackend{
				Backend:                flags.DecisionBackendRules,
				ActConfidenceThreshold: flags.DefaultDecisionActConfidenceThreshold,
			},
		})
		engine := newTestEngine(fx.cfg, &clock)
		// R4's catch-up reads the loop's shield grants as main.go wires it (UC-040
		// A41, BR-030; #2395): the one intended golden change, in the balanced fixture.
		engine.SetShieldGrantSource(l.ShieldGrants)
		l.Rules = engine
		l.now = func() time.Time { return clock }
		sink := &capturingSink{}
		l.Actions = sink

		rec := rulesGoldenFixture{Name: fx.name}
		for step := 0; step < 8; step++ {
			st := l.OnEvaluate(context.Background(), fx.ctx, testTrigger())
			rec.Steps = append(rec.Steps, rulesGoldenStep{Step: step, Outcomes: st.Outcomes})
			advance(&clock, 7*time.Second)
		}
		// GameID is the sink-call addressing runDecision stamps (#1310), not
		// part of what the rules decided; the golden pins the rules path only.
		applied := sink.decisions()
		for i := range applied {
			applied[i].GameID = ""
		}
		rec.Applied = applied
		got = append(got, rec)
	}

	gotJSON, err := json.MarshalIndent(got, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join("testdata", "rules_path_golden.json")
	if *updateRulesGolden {
		if err := os.MkdirAll("testdata", 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, append(gotJSON, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read golden: %v (run with -update-rules-golden on main to create it)", err)
	}
	if string(append(gotJSON, '\n')) != string(want) {
		t.Fatalf("rules path diverged from the pre-#1234 golden.\n got: %s\nwant: %s", gotJSON, want)
	}
}
