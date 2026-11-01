package flags

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/open-feature/go-sdk/openfeature"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"gopkg.in/yaml.v3"

	"github.com/joustmania/agent/goal"
)

// UC: UC-042 A12 BR-009
// goal_fr201_test.go: goal.profile is read per game, validated, and falls back to
// from_objectives; both agent.json files ship valid variants (#2479, FR-201,
// flags.md §3.2, decide-contract.md §14.4).

// goalEvaluator answers goal.profile with value / variant / err and reports the
// variant through ObjectValueDetails, as *openfeature.Client does.
type goalEvaluator struct {
	stubEvaluator
	value   any
	variant string
	err     error
	seen    []openfeature.EvaluationContext
}

func (g *goalEvaluator) ObjectValueDetails(_ context.Context, flag string, def any, ec openfeature.EvaluationContext, _ ...openfeature.Option) (openfeature.InterfaceEvaluationDetails, error) {
	if flag != keyGoalProfile {
		return openfeature.InterfaceEvaluationDetails{Value: def}, nil
	}
	g.seen = append(g.seen, ec)
	d := openfeature.InterfaceEvaluationDetails{Value: g.value}
	d.Variant = g.variant
	return d, g.err
}

func longRunningValue() map[string]any {
	return map[string]any{"terms": []any{
		map[string]any{"characteristic": "duration", "min": 180.0},
		map[string]any{"characteristic": "activity", "min": 0.3, "max": 0.6},
	}}
}

func TestGoalProfile_Read(t *testing.T) {
	ctx := context.Background()
	attrs := GameAttrs{GameKind: "shadow", ExperimentID: "exp-1", Arm: "b"}
	t.Run("UC-042/A12 a terms variant is the profile, named by its variant, read in the game's context", func(t *testing.T) {
		ev := &goalEvaluator{value: longRunningValue(), variant: "long_running_game"}
		p := New(ev, nil).GoalProfile(ctx, "g-1", attrs)
		if p.Name != "long_running_game" || p.FromObjectives || len(p.Terms) != 2 {
			t.Fatalf("profile = %+v", p)
		}
		got := ev.seen[0].Attributes()
		if got["gameId"] != "g-1" || got["game_kind"] != "shadow" || got["experiment_id"] != "exp-1" || got["arm"] != "b" {
			t.Fatalf("evaluation context = %v", got)
		}
	})
	t.Run("UC-042/A12 the sentinel derives from the objectives and the fitness flags", func(t *testing.T) {
		ev := &goalEvaluator{
			value: map[string]any{"from_objectives": true}, variant: "from_objectives",
			stubEvaluator: stubEvaluator{
				objects: map[string]any{keyObjectives: map[string]any{"endurance": 0.7, "balanced": 0.1, "accelerate": 0.1, "chaos": 0.1}},
				ints:    map[string]int64{keyEnduranceMinSessionSeconds: 300},
			},
		}
		p := New(ev, nil).GoalProfile(ctx, "g-1", attrs)
		if p.Name != goal.NameLongRunningGame || !p.FromObjectives || *p.Terms[0].Min != 300 {
			t.Fatalf("profile = %+v", p)
		}
	})
	t.Run("UC-042/A12 a read error is from_objectives (the code default objectives: endurance)", func(t *testing.T) {
		ev := &goalEvaluator{value: longRunningValue(), variant: "long_running_game", err: errors.New("PROVIDER_NOT_READY")}
		p := New(ev, nil).GoalProfile(ctx, "g-1", attrs)
		if !p.FromObjectives || p.Name != goal.NameLongRunningGame {
			t.Fatalf("profile = %+v", p)
		}
	})
	t.Run("UC-042/A12 a nil wrapper or client is from_objectives", func(t *testing.T) {
		var f *Flags
		if p := f.GoalProfile(ctx, "g-1", attrs); !p.FromObjectives {
			t.Fatalf("nil Flags = %+v", p)
		}
		if p := New(nil, nil).GoalProfile(ctx, "g-1", attrs); !p.FromObjectives {
			t.Fatalf("nil client = %+v", p)
		}
	})
	t.Run("UC-042/A12 an evaluator without details names a valid profile custom", func(t *testing.T) {
		stub := stubEvaluator{objects: map[string]any{keyGoalProfile: longRunningValue()}}
		if p := New(stub, nil).GoalProfile(ctx, "g-1", attrs); p.Name != "custom" || p.FromObjectives {
			t.Fatalf("profile = %+v", p)
		}
	})
}

