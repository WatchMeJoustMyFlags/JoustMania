// Package goal is the goal-profile half of the shared characteristics vocabulary
// (#2479, FR-201, decide-contract.md §14.4): a human-chosen set of up to four
// target bands over the measurable characteristics (services/agent/characteristics),
// the from_objectives mapping, the game-end attainment and the "goal:" / "now:"
// prompt lines.
//
// Report-only: a goal never reaches the permission chain, the allow-list, the
// budgets, the writer or the rules weights. The worst a wrong goal can do is a
// misleading attainment number or prompt line.
package goal

import (
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"

	"github.com/joustmania/agent/decide"
)

// Phases of a term: the whole game, or the endgame (at most 2 players alive).
const (
	PhaseWhole   = "whole"
	PhaseEndgame = "endgame"
)

// NameFromObjectives is the goal.profile variant (and sentinel key) that derives
// the profile from the active objectives weights.
const NameFromObjectives = "from_objectives"

// Named profiles (flags.md §3.2; decide-contract.md §14.4). NameChaos is the
// from_objectives form of chaos_focused, which has no named variant.
const (
	NameLongRunningGame = "long_running_game"
	NameLivelyFinale    = "lively_finale"
	NameBalancedContest = "balanced_contest"
	NameQuickGame       = "quick_game"
	NameChaos           = "chaos_focused"
)

// MaxTerms and MaxWeight bound a profile (flags.md §3.2).
const (
	MaxTerms  = 4
	MaxWeight = 10.0
)

// Bound is a characteristic's hard bound: every band edge lies inside it.
type Bound struct{ Lower, Upper float64 }

// HardBounds are the per-characteristic hard bounds (decide-contract.md §14.4).
var HardBounds = map[string]Bound{
	decide.CharDuration:        {0, 3600},
	decide.CharActivity:        {0, 2},
	decide.CharChange:          {0, 2},
	decide.CharEliminationPace: {0, 30},
	decide.CharCloseness:       {0, 2},
	decide.CharSurvival:        {0, 1},
}

// Term is one target band: a characteristic, a band (nil = open edge), a phase
// and a weight.
type Term struct {
	Characteristic string
	Min, Max       *float64
	Phase          string
	Weight         float64
}

// Profile is a resolved goal profile. Name is the goal.profile variant, or the
// derived profile's name when FromObjectives is set. No terms = no goal (the
// mixed objectives): nothing is scored, recorded or rendered.
type Profile struct {
	Name           string
	FromObjectives bool
	Terms          []Term
}

// Empty reports whether p has no terms.
func (p Profile) Empty() bool { return len(p.Terms) == 0 }

// Reasons a goal.profile value is invalid (the agent_goal_profile_invalid_total
// reason label).
const (
	ReasonShape          = "shape"
	ReasonUnknownKey     = "unknown_key"
	ReasonCharacteristic = "unknown_characteristic"
	ReasonBand           = "bad_band"
	ReasonPhase          = "bad_phase"
	ReasonWeight         = "bad_weight"
	ReasonTermCount      = "term_count"
)

// ParseError is an invalid goal.profile value; Reason is one of the Reason* labels.
type ParseError struct {
	Reason string
	Detail string
}

func (e *ParseError) Error() string { return "goal.profile invalid (" + e.Reason + "): " + e.Detail }

func invalid(reason, format string, args ...any) error {
	return &ParseError{Reason: reason, Detail: fmt.Sprintf(format, args...)}
}

// Parse validates one goal.profile variant value (flags.md §3.2): exactly one of
// {"terms": [...]} or the sentinel {"from_objectives": true}, no unknown keys, 1
// to MaxTerms terms over the six characteristics, phase whole (the default) or
// endgame, finite band edges inside the hard bounds with at least one edge and
// min <= max, weight finite in (0, MaxWeight] (default 1). fromObjectives reports
// the sentinel. Any violation is a *ParseError.
func Parse(raw any) (terms []Term, fromObjectives bool, err error) {
	obj, ok := raw.(map[string]any)
	if !ok {
		return nil, false, invalid(ReasonShape, "not an object: %T", raw)
	}
	for k := range obj {
		if k != "terms" && k != NameFromObjectives {
			return nil, false, invalid(ReasonUnknownKey, "key %q", k)
		}
	}
	sentinel, hasSentinel := obj[NameFromObjectives]
	rawTerms, hasTerms := obj["terms"]
	switch {
	case hasSentinel && hasTerms:
		return nil, false, invalid(ReasonShape, "both terms and from_objectives")
	case hasSentinel:
		if b, ok := sentinel.(bool); !ok || !b {
			return nil, false, invalid(ReasonShape, "from_objectives must be true")
		}
		return nil, true, nil
	case !hasTerms:
		return nil, false, invalid(ReasonShape, "neither terms nor from_objectives")
	}
	list, ok := rawTerms.([]any)
	if !ok {
		return nil, false, invalid(ReasonShape, "terms is not a list: %T", rawTerms)
	}
	if len(list) == 0 || len(list) > MaxTerms {
		return nil, false, invalid(ReasonTermCount, "%d terms, want 1 to %d", len(list), MaxTerms)
	}
	out := make([]Term, 0, len(list))
	for i, rt := range list {
		t, err := parseTerm(rt)
		if err != nil {
			pe := err.(*ParseError)
			pe.Detail = fmt.Sprintf("term %d: %s", i, pe.Detail)
			return nil, false, pe
		}
		out = append(out, t)
	}
	return out, false, nil
}

