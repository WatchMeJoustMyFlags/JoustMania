package decide

import (
	"encoding/json"
	"errors"
	"math"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

func fptr(v float64) *float64 { return &v }
func bptr(v bool) *bool       { return &v }
func sptr(v string) *string   { return &v }

// sampleCtx is a mid-game FFA snapshot with every kind of signal the State
// builder reads, including a timeline and a movement history.
func sampleCtx() gamecontext.GameContext {
	base := time.Unix(1_700_000_000, 0)
	c := gamecontext.GameContext{
		SessionID: "game_abc123",
		GameKind:  "real",
		UpdatedAt: base.Add(30 * time.Second),
		Players:   map[string]*gamecontext.PlayerSignals{},
	}
	c.Session.DurationSeconds = fptr(30)
	c.Session.GameMode = sptr("FFA")
	c.Session.MusicTempo = fptr(1.15)
	c.Session.DeathThreshold = fptr(2.3)
	c.Session.EliminationSequence = []string{"C"}
	c.Players["A"] = &gamecontext.PlayerSignals{Serial: "A", Active: bptr(true), MovementIntensity: fptr(0.4), SkillLevel: fptr(0.8), BatteryPct: fptr(60),
		History: []gamecontext.MovementSample{{At: base.Add(28 * time.Second), Intensity: 0.2}, {At: base.Add(29 * time.Second), Intensity: 1.9}}}
	// #2514: B carries a fresh reading too, so both alive players' readings are live
	// at UpdatedAt and the state keeps their values (UC-040 A50).
	c.Players["B"] = &gamecontext.PlayerSignals{Serial: "B", Active: bptr(true), MovementIntensity: fptr(1.1), SkillLevel: fptr(0.3),
		History: []gamecontext.MovementSample{{At: base.Add(29 * time.Second), Intensity: 1.1}}}
	c.Players["C"] = &gamecontext.PlayerSignals{Serial: "C", Active: bptr(false)}
	c.Timeline = []gamecontext.TimelineEvent{
		{At: base, Kind: gamecontext.EventPhase, Detail: "start"},
		{At: base.Add(20 * time.Second), Kind: gamecontext.EventElimination, Serial: "C", Order: 1},
	}
	return c
}

func TestBuildState_Fields(t *testing.T) {
	st := BuildState(sampleCtx())
	if st.GameID != "game_abc123" || st.GameKind != "real" || st.ElapsedSeconds != 30 || st.Elapsed() != 30*time.Second {
		t.Fatalf("state header = %q %q %v", st.GameID, st.GameKind, st.ElapsedSeconds)
	}
	raw, err := json.Marshal(st)
	if err != nil || !strings.Contains(string(raw), `"elapsed_s":30`) {
		t.Errorf("JSON should carry elapsed_s in seconds: %s (%v)", raw, err)
	}
	var serials []string
	for _, p := range st.Players {
		serials = append(serials, p.Serial)
	}
	if !reflect.DeepEqual(serials, []string{"A", "B", "C"}) {
		t.Fatalf("players not sorted by serial: %v", serials)
	}
	if c := st.Players[2]; c.Alive || c.EliminationOrder != 1 {
		t.Errorf("C = %+v, want eliminated #1", c)
	}
	if !reflect.DeepEqual(st.AliveSerials(), []string{"A", "B"}) {
		t.Errorf("AliveSerials = %v", st.AliveSerials())
	}
	for _, want := range []string{
		"game: id=game_abc123 kind=real mode=FFA elapsed=30.0s",
		"session: alive=2 eliminated=1 tempo=1.15 death_threshold=2.30",
		"  A alive intensity=0.40 skill=0.80 battery=60.00 movement=",
		"  C eliminated#1",
		"timeline:\n  -30s phase: start\n  -10s elimination: C (#1)",
	} {
		if !strings.Contains(st.Narrative, want) {
			t.Errorf("narrative missing %q:\n%s", want, st.Narrative)
		}
	}
	if len(st.Digest) != 64 {
		t.Errorf("digest %q is not sha256 hex", st.Digest)
	}
}

func TestBuildState_DigestDeterministic(t *testing.T) {
	// Map iteration order is randomized per range, so repeated builds exercise it.
	want := BuildState(sampleCtx())
	for i := 0; i < 50; i++ {
		got := BuildState(sampleCtx())
		if got.Digest != want.Digest || got.Narrative != want.Narrative {
			t.Fatalf("build %d differs: %s vs %s", i, got.Digest, want.Digest)
		}
	}
}

func TestBuildState_DigestChangesWithState(t *testing.T) {
	base := BuildState(sampleCtx()).Digest
	mutations := map[string]func(*gamecontext.GameContext){
		"intensity":      func(c *gamecontext.GameContext) { c.Players["A"].MovementIntensity = fptr(0.41) },
		"tiny intensity": func(c *gamecontext.GameContext) { c.Players["A"].MovementIntensity = fptr(0.4000001) },
		"battery zero vs unset": func(c *gamecontext.GameContext) {
			c.Players["B"].BatteryPct = fptr(0)
		},
		"alive": func(c *gamecontext.GameContext) { c.Players["B"].Active = bptr(false) },
		"new player": func(c *gamecontext.GameContext) {
			c.Players["D"] = &gamecontext.PlayerSignals{Serial: "D", Active: bptr(true)}
		},
		"elapsed":   func(c *gamecontext.GameContext) { c.Session.DurationSeconds = fptr(31) },
		"tempo":     func(c *gamecontext.GameContext) { c.Session.MusicTempo = fptr(1.3) },
		"game id":   func(c *gamecontext.GameContext) { c.SessionID = "game_other" },
		"game kind": func(c *gamecontext.GameContext) { c.GameKind = "shadow" },
		"timeline": func(c *gamecontext.GameContext) {
			c.Timeline = append(c.Timeline, gamecontext.TimelineEvent{At: c.UpdatedAt, Kind: gamecontext.EventPhase, Detail: "end"})
		},
	}
	for name, mutate := range mutations {
		c := sampleCtx()
		mutate(&c)
		if BuildState(c).Digest == base {
			t.Errorf("%s: digest unchanged", name)
		}
	}
}

func TestBuildState_Empty(t *testing.T) {
	st := BuildState(gamecontext.GameContext{})
	if len(st.Players) != 0 || st.ElapsedSeconds != 0 || len(st.Digest) != 64 {
		t.Fatalf("empty state = %+v", st)
	}
	if !strings.HasPrefix(st.Narrative, "game: id=unknown kind=unknown mode=unknown elapsed=0.0s") {
		t.Errorf("narrative = %q", st.Narrative)
	}
}

func TestFFAQuestions_AlivePlayers(t *testing.T) {
	allowed := []string{"grant_shield", "adjust_music_tempo", "noop", "", "grant_shield", "play_audio_cue"}
	wantIV := []string{"noop", "adjust_music_tempo", "grant_shield", "play_audio_cue"}
	cases := []struct {
		name    string
		players map[string]bool // serial -> alive
		targets []string
	}{
		{"no players", nil, []string{"none"}},
		{"all eliminated", map[string]bool{"A": false, "B": false}, []string{"none"}},
		{"one alive", map[string]bool{"A": false, "B": true}, []string{"none", "B"}},
		{"many alive", map[string]bool{"Z": true, "A": true, "M": true, "Q": false}, []string{"none", "A", "M", "Z"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			c := gamecontext.GameContext{Players: map[string]*gamecontext.PlayerSignals{}}
			for s, alive := range tc.players {
				c.Players[s] = &gamecontext.PlayerSignals{Serial: s, Active: bptr(alive)}
			}
			qs := FFAQuestions(BuildState(c), allowed)
			if len(qs) != 3 {
				t.Fatalf("got %d questions", len(qs))
			}
			iv, tgt, mag := qs[0], qs[1], qs[2]
			if iv.ID != QuestionFFAIntervention || iv.Kind != KindChoice || !reflect.DeepEqual(iv.Options, wantIV) {
				t.Errorf("intervention question = %+v", iv)
			}
			if tgt.ID != QuestionFFATarget || tgt.Kind != KindChoice || !reflect.DeepEqual(tgt.Options, tc.targets) {
				t.Errorf("target options = %v, want %v", tgt.Options, tc.targets)
			}
			if mag.ID != QuestionFFAMagnitude || mag.Kind != KindScore || mag.Range != [2]float64{0, 1} {
				t.Errorf("magnitude question = %+v", mag)
			}
			for _, want := range []string{
				"0 = the lowest listed value and 1 = the highest",
				"adjust_music_tempo: lowest 1, highest 1.3 tempo (default 1.15)",
				"grant_shield: lowest 3, highest 10 seconds, neutral 0 (default 5)",
			} {
				if !strings.Contains(mag.Text, want) {
					t.Errorf("magnitude text missing %q:\n%s", want, mag.Text)
				}
			}
			if strings.Contains(mag.Text, "play_audio_cue:") || strings.Contains(mag.Text, "set_player_handicap:") {
				t.Errorf("magnitude text lists interventions that are unbounded or not allowed:\n%s", mag.Text)
			}
			// The question set itself must be well formed for Validate.
			for _, q := range qs {
				if err := validateQuestion(q); err != nil {
					t.Errorf("question %s invalid: %v", q.ID, err)
				}
			}
		})
	}
}

func TestFFAQuestions_EmptyAllowList(t *testing.T) {
	qs := FFAQuestions(State{}, nil)
	if !reflect.DeepEqual(qs[0].Options, []string{"noop"}) {
		t.Fatalf("options = %v, want [noop]", qs[0].Options)
	}
}

func TestFFAQuestions_MagnitudeTextPerIntervention(t *testing.T) {
	qs := FFAQuestions(State{}, []string{"set_player_handicap", "adjust_global_sensitivity", "adjust_volume"})
	for _, want := range []string{
		"set_player_handicap: lowest 0.75, highest 1.75 factor, neutral 1 (default 1.25)",
		"adjust_global_sensitivity: lowest 0, highest 4 level, whole numbers only (default 2)",
		"adjust_volume: lowest 0.3, highest 1 volume (default 0.7)",
	} {
		if !strings.Contains(qs[2].Text, want) {
			t.Errorf("magnitude text missing %q:\n%s", want, qs[2].Text)
		}
	}
}

func TestMagnitude_ResolveAndNormalize(t *testing.T) {
	for _, iv := range MagnitudeInterventions() {
		b, _ := MagnitudeBounds(iv)
		if v, _ := ResolveMagnitude(iv, 0); v != b.Min {
			t.Errorf("%s: resolve(0) = %v, want %v", iv, v, b.Min)
		}
		if v, _ := ResolveMagnitude(iv, 1); v != b.Max {
			t.Errorf("%s: resolve(1) = %v, want %v", iv, v, b.Max)
		}
		n, _ := NormalizeMagnitude(iv, b.Default)
		if v, _ := ResolveMagnitude(iv, n); math.Abs(v-b.Default) > 1e-9 {
			t.Errorf("%s: default round-trips to %v, want %v", iv, v, b.Default)
		}
	}
	// Out-of-range and non-finite scores clamp into the bounds.
	for _, s := range []float64{-5, 7, math.NaN(), math.Inf(1)} {
		v, ok := ResolveMagnitude("grant_shield", s)
		if !ok || v < 3 || v > 10 {
			t.Errorf("resolve(%v) = %v, %v; want within [3,10]", s, v, ok)
		}
	}
	if _, ok := ResolveMagnitude("end_game", 0.5); ok {
		t.Error("end_game takes no magnitude")
	}
	if _, ok := NormalizeMagnitude("noop", 1); ok {
		t.Error("noop takes no magnitude")
	}
}

// validPair is a well-formed FFA request and a response that passes Validate.
func validPair() (Request, Response) {
	req := Request{Questions: []Question{
		{ID: "iv", Kind: KindChoice, Options: []string{"noop", "grant_shield"}},
		{ID: "mag", Kind: KindScore, Range: [2]float64{0, 1}},
	}}
	resp := Response{Answers: []Answer{
		{QuestionID: "iv", Choice: "grant_shield", Probabilities: map[string]float64{"noop": 0.2, "grant_shield": 0.8}, Confidence: 0.6},
		{QuestionID: "mag", Score: 1, Confidence: 0},
	}}
	return req, resp
}

func TestValidate(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(*Request, *Response)
		ok     bool
	}{
		{"valid", func(*Request, *Response) {}, true},
		{"rounded probabilities within tolerance", func(_ *Request, r *Response) {
			r.Answers[0].Probabilities = map[string]float64{"noop": 0.333, "grant_shield": 0.6665}
		}, true},
		{"zero-probability option omitted", func(_ *Request, r *Response) {
			r.Answers[0].Probabilities = map[string]float64{"grant_shield": 1}
		}, true},
		{"score at lower bound", func(_ *Request, r *Response) { r.Answers[1].Score = 0 }, true},
		{"answers out of order", func(_ *Request, r *Response) {
			r.Answers[0], r.Answers[1] = r.Answers[1], r.Answers[0]
		}, true},

		{"missing answer", func(_ *Request, r *Response) { r.Answers = r.Answers[:1] }, false},
		{"duplicate answer", func(_ *Request, r *Response) { r.Answers = append(r.Answers, r.Answers[1]) }, false},
		{"unknown question answered", func(_ *Request, r *Response) {
			r.Answers = append(r.Answers, Answer{QuestionID: "other"})
		}, false},
		{"choice not in options", func(_ *Request, r *Response) { r.Answers[0].Choice = "end_game" }, false},
		{"empty choice", func(_ *Request, r *Response) { r.Answers[0].Choice = "" }, false},
		{"no probabilities", func(_ *Request, r *Response) { r.Answers[0].Probabilities = nil }, false},
		{"probability for unknown option", func(_ *Request, r *Response) {
			r.Answers[0].Probabilities = map[string]float64{"noop": 0.2, "end_game": 0.8}
		}, false},
		{"negative probability", func(_ *Request, r *Response) {
			r.Answers[0].Probabilities = map[string]float64{"noop": -0.2, "grant_shield": 1.2}
		}, false},
		{"NaN probability", func(_ *Request, r *Response) {
			r.Answers[0].Probabilities = map[string]float64{"noop": math.NaN(), "grant_shield": 1}
		}, false},
		{"probabilities sum below 1", func(_ *Request, r *Response) {
			r.Answers[0].Probabilities = map[string]float64{"noop": 0.2, "grant_shield": 0.7}
		}, false},
		{"probabilities sum above 1", func(_ *Request, r *Response) {
			r.Answers[0].Probabilities = map[string]float64{"noop": 0.5, "grant_shield": 0.502}
		}, false},
		{"confidence above 1", func(_ *Request, r *Response) { r.Answers[0].Confidence = 1.01 }, false},
		{"confidence negative", func(_ *Request, r *Response) { r.Answers[1].Confidence = -0.1 }, false},
		{"confidence NaN", func(_ *Request, r *Response) { r.Answers[1].Confidence = math.NaN() }, false},
		{"score above range", func(_ *Request, r *Response) { r.Answers[1].Score = 1.0001 }, false},
		{"score below range", func(_ *Request, r *Response) { r.Answers[1].Score = -0.5 }, false},
		{"score infinite", func(_ *Request, r *Response) { r.Answers[1].Score = math.Inf(1) }, false},
		{"score with probabilities", func(_ *Request, r *Response) {
			r.Answers[1].Probabilities = map[string]float64{"x": 1}
		}, false},
		{"score with choice", func(_ *Request, r *Response) { r.Answers[1].Choice = "x" }, false},
		{"score NaN", func(_ *Request, r *Response) { r.Answers[1].Score = math.NaN() }, false},
		{"confidence Inf", func(_ *Request, r *Response) { r.Answers[0].Confidence = math.Inf(1) }, false},
		{"Inf probability", func(_ *Request, r *Response) {
			r.Answers[0].Probabilities = map[string]float64{"noop": math.Inf(1), "grant_shield": 1}
		}, false},
		{"choice with probability 0", func(_ *Request, r *Response) {
			r.Answers[0].Probabilities = map[string]float64{"noop": 1, "grant_shield": 0}
		}, false},
		{"choice not the argmax", func(_ *Request, r *Response) {
			r.Answers[0].Probabilities = map[string]float64{"noop": 0.6, "grant_shield": 0.4}
		}, false},
		{"choice contradicts certain noop", func(q *Request, r *Response) {
			q.Questions[0].Options = []string{"noop", "end_game"}
			r.Answers[0] = Answer{QuestionID: "iv", Choice: "end_game", Probabilities: map[string]float64{"noop": 1}, Confidence: 1}
		}, false},
		{"choice with a score", func(_ *Request, r *Response) { r.Answers[0].Score = 0.5 }, false},
		{"choice with NaN score", func(_ *Request, r *Response) { r.Answers[0].Score = math.NaN() }, false},
		{"tie within tolerance is an argmax", func(_ *Request, r *Response) {
			r.Answers[0].Probabilities = map[string]float64{"noop": 0.5004, "grant_shield": 0.4996}
		}, true},
		{"confidence above p(choice) is allowed", func(_ *Request, r *Response) {
			r.Answers[0].Probabilities = map[string]float64{"noop": 0.4, "grant_shield": 0.6}
			r.Answers[0].Confidence = 0.95
		}, true},

		{"question unknown kind", func(q *Request, _ *Response) { q.Questions[1].Kind = "boolean" }, false},
		{"score answer with a noul", func(_ *Request, r *Response) { r.Answers[1].Noul = fptr(0.5) }, false},
		{"choice answer with a noul", func(_ *Request, r *Response) { r.Answers[0].Noul = fptr(0.5) }, false},
		{"question duplicate id", func(q *Request, r *Response) {
			q.Questions[1].ID = "iv"
			r.Answers[1].QuestionID = "iv"
		}, false},
		{"choice without options", func(q *Request, _ *Response) { q.Questions[0].Options = nil }, false},
		{"duplicate option", func(q *Request, _ *Response) {
			q.Questions[0].Options = []string{"noop", "grant_shield", "noop"}
		}, false},
		{"inverted range", func(q *Request, _ *Response) { q.Questions[1].Range = [2]float64{1, 0} }, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req, resp := validPair()
			tc.mutate(&req, &resp)
			err := Validate(req, resp)
			if tc.ok && err != nil {
				t.Fatalf("Validate = %v, want nil", err)
			}
			if !tc.ok {
				if err == nil {
					t.Fatal("Validate = nil, want an error")
				}
				if !errors.Is(err, ErrInvalid) {
					t.Fatalf("error %v does not wrap ErrInvalid", err)
				}
			}
		})
	}
}

