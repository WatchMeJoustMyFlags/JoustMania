package jev

import (
	"encoding/json"
	"errors"
	"math"
	"strconv"
	"strings"
	"testing"

	"github.com/joustmania/agent/decide"
)

// TestScoreToRange: without level probabilities Jev's fractional score snaps
// to the NEAREST level (#1262), never an interpolation between levels; with
// them the most likely level wins (TestMostLikelyLevel, #1281).
func TestScoreToRange(t *testing.T) {
	cases := []struct {
		s    float64
		rng  [2]float64
		want float64
		ok   bool
	}{
		{0, [2]float64{0, 1}, 0, true},
		{4, [2]float64{0, 1}, 1, true},
		{2, [2]float64{0, 1}, 0.5, true},
		{2.19, [2]float64{0, 1}, 0.5, true}, // nearest level 2, not 2.19/4
		{2.5, [2]float64{0, 1}, 0.75, true}, // half rounds up
		{0.49, [2]float64{0, 1}, 0, true},
		{1, [2]float64{10, 20}, 12.5, true},
		{4 + 1e-9, [2]float64{0, 1}, 1, true}, // float noise at the top is clamped
		{-1e-9, [2]float64{0, 1}, 0, true},
		{3, [2]float64{5, 5}, 5, true},
		{4.01, [2]float64{0, 1}, 0, false},
		{-0.5, [2]float64{0, 1}, 0, false},
		{math.NaN(), [2]float64{0, 1}, 0, false},
		{math.Inf(1), [2]float64{0, 1}, 0, false},
	}
	for _, c := range cases {
		q := decide.Question{ID: "s", Kind: decide.KindScore, Range: c.rng}
		got, err := scoreToRange(c.s, nil, q, "")
		if (err == nil) != c.ok || (c.ok && math.Abs(got-c.want) > 1e-12) {
			t.Errorf("scoreToRange(%v, %v) = %v, %v; want %v ok=%v", c.s, c.rng, got, err, c.want, c.ok)
		}
	}
}

// TestScoreToRange_Conditioned: a details-stage ffa.magnitude snaps onto the
// chosen intervention's own levels, and the normalized answer resolves back to
// exactly that level's value (integer levers stay whole).
func TestScoreToRange_Conditioned(t *testing.T) {
	mag := decide.Question{ID: decide.QuestionFFAMagnitude, Kind: decide.KindScore, Range: [2]float64{0, 1}}
	for _, c := range []struct {
		iv    string
		s     float64
		value float64
	}{
		{"grant_shield", 0, 3},
		{"grant_shield", 3.6, 10},
		{"grant_shield", 2.19, 6.5},
		{"adjust_global_sensitivity", 1.4, 1},
		{"adjust_global_sensitivity", 2.5, 3},
		{"adjust_music_tempo", 1.2, 1.075},
		{"adjust_player_sensitivity", 0.2, 0.7},
	} {
		norm, err := scoreToRange(c.s, nil, mag, c.iv)
		if err != nil {
			t.Fatalf("%s %v: %v", c.iv, c.s, err)
		}
		v, _ := decide.ResolveMagnitude(c.iv, norm)
		if math.Abs(v-c.value) > 1e-9 {
			t.Errorf("%s score %v -> norm %v -> %v, want %v", c.iv, c.s, norm, v, c.value)
		}
	}
	if _, err := scoreToRange(4.5, nil, mag, "grant_shield"); err == nil {
		t.Error("score above the ladder must fail")
	}
}

func TestActingConfidence(t *testing.T) {
	p := map[string]float64{"noop": 0.3, "grant_shield": 0.4, "end_game": 0.3}
	if got := actingConfidence("grant_shield", p); math.Abs(got-0.7) > 1e-12 {
		t.Errorf("intervention: %v, want 1-p(noop)=0.7", got)
	}
	if got := actingConfidence("noop", map[string]float64{"noop": 0.8, "end_game": 0.2}); got != 0.8 {
		t.Errorf("noop: %v, want p(noop)", got)
	}
	if got := actingConfidence("end_game", map[string]float64{"end_game": 1}); got != 1 {
		t.Errorf("no noop mass: %v", got)
	}
}

