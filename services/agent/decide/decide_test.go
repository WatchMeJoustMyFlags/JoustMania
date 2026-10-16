package decide

import (
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
		History: []gamecontext.MovementSample{{At: base, Intensity: 0.2}, {At: base.Add(time.Second), Intensity: 1.9}}}
	c.Players["B"] = &gamecontext.PlayerSignals{Serial: "B", Active: bptr(true), MovementIntensity: fptr(1.1), SkillLevel: fptr(0.3)}
	c.Players["C"] = &gamecontext.PlayerSignals{Serial: "C", Active: bptr(false)}
	c.Timeline = []gamecontext.TimelineEvent{
		{At: base, Kind: gamecontext.EventPhase, Detail: "start"},
		{At: base.Add(20 * time.Second), Kind: gamecontext.EventElimination, Serial: "C", Order: 1},
	}
	return c
}

func TestBuildState_Fields(t *testing.T) {
	st := BuildState(sampleCtx())
	if st.GameID != "game_abc123" || st.GameKind != "real" || st.Elapsed != 30*time.Second {
		t.Fatalf("state header = %q %q %v", st.GameID, st.GameKind, st.Elapsed)
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
	if len(st.Players) != 0 || st.Elapsed != 0 || len(st.Digest) != 64 {
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
			if !strings.Contains(mag.Text, "adjust_music_tempo: 1..1.3 tempo (default 1.15)") ||
				!strings.Contains(mag.Text, "grant_shield: 3..10 seconds (default 5)") ||
				strings.Contains(mag.Text, "play_audio_cue:") {
				t.Errorf("magnitude text lists the wrong bounds:\n%s", mag.Text)
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

		{"question unknown kind", func(q *Request, _ *Response) { q.Questions[1].Kind = "noul" }, false},
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
