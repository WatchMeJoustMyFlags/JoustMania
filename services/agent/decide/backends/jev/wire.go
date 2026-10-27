package jev

// wire.go is the ONLY file that knows Jev's wire format (TypeSafe's System One
// API, https://docs.typesafe.ai/api). criteria.go holds the natural-language
// option/level descriptions it sends. Everything else in this package (jev.go:
// availability, timeout, HTTP call, span, metrics) is format-agnostic and talks
// to this file through:
//
//	wirePath        the path appended to Config.BaseURL
//	setAuth         the auth header(s)
//	encodeRequest   decide.Request -> request body (+ what to record as content)
//	decodeResponse  2xx response body -> decide.Response (+ telemetry fields)
//
// The API is typed, not prompt-based:
//
//	POST {base_url}/systemone   Authorization: Bearer <JEV_API_KEY>
//	{"model": "jev-latest", "state": {...}, "questions": {"<id>": {type, instructions, criteria}}}
//	-> {"model": "jev-1.13.0", "answers": {"<id>": {...}}, "usage": {input_tokens, output_tokens}}
//
// Mapping of our contract (decide.Question kinds) to Jev's primitives:
//
//	KindChoice -> "choice": criteria = ordered {option: description} (criteria.go);
//	              answer {choice, probabilities (every option, sum 1), confidence}.
//	KindScore  -> "score":  criteria = an ordered ladder of N level
//	              descriptions; the answer is Jev's MOST LIKELY level (argmax
//	              of its per-level probabilities, a tie going to the gentler
//	              level: mostLikelyLevel), not the expected score s in [0, N-1]
//	              (#1281: s lands between modes, e.g. 0.29 / 0.28 / 0.30 gives
//	              s=1.3, rounded to level 1 although level 2 is most likely).
//	              Only without level probabilities is s rounded to the nearest
//	              level. The level's position maps onto Question.Range:
//	              lo + l/(N-1)*(hi-lo). A
//	              details-stage ffa.magnitude uses the chosen intervention's own
//	              levels (criteria.go magnitudeLevels), so its answer is the
//	              normalized position of a concrete value of that lever.
//	KindNoul   -> "noul":   criteria {true, false} when criteria.go has them
//	              (the gate, #1281), else none; answer {noul: P(yes)}.
//
// Confidence. decide's contract is "confidence that ACTING on the answer is
// right"; Jev's is the peakedness of the distribution (choice:
// (p_max-1/n)/(1-1/n), score: 1 - spread/MAD_uniform). We keep Jev's native
// confidence for every question EXCEPT ffa.intervention, where we use the same
// acting-confidence as the rules adapter, computed from Jev's calibrated
// probabilities: 1 - p(noop) when the choice is an intervention, p(noop) when it
// is noop. Jev's peakedness would punish two competing interventions that
// together dominate noop (the case the contract explicitly calls a confident
// call to act), and with ~15 options it collapses to roughly p_max. Jev's own
// value is still recorded on the chat span (jev.native_confidence). A noul has
// no Jev confidence; we use |2p-1|, the formula TypeSafe documents for it.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"sort"
	"strconv"
	"strings"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/decide/llmio"
)

// wirePath is the System One evaluation endpoint, relative to
// decision.jev.base_url (https://api.typesafe.ai/v1 by default).
const wirePath = "/systemone"

// requestIDHeader carries Jev's request id (there is none in the body); it
// becomes gen_ai.response.id.
const requestIDHeader = "X-Typesafe-Request-Id"

func setAuth(h http.Header, apiKey string) {
	h.Set("Authorization", "Bearer "+apiKey)
}

// Jev answer/question type names.
const (
	typeChoice = "choice"
	typeScore  = "score"
	typeNoul   = "noul"
)

// wireRequestOut is what encodeRequest hands to jev.go.
type wireRequestOut struct {
	// Body is the JSON request body.
	Body []byte
	// Content is what gen_ai.input.messages records when content capture is on:
	// the request body as the user message (the API has no prompt).
	Content llmio.Prompt
}

