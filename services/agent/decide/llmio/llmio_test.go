package llmio

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/gamecontext"
)

// testRequest is a small FFA request: two alive players, one eliminated.
func testRequest() decide.Request {
	st := decide.State{
		GameID:    "game_abc",
		GameKind:  "real",
		Digest:    "d1",
		Narrative: "game: id=game_abc kind=real mode=ffa elapsed=42.0s\nplayers:\n  MOCK0001 alive\n  MOCK0002 alive\n  MOCK0003 eliminated#1\n",
		Players: []decide.PlayerState{
			{Serial: "MOCK0001", Alive: true},
			{Serial: "MOCK0002", Alive: true},
			{Serial: "MOCK0003", EliminationOrder: 1},
		},
		ElapsedSeconds: 42,
	}
	return decide.Request{
		State:     st,
		Questions: decide.FFAQuestions(st, []string{"grant_shield", "adjust_music_tempo"}),
		// Remote backends must never read Context; the sentinel proves it.
		Context: &gamecontext.GameContext{SessionID: "CONTEXT-SENTINEL"},
	}
}

const validAnswer = `{
  "answers": [
    {"id": "ffa.intervention", "choice": "grant_shield", "score": 0,
     "probabilities": [{"option": "grant_shield", "p": 0.7}, {"option": "noop", "p": 0.3}],
     "confidence": 0.85},
    {"id": "ffa.target", "choice": "MOCK0002", "score": 0,
     "probabilities": [{"option": "MOCK0002", "p": 0.9}, {"option": "MOCK0001", "p": 0.1}],
     "confidence": 0.8},
    {"id": "ffa.magnitude", "choice": "", "score": 0.5, "probabilities": [], "confidence": 0.6}
  ],
  "rationale": "MOCK0002 is about to be eliminated; a shield keeps the duel alive."
}`

func TestRender_StateAndQuestionsOnly(t *testing.T) {
	req := testRequest()
	p := Render(req)

	if strings.Contains(p.System+p.User, "CONTEXT-SENTINEL") {
		t.Fatal("prompt leaked Request.Context")
	}
	if !strings.Contains(p.User, req.State.Narrative[:20]) {
		t.Error("user message misses the narrative")
	}
	if !strings.Contains(p.User, "alive_players: MOCK0001, MOCK0002") {
		t.Errorf("user message misses the alive list:\n%s", p.User)
	}
	for _, q := range req.Questions {
		if !strings.Contains(p.User, "id: "+q.ID) {
			t.Errorf("question %s missing", q.ID)
		}
		// The magnitude text is multi-line; its bounds lines must survive.
		for _, line := range strings.Split(q.Text, "\n") {
			if !strings.Contains(p.User, strings.TrimSpace(line)) {
				t.Errorf("question %s text line %q missing", q.ID, line)
			}
		}
	}
	if !strings.Contains(p.User, `options: ["noop", "adjust_music_tempo", "grant_shield"]`) {
		t.Errorf("intervention options not rendered:\n%s", p.User)
	}
	if !strings.Contains(p.User, "range: [0, 1]") {
		t.Error("score range not rendered")
	}
	if !strings.Contains(p.System, "rationale") || !strings.Contains(p.System, "FFA") {
		t.Error("system prompt misses the game or answer contract")
	}
}

func TestRender_Deterministic(t *testing.T) {
	a, b := Render(testRequest()), Render(testRequest())
	if a != b {
		t.Fatal("Render is not deterministic")
	}
	if Render(decide.Request{}).System != a.System {
		t.Fatal("system prompt must be static")
	}
}