func TestGoalProfile_InvalidFallsBack(t *testing.T) {
	ctx := context.Background()
	var logs bytes.Buffer
	ev := &goalEvaluator{
		value:   map[string]any{"terms": []any{map[string]any{"characteristic": "fun", "min": 1.0}}},
		variant: "broken",
	}
	f := New(ev, slog.New(slog.NewTextHandler(&logs, nil)))
	reader := sdkmetric.NewManualReader()
	f.SetGoalMeterProvider(sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader)))

	for i := 0; i < 3; i++ {
		p := f.GoalProfile(ctx, "g-1", GameAttrs{})
		if !p.FromObjectives || p.Name != goal.NameLongRunningGame {
			t.Fatalf("UC-042/A12 an invalid value must fall back to from_objectives, got %+v", p)
		}
	}
	if n := strings.Count(logs.String(), "level=WARN"); n != 1 {
		t.Fatalf("UC-042/A12 WARN once, got %d:\n%s", n, logs.String())
	}
	points := collectMetrics(t, reader)[MetricGoalProfileInvalid]
	if len(points) != 1 || points[0].Value != 3 {
		t.Fatalf("UC-042/A12 %s = %+v, want one series at 3", MetricGoalProfileInvalid, points)
	}
	if r, _ := attr(points[0].Attributes, "reason"); r != goal.ReasonCharacteristic {
		t.Fatalf("reason = %q", r)
	}
}

// agentFlagFiles are both agent.json files (stack and CI baseline).
var agentFlagFiles = []string{
	filepath.Join("..", "..", "flagd", "agent.json"),
	filepath.Join("..", "..", "flagd", "ci", "agent.json"),
}

type flagDef struct {
	Variants       map[string]any `json:"variants"`
	DefaultVariant string         `json:"defaultVariant"`
	Targeting      any            `json:"targeting"`
}

func readAgentFlags(t *testing.T, path string) map[string]flagDef {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Metadata struct {
			FlagSetID string `json:"flagSetId"`
		} `json:"metadata"`
		Flags map[string]flagDef `json:"flags"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	if doc.Metadata.FlagSetID != "agent" {
		t.Fatalf("%s flagSetId = %q", path, doc.Metadata.FlagSetID)
	}
	return doc.Flags
}

// TestGoalFlags_Contract checks every goal.profile variant of both agent.json
// files against the schema, the shipped variant set and the default, and that
// decision.goal_in_prompt ships off.
func TestGoalFlags_Contract(t *testing.T) {
	wantVariants := []string{"from_objectives", "long_running_game", "lively_finale", "balanced_contest", "quick_game"}
	for _, path := range agentFlagFiles {
		flags := readAgentFlags(t, path)
		t.Run("UC-042/A12 goal.profile "+path, func(t *testing.T) {
			def, ok := flags[keyGoalProfile]
			if !ok {
				t.Fatalf("no %s", keyGoalProfile)
			}
			if def.DefaultVariant != goal.NameFromObjectives {
				t.Fatalf("defaultVariant = %q", def.DefaultVariant)
			}
			if def.Targeting != nil {
				t.Fatalf("goal.profile must ship without targeting, got %v", def.Targeting)
			}
			if len(def.Variants) != len(wantVariants) {
				t.Fatalf("variants = %v, want %v", def.Variants, wantVariants)
			}
			for _, name := range wantVariants {
				v, ok := def.Variants[name]
				if !ok {
					t.Fatalf("missing variant %s", name)
				}
				terms, fromObj, err := goal.Parse(v)
				if err != nil {
					t.Fatalf("variant %s invalid: %v", name, err)
				}
				if fromObj != (name == goal.NameFromObjectives) {
					t.Fatalf("variant %s: sentinel = %v", name, fromObj)
				}
				if name != goal.NameFromObjectives && len(terms) == 0 {
					t.Fatalf("variant %s has no terms", name)
				}
			}
			finale, _, _ := goal.Parse(def.Variants["lively_finale"])
			if finale[0].Phase != goal.PhaseEndgame {
				t.Fatalf("lively_finale's activity term must be phase endgame, got %+v", finale[0])
			}
		})
		t.Run("UC-042/A12 decision.goal_in_prompt "+path, func(t *testing.T) {
			def, ok := flags[keyDecisionGoalInPrompt]
			if !ok || def.DefaultVariant != "off" || def.Variants["off"] != false || def.Variants["on"] != true || def.Targeting != nil {
				t.Fatalf("decision.goal_in_prompt = %+v", def)
			}
		})
	}
}

// TestGoalFlags_NeverWrittenC034: both flags live in the agent set, which the agent
// never writes (C-034, agent_set_never_written_c034_test.go holds no key list),
// and the flag-service registry gives no writer a row for them.
func TestGoalFlags_NeverWrittenC034(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "flag-service", "registry.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	var reg map[string]any
	if err := yaml.Unmarshal(raw, &reg); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{keyGoalProfile, keyDecisionGoalInPrompt} {
		for _, path := range agentFlagFiles {
			if _, ok := readAgentFlags(t, path)[key]; !ok {
				t.Fatalf("UC-042/BR-009 %s must be in the agent set (%s)", key, path)
			}
		}
		if strings.Contains(string(raw), "key: "+key+",") || strings.Contains(string(raw), "key: "+key+"}") {
			t.Fatalf("UC-042/BR-009 %s must have no flag-service registry row (human edit only)", key)
		}
	}
}