// wireResult is what decodeResponse hands back to jev.go.
type wireResult struct {
	// Response is the mapped and validated answer.
	Response decide.Response
	// Model is gen_ai.response.model (the versioned id, e.g. jev-1.13.0).
	Model string
	// Usage is nil when Jev reports no token usage.
	Usage *wireUsage
	// Text is the raw answers JSON, for the gen_ai.output.messages capture.
	Text string
	// NativeConfidence is Jev's own confidence per question id (choice and
	// score only), recorded on the span as jev.native_confidence.
	NativeConfidence map[string]float64
}

type wireUsage struct {
	InputTokens, OutputTokens int
}

// --- request ---

type jevRequest struct {
	Model     string                 `json:"model"`
	State     jevState               `json:"state"`
	Questions map[string]jevQuestion `json:"questions"`
}

// jevState is decide.State as Jev's structured state. It never carries
// Request.Context, and leaves out the digest and game id (noise to the model).
type jevState struct {
	Game string `json:"game"`
	// ChosenIntervention is set in the details stage (#1262): the intervention
	// the agent will apply, which target and magnitude are conditioned on.
	ChosenIntervention string               `json:"chosen_intervention,omitempty"`
	GameKind           string               `json:"game_kind,omitempty"`
	ElapsedSeconds     float64              `json:"elapsed_s"`
	AlivePlayers       []string             `json:"alive_players"`
	Players            []decide.PlayerState `json:"players"`
	Narrative          string               `json:"narrative"`
	// MostHeadroom / Struggling are decide.State's session-level hints (#1262).
	MostHeadroom string `json:"most_headroom,omitempty"`
	Struggling   string `json:"struggling,omitempty"`
	// Hold mode only (#2096): decide.State's latched hold values; absent otherwise.
	ShieldActivation   string   `json:"shield_activation,omitempty"`
	ShieldEnergyMaxS   *float64 `json:"shield_energy_max_s,omitempty"`
	ShieldBudgetS      *float64 `json:"shield_budget_s,omitempty"`
	ShieldRechargePerS *float64 `json:"shield_recharge_per_s,omitempty"`
	ShieldMinBurstS    *float64 `json:"shield_min_burst_s,omitempty"`
}

type jevQuestion struct {
	Type         string `json:"type"`
	Instructions string `json:"instructions"`
	// Criteria is an orderedCriteria (choice), a []string ladder (score), a
	// noulCriterion (a noul with criteria, e.g. the gate) or nil.
	Criteria any `json:"criteria,omitempty"`
}

// orderedCriteria is a choice's {option: description} map serialized in
// Question.Options order (Go maps marshal sorted). Order matters: Jev 1.13 leans
// toward the first option, and ours is noop / none, the conservative one.
// An empty description is sent as null ("needs no extra detail").
type orderedCriteria []criterion

type criterion struct{ option, description string }

func (c orderedCriteria) MarshalJSON() ([]byte, error) {
	var b bytes.Buffer
	b.WriteByte('{')
	for i, kv := range c {
		if i > 0 {
			b.WriteByte(',')
		}
		k, err := json.Marshal(kv.option)
		if err != nil {
			return nil, err
		}
		b.Write(k)
		b.WriteByte(':')
		if kv.description == "" {
			b.WriteString("null")
			continue
		}
		v, err := json.Marshal(kv.description)
		if err != nil {
			return nil, err
		}
		b.Write(v)
	}
	b.WriteByte('}')
	return b.Bytes(), nil
}

// maxChoiceOptions is Jev's documented per-choice option limit.
const maxChoiceOptions = 255

// encodeRequest maps req (State + Questions only, never Context) to Jev's
// request body. chosen is the intervention a details-stage request is
// conditioned on ("" otherwise).
func encodeRequest(model string, req decide.Request, chosen string) (wireRequestOut, error) {
	questions := make(map[string]jevQuestion, len(req.Questions))
	for _, q := range req.Questions {
		jq, err := encodeQuestion(q, req, chosen)
		if err != nil {
			return wireRequestOut{}, fmt.Errorf("jev: %w: question %q: %v", decide.ErrInvalid, q.ID, err)
		}
		questions[q.ID] = jq
	}
	body, err := json.Marshal(jevRequest{Model: model, State: encodeState(req.State, chosen), Questions: questions})
	if err != nil {
		return wireRequestOut{}, fmt.Errorf("jev: marshal request: %w", err)
	}
	return wireRequestOut{Body: body, Content: llmio.Prompt{User: string(body)}}, nil
}