func TestRenormalize(t *testing.T) {
	got := renormalize(map[string]float64{"a": 0.5, "b": 0.48})
	if math.Abs(got["a"]+got["b"]-1) > 1e-12 {
		t.Errorf("near-1 sum not rescaled: %v", got)
	}
	in := map[string]float64{"a": 0.5, "b": 0.3}
	if got := renormalize(in); got["a"] != 0.5 {
		t.Errorf("far-off sum must be left for Validate: %v", got)
	}
	exact := map[string]float64{"a": 0.77, "b": 0.23}
	if got := renormalize(exact); got["a"] != 0.77 {
		t.Errorf("exact sum must be untouched: %v", got)
	}
	// 17 options rounded to 2 decimals can miss 1 by up to 0.085: rescaled.
	wide := map[string]float64{}
	for i := 0; i < 17; i++ {
		wide[string(rune('a'+i))] = 0.0635 // sum 1.0795
	}
	if got := renormalize(wide); math.Abs(got["a"]*17-1) > 1e-12 {
		t.Errorf("17-option rounding miss not rescaled: %v", got["a"])
	}
	if got := renormalize(map[string]float64{"a": 0.5, "b": 0.44}); got["a"] != 0.5 {
		t.Errorf("2 options: 0.06 off exceeds max(0.05, 2*0.005), must be left for Validate: %v", got)
	}
	if got := renormalize(nil); got != nil {
		t.Errorf("nil = %v", got)
	}
}

func TestRankPhraseAndOrdinal(t *testing.T) {
	alive := []decide.PlayerState{
		{Serial: "a", Alive: true, Intensity: f64(1)},
		{Serial: "b", Alive: true, Intensity: f64(2)},
		{Serial: "c", Alive: true, Intensity: f64(3)},
		{Serial: "d", Alive: true, Intensity: f64(4)},
		{Serial: "e", Alive: true}, // unobserved: not counted
	}
	get := func(p decide.PlayerState) *float64 { return p.Intensity }
	for v, want := range map[float64]string{
		4: "highest of 4 alive players",
		1: "lowest of 4 alive players",
		3: "2nd highest of 4 alive players",
		2: "3rd highest of 4 alive players",
	} {
		if got := rankPhrase(v, alive, get); got != want {
			t.Errorf("rank(%v) = %q, want %q", v, got, want)
		}
	}
	if got := rankPhrase(1, alive[:1], get); got != "" {
		t.Errorf("single observation must not rank: %q", got)
	}
	for n, want := range map[int]string{1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 11: "11th", 12: "12th", 13: "13th", 21: "21st", 22: "22nd", 101: "101st"} {
		if got := ordinal(n); got != want {
			t.Errorf("ordinal(%d) = %q", n, got)
		}
	}
}

func TestDescribePlayers(t *testing.T) {
	d := describePlayers(testRequest().State)
	if !strings.Contains(d["MOCK0003"], "Movement intensity 2.20 (highest of 3 alive players)") {
		t.Errorf("MOCK0003 = %q", d["MOCK0003"])
	}
	if !strings.Contains(d["MOCK0001"], "eliminated (1st out)") || strings.Contains(d["MOCK0001"], "alive players") {
		t.Errorf("an eliminated player is described but not ranked: %q", d["MOCK0001"])
	}
}