func parseTerm(raw any) (Term, error) {
	obj, ok := raw.(map[string]any)
	if !ok {
		return Term{}, invalid(ReasonShape, "not an object: %T", raw)
	}
	for k := range obj {
		switch k {
		case "characteristic", "min", "max", "phase", "weight":
		default:
			return Term{}, invalid(ReasonUnknownKey, "key %q", k)
		}
	}
	char, _ := obj["characteristic"].(string)
	bound, known := HardBounds[char]
	if !known {
		return Term{}, invalid(ReasonCharacteristic, "characteristic %v", obj["characteristic"])
	}
	t := Term{Characteristic: char, Phase: PhaseWhole, Weight: 1}
	if v, present := obj["phase"]; present {
		s, _ := v.(string)
		if s != PhaseWhole && s != PhaseEndgame {
			return Term{}, invalid(ReasonPhase, "phase %v", v)
		}
		t.Phase = s
	}
	if v, present := obj["weight"]; present {
		w, ok := number(v)
		if !ok || w <= 0 || w > MaxWeight {
			return Term{}, invalid(ReasonWeight, "weight %v", v)
		}
		t.Weight = w
	}
	var err error
	if t.Min, err = edge(obj, "min", char, bound); err != nil {
		return Term{}, err
	}
	if t.Max, err = edge(obj, "max", char, bound); err != nil {
		return Term{}, err
	}
	if t.Min == nil && t.Max == nil {
		return Term{}, invalid(ReasonBand, "%s: no band edge", char)
	}
	if t.Min != nil && t.Max != nil && *t.Min > *t.Max {
		return Term{}, invalid(ReasonBand, "%s: min %v > max %v", char, *t.Min, *t.Max)
	}
	return t, nil
}

// edge reads one band edge: absent or null is open; otherwise a finite number
// inside the characteristic's hard bound.
func edge(obj map[string]any, key, char string, b Bound) (*float64, error) {
	v, present := obj[key]
	if !present || v == nil {
		return nil, nil
	}
	f, ok := number(v)
	if !ok || f < b.Lower || f > b.Upper {
		return nil, invalid(ReasonBand, "%s %s %v outside [%v, %v]", char, key, v, b.Lower, b.Upper)
	}
	return &f, nil
}

// number accepts the numeric shapes a flag object can carry; NaN and infinities
// are not numbers here.
func number(v any) (float64, bool) {
	var f float64
	switch n := v.(type) {
	case float64:
		f = n
	case float32:
		f = float64(n)
	case int:
		f = float64(n)
	case int64:
		f = float64(n)
	default:
		return 0, false
	}
	if math.IsNaN(f) || math.IsInf(f, 0) {
		return 0, false
	}
	return f, true
}

func ptr(v float64) *float64 { return &v }

func term(char string, lo, hi *float64) Term {
	return Term{Characteristic: char, Min: lo, Max: hi, Phase: PhaseWhole, Weight: 1}
}

// clampBound keeps a fitness-flag value inside the characteristic's hard bound.
func clampBound(char string, v float64) float64 {
	b := HardBounds[char]
	if math.IsNaN(v) {
		return b.Lower
	}
	return math.Max(b.Lower, math.Min(b.Upper, v))
}

// Objective weight keys (services/flagd/agent.json objectives).
const (
	objEndurance  = "endurance"
	objBalanced   = "balanced"
	objAccelerate = "accelerate"
	objChaos      = "chaos"
)

// balancedMinDuration is balanced_contest's duration edge: no fitness flag backs it.
const balancedMinDuration = 120

