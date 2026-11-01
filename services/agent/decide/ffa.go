package decide

import (
	"math"
	"slices"
	"sort"
	"strconv"
	"strings"
)

// FFA question IDs (epic #1225). The three questions are asked together in one
// Request: which intervention, on whom, and how strongly.
const (
	QuestionFFAIntervention = "ffa.intervention"
	QuestionFFATarget       = "ffa.target"
	QuestionFFAMagnitude    = "ffa.magnitude"
)

// OptionNoop is the "do nothing" intervention option; every ffa.intervention
// question offers it. It matches decision.InterventionNoop.
const OptionNoop = "noop"

// TargetNone is the ffa.target option for a session-scoped intervention (tempo,
// volume, end_game, ...) and for noop. It is always offered, so the target
// question stays answerable when no player is alive.
const TargetNone = "none"

// Group options of ffa.target (#2467, UC-040 A47, BR-033). They are offered only
// when the allow-list handed to FFAQuestions holds a GroupTargeted lever (the
// decision loop drops it unless the group gate is open) and at least two players
// are alive. "group:all" is every alive player; "group:all_except:<serial>" (only
// with three or more alive) is every alive player but that one. A group lever
// takes one of them, every other intervention never does.
const (
	TargetGroupPrefix          = "group:"
	TargetGroupAll             = "group:all"
	TargetGroupAllExceptPrefix = "group:all_except:"
)

// groupTargeted are the interventions applied to a set of players.
var groupTargeted = Set(func(iv Intervention) bool { return iv.GroupTargeted })

// GroupTargeted reports whether intervention is applied to a set of alive players
// chosen with a group option of ffa.target.
func GroupTargeted(intervention string) bool { return groupTargeted[intervention] }

// IsGroupTarget reports whether an ffa.target choice is a group option.
func IsGroupTarget(choice string) bool { return strings.HasPrefix(choice, TargetGroupPrefix) }

// GroupTargetOptions returns the group options for the alive serials: none below
// two alive, "group:all" with two, plus one "group:all_except:<serial>" per alive
// serial (sorted) with three or more, so every option names at least two players.
func GroupTargetOptions(alive []string) []string {
	if len(alive) < 2 {
		return nil
	}
	sorted := append([]string(nil), alive...)
	sort.Strings(sorted)
	out := []string{TargetGroupAll}
	if len(sorted) >= 3 {
		for _, s := range sorted {
			out = append(out, TargetGroupAllExceptPrefix+s)
		}
	}
	return out
}

// ResolveGroupTarget maps a group option onto the alive serials it names, sorted.
// ok is false for a choice that is not a group option of these alive serials (an
// unknown form, an excluded serial that is not alive, or fewer than two players).
func ResolveGroupTarget(choice string, alive []string) (serials []string, ok bool) {
	if !slices.Contains(GroupTargetOptions(alive), choice) {
		return nil, false
	}
	excluded := strings.TrimPrefix(choice, TargetGroupAllExceptPrefix)
	if choice == TargetGroupAll {
		excluded = ""
	}
	for _, s := range alive {
		if s != excluded {
			serials = append(serials, s)
		}
	}
	sort.Strings(serials)
	return serials, len(serials) >= 2
}

// groupTargetText is appended to the ffa.target question text only when group
// options are offered, so every existing request stays byte-identical. It says
// what the options mean, not when to use them.
const groupTargetText = " For a group intervention answer \"group:all\" (every alive player) or " +
	"\"group:all_except:<serial>\" (every alive player except that one)."

// playerTargeted are the interventions applied to ONE player (their ffa.target
// is an alive serial); every other intervention is session-scoped and takes
// TargetNone. Derived from Catalog; decision derives its own set the same way.
var playerTargeted = Set(func(iv Intervention) bool { return iv.PlayerTargeted })

// PlayerTargeted reports whether intervention is applied to one player (its
// ffa.target must be a serial) rather than to the whole session.
func PlayerTargeted(intervention string) bool { return playerTargeted[intervention] }

// PlayerTargetedInterventions returns the player-targeted interventions, sorted.
func PlayerTargetedInterventions() []string {
	return Names(func(iv Intervention) bool { return iv.PlayerTargeted })
}