func encodeQuestion(q decide.Question, req decide.Request, chosen string) (jevQuestion, error) {
	switch q.Kind {
	case decide.KindChoice:
		if len(q.Options) == 0 || len(q.Options) > maxChoiceOptions {
			return jevQuestion{}, fmt.Errorf("%d options, want 1..%d", len(q.Options), maxChoiceOptions)
		}
		return jevQuestion{Type: typeChoice, Instructions: q.Text, Criteria: choiceCriteria(q, req.State, chosen)}, nil
	case decide.KindScore:
		instructions, ladder := scoreLadder(q, req, chosen)
		return jevQuestion{Type: typeScore, Instructions: instructions, Criteria: ladder}, nil
	case decide.KindNoul:
		jq := jevQuestion{Type: typeNoul, Instructions: q.Text}
		if c, ok := noulCriteria[q.ID]; ok {
			jq.Criteria = c
		}
		return jq, nil
	default:
		return jevQuestion{}, fmt.Errorf("unsupported question kind %q", clip(string(q.Kind), 32))
	}
}

// encodeState copies st into the wire state. Jev rejects non-finite numbers
// (400), so an unobservable NaN/Inf reading is dropped like a missing one.
func encodeState(st decide.State, chosen string) jevState {
	players := make([]decide.PlayerState, len(st.Players))
	for i, p := range st.Players {
		p.Intensity = finiteOrNil(p.Intensity)
		p.Variance = finiteOrNil(p.Variance)
		p.Skill = finiteOrNil(p.Skill)
		p.BatteryPct = finiteOrNil(p.BatteryPct)
		p.ShieldEnergyS = finiteOrNil(p.ShieldEnergyS)
		p.ShieldBudgetRemainingS = finiteOrNil(p.ShieldBudgetRemainingS)
		players[i] = p
	}
	alive := st.AliveSerials()
	if alive == nil {
		alive = []string{}
	}
	elapsed := st.ElapsedSeconds
	if !isFinite(elapsed) {
		elapsed = 0
	}
	return jevState{
		Game:           gameDescription,
		GameKind:       st.GameKind,
		ElapsedSeconds: elapsed,
		AlivePlayers:   alive,
		Players:        players,
		Narrative:      st.Narrative,
		MostHeadroom:   st.MostHeadroom,
		Struggling:     st.Struggling,

		ShieldActivation:   st.ShieldActivation,
		ShieldEnergyMaxS:   finiteOrNil(st.ShieldEnergyMaxS),
		ShieldBudgetS:      finiteOrNil(st.ShieldBudgetS),
		ShieldRechargePerS: finiteOrNil(st.ShieldRechargePerS),
		ShieldMinBurstS:    finiteOrNil(st.ShieldMinBurstS),

		ChosenIntervention: chosen,
	}
}

func finiteOrNil(v *float64) *float64 {
	if v == nil || !isFinite(*v) {
		return nil
	}
	return v
}

func isFinite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }

// --- response ---

type jevResponse struct {
	Model   string                     `json:"model"`
	Answers map[string]json.RawMessage `json:"answers"`
	Usage   *struct {
		InputTokens  *int `json:"input_tokens"`
		OutputTokens *int `json:"output_tokens"`
	} `json:"usage"`
}

type jevAnswer struct {
	Type   string  `json:"type"`
	Choice *string `json:"choice"`
	// Probabilities is per option (choice) or per level index "0".."N-1"
	// (score).
	Probabilities map[string]float64 `json:"probabilities"`
	Confidence    *float64           `json:"confidence"`
	Score         *float64           `json:"score"`
	Noul          *float64           `json:"noul"`
}

// renormalizeTolerance is the minimum rescale tolerance. Jev's probabilities
// are floats rounded to two decimals (observed on jev-1.13.0), so over n options
// their sum can miss 1 by up to n*roundingSlack; a sum within
// max(renormalizeTolerance, n*roundingSlack) of 1 is rescaled before
// decide.Validate's tighter check. A sum further off is left for Validate to
// reject. Rescaling is safe because toAnswer first requires every option to be
// present (no missing mass is silently redistributed).
const (
	renormalizeTolerance = 5e-2
	roundingSlack        = 5e-3
)