// FromObjectives derives the from_objectives profile (decide-contract.md §14.4)
// from the objectives weights: the objective with the strictly largest weight
// picks it (endurance -> long_running_game, accelerate -> quick_game, balanced
// -> balanced_contest, chaos -> chaos_focused); a tie for the largest (mixed) is
// no goal. The duration edges read the fitness flags, so the goal and the
// fitness thresholds cannot drift: enduranceMinS is
// fitness.endurance.min_session_seconds, accelerateTargetS
// fitness.accelerate.target_session_seconds. This is a mapping, not a migration:
// the rules weights and the fitness functions are unchanged.
func FromObjectives(weights map[string]float64, enduranceMinS, accelerateTargetS float64) Profile {
	top, best, tie := "", math.Inf(-1), false
	for _, k := range []string{objEndurance, objBalanced, objAccelerate, objChaos} {
		w, ok := weights[k]
		if !ok || math.IsNaN(w) || math.IsInf(w, 0) {
			continue
		}
		switch {
		case w > best:
			top, best, tie = k, w, false
		case w == best:
			tie = true
		}
	}
	p := Profile{FromObjectives: true}
	if top == "" || tie {
		return p
	}
	switch top {
	case objEndurance:
		p.Name = NameLongRunningGame
		p.Terms = []Term{
			term(decide.CharDuration, ptr(clampBound(decide.CharDuration, enduranceMinS)), nil),
			term(decide.CharActivity, ptr(0.3), ptr(0.6)),
			term(decide.CharEliminationPace, nil, ptr(1)),
		}
	case objAccelerate:
		p.Name = NameQuickGame
		p.Terms = []Term{
			term(decide.CharDuration, nil, ptr(clampBound(decide.CharDuration, accelerateTargetS))),
			term(decide.CharEliminationPace, ptr(2), nil),
		}
	case objBalanced:
		p.Name = NameBalancedContest
		p.Terms = []Term{
			term(decide.CharCloseness, nil, ptr(0.25)),
			term(decide.CharDuration, ptr(balancedMinDuration), nil),
		}
	case objChaos:
		p.Name = NameChaos
		p.Terms = []Term{
			term(decide.CharActivity, ptr(0.6), nil),
			term(decide.CharChange, ptr(0.3), nil),
		}
	}
	return p
}

// FormatValue renders one measured value of char in its unit: duration in whole
// seconds ("95s"), elimination_pace per minute ("0.6/min"), the rest with two
// decimals.
func FormatValue(char string, v float64) string {
	switch char {
	case decide.CharDuration:
		return strconv.FormatFloat(math.Round(v), 'f', -1, 64) + "s"
	case decide.CharEliminationPace:
		return strconv.FormatFloat(math.Round(v*100)/100, 'f', -1, 64) + "/min"
	case decide.CharSurvival:
		return strconv.FormatFloat(v, 'f', -1, 64)
	default:
		return strconv.FormatFloat(v, 'f', 2, 64)
	}
}

// Label is a term's characteristic with its phase: "activity" for the whole game,
// "activity@endgame" for the endgame.
func Label(char, phase string) string {
	if phase == PhaseEndgame {
		return char + "@" + PhaseEndgame
	}
	return char
}

// FormatBand renders a term's band: "duration>=180s", "activity=0.30..0.60",
// "elimination_pace<=1/min", "activity@endgame>=0.60".
func FormatBand(char, phase string, lo, hi *float64) string {
	label := Label(char, phase)
	switch {
	case lo != nil && hi != nil:
		return label + "=" + strings.TrimSuffix(FormatValue(char, *lo), unit(char)) + ".." + FormatValue(char, *hi)
	case lo != nil:
		return label + ">=" + FormatValue(char, *lo)
	case hi != nil:
		return label + "<=" + FormatValue(char, *hi)
	}
	return label
}

func unit(char string) string {
	switch char {
	case decide.CharDuration:
		return "s"
	case decide.CharEliminationPace:
		return "/min"
	}
	return ""
}

// GoalLine is the prompt's goal line without its "goal: " prefix:
// "long_running_game duration>=180s activity=0.30..0.60 elimination_pace<=1/min".
func (p Profile) GoalLine() string {
	parts := []string{p.Name}
	for _, t := range p.Terms {
		parts = append(parts, FormatBand(t.Characteristic, t.Phase, t.Min, t.Max))
	}
	return strings.Join(parts, " ")
}

// nowCharacteristics are always on the now line (when measured), in this order.
var nowCharacteristics = []string{decide.CharDuration, decide.CharActivity, decide.CharEliminationPace, decide.CharCloseness}

// NowLine is the prompt's now line without its "now: " prefix: the measured
// duration, activity, elimination_pace and closeness of the whole game so far,
// then every other (characteristic, phase) a term of p uses (change, an endgame
// phase). An unmeasured value is left out; nothing measured is "no data yet".
func NowLine(p Profile, measure func(char, phase string) (float64, bool)) string {
	type key struct{ char, phase string }
	keys := make([]key, 0, len(nowCharacteristics)+len(p.Terms))
	seen := map[key]bool{}
	for _, c := range nowCharacteristics {
		k := key{c, PhaseWhole}
		keys, seen[k] = append(keys, k), true
	}
	extra := []key{}
	for _, t := range p.Terms {
		k := key{t.Characteristic, t.Phase}
		if !seen[k] {
			extra, seen[k] = append(extra, k), true
		}
	}
	sort.SliceStable(extra, func(i, j int) bool {
		return Label(extra[i].char, extra[i].phase) < Label(extra[j].char, extra[j].phase)
	})
	keys = append(keys, extra...)
	var parts []string
	for _, k := range keys {
		if v, ok := measure(k.char, k.phase); ok {
			parts = append(parts, Label(k.char, k.phase)+"="+FormatValue(k.char, v))
		}
	}
	if len(parts) == 0 {
		return "no data yet"
	}
	return strings.Join(parts, " ")
}