// Bounds is the numeric range of one intervention's Decision.Value.
type Bounds struct {
	Min, Max float64
	// Default is what the action sink writes when a Decision carries no Value
	// (services/agent/actions/writer.go), i.e. what the rules engine applies today.
	Default float64
	// Neutral is the value that leaves the game unchanged (the flag's neutral
	// variant), valid only when HasNeutral. Tempo, volume and global sensitivity
	// have none: their neutral variant is a "no override" sentinel (0 / -1 / -1)
	// that hands control back to the game's own schedule or admin setting, not a
	// value the intervention could write.
	Neutral    float64
	HasNeutral bool
	// Integer marks an int-valued flag (value_kind="int" in
	// services/game_coordinator/interventions.py): ResolveMagnitude rounds to the
	// nearest whole number.
	Integer bool
	Unit    string
}

// magnitudeBounds is the FFA bounds table for the interventions whose Value is a
// single number. Nothing here is invented: Min/Max are the lowest and highest
// non-neutral variants of the matching flag in services/flagd/interventions.json
// (the vetted range the admin UI and agent already use), each of which lies
// inside the game-side clamp cited below; Default is the writer default from
// services/agent/actions/writer.go; Neutral is the flag's neutral variant (a test
// in services/agent/actions keeps all three in sync).
//
//	intervention               flag (variants)                         game-side clamp (services/game_coordinator)
//	adjust_music_tempo         music_tempo_override slow..fast         SLOW/FAST_MUSIC_SPEED 1.0..1.3 (games/base.py)
//	adjust_volume              volume_override quiet..loud             value >= 0 (interventions.py volume handler)
//	adjust_global_sensitivity  global_sensitivity_override 0..4        SLOW_MAX/FAST_MAX index 0..4 (games/base.py)
//	adjust_player_sensitivity  player_sensitivity_factor protect..handicap  combined factor [0.5,2.0] (base.py)
//	adjust_global_difficulty   global_difficulty_factor easier..harder combined factor [0.5,2.0] (base.py)
//	set_player_handicap        player_handicap_factor rein_in..big_help handicap [0.5,2.0] (base.py)
//	grant_shield               shield_seconds short..long              none (grace_until += seconds)
//	toughness_boost             toughness_boost_seconds short..long      TOUGHNESS_BOOST_MAX_SECONDS 30 (base.py)
//
// Interventions absent from the table (audio cue, controller effect, eliminate,
// revive, end_game, pacing_profile, ramp_tempo, auto_rubberband, soft_penalty,
// noop) take no numeric magnitude; their ffa.magnitude answer is ignored.
var magnitudeBounds = map[string]Bounds{
	"adjust_music_tempo":        {Min: 1.0, Max: 1.3, Default: 1.15, Unit: "tempo"},
	"adjust_volume":             {Min: 0.3, Max: 1.0, Default: 0.7, Unit: "volume"},
	"adjust_global_sensitivity": {Min: 0, Max: 4, Default: 2, Integer: true, Unit: "level"},
	"adjust_player_sensitivity": {Min: 0.7, Max: 1.5, Default: 1.5, Neutral: 1.0, HasNeutral: true, Unit: "factor"},
	"adjust_global_difficulty":  {Min: 0.5, Max: 2.0, Default: 1.5, Neutral: 1.0, HasNeutral: true, Unit: "factor"},
	"set_player_handicap":       {Min: 0.75, Max: 1.75, Default: 1.25, Neutral: 1.0, HasNeutral: true, Unit: "factor"},
	"grant_shield":              {Min: 3, Max: 10, Default: 5, Neutral: 0, HasNeutral: true, Unit: "seconds"},
	"toughness_boost":           {Min: 3, Max: 10, Default: 5, Neutral: 0, HasNeutral: true, Unit: "seconds"},
}

// MagnitudeBounds returns the bounds of intervention's numeric Value, and false
// when the intervention takes no numeric magnitude.
func MagnitudeBounds(intervention string) (Bounds, bool) {
	b, ok := magnitudeBounds[intervention]
	return b, ok
}