// exactSumTolerance: a sum this close to 1 is left as is (no float noise from
// rescaling an already normalized distribution).
const exactSumTolerance = 1e-9

// decodeResponse maps a 2xx response body to a validated decide.Response.
// On failure it returns a non-empty low-cardinality errType (the span's
// error.type) and an error; malformed or invalid answers wrap
// decide.ErrInvalid. The partially filled wireResult is still returned so the
// span gets whatever telemetry the body carried.
func decodeResponse(req decide.Request, chosen string, body []byte) (wireResult, string, error) {
	var r jevResponse
	if err := json.Unmarshal(body, &r); err != nil {
		return wireResult{}, "invalid_response", fmt.Errorf("jev: %w: unparseable response body: %v", llmio.ErrMalformed, err)
	}
	res := wireResult{Model: r.Model, NativeConfidence: map[string]float64{}}
	if u := r.Usage; u != nil {
		res.Usage = &wireUsage{}
		if u.InputTokens != nil {
			res.Usage.InputTokens = *u.InputTokens
		}
		if u.OutputTokens != nil {
			res.Usage.OutputTokens = *u.OutputTokens
		}
	}
	if raw, err := json.Marshal(r.Answers); err == nil && r.Answers != nil {
		res.Text = string(raw)
	}
	if len(r.Answers) == 0 {
		return res, "no_answers", fmt.Errorf("jev: %w: no answers", llmio.ErrMalformed)
	}

	resp := decide.Response{Backend: Name, Answers: make([]decide.Answer, 0, len(req.Questions))}
	for _, q := range req.Questions {
		raw, ok := r.Answers[q.ID]
		if !ok {
			return res, "invalid_output", fmt.Errorf("jev: %w: question %q not answered", decide.ErrInvalid, q.ID)
		}
		var ja jevAnswer
		if err := json.Unmarshal(raw, &ja); err != nil {
			return res, "invalid_output", fmt.Errorf("jev: %w: answer %q: %v", llmio.ErrMalformed, q.ID, err)
		}
		a, native, err := toAnswer(q, ja, chosen)
		if err != nil {
			return res, "invalid_output", fmt.Errorf("jev: %w: answer %q: %v", decide.ErrInvalid, q.ID, err)
		}
		if native != nil {
			res.NativeConfidence[q.ID] = *native
		}
		resp.Answers = append(resp.Answers, a)
	}
	if err := decide.Validate(req, resp); err != nil {
		return res, "invalid_output", fmt.Errorf("jev: %w", err)
	}
	res.Response = resp
	return res, "", nil
}

// toAnswer maps one Jev answer onto q's decide.Answer. native is Jev's own
// confidence (nil for a noul, which has none). chosen is the intervention of a
// details-stage request ("" otherwise).
func toAnswer(q decide.Question, ja jevAnswer, chosen string) (decide.Answer, *float64, error) {
	a := decide.Answer{QuestionID: q.ID}
	switch q.Kind {
	case decide.KindChoice:
		if ja.Type != typeChoice || ja.Choice == nil || ja.Confidence == nil {
			return a, nil, fmt.Errorf("want a choice answer with choice and confidence, got type %q", clip(ja.Type, 32))
		}
		if err := coversOptions(q, ja.Probabilities); err != nil {
			return a, nil, err
		}
		a.Choice = *ja.Choice
		a.Probabilities = renormalize(ja.Probabilities)
		a.Confidence = *ja.Confidence
		if q.ID == decide.QuestionFFAIntervention {
			a.Confidence = actingConfidence(a.Choice, a.Probabilities)
		}
		return a, ja.Confidence, nil
	case decide.KindScore:
		if ja.Type != typeScore || ja.Score == nil || ja.Confidence == nil {
			return a, nil, fmt.Errorf("want a score answer with score and confidence, got type %q", clip(ja.Type, 32))
		}
		s, err := scoreToRange(*ja.Score, ja.Probabilities, q, chosen)
		if err != nil {
			return a, nil, err
		}
		a.Score = s
		a.Confidence = *ja.Confidence
		return a, ja.Confidence, nil
	case decide.KindNoul:
		if ja.Type != typeNoul || ja.Noul == nil {
			return a, nil, fmt.Errorf("want a noul answer, got type %q", clip(ja.Type, 32))
		}
		p := *ja.Noul
		a.Noul = &p
		// Jev returns no confidence for a noul; |2p-1| is its documented
		// equivalent (the choice formula for two outcomes). A NaN p is left to
		// decide.Validate.
		a.Confidence = math.Abs(2*p - 1)
		return a, nil, nil
	}
	return a, nil, fmt.Errorf("unsupported question kind %q", clip(string(q.Kind), 32))
}

