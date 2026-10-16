package jev

import (
	"encoding/json"
	"errors"
	"math"
	"strings"
	"testing"

	"github.com/joustmania/agent/decide"
)

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
		got, err := scoreToRange(c.s, c.rng)
		if (err == nil) != c.ok || (c.ok && math.Abs(got-c.want) > 1e-12) {
			t.Errorf("scoreToRange(%v, %v) = %v, %v; want %v ok=%v", c.s, c.rng, got, err, c.want, c.ok)
		}
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
	out, err := encodeRequest("jev-latest", req)
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
	if _, err := encodeRequest("m", bad); !errors.Is(err, decide.ErrInvalid) {
		t.Errorf("unknown kind: %v", err)
	}
	many := make([]string, maxChoiceOptions+1)
	for i := range many {
		many[i] = strings.Repeat("o", i+1)
	}
	bad.Questions = []decide.Question{{ID: "x", Kind: decide.KindChoice, Options: many}}
	if _, err := encodeRequest("m", bad); !errors.Is(err, decide.ErrInvalid) {
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
	instr, ladder := scoreLadder(mag, req)
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
	if got := nativeConfidenceJSON(map[string]float64{"b": 0.5, "a": 0.25}); got != `{"a":0.25,"b":0.5}` {
		t.Errorf("got %s", got)
	}
	if nativeConfidenceJSON(nil) != "" {
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
