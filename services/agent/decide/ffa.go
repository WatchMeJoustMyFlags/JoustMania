package decide

import (
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

// Bounds is the numeric range of one intervention's Decision.Value.
type Bounds struct {
	Min, Max float64
	// Default is what the action sink writes when a Decision carries no Value
	// (services/agent/actions/writer.go), i.e. what the rules engine applies today.
	Default float64
	Unit    string
}

// magnitudeBounds is the FFA bounds table for the interventions whose Value is a
// single number. Nothing here is invented: Min/Max are the lowest and highest
// non-neutral variants of the matching flag in services/flagd/interventions.json
// (the vetted range the admin UI and agent already use), each of which lies
// inside the game-side clamp cited below; Default is the writer default from
// services/agent/actions/writer.go (a test there keeps the two in sync).
//
//	intervention               flag (variants)                         game-side clamp (services/game_coordinator)
//	adjust_music_tempo         music_tempo_override slow..fast         SLOW/FAST_MUSIC_SPEED 1.0..1.3 (games/base.py)
//	adjust_volume              volume_override quiet..loud             value >= 0 (interventions.py volume handler)
//	adjust_global_sensitivity  global_sensitivity_override 0..4        SLOW_MAX/FAST_MAX index 0..4 (games/base.py)
//	adjust_player_sensitivity  player_sensitivity_factor protect..handicap  combined factor [0.5,2.0] (base.py)
//	adjust_global_difficulty   global_difficulty_factor easier..harder combined factor [0.5,2.0] (base.py)
//	set_player_handicap        player_handicap_factor rein_in..big_help handicap [0.5,2.0] (base.py)
//	grant_shield               shield_seconds short..long              none (grace_until += seconds)
//	partial_shield             partial_shield_seconds short..long      PARTIAL_SHIELD_MAX_SECONDS 30 (base.py)
//
// Interventions absent from the table (audio cue, controller effect, eliminate,
// revive, end_game, pacing_profile, ramp_tempo, auto_rubberband, soft_penalty,
// noop) take no numeric magnitude; their ffa.magnitude answer is ignored.
var magnitudeBounds = map[string]Bounds{
	"adjust_music_tempo":        {Min: 1.0, Max: 1.3, Default: 1.15, Unit: "tempo"},
	"adjust_volume":             {Min: 0.3, Max: 1.0, Default: 0.7, Unit: "volume"},
	"adjust_global_sensitivity": {Min: 0, Max: 4, Default: 2, Unit: "level"},
	"adjust_player_sensitivity": {Min: 0.7, Max: 1.5, Default: 1.5, Unit: "factor"},
	"adjust_global_difficulty":  {Min: 0.5, Max: 2.0, Default: 1.5, Unit: "factor"},
	"set_player_handicap":       {Min: 0.75, Max: 1.75, Default: 1.25, Unit: "factor"},
	"grant_shield":              {Min: 3, Max: 10, Default: 5, Unit: "seconds"},
	"partial_shield":            {Min: 3, Max: 10, Default: 5, Unit: "seconds"},
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

// ResolveMagnitude maps a normalized ffa.magnitude score (0 = gentlest, 1 =
// strongest) onto intervention's bounds. The score is clamped to [0,1] first, so
// the result is always inside the bounds whatever a backend sent. ok is false
// when the intervention takes no numeric magnitude.
func ResolveMagnitude(intervention string, score float64) (value float64, ok bool) {
	b, ok := magnitudeBounds[intervention]
	if !ok {
		return 0, false
	}
	if !finite(score) {
		score = 0
	}
	score = clamp01(score)
	return b.Min + score*(b.Max-b.Min), true
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
//     bounds (see ResolveMagnitude). The three questions are answered in one
//     round, before the intervention is known, so the range is normalized and
//     the question text lists each allowed intervention's native bounds.
func FFAQuestions(st State, allowed []string) []Question {
	interventions := interventionOptions(allowed)
	targets := append([]string{TargetNone}, st.AliveSerials()...)
	return []Question{
		{
			ID:      QuestionFFAIntervention,
			Kind:    KindChoice,
			Text:    "Which intervention should the agent apply now? \"noop\" means do nothing.",
			Options: interventions,
		},
		{
			ID:   QuestionFFATarget,
			Kind: KindChoice,
			Text: "Which alive player should the intervention target? \"none\" for a session-scoped " +
				"intervention or noop.",
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
	b.WriteString("How strong should the chosen intervention be, from 0 (gentlest) to 1 (strongest)? " +
		"It maps linearly onto the intervention's bounds; ignored for interventions without bounds.")
	for _, i := range interventions {
		bd, ok := magnitudeBounds[i]
		if !ok {
			continue
		}
		b.WriteString("\n  " + i + ": " + fmtNum(bd.Min) + ".." + fmtNum(bd.Max) + " " + bd.Unit +
			" (default " + fmtNum(bd.Default) + ")")
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