// actingConfidence is ffa.intervention's confidence that acting on the choice
// is right, the rules adapter's definition: 1 - p(noop) for an intervention,
// p(noop) for noop.
func actingConfidence(choice string, probs map[string]float64) float64 {
	pNoop := probs[decide.OptionNoop]
	c := pNoop
	if choice != decide.OptionNoop {
		c = 1 - pNoop
	}
	return math.Max(0, math.Min(1, c))
}

// coversOptions requires a choice answer's probabilities to cover exactly q's
// options. Jev documents "every option mapped to its probability"; a missing
// option would otherwise read as p=0 (decide.Validate allows omitted options),
// and a missing noop would turn ffa.intervention's acting confidence
// 1 - p(noop) into a false 1.0.
func coversOptions(q decide.Question, probs map[string]float64) error {
	if len(probs) != len(q.Options) {
		return fmt.Errorf("probabilities cover %d options, want all %d", len(probs), len(q.Options))
	}
	for _, o := range q.Options {
		if _, ok := probs[o]; !ok {
			return fmt.Errorf("no probability for option %q", clip(o, 64))
		}
	}
	return nil
}

// renormalize rescales a distribution whose sum is within
// max(renormalizeTolerance, n*roundingSlack) of 1; anything else is left for
// decide.Validate to reject.
func renormalize(in map[string]float64) map[string]float64 {
	if len(in) == 0 {
		return in
	}
	tolerance := math.Max(renormalizeTolerance, float64(len(in))*roundingSlack)
	var sum float64
	for _, p := range in {
		sum += p
	}
	if !isFinite(sum) || sum <= 0 || math.Abs(sum-1) <= exactSumTolerance || math.Abs(sum-1) > tolerance {
		return in
	}
	out := make(map[string]float64, len(in))
	for k, p := range in {
		out[k] = p / sum
	}
	return out
}

// scoreEpsilon tolerates float noise at the ends of Jev's [0, N-1] score.
const scoreEpsilon = 1e-6

// snapLevel range-checks Jev's expected score s in [0, n-1] and rounds it to
// the nearest level (half rounds up, math.Round). It is the fallback when an
// answer carries no level probabilities.
func snapLevel(s float64, n int) (int, error) {
	top := float64(n - 1)
	if !isFinite(s) || s < -scoreEpsilon || s > top+scoreEpsilon {
		return 0, fmt.Errorf("score %v outside [0, %v]", s, top)
	}
	return int(math.Max(0, math.Min(top, math.Round(s)))), nil
}

// mostLikelyLevel is the level with the highest probability in probs (keys
// "0".."n-1", Jev's per-level distribution). A tie goes to the gentler level
// (gentler(i, j) reports whether level i is gentler than level j). probs must
// cover exactly the n levels with finite non-negative values that sum to 1
// within the choice path's rescale tolerance (max(renormalizeTolerance,
// n*roundingSlack)); an all-zero or otherwise far-off distribution is invalid,
// as it is for a choice.
func mostLikelyLevel(probs map[string]float64, n int, gentler func(i, j int) bool) (int, error) {
	if len(probs) != n {
		return 0, fmt.Errorf("level probabilities cover %d levels, want %d", len(probs), n)
	}
	best, pBest, sum := -1, 0.0, 0.0
	for i := 0; i < n; i++ {
		p, ok := probs[strconv.Itoa(i)]
		if !ok {
			return 0, fmt.Errorf("no probability for level %d", i)
		}
		if !isFinite(p) || p < 0 {
			return 0, fmt.Errorf("level %d probability %v", i, p)
		}
		sum += p
		switch {
		case best < 0, p > pBest+levelTieTolerance:
			best, pBest = i, p
		case math.Abs(p-pBest) <= levelTieTolerance && gentler(i, best):
			best, pBest = i, p
		}
	}
	if math.Abs(sum-1) > math.Max(renormalizeTolerance, float64(n)*roundingSlack) {
		return 0, fmt.Errorf("level probabilities sum to %v, want 1", sum)
	}
	return best, nil
}

