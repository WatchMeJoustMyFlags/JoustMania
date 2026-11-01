package goal

import (
	"encoding/json"
	"errors"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/gamecontext"
)

// UC: UC-042 A12 BR-009
// goal_fr201_test.go: the goal-profile parser, the from_objectives mapping, the
// prompt lines and the attainment math (#2479, FR-201, decide-contract.md §14.4,
// flags.md §3.2).

func f(v float64) *float64 { return &v }

func termObj(char string, kv ...any) map[string]any {
	m := map[string]any{"characteristic": char}
	for i := 0; i+1 < len(kv); i += 2 {
		m[kv[i].(string)] = kv[i+1]
	}
	return m
}

func termsVal(terms ...any) map[string]any { return map[string]any{"terms": terms} }

func TestParse_Rejects(t *testing.T) {
	five := []any{}
	for i := 0; i < 5; i++ {
		five = append(five, termObj("duration", "min", 10.0))
	}
	for _, tc := range []struct {
		name   string
		raw    any
		reason string
	}{
		{"UC-042/A12 not an object", "long_running_game", ReasonShape},
		{"UC-042/A12 neither terms nor sentinel", map[string]any{}, ReasonShape},
		{"UC-042/A12 sentinel false", map[string]any{"from_objectives": false}, ReasonShape},
		{"UC-042/A12 both terms and sentinel", map[string]any{"from_objectives": true, "terms": []any{termObj("duration", "min", 1.0)}}, ReasonShape},
		{"UC-042/A12 unknown top-level key", map[string]any{"terms": []any{termObj("duration", "min", 1.0)}, "name": "x"}, ReasonUnknownKey},
		{"UC-042/A12 unknown term key", termsVal(termObj("duration", "min", 1.0, "target", 3.0)), ReasonUnknownKey},
		{"UC-042/A12 unknown characteristic", termsVal(termObj("tempo_followed", "min", 1.0)), ReasonCharacteristic},
		{"UC-042/A12 missing characteristic", termsVal(map[string]any{"min": 1.0}), ReasonCharacteristic},
		{"UC-042/A12 band above the hard bound", termsVal(termObj("duration", "max", 3601.0)), ReasonBand},
		{"UC-042/A12 band below the hard bound", termsVal(termObj("activity", "min", -0.1)), ReasonBand},
		{"UC-042/A12 pace above 30/min", termsVal(termObj("elimination_pace", "min", 31.0)), ReasonBand},
		{"UC-042/A12 NaN edge", termsVal(termObj("activity", "min", math.NaN())), ReasonBand},
		{"UC-042/A12 infinite edge", termsVal(termObj("duration", "max", math.Inf(1))), ReasonBand},
		{"UC-042/A12 string edge", termsVal(termObj("duration", "max", "120")), ReasonBand},
		{"UC-042/A12 no edge", termsVal(termObj("duration")), ReasonBand},
		{"UC-042/A12 min above max", termsVal(termObj("activity", "min", 0.6, "max", 0.3)), ReasonBand},
		{"UC-042/A12 unknown phase", termsVal(termObj("activity", "min", 0.6, "phase", "finale")), ReasonPhase},
		{"UC-042/A12 zero weight", termsVal(termObj("activity", "min", 0.6, "weight", 0.0)), ReasonWeight},
		{"UC-042/A12 weight above 10", termsVal(termObj("activity", "min", 0.6, "weight", 11.0)), ReasonWeight},
		{"UC-042/A12 no terms", termsVal(), ReasonTermCount},
		{"UC-042/A12 more than 4 terms", map[string]any{"terms": five}, ReasonTermCount},
		{"UC-042/A12 terms not a list", map[string]any{"terms": "duration>=180"}, ReasonShape},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, _, err := Parse(tc.raw)
			var pe *ParseError
			if !errors.As(err, &pe) {
				t.Fatalf("Parse(%v) err = %v, want a ParseError", tc.raw, err)
			}
			if pe.Reason != tc.reason {
				t.Fatalf("reason = %q, want %q (%v)", pe.Reason, tc.reason, err)
			}
		})
	}
}