// TestEncodeRequest_Shapes: every question kind maps onto its Jev primitive,
// and unsupported input is refused before any I/O.
func TestEncodeRequest_Shapes(t *testing.T) {
	req := decide.Request{
		State: decide.State{Players: []decide.PlayerState{{Serial: "P1", Alive: true, Intensity: f64(math.NaN())}}, ElapsedSeconds: math.Inf(1)},
		Questions: []decide.Question{
			{ID: "c", Kind: decide.KindChoice, Text: "pick", Options: []string{"z", "a"}},
			{ID: "s", Kind: decide.KindScore, Text: "how much", Range: [2]float64{0, 10}},
			{ID: "n", Kind: decide.KindNoul, Text: "yes?"},
		},
	}
	out, err := encodeRequest("jev-latest", req, "")
	if err != nil {
		t.Fatal(err) // NaN/Inf state readings must be dropped, not fail the marshal
	}
	var body struct {
		State     map[string]any            `json:"state"`
		Questions map[string]map[string]any `json:"questions"`
	}
	if err := json.Unmarshal(out.Body, &body); err != nil {
		t.Fatal(err)
	}
	if body.Questions["c"]["type"] != "choice" || body.Questions["s"]["type"] != "score" || body.Questions["n"]["type"] != "noul" {
		t.Errorf("types = %v", body.Questions)
	}
	if !strings.Contains(string(out.Body), `"criteria":{"z":null,"a":null}`) {
		t.Errorf("generic choice criteria must be null and in option order: %s", out.Body)
	}
	if _, ok := body.Questions["n"]["criteria"]; ok {
		t.Error("noul must carry no criteria")
	}
	if l := body.Questions["s"]["criteria"].([]any); len(l) != scoreLevels || body.Questions["s"]["instructions"] != "how much" {
		t.Errorf("score = %v", body.Questions["s"])
	}
	if body.State["elapsed_s"] != 0.0 || strings.Contains(string(out.Body), "intensity") {
		t.Errorf("non-finite readings leaked: %s", body.State)
	}

	bad := req
	bad.Questions = []decide.Question{{ID: "x", Kind: "boolean"}}
	if _, err := encodeRequest("m", bad, ""); !errors.Is(err, decide.ErrInvalid) {
		t.Errorf("unknown kind: %v", err)
	}
	many := make([]string, maxChoiceOptions+1)
	for i := range many {
		many[i] = strings.Repeat("o", i+1)
	}
	bad.Questions = []decide.Question{{ID: "x", Kind: decide.KindChoice, Options: many}}
	if _, err := encodeRequest("m", bad, ""); !errors.Is(err, decide.ErrInvalid) {
		t.Errorf("too many options: %v", err)
	}
}

// TestMagnitudeLadder: the ffa.magnitude ladder lists only the allowed
// numeric levers, with their resolved value per level and the meaning of both
// ends.
func TestMagnitudeLadder(t *testing.T) {
	req := testRequest()
	var mag decide.Question
	for _, q := range req.Questions {
		if q.ID == decide.QuestionFFAMagnitude {
			mag = q
		}
	}
	instr, ladder := scoreLadder(mag, req, "")
	if instr != magnitudeInstructions || len(ladder) != scoreLevels {
		t.Fatalf("instructions %q, %d levels", instr, len(ladder))
	}
	if !strings.Contains(ladder[0], "grant_shield 3 seconds (shortest shield)") || !strings.Contains(ladder[4], "grant_shield 10 seconds (longest shield)") {
		t.Errorf("ends = %q / %q", ladder[0], ladder[4])
	}
	if strings.Contains(strings.Join(ladder, " "), "partial_shield") {
		t.Error("partial_shield is not in the allow-list and must not be listed")
	}
	if !strings.Contains(ladder[2], "adjust_music_tempo 1.15 tempo") {
		t.Errorf("middle = %q", ladder[2])
	}
	for _, iv := range decide.MagnitudeInterventions() {
		if _, ok := magnitudeEnds[iv]; !ok {
			t.Errorf("magnitudeEnds misses %s", iv)
		}
	}
}

func TestNativeConfidenceJSON(t *testing.T) {
	if got := floatMapJSON(map[string]float64{"b": 0.5, "a": 0.25}); got != `{"a":0.25,"b":0.5}` {
		t.Errorf("got %s", got)
	}
	if floatMapJSON(nil) != "" {
		t.Error("empty map must render empty")
	}
}