// levelTieTolerance: level probabilities this close are a tie. Jev rounds to
// two decimals, so a real tie is exact; this only absorbs float noise.
const levelTieTolerance = 1e-9

// lowerIsGentler is the tie-break of a ladder without a neutral: the lower
// level (a shorter shield, a quieter volume, a lower position).
func lowerIsGentler(i, j int) bool { return i < j }

// nearerNeutral is the tie-break on intervention iv's own levels: the value
// nearer the lever's neutral ("no change"), so the smaller change; the lower
// level when the lever has no neutral or both are equally near. For a
// two-way lever's half-ladder (weakest first) that is the weaker strength.
func nearerNeutral(iv string, levels []float64) func(i, j int) bool {
	b, _ := decide.MagnitudeBounds(iv)
	if !b.HasNeutral {
		return lowerIsGentler
	}
	return func(i, j int) bool {
		di, dj := math.Abs(levels[i]-b.Neutral), math.Abs(levels[j]-b.Neutral)
		if math.Abs(di-dj) > 1e-9 {
			return di < dj
		}
		return i < j
	}
}

// pickLevel returns the level of a score answer on an n-level ladder: Jev's
// most likely level when the answer carries level probabilities, else the
// expected score s rounded to the nearest level. s is range-checked either way.
func pickLevel(s float64, probs map[string]float64, n int, gentler func(i, j int) bool) (int, error) {
	l, err := snapLevel(s, n)
	if err != nil || len(probs) == 0 {
		return l, err
	}
	return mostLikelyLevel(probs, n, gentler)
}

// scoreToRange picks Jev's most likely level of q's ladder (pickLevel) and
// maps it onto q.Range. For a details-stage ffa.magnitude the ladder is the
// chosen intervention's own values: the answer is that value's normalized
// position (decide.NormalizeMagnitude), so decide.ResolveMagnitude returns
// exactly the level's value (integer levers included). Every other score
// question uses the evenly spaced scoreLevels ladder: lo + l/(N-1)*(hi-lo).
func scoreToRange(s float64, probs map[string]float64, q decide.Question, chosen string) (float64, error) {
	lo, hi := q.Range[0], q.Range[1]
	if levels := conditionedLevels(q, chosen); levels != nil {
		l, err := pickLevel(s, probs, len(levels), nearerNeutral(chosen, levels))
		if err != nil {
			return 0, err
		}
		norm, _ := decide.NormalizeMagnitude(chosen, levels[l])
		return math.Max(lo, math.Min(hi, lo+norm*(hi-lo))), nil
	}
	l, err := pickLevel(s, probs, scoreLevels, lowerIsGentler)
	if err != nil {
		return 0, err
	}
	v := lo + float64(l)/float64(scoreLevels-1)*(hi-lo)
	return math.Max(lo, math.Min(hi, v)), nil
}

// floatMapJSON renders m as a compact JSON object with sorted keys, for the
// jev.native_confidence and jev.stage1.distribution span attributes.
func floatMapJSON(m map[string]float64) string {
	if len(m) == 0 {
		return ""
	}
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	b.WriteByte('{')
	for i, k := range keys {
		if i > 0 {
			b.WriteByte(',')
		}
		kq, _ := json.Marshal(k)
		b.Write(kq)
		b.WriteByte(':')
		b.WriteString(strconv.FormatFloat(m[k], 'g', 6, 64))
	}
	b.WriteByte('}')
	return b.String()
}

// statusErrorType is the error.type of a non-2xx response: the status code,
// except a 400 for a request over the model's context length, which Jev marks
// with max_tokens_exceeded.
func statusErrorType(status int, body []byte) string {
	if status == http.StatusBadRequest && bytes.Contains(body, []byte("max_tokens_exceeded")) {
		return "max_tokens_exceeded"
	}
	return strconv.Itoa(status)
}