func TestParse_Accepts(t *testing.T) {
	t.Run("UC-042/A12 sentinel", func(t *testing.T) {
		terms, fromObj, err := Parse(map[string]any{"from_objectives": true})
		if err != nil || !fromObj || terms != nil {
			t.Fatalf("got %v %v %v", terms, fromObj, err)
		}
	})
	t.Run("UC-042/A12 defaults: phase whole, weight 1, open edges", func(t *testing.T) {
		terms, fromObj, err := Parse(termsVal(
			termObj("duration", "min", 180.0, "max", nil),
			termObj("activity", "min", 0.6, "phase", "endgame", "weight", 2.0),
			termObj("survival", "min", 1.0),
			termObj("change", "max", 2.0),
		))
		if err != nil || fromObj || len(terms) != 4 {
			t.Fatalf("got %v %v %v", terms, fromObj, err)
		}
		if terms[0].Phase != PhaseWhole || terms[0].Weight != 1 || terms[0].Max != nil || *terms[0].Min != 180 {
			t.Fatalf("term 0 = %+v", terms[0])
		}
		if terms[1].Phase != PhaseEndgame || terms[1].Weight != 2 {
			t.Fatalf("term 1 = %+v", terms[1])
		}
	})
}

// objectivesVariants reads the objectives variants of services/flagd/agent.json.
func objectivesVariants(t *testing.T) map[string]map[string]float64 {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "flagd", "agent.json"))
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Flags map[string]json.RawMessage `json:"flags"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	var obj struct {
		Variants map[string]map[string]float64 `json:"variants"`
	}
	if err := json.Unmarshal(doc.Flags["objectives"], &obj); err != nil {
		t.Fatal(err)
	}
	return obj.Variants
}

func TestFromObjectives_PerVariant(t *testing.T) {
	variants := objectivesVariants(t)
	for variant, want := range map[string]string{
		"endurance_focused":  NameLongRunningGame,
		"accelerate_focused": NameQuickGame,
		"balanced_focused":   NameBalancedContest,
		"chaos_focused":      NameChaos,
		"mixed":              "",
	} {
		t.Run("UC-042/A12 "+variant, func(t *testing.T) {
			w, ok := variants[variant]
			if !ok {
				t.Fatalf("agent.json objectives has no %s", variant)
			}
			p := FromObjectives(w, 120, 60)
			if !p.FromObjectives || p.Name != want {
				t.Fatalf("FromObjectives(%s) = %+v, want %q", variant, p, want)
			}
			if want == "" && !p.Empty() {
				t.Fatalf("mixed must have no goal, got %+v", p)
			}
		})
	}
	t.Run("UC-042/A12 chaos is activity >= 0.6 plus change >= 0.3", func(t *testing.T) {
		got := FromObjectives(variants["chaos_focused"], 120, 60).GoalLine()
		if got != "chaos_focused activity>=0.60 change>=0.30" {
			t.Fatalf("chaos line = %q", got)
		}
	})
}

func TestFromObjectives_DurationBandsReadFitnessFlags(t *testing.T) {
	variants := objectivesVariants(t)
	t.Run("UC-042/A12 endurance reads fitness.endurance.min_session_seconds", func(t *testing.T) {
		for _, minS := range []float64{60, 120, 300} {
			p := FromObjectives(variants["endurance_focused"], minS, 60)
			d := p.Terms[0]
			if d.Characteristic != decide.CharDuration || d.Min == nil || *d.Min != minS || d.Max != nil {
				t.Fatalf("endurance duration term = %+v, want >= %v", d, minS)
			}
		}
	})
	t.Run("UC-042/A12 accelerate reads fitness.accelerate.target_session_seconds", func(t *testing.T) {
		for _, target := range []float64{30, 60, 120} {
			p := FromObjectives(variants["accelerate_focused"], 120, target)
			d := p.Terms[0]
			if d.Characteristic != decide.CharDuration || d.Max == nil || *d.Max != target || d.Min != nil {
				t.Fatalf("accelerate duration term = %+v, want <= %v", d, target)
			}
		}
	})
	t.Run("UC-042/A12 a fitness value outside the hard bound is clamped", func(t *testing.T) {
		p := FromObjectives(variants["endurance_focused"], 99999, 60)
		if *p.Terms[0].Min != 3600 {
			t.Fatalf("clamped min = %v", *p.Terms[0].Min)
		}
	})
}

func TestGoalLine(t *testing.T) {
	p := Profile{Name: NameLongRunningGame, Terms: []Term{
		{Characteristic: decide.CharDuration, Min: f(180), Phase: PhaseWhole, Weight: 1},
		{Characteristic: decide.CharActivity, Min: f(0.3), Max: f(0.6), Phase: PhaseWhole, Weight: 1},
		{Characteristic: decide.CharEliminationPace, Max: f(1), Phase: PhaseWhole, Weight: 1},
	}}
	if got, want := p.GoalLine(), "long_running_game duration>=180s activity=0.30..0.60 elimination_pace<=1/min"; got != want {
		t.Fatalf("GoalLine = %q, want %q", got, want)
	}
	finale := Profile{Name: NameLivelyFinale, Terms: []Term{
		{Characteristic: decide.CharActivity, Min: f(0.6), Phase: PhaseEndgame, Weight: 1},
		{Characteristic: decide.CharDuration, Min: f(90), Max: f(240), Phase: PhaseWhole, Weight: 1},
	}}
	if got, want := finale.GoalLine(), "lively_finale activity@endgame>=0.60 duration=90..240s"; got != want {
		t.Fatalf("GoalLine = %q, want %q", got, want)
	}
	values := map[string]float64{"duration": 95, "activity": 0.42, "elimination_pace": 0.6, "closeness": 0.21, "activity@endgame": 0.7}
	measure := func(char, phase string) (float64, bool) { v, ok := values[Label(char, phase)]; return v, ok }
	if got, want := NowLine(p, measure), "duration=95s activity=0.42 elimination_pace=0.6/min closeness=0.21"; got != want {
		t.Fatalf("NowLine = %q, want %q", got, want)
	}
	if got := NowLine(finale, measure); !strings.HasSuffix(got, " activity@endgame=0.70") {
		t.Fatalf("NowLine(finale) = %q, want the endgame value last", got)
	}
	if got := NowLine(p, func(string, string) (float64, bool) { return 0, false }); got != "no data yet" {
		t.Fatalf("NowLine without data = %q", got)
	}
}

func TestAttainment(t *testing.T) {
	durMin := Term{Characteristic: decide.CharDuration, Min: f(180), Phase: PhaseWhole, Weight: 1}
	for _, tc := range []struct {
		name string
		term Term
		v    float64
		want float64
	}{
		{"UC-042/A12 inside the band is 1", durMin, 200, 1},
		{"UC-042/A12 on the edge is 1", durMin, 180, 1},
		{"UC-042/A12 half way below the min", durMin, 90, 0.5},
		{"UC-042/A12 far below decays to 0", durMin, 0, 0},
		{"UC-042/A12 above the max, relative to the edge", Term{Characteristic: decide.CharActivity, Min: f(0.3), Max: f(0.6), Weight: 1}, 0.75, 0.75},
		{"UC-042/A12 an edge at 0 decays over the floor", Term{Characteristic: decide.CharEliminationPace, Max: f(0), Weight: 1}, 0.5, 0.5},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := TermScore(tc.term, tc.v); math.Abs(got-tc.want) > 1e-9 {
				t.Fatalf("TermScore = %v, want %v", got, tc.want)
			}
		})
	}
	t.Run("UC-042/A12 weighted mean of the measured terms", func(t *testing.T) {
		p := Profile{Name: "x", Terms: []Term{
			{Characteristic: decide.CharDuration, Min: f(180), Phase: PhaseWhole, Weight: 1},   // 90 -> 0.5
			{Characteristic: decide.CharActivity, Max: f(0.6), Phase: PhaseWhole, Weight: 3},   // 0.4 -> 1
			{Characteristic: decide.CharCloseness, Max: f(0.25), Phase: PhaseWhole, Weight: 5}, // unmeasured
		}}
		vals := map[string]float64{"duration": 90, "activity": 0.4}
		r := Score(p, func(char, _ string) (float64, bool) { v, ok := vals[char]; return v, ok })
		if !r.HasAttainment || math.Abs(r.Attainment-(0.5+3)/4) > 1e-9 {
			t.Fatalf("attainment = %v (%v), want 0.875", r.Attainment, r.HasAttainment)
		}
		if r.Terms[2].Measured {
			t.Fatal("closeness must be unmeasured")
		}
		g := r.Summary()
		if g.Attainment == nil || g.Terms[2].Value != nil || g.Terms[0].Score == nil || *g.Terms[0].Score != 0.5 {
			t.Fatalf("summary = %+v", g)
		}
	})
	t.Run("UC-042/A12 nothing measured is no attainment", func(t *testing.T) {
		r := Score(Profile{Name: "x", Terms: []Term{{Characteristic: decide.CharSurvival, Min: f(1), Weight: 1}}},
			func(string, string) (float64, bool) { return 0, false })
		if r.HasAttainment || r.Summary().Attainment != nil {
			t.Fatal("no measured term must give no attainment")
		}
	})
}

// sampledGame is a running game with serials alive, each with 1 s movement
// samples of intensity up to end, threshold 1.
func sampledGame(end time.Time, dur float64, intensity map[string]float64, eliminated ...string) gamecontext.GameContext {
	active, th := true, 1.0
	c := gamecontext.GameContext{SessionID: "g1", Players: map[string]*gamecontext.PlayerSignals{}}
	c.Session.DurationSeconds = &dur
	c.Session.DeathThreshold = &th
	c.Session.EliminationSequence = eliminated
	for serial, v := range intensity {
		p := &gamecontext.PlayerSignals{Active: &active}
		for i := 30; i >= 0; i-- {
			p.History = append(p.History, gamecontext.MovementSample{At: end.Add(-time.Duration(i) * time.Second), Intensity: v})
		}
		c.Players[serial] = p
	}
	for _, s := range eliminated {
		inactive := false
		c.Players[s] = &gamecontext.PlayerSignals{Active: &inactive}
	}
	return c
}

func TestAccumulator(t *testing.T) {
	t0 := time.Unix(1_700_000_000, 0)
	t.Run("UC-042/A12 whole-game activity and closeness are sample means", func(t *testing.T) {
		var a Accumulator
		a.Sample(sampledGame(t0, 20, map[string]float64{"A": 0.2, "B": 0.4, "C": 0.6}), t0)
		a.Sample(sampledGame(t0.Add(2*time.Second), 22, map[string]float64{"A": 0.9, "B": 0.9, "C": 0.9}), t0.Add(2*time.Second)) // inside SampleEvery: skipped
		c := sampledGame(t0.Add(5*time.Second), 25, map[string]float64{"A": 0.6, "B": 0.6, "C": 0.6})
		a.Sample(c, t0.Add(5*time.Second))
		if v, ok := a.Measure(c, decide.CharActivity, PhaseWhole); !ok || math.Abs(v-0.5) > 1e-9 {
			t.Fatalf("activity = %v %v, want 0.5", v, ok)
		}
		if v, ok := a.Measure(c, decide.CharCloseness, PhaseWhole); !ok || math.Abs(v-0.2) > 1e-9 {
			t.Fatalf("closeness = %v %v, want 0.2", v, ok)
		}
		if _, ok := a.Measure(c, decide.CharActivity, PhaseEndgame); ok {
			t.Fatal("no endgame yet")
		}
	})
	t.Run("UC-042/A12 endgame values start when at most 2 are alive", func(t *testing.T) {
		var a Accumulator
		a.Sample(sampledGame(t0, 60, map[string]float64{"A": 0.2, "B": 0.2, "C": 0.2}), t0)
		end := sampledGame(t0.Add(10*time.Second), 70, map[string]float64{"A": 0.8, "B": 0.8}, "C")
		a.Sample(end, t0.Add(10*time.Second))
		end.Session.DurationSeconds = f(100)
		end.Session.EliminationSequence = []string{"C", "B"}
		if v, ok := a.Measure(end, decide.CharActivity, PhaseEndgame); !ok || math.Abs(v-0.8) > 1e-9 {
			t.Fatalf("endgame activity = %v %v", v, ok)
		}
		if v, ok := a.Measure(end, decide.CharDuration, PhaseEndgame); !ok || v != 30 {
			t.Fatalf("endgame duration = %v %v, want 30", v, ok)
		}
		if v, ok := a.Measure(end, decide.CharEliminationPace, PhaseWhole); !ok || math.Abs(v-1.2) > 1e-9 {
			t.Fatalf("whole pace = %v %v, want 1.2/min", v, ok)
		}
		if v, ok := a.Measure(end, decide.CharEliminationPace, PhaseEndgame); !ok || math.Abs(v-2) > 1e-9 {
			t.Fatalf("endgame pace = %v %v, want 2/min (1 in 30 s)", v, ok)
		}
		if _, ok := a.Measure(end, decide.CharSurvival, PhaseWhole); ok {
			t.Fatal("survival has no game-level value")
		}
	})
}