func TestAnswerSchema_StrictObjects(t *testing.T) {
	s := AnswerSchema()
	if _, err := json.Marshal(s); err != nil {
		t.Fatalf("schema does not marshal: %v", err)
	}
	var walk func(path string, node map[string]any)
	walk = func(path string, node map[string]any) {
		if node["type"] == "object" {
			if node["additionalProperties"] != false {
				t.Errorf("%s: additionalProperties must be false", path)
			}
			props, _ := node["properties"].(map[string]any)
			req, _ := node["required"].([]any)
			if len(req) != len(props) {
				t.Errorf("%s: every property must be required (%d props, %d required)", path, len(props), len(req))
			}
			for k, v := range props {
				walk(path+"."+k, v.(map[string]any))
			}
		}
		if items, ok := node["items"].(map[string]any); ok {
			walk(path+"[]", items)
		}
		for _, banned := range []string{"minimum", "maximum", "minLength", "maxLength"} {
			if _, ok := node[banned]; ok {
				t.Errorf("%s: unsupported constraint %s", path, banned)
			}
		}
	}
	walk("$", s)

	// A fresh copy each call: mutating one must not leak into the next.
	s["mutated"] = true
	if _, ok := AnswerSchema()["mutated"]; ok {
		t.Error("AnswerSchema returned shared state")
	}
}

func TestParse_Valid(t *testing.T) {
	req := testRequest()
	resp, err := Parse(req, []byte(validAnswer), "claude")
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if resp.Backend != "claude" {
		t.Errorf("backend = %q", resp.Backend)
	}
	a, ok := resp.Find(decide.QuestionFFAIntervention)
	if !ok || a.Choice != "grant_shield" || a.Probabilities["noop"] != 0.3 || a.Confidence != 0.85 {
		t.Errorf("intervention answer = %+v", a)
	}
	m, _ := resp.Find(decide.QuestionFFAMagnitude)
	if m.Score != 0.5 || m.Probabilities != nil || m.Choice != "" {
		t.Errorf("magnitude answer = %+v", m)
	}
	if !strings.HasPrefix(resp.Rationale, "MOCK0002 is about") {
		t.Errorf("rationale = %q", resp.Rationale)
	}
}

func TestParse_Normalizations(t *testing.T) {
	req := testRequest()
	cases := map[string]string{
		"code fence": "```json\n" + validAnswer + "\n```",
		"unknown fields ignored": strings.Replace(validAnswer, `"rationale"`,
			`"extra": {"nested": [1,2,3]}, "rationale"`, 1),
		"score question carries choice noise": strings.Replace(validAnswer,
			`"choice": "", "score": 0.5, "probabilities": []`,
			`"choice": "grant_shield", "score": 0.5, "probabilities": [{"option": "x", "p": 1}]`, 1),
		"probabilities rounded": strings.Replace(validAnswer,
			`{"option": "grant_shield", "p": 0.7}, {"option": "noop", "p": 0.3}`,
			`{"option": "grant_shield", "p": 0.67}, {"option": "noop", "p": 0.32}`, 1),
	}
	for name, raw := range cases {
		t.Run(name, func(t *testing.T) {
			resp, err := Parse(req, []byte(raw), "claude")
			if err != nil {
				t.Fatalf("Parse: %v", err)
			}
			if err := decide.Validate(req, resp); err != nil {
				t.Fatalf("Validate: %v", err)
			}
		})
	}
}

