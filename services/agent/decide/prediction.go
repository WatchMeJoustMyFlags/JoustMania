package decide

import (
	"slices"
	"strconv"
)

// prediction.go is the closed prediction vocabulary of a decision (#2479, UC-040
// A44, BR-034): what the deciding backend expects to happen if its answer is
// applied, as one template over a measurable characteristic with a horizon from a
// closed set and a probability. The agent scores it later against telemetry
// (services/agent/prediction). A prediction never changes what is allowed: it is
// carried on Response, outside the Questions, so Validate, the permission chain
// and the action sink never read it.

// Characteristics (services/agent/characteristics measures them).
const (
	CharDuration        = "duration"
	CharActivity        = "activity"
	CharChange          = "change"
	CharEliminationPace = "elimination_pace"
	CharSurvival        = "survival"
	CharCloseness       = "closeness"
)

// Templates: the closed set of things a prediction can claim.
const (
	TemplateSurvives       = "survives"
	TemplateNoElimination  = "no_elimination"
	TemplateElimination    = "elimination"
	TemplateActivityRises  = "activity_rises"
	TemplateClosenessRises = "closeness_rises"
	TemplateGameEnds       = "game_ends"
)

// Scopes: who a prediction is about.
const (
	ScopeTarget = "target"
	ScopeField  = "field"
	ScopeGame   = "game"
)

// Sources: who stated the prediction.
const (
	// SourceRules: the rules engine's fixed per-rule table.
	SourceRules = "rules"
	// SourceLeverDefault: the per-intervention default for a backend that stated
	// none (it scores the lever map, not the model).
	SourceLeverDefault = "lever_default"
	// SourceModel: stated by a model (not produced yet, #2479 PR D).
	SourceModel = "model"
	// SourceControl: a base-rate control the agent registers itself.
	SourceControl = "control"
)

// LeverDefaultP is the probability every lever-default prediction carries: a
// documented prior, not a claim.
const LeverDefaultP = 0.6

// Prediction is one decision's expectation. Rule is the rule id (R1..R9, R4d)
// for source rules and the intervention for source lever_default. Target is the
// player a target-scoped prediction is about.
type Prediction struct {
	Characteristic string  `json:"characteristic"`
	Scope          string  `json:"scope"`
	Target         string  `json:"target,omitempty"`
	Template       string  `json:"template"`
	HorizonS       int     `json:"horizon_s"`
	P              float64 `json:"p"`
	Source         string  `json:"source"`
	Rule           string  `json:"rule,omitempty"`
}

// templateSpec is one template's characteristic and closed horizon set.
type templateSpec struct {
	characteristic string
	horizons       []int
}

var templates = map[string]templateSpec{
	TemplateSurvives:       {CharSurvival, []int{10, 15, 30}},
	TemplateNoElimination:  {CharEliminationPace, []int{10, 15, 30}},
	TemplateElimination:    {CharEliminationPace, []int{15, 30, 60}},
	TemplateActivityRises:  {CharActivity, []int{10, 15}},
	TemplateClosenessRises: {CharCloseness, []int{15}},
	TemplateGameEnds:       {CharDuration, []int{30, 60, 120}},
}

// Templates returns the template names, sorted.
func Templates() []string {
	out := make([]string, 0, len(templates))
	for k := range templates {
		out = append(out, k)
	}
	slices.Sort(out)
	return out
}

// ValidPrediction reports whether p uses a known template with one of its
// horizons, the template's characteristic, a scope that fits (survives needs a
// target), and a finite probability in [0,1].
func ValidPrediction(p Prediction) bool {
	spec, ok := templates[p.Template]
	if !ok || p.Characteristic != spec.characteristic || !slices.Contains(spec.horizons, p.HorizonS) {
		return false
	}
	if !finite(p.P) || p.P < 0 || p.P > 1 {
		return false
	}
	switch p.Scope {
	case ScopeTarget:
		return p.Target != "" && (p.Template == TemplateSurvives || p.Template == TemplateActivityRises)
	case ScopeField:
		return p.Target == "" && (p.Template == TemplateActivityRises || p.Template == TemplateClosenessRises)
	case ScopeGame:
		return p.Target == "" && (p.Template == TemplateNoElimination || p.Template == TemplateElimination ||
			p.Template == TemplateGameEnds)
	}
	return false
}