func TestScrub(t *testing.T) {
	for _, in := range []string{
		`{"detail":"bad key apikey_AbC123-xyz_456"}`,
		`{"detail":"sk-abcdef123456"}`,
		`Authorization: Bearer abc.def-123`,
	} {
		out := scrub(in)
		if !strings.Contains(out, "[redacted]") || strings.Contains(out, "AbC123") || strings.Contains(out, "abcdef123456") || strings.Contains(out, "abc.def-123") {
			t.Errorf("scrub(%q) = %q", in, out)
		}
	}
	if got := scrub("apikey_ab"); got != "apikey_ab" {
		t.Errorf("too short to be a key: %q", got)
	}
}

// TestMostLikelyLevel (#1281): the magnitude level is the argmax of Jev's
// level probabilities; a tie goes to the gentler level (nearer the lever's
// neutral, else the lower level).
// Spec: decide-contract.md §3.2, UC-040 A13, FR-046.
func TestMostLikelyLevel(t *testing.T) {
	levels := func(ps ...float64) map[string]float64 {
		m := map[string]float64{}
		for i, p := range ps {
			m[strconv.Itoa(i)] = p
		}
		return m
	}
	diff := magnitudeLevels("adjust_global_difficulty") // 0.5, 0.875, 1.25, 1.625, 2 (neutral 1)
	for _, c := range []struct {
		name    string
		probs   map[string]float64
		gentler func(i, j int) bool
		want    int
		wantErr bool
	}{
		{"argmax", levels(0.29, 0.28, 0.30, 0.10, 0.03), lowerIsGentler, 2, false},
		{"argmax at the top", levels(0.1, 0.1, 0.1, 0.1, 0.6), lowerIsGentler, 4, false},
		{"tie, no neutral: lower", levels(0.05, 0.10, 0.35, 0.35, 0.15), lowerIsGentler, 2, false},
		{"tie, neutral: nearer neutral wins over lower", levels(0.4, 0.0, 0.4, 0.1, 0.1), nearerNeutral("adjust_global_difficulty", diff), 2, false},
		{"tie, neutral: nearer neutral", levels(0.1, 0.35, 0.35, 0.1, 0.1), nearerNeutral("adjust_global_difficulty", diff), 1, false},
		{"three-way tie", levels(0.3, 0.3, 0.3, 0.1), lowerIsGentler, 0, false},
		{"missing level", levels(0.5, 0.5), lowerIsGentler, 0, true},
		{"extra level", levels(0.2, 0.2, 0.2, 0.2, 0.1, 0.1), lowerIsGentler, 0, true},
		{"NaN", levels(math.NaN(), 0.2, 0.2, 0.2, 0.2), lowerIsGentler, 0, true},
	} {
		n := 5
		if c.name == "three-way tie" {
			n = 4
		}
		got, err := mostLikelyLevel(c.probs, n, c.gentler)
		if (err != nil) != c.wantErr || (!c.wantErr && got != c.want) {
			t.Errorf("%s: level %d, err %v; want %d (err %v)", c.name, got, err, c.want, c.wantErr)
		}
	}
	// A two-way half-ladder (weakest first): a tie goes to the weaker change.
	help := strengthLevels("set_player_handicap", directionHelp) // 1.25, 1.5, 1.75
	if got, _ := mostLikelyLevel(levels(0.1, 0.45, 0.45), 3, nearerNeutral("set_player_handicap", help)); got != 1 {
		t.Errorf("half-ladder tie = level %d, want 1 (1.5, the weaker)", got)
	}
	// Probabilities decide the level; without them the score is rounded.
	q := decide.Question{ID: "s", Kind: decide.KindScore, Range: [2]float64{0, 1}}
	if v, _ := scoreToRange(1.3, levels(0.29, 0.28, 0.30, 0.10, 0.03), q, ""); v != 0.5 {
		t.Errorf("with probabilities = %v, want 0.5 (level 2)", v)
	}
	if v, _ := scoreToRange(1.3, nil, q, ""); v != 0.25 {
		t.Errorf("without probabilities = %v, want 0.25 (round(1.3) = level 1)", v)
	}
	if _, err := scoreToRange(4.5, levels(0.2, 0.2, 0.2, 0.2, 0.2), q, ""); err == nil {
		t.Error("a score outside the ladder is invalid even with probabilities")
	}
}