func TestParse_Rejects(t *testing.T) {
	req := testRequest()
	cases := map[string]string{
		"empty":             "",
		"not json":          "I think you should grant a shield.",
		"truncated":         validAnswer[:len(validAnswer)/2],
		"array":             `[1,2,3]`,
		"null":              `null`,
		"trailing data":     validAnswer + `{"answers": []}`,
		"trailing brace":    validAnswer + `}`,
		"trailing bracket":  validAnswer + " ]",
		"no answers":        `{"answers": [], "rationale": "x"}`,
		"wrong types":       `{"answers": "lots", "rationale": 7}`,
		"missing question":  strings.Replace(validAnswer, `"id": "ffa.target"`, `"id": "ffa.bogus"`, 1),
		"choice not option": strings.Replace(validAnswer, `"choice": "grant_shield"`, `"choice": "eliminate"`, 1),
		"choice not argmax": strings.Replace(validAnswer, `"p": 0.7}, {"option": "noop", "p": 0.3}`,
			`"p": 0.3}, {"option": "noop", "p": 0.7}`, 1),
		"probabilities way off": strings.Replace(validAnswer, `"p": 0.3}`, `"p": 0.9}`, 1),
		"duplicate probability": strings.Replace(validAnswer, `{"option": "noop", "p": 0.3}`,
			`{"option": "grant_shield", "p": 0.3}`, 1),
		"missing p":          strings.Replace(validAnswer, `{"option": "noop", "p": 0.3}`, `{"option": "noop"}`, 1),
		"missing confidence": strings.Replace(validAnswer, `, "confidence": 0.6`, ``, 1),
		"missing score":      strings.Replace(validAnswer, `"score": 0.5, `, ``, 1),
		"confidence > 1":     strings.Replace(validAnswer, `"confidence": 0.85`, `"confidence": 1.5`, 1),
		"score out of range": strings.Replace(validAnswer, `"score": 0.5`, `"score": 7`, 1),
		"duplicate answer":   strings.Replace(validAnswer, `"id": "ffa.target"`, `"id": "ffa.intervention"`, 1),
		"too large":          `{"rationale": "` + strings.Repeat("a", MaxOutputBytes) + `"}`,
		"too many answers":   `{"answers": [` + strings.TrimSuffix(strings.Repeat(`{"id":"x","confidence":0},`, MaxAnswers+1), ",") + `]}`,
		"huge number":        strings.Replace(validAnswer, `"score": 0.5`, `"score": 1e999`, 1),
	}
	for name, raw := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := Parse(req, []byte(raw), "claude")
			if err == nil {
				t.Fatal("Parse accepted invalid output")
			}
			if !errors.Is(err, decide.ErrInvalid) {
				t.Fatalf("error %v does not wrap decide.ErrInvalid", err)
			}
			if len(err.Error()) > 1024 {
				t.Errorf("error message is %d bytes", len(err.Error()))
			}
		})
	}
}

func TestSanitizeRationale(t *testing.T) {
	got := SanitizeRationale("  line one\n\tline\x00two  ")
	if got != "line one line two" {
		t.Errorf("got %q", got)
	}
	long := SanitizeRationale(strings.Repeat("ü", 500))
	if n := len([]rune(long)); n != MaxRationaleRunes {
		t.Errorf("rationale runes = %d, want %d", n, MaxRationaleRunes)
	}
}

func FuzzParse(f *testing.F) {
	f.Add([]byte(validAnswer))
	f.Add([]byte("```\n{}\n```"))
	f.Add([]byte(`{"answers":[{"id":"ffa.target","probabilities":[{"option":"","p":-1}]}]}`))
	req := testRequest()
	f.Fuzz(func(t *testing.T, raw []byte) {
		resp, err := Parse(req, raw, "fuzz")
		if err == nil {
			if verr := decide.Validate(req, resp); verr != nil {
				t.Fatalf("Parse returned a response Validate rejects: %v", verr)
			}
		}
	})
}

func TestSupported(t *testing.T) {
	ok := decide.Request{Questions: []decide.Question{
		{ID: "a", Kind: decide.KindChoice, Options: []string{"x"}},
		{ID: "b", Kind: decide.KindScore},
	}}
	if err := Supported(ok); err != nil {
		t.Errorf("choice+score: %v", err)
	}
	bad := ok
	bad.Questions = append(bad.Questions, decide.Question{ID: "c", Kind: decide.KindNoul, Text: "?"})
	err := Supported(bad)
	if !errors.Is(err, decide.ErrInvalid) || !errors.Is(err, ErrUnsupportedKind) {
		t.Errorf("noul: err = %v", err)
	}
}