// MagnitudeInterventions returns the interventions in the bounds table, sorted.
func MagnitudeInterventions() []string {
	out := make([]string, 0, len(magnitudeBounds))
	for k := range magnitudeBounds {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// ResolveMagnitude maps a normalized ffa.magnitude score (0 = Min, 1 = Max)
// linearly onto intervention's bounds, rounding to a whole number for an Integer
// flag. The score is clamped to [0,1] first, so the result is always inside the
// bounds whatever a backend sent. ok is false when the intervention takes no
// numeric magnitude.
func ResolveMagnitude(intervention string, score float64) (value float64, ok bool) {
	b, ok := magnitudeBounds[intervention]
	if !ok {
		return 0, false
	}
	if !finite(score) {
		score = 0
	}
	score = clamp01(score)
	v := b.Min + score*(b.Max-b.Min)
	if b.Integer {
		v = math.Round(v)
	} else {
		// Drop float noise from the normalize/resolve round trip (0.7 + 0.25*0.8
		// is 0.8999999999999999), so Decision.Value reads like the flag variant.
		v = math.Round(v*1e9) / 1e9
	}
	return v, true
}

// NormalizeMagnitude is the inverse of ResolveMagnitude: the normalized score of
// value within intervention's bounds (clamped to [0,1]). ok is false when the
// intervention takes no numeric magnitude.
func NormalizeMagnitude(intervention string, value float64) (score float64, ok bool) {
	b, ok := magnitudeBounds[intervention]
	if !ok {
		return 0, false
	}
	if b.Max == b.Min {
		return 0, true
	}
	return clamp01((value - b.Min) / (b.Max - b.Min)), true
}

// FFAQuestions builds the FFA question set for st:
//   - ffa.intervention: a choice over OptionNoop plus the allowed interventions
//     (deduplicated, sorted, empty ids dropped);
//   - ffa.target: a choice over TargetNone plus the alive serials (sorted);
//   - ffa.magnitude: a score in [0,1], normalized over the CHOSEN intervention's
//     bounds: 0 = Min, 1 = Max (see ResolveMagnitude). The three questions are answered in one
//     round, before the intervention is known, so the range is normalized and
//     the question text lists each allowed intervention's native bounds.
//
// When allowed holds a GroupTargeted lever and at least two players are alive,
// ffa.target also offers GroupTargetOptions after the serials, and its text gains
// one sentence (groupTargetText). Without one the questions are unchanged.
func FFAQuestions(st State, allowed []string) []Question {
	interventions := interventionOptions(allowed)
	alive := st.AliveSerials()
	targets := append([]string{TargetNone}, alive...)
	targetText := "Which alive player should the intervention target? \"none\" for a session-scoped " +
		"intervention or noop."
	if offersGroupLever(interventions) {
		if group := GroupTargetOptions(alive); len(group) > 0 {
			targets = append(targets, group...)
			targetText += groupTargetText
		}
	}
	return []Question{
		{
			ID:      QuestionFFAIntervention,
			Kind:    KindChoice,
			Text:    "Which intervention should the agent apply now? \"noop\" means do nothing.",
			Options: interventions,
		},
		{
			ID:      QuestionFFATarget,
			Kind:    KindChoice,
			Text:    targetText,
			Options: targets,
		},
		{
			ID:    QuestionFFAMagnitude,
			Kind:  KindScore,
			Text:  magnitudeText(interventions),
			Range: [2]float64{0, 1},
		},
	}
}

// offersGroupLever reports whether the intervention options hold a group lever.
func offersGroupLever(options []string) bool {
	for _, o := range options {
		if GroupTargeted(o) {
			return true
		}
	}
	return false
}

func interventionOptions(allowed []string) []string {
	seen := map[string]bool{OptionNoop: true}
	rest := make([]string, 0, len(allowed))
	for _, a := range allowed {
		if a == "" || seen[a] {
			continue
		}
		seen[a] = true
		rest = append(rest, a)
	}
	sort.Strings(rest)
	return append([]string{OptionNoop}, rest...)
}

func magnitudeText(interventions []string) string {
	var b strings.Builder
	b.WriteString("What value should the chosen intervention use? Answer on a 0..1 scale where " +
		"0 = the lowest listed value and 1 = the highest; it maps linearly onto the range below " +
		"(in the intervention's own units). Ignored for interventions not listed.")
	for _, i := range interventions {
		bd, ok := magnitudeBounds[i]
		if !ok {
			continue
		}
		line := "\n  " + i + ": lowest " + fmtNum(bd.Min) + ", highest " + fmtNum(bd.Max) + " " + bd.Unit
		if bd.HasNeutral {
			line += ", neutral " + fmtNum(bd.Neutral)
		}
		if bd.Integer {
			line += ", whole numbers only"
		}
		b.WriteString(line + " (default " + fmtNum(bd.Default) + ")")
	}
	return b.String()
}

func fmtNum(v float64) string { return strconv.FormatFloat(v, 'g', -1, 64) }

func clamp01(v float64) float64 {
	switch {
	case v < 0:
		return 0
	case v > 1:
		return 1
	default:
		return v
	}
}