func TestValidate_TruncatesBackendStrings(t *testing.T) {
	req, resp := validPair()
	resp.Answers[0].Choice = strings.Repeat("x", 10_000)
	err := Validate(req, resp)
	if err == nil {
		t.Fatal("want an error")
	}
	if len(err.Error()) > 300 {
		t.Fatalf("error message is %d bytes; backend strings must be truncated", len(err.Error()))
	}
}

func TestMagnitude_IntegerRounds(t *testing.T) {
	cases := map[float64]float64{0: 0, 0.1: 0, 0.13: 1, 0.5: 2, 0.62: 2, 0.63: 3, 0.9: 4, 1: 4}
	for score, want := range cases {
		if v, _ := ResolveMagnitude("adjust_global_sensitivity", score); v != want {
			t.Errorf("adjust_global_sensitivity: resolve(%v) = %v, want %v", score, v, want)
		}
	}
	// Non-integer flags are not rounded.
	if v, _ := ResolveMagnitude("adjust_music_tempo", 0.5); math.Abs(v-1.15) > 1e-9 {
		t.Errorf("tempo resolve(0.5) = %v, want 1.15", v)
	}
}

// TestValidate_RetiredInterventionIDIsInvalid pins the #1838 decision that there
// is no answer-side alias: a backend that still answers with the retired id
// partial_shield (renamed toughness_boost) gets invalid_answer, not a silent remap.
// UC: UC-040 main step 6
func TestValidate_RetiredInterventionIDIsInvalid(t *testing.T) {
	req := Request{Questions: []Question{
		{ID: "iv", Kind: KindChoice, Options: []string{"noop", "toughness_boost"}},
	}}
	ok := Response{Answers: []Answer{{QuestionID: "iv", Choice: "toughness_boost",
		Probabilities: map[string]float64{"noop": 0.2, "toughness_boost": 0.8}, Confidence: 0.5}}}
	if err := Validate(req, ok); err != nil {
		t.Fatalf("the new id must validate: %v", err)
	}
	old := Response{Answers: []Answer{{QuestionID: "iv", Choice: "partial_shield",
		Probabilities: map[string]float64{"noop": 0.2, "partial_shield": 0.8}, Confidence: 0.5}}}
	if err := Validate(req, old); !errors.Is(err, ErrInvalid) {
		t.Fatalf("retired id answer: err = %v, want ErrInvalid", err)
	}
}