// NewPrediction builds a prediction for template with the derived characteristic
// and scope: survives is about target; activity_rises is about target when there
// is one, else the field; closeness_rises is the field; the elimination and
// duration templates are about the game.
func NewPrediction(template string, horizonS int, p float64, source, rule, target string) Prediction {
	pr := Prediction{
		Characteristic: templates[template].characteristic,
		Template:       template,
		HorizonS:       horizonS,
		P:              p,
		Source:         source,
		Rule:           rule,
	}
	switch template {
	case TemplateSurvives:
		pr.Scope, pr.Target = ScopeTarget, target
	case TemplateActivityRises:
		pr.Scope = ScopeField
		if target != "" {
			pr.Scope, pr.Target = ScopeTarget, target
		}
	case TemplateClosenessRises:
		pr.Scope = ScopeField
	default:
		pr.Scope = ScopeGame
	}
	return pr
}

// LeverPrediction is the per-intervention default prediction (source
// lever_default) for a decision whose backend stated none: jev, and claude or
// openai while the model is not asked. value is Decision.Value ("" when the
// intervention takes no number), target the decision's target serial, and
// tempo the game's current music tempo (nil when unknown; unused while the tempo
// lever has no default). It returns nil for the interventions with nothing
// measurable to expect (audio cue, volume, music tempo, eliminate, revive,
// end_game, noop, ...), for a value that does not say a direction, and for an
// unparseable value.
//
// The direction of each lever follows its flag (services/flagd/interventions.json):
// player sensitivity below 1 protects, a handicap factor above 1 helps, a
// global difficulty below 1 is easier, a global sensitivity level above the
// middle (2) is harder to be eliminated at, pacing calm or frantic.
func LeverPrediction(intervention, value, target string, tempo *float64) *Prediction {
	num := func() (float64, bool) {
		v, err := strconv.ParseFloat(value, 64)
		return v, err == nil && finite(v)
	}
	mk := func(template string, h int, tgt string) *Prediction {
		p := NewPrediction(template, h, LeverDefaultP, SourceLeverDefault, intervention, tgt)
		return &p
	}
	switch intervention {
	case IntvGrantShield, IntvToughnessBoost:
		if target == "" {
			return nil
		}
		return mk(TemplateSurvives, 15, target)
	case IntvAdjustPlayerSensitivity:
		if v, ok := num(); ok && v < 1 && target != "" {
			return mk(TemplateSurvives, 15, target)
		}
	case IntvSetPlayerHandicap:
		if v, ok := num(); ok && v > 1 && target != "" {
			return mk(TemplateSurvives, 15, target)
		}
	case IntvAdjustMusicTempo:
		// None in either direction: the death threshold slides with the tempo, so
		// the tempo's effect on eliminations and on activity (intensity / threshold)
		// is confounded with the threshold change (review of #2485).
		return nil
	case IntvSendControllerEffect:
		if target == "" {
			return nil
		}
		return mk(TemplateActivityRises, 10, target)
	case IntvAdjustGlobalDifficulty:
		if v, ok := num(); ok {
			switch {
			case v < 1:
				return mk(TemplateNoElimination, 15, "")
			case v > 1:
				return mk(TemplateElimination, 30, "")
			}
		}
	case IntvAdjustGlobalSensitivity:
		if v, ok := num(); ok {
			switch {
			case v > 2:
				return mk(TemplateNoElimination, 15, "")
			case v < 2:
				return mk(TemplateElimination, 30, "")
			}
		}
	case IntvSetPacingProfile:
		switch value {
		case "calm":
			return mk(TemplateNoElimination, 15, "")
		case "frantic":
			return mk(TemplateElimination, 30, "")
		}
	}
	return nil
}