func TestValidate_EmptyRequest(t *testing.T) {
	if err := Validate(Request{}, Response{}); err != nil {
		t.Fatalf("no questions, no answers: %v", err)
	}
}

func TestResponse_Find(t *testing.T) {
	_, resp := validPair()
	if a, ok := resp.Find("mag"); !ok || a.Score != 1 {
		t.Fatalf("Find(mag) = %+v, %v", a, ok)
	}
	if _, ok := resp.Find("nope"); ok {
		t.Fatal("Find(nope) found something")
	}
}

// TestValidate_Noul covers the KindNoul question (#1229): Text only, answered
// with a finite P(yes) in [0,1] and nothing else.
// UC: UC-040 A12
func TestValidate_Noul(t *testing.T) {
	req := Request{Questions: []Question{{ID: "yn", Kind: KindNoul, Text: "Is the game close?"}}}
	cases := []struct {
		name string
		a    Answer
		ok   bool
	}{
		{"valid", Answer{QuestionID: "yn", Noul: fptr(0.8), Confidence: 0.6}, true},
		{"zero is a valid noul", Answer{QuestionID: "yn", Noul: fptr(0), Confidence: 1}, true},
		{"one is a valid noul", Answer{QuestionID: "yn", Noul: fptr(1), Confidence: 1}, true},
		{"missing noul", Answer{QuestionID: "yn", Confidence: 0.5}, false},
		{"noul above 1", Answer{QuestionID: "yn", Noul: fptr(1.01)}, false},
		{"noul negative", Answer{QuestionID: "yn", Noul: fptr(-0.01)}, false},
		{"noul NaN", Answer{QuestionID: "yn", Noul: fptr(math.NaN())}, false},
		{"noul Inf", Answer{QuestionID: "yn", Noul: fptr(math.Inf(1))}, false},
		{"noul with choice", Answer{QuestionID: "yn", Noul: fptr(0.5), Choice: "yes"}, false},
		{"noul with probabilities", Answer{QuestionID: "yn", Noul: fptr(0.5), Probabilities: map[string]float64{"yes": 1}}, false},
		{"noul with score", Answer{QuestionID: "yn", Noul: fptr(0.5), Score: 0.5}, false},
		{"noul confidence above 1", Answer{QuestionID: "yn", Noul: fptr(0.5), Confidence: 1.5}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := Validate(req, Response{Answers: []Answer{tc.a}})
			if (err == nil) != tc.ok {
				t.Fatalf("Validate = %v, want ok=%v", err, tc.ok)
			}
			if err != nil && !errors.Is(err, ErrInvalid) {
				t.Errorf("error does not wrap ErrInvalid: %v", err)
			}
		})
	}
}

// TestAnswerJSON_NoulOmitted: choice and score answers serialize exactly as
// before the noul kind existed (no "noul" key).
func TestAnswerJSON_NoulOmitted(t *testing.T) {
	b, err := json.Marshal(Answer{QuestionID: "iv", Choice: "noop", Probabilities: map[string]float64{"noop": 1}, Confidence: 1})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(b), "noul") {
		t.Errorf("choice answer JSON carries a noul: %s", b)
	}
	b, _ = json.Marshal(Answer{QuestionID: "yn", Noul: fptr(0), Confidence: 1})
	if !strings.Contains(string(b), `"noul":0`) {
		t.Errorf("noul 0 must serialize: %s", b)
	}
}

// TestBuildState_ElapsedFromLiveStore checks that the elapsed_s a backend (Jev,
// Claude, rules) is given mid-game comes from a real Store snapshot: the live
// game_duration_seconds gauge when it advances, and the time since the game
// started when the gauge is missing (#1305: it used to be 0 for the whole game).
//
// UC: UC-041 main A3 BR-003
func TestBuildState_ElapsedFromLiveStore(t *testing.T) {
	newStore := func() (*gamecontext.Store, *time.Time) {
		now := time.Unix(5_000, 0)
		s := gamecontext.NewStore(time.Hour, time.Hour, func() time.Time { return now })
		s.SetGameActive(true)
		return s, &now
	}

	t.Run("UC-041/main elapsed_s comes from the live gauge", func(t *testing.T) {
		s, now := newStore()
		*now = now.Add(12 * time.Second)
		s.SetSessionDuration(12.5)
		st := BuildState(s.Snapshot())
		if st.ElapsedSeconds != 12.5 {
			t.Fatalf("ElapsedSeconds = %v, want 12.5", st.ElapsedSeconds)
		}
		if !strings.Contains(st.Narrative, "elapsed=12.5s") {
			t.Fatalf("narrative should carry the live elapsed time: %q", st.Narrative)
		}
	})

	t.Run("UC-041/A3 elapsed_s falls back to time since start without a gauge", func(t *testing.T) {
		s, now := newStore()
		*now = now.Add(45 * time.Second)
		st := BuildState(s.Snapshot())
		if st.ElapsedSeconds != 45 {
			t.Fatalf("ElapsedSeconds = %v, want 45 (fallback)", st.ElapsedSeconds)
		}
		raw, err := json.Marshal(st)
		if err != nil || !strings.Contains(string(raw), `"elapsed_s":45`) {
			t.Fatalf("JSON should carry elapsed_s 45: %s (%v)", raw, err)
		}
	})
}
