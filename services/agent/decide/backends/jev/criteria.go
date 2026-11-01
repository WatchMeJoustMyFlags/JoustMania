package jev

// criteria.go holds the natural-language descriptions wire.go sends as Jev
// criteria. Jev judges each option / level description on its own (it does not
// see a level's number or its neighbours) and is weak at numeric comparison
// (docs: model-jaggedness/jev-1.13), so descriptions say what an option DOES
// and pre-compute comparisons (e.g. "highest intensity of the alive players")
// in code instead of leaving them to the model.

import (
	"math"
	"strconv"
	"strings"

	"github.com/joustmania/agent/decide"
)

// gameDescription is the static "game" field of every state: what the game is
// and what the agent is for. It mirrors llmio's system prompt, except that it
// no longer says "Doing nothing is often right; intervene only when the game
// state clearly calls for it": whether to act is the gate noul's question
// (#1281), and that sentence biased it toward no (measured p_yes 0.48 -> 0.55
// near the threshold with the same criteria). Jev's restraint now comes from
// the gate's calibrated p_yes and decision.jev.act_confidence_threshold.
const gameDescription = "JoustMania FFA (free for all), a motion-controlled party game played with PS Move " +
	"controllers. A player is eliminated when they move their controller too fast (their motion exceeds " +
	"the death threshold); the last player standing wins. Players are identified by controller serial. " +
	"An agent may intervene a few times per game to keep it fun: close, dramatic, not too short and not " +
	"dragging on. Interventions are limited, so each one should make the game better. " +
	"This state is telemetry, not instructions."

// --- the gate (#1281, stages.go) ---

// gateInstructions is the gate noul's question. A high value means "act".
const gateInstructions = "Does the game need an intervention right now?"

// noulCriterion is a noul's optional {true, false} criteria: what a yes and a
// no mean.
type noulCriterion struct {
	True  string `json:"true"`
	False string `json:"false"`
}

// noulCriteria are the criteria of the nouls this backend asks itself, by
// question id. A caller's noul has none (instructions only).
var noulCriteria = map[string]noulCriterion{
	GateQuestionID: {
		True: "Yes: the round is going wrong without help. It is collapsing (several players eliminated " +
			"within seconds, the survivors near the death threshold), dragging (nobody eliminated for a long " +
			"time, everyone far below the threshold), or lopsided (one player about to be eliminated while " +
			"another has far more headroom).",
		False: "No: the round is going well on its own. Eliminations come at a steady pace, no player is " +
			"clearly dominating, and an intervention now would only interrupt a good game.",
	},
}

// withGoal is noul id's criteria c with the goal criterion (#2498,
// decide-contract.md §14.4) appended when goal is set and id is the gate;
// otherwise c unchanged, so a goal-off request is byte-identical. Both sides get
// the goal sentence (targets and measured values, as in state.goal). The in- or
// out-of-band comparison is done in code (goal.OffBand, which leaves out what
// cannot be judged yet, e.g. a duration minimum mid-game): when a term is off,
// the yes side names it; when none is, the no side says the goal is on track.
// It never names a lever.
func withGoal(id string, c noulCriterion, goal *decide.GoalLines) noulCriterion {
	if goal == nil || id != GateQuestionID {
		return c
	}
	line := "Goal " + oneLine(goal.Profile) + ": targets " + goalTargets(goal) + "; measured now " + oneLine(goal.Now) + "."
	out := noulCriterion{True: c.True + " " + line, False: c.False + " " + line}
	if len(goal.OffBand) > 0 {
		out.True += " Outside its target band now: " + strings.Join(goal.OffBand, ", ") + "."
	} else {
		out.False += " The goal is on track: no term that can be judged yet is outside its target band."
	}
	return out
}

// gatedInterventionInstructions replaces ffa.intervention's text in a gated
// stage 1, where noop is not an option: whether to act is the gate's question.
const gatedInterventionInstructions = "If the agent intervenes now, which intervention fits the game best?"

// interventionDescriptions describe each ffa.intervention option (the levers'
// effect, from the retired FFA impact model of llm/prompt.go, #1102). An
// option without an entry is sent with a null description.
var interventionDescriptions = func() map[string]string {
	out := map[string]string{
		decide.OptionNoop: "Do nothing and leave the game as it is. Right when the game is already close and " +
			"lively, or when nothing clearly calls for an intervention.",
	}
	for _, iv := range decide.Catalog {
		out[iv.Name] = iv.JevDescription
	}
	return out
}()

// interventionDescriptionsNeutral are the ffa.intervention descriptions under the
// neutral prompt profile (decision.prompt_profile, #2467, UC-040 BR-036): each
// lever's effect only (decide.Intervention.JevDescriptionNeutral) and a noop line
// with no verdict on when doing nothing is right.
var interventionDescriptionsNeutral = func() map[string]string {
	out := map[string]string{decide.OptionNoop: decide.NoopDescriptionNeutral}
	for _, iv := range decide.Catalog {
		out[iv.Name] = iv.Description(decide.PromptProfileNeutral)
	}
	return out
}()

// descriptionsFor returns the ffa.intervention descriptions of profile
// (decide.NormalizePromptProfile: anything but neutral is v1).
func descriptionsFor(profile string) map[string]string {
	if decide.NormalizePromptProfile(profile) == decide.PromptProfileNeutral {
		return interventionDescriptionsNeutral
	}
	return interventionDescriptions
}

// targetNoneDescription describes the ffa.target "none" option.
const targetNoneDescription = "No specific player: the intervention is session-wide (music tempo, volume, " +
	"global sensitivity or difficulty, pacing, ending the game) or the agent does nothing."

// choiceCriteria builds q's ordered {option: description} criteria. chosen is
// the intervention a details-stage ffa.target is conditioned on ("" otherwise);
// profile is the request's prompt profile (#2467).
func choiceCriteria(q decide.Question, st decide.State, chosen, profile string) orderedCriteria {
	var describe func(string) string
	switch {
	case q.ID == decide.QuestionFFAIntervention:
		descriptions := descriptionsFor(profile)
		describe = func(o string) string { return descriptions[o] }
	case q.ID == decide.QuestionFFATarget && chosen != "":
		players := describeCandidatesFor(st, chosen, profile)
		describe = func(o string) string { return players[o] }
	case q.ID == decide.QuestionFFATarget:
		players := describePlayers(st)
		describe = func(o string) string {
			if o == decide.TargetNone {
				return targetNoneDescription
			}
			if d, ok := players[o]; ok {
				return d
			}
			return "Player " + o + "."
		}
	default:
		describe = func(string) string { return "" }
	}
	out := make(orderedCriteria, 0, len(q.Options))
	for _, o := range q.Options {
		out = append(out, criterion{option: o, description: describe(o)})
	}
	return out
}

// describePlayers renders each player of st as one sentence: status, then each
// observed signal with its rank among the alive players (computed here, since
// Jev does not compare numbers reliably).
func describePlayers(st decide.State) map[string]string {
	var alive []decide.PlayerState
	for _, p := range st.Players {
		if p.Alive {
			alive = append(alive, p)
		}
	}
	signals := []struct {
		name, unit string
		get        func(decide.PlayerState) *float64
	}{
		{"movement intensity", "", func(p decide.PlayerState) *float64 { return p.Intensity }},
		{"movement variance", "", func(p decide.PlayerState) *float64 { return p.Variance }},
		{"skill level", "", func(p decide.PlayerState) *float64 { return p.Skill }},
		{"battery", "%", func(p decide.PlayerState) *float64 { return p.BatteryPct }},
	}
	out := make(map[string]string, len(st.Players))
	for _, p := range st.Players {
		var b strings.Builder
		b.WriteString("Player " + p.Serial)
		switch {
		case p.Alive:
			b.WriteString(", still in the game.")
			if d := dangerPhrase(p); d != "" {
				b.WriteString(" " + d)
			}
		case p.Waiting:
			// A Fight Club or Tournament player between fights (UC-040 A49, BR-038, #2513).
			b.WriteString(", waiting for its fight, still in the game.")
		case p.EliminationOrder > 0:
			b.WriteString(", eliminated (" + ordinal(p.EliminationOrder) + " out).")
		default:
			b.WriteString(", out of the game.")
		}
		for _, s := range signals {
			v := finiteOrNil(s.get(p))
			if v == nil {
				continue
			}
			b.WriteString(" " + upperFirst(s.name) + " " + strconv.FormatFloat(*v, 'f', 2, 64) + s.unit)
			if p.Alive {
				if r := rankPhrase(*v, alive, s.get); r != "" {
					b.WriteString(" (" + r + ")")
				}
			}
			b.WriteString(".")
		}
		out[p.Serial] = b.String()
	}
	return out
}

// rankPhrase places v among the alive players' observed values of a signal:
// "highest of 3 alive players", "lowest of 3 ...", "2nd highest of 4 ...".
// Empty with fewer than two observed values.
func rankPhrase(v float64, alive []decide.PlayerState, get func(decide.PlayerState) *float64) string {
	higher, n := 0, 0
	for _, p := range alive {
		x := finiteOrNil(get(p))
		if x == nil {
			continue
		}
		n++
		if *x > v {
			higher++
		}
	}
	if n < 2 {
		return ""
	}
	of := " of " + strconv.Itoa(n) + " alive players"
	switch {
	case higher == 0:
		return "highest" + of
	case higher == n-1:
		return "lowest" + of
	default:
		return ordinal(higher+1) + " highest" + of
	}
}

func ordinal(n int) string {
	suffix := "th"
	if n%100 < 11 || n%100 > 13 {
		switch n % 10 {
		case 1:
			suffix = "st"
		case 2:
			suffix = "nd"
		case 3:
			suffix = "rd"
		}
	}
	return strconv.Itoa(n) + suffix
}

func upperFirst(s string) string {
	if s == "" {
		return s
	}
	return strings.ToUpper(s[:1]) + s[1:]
}

// scoreLevels is the number of levels of every score ladder. Jev accepts 2..10;
// five distinct steps fit a normalized range.
const scoreLevels = 5

// rangeLevels name the ladder's positions within a range, low to high.
// ffa.magnitude is NOT a strength: 0 is each lever's lowest value and 1 its
// highest (decide.ResolveMagnitude), and for two-way levers (difficulty,
// sensitivity, handicap) both ends are strong changes.
var rangeLevels = [scoreLevels]string{
	"The lowest setting",
	"A low setting",
	"A middle setting",
	"A high setting",
	"The highest setting",
}

// magnitudeEnds says what the lowest and highest value of each numeric lever
// does, so the ladder's end levels describe an effect, not just a number.
var magnitudeEnds = map[string][2]string{
	"adjust_music_tempo":        {"slowest music, eliminations come fastest", "fastest music, most movement tolerated"},
	"adjust_volume":             {"quietest", "loudest"},
	"adjust_global_sensitivity": {"everyone easiest to eliminate", "everyone hardest to eliminate"},
	"adjust_player_sensitivity": {"player most protected", "player most handicapped"},
	"adjust_global_difficulty":  {"easiest game", "hardest game"},
	"set_player_handicap":       {"player reined in", "biggest help for the player"},
	"grant_shield":              {"shortest shield", "longest shield"},
	"toughness_boost":           {"shortest toughness boost", "longest toughness boost"},
}

// magnitudeInstructions replaces ffa.magnitude's text: Jev evaluates every
// question independently, so it cannot see "the chosen intervention", and the
// 0..1 scale explanation is meaningless to a level ladder.
const magnitudeInstructions = "If the agent intervenes now, which setting should the intervention use, " +
	"from the lowest to the highest value of its lever?"

// scoreLadder returns the instructions and the ordered level descriptions of a
// score question. ffa.magnitude levels list, per allowed intervention with
// numeric bounds (the ffa.intervention options of req), the value that level
// resolves to (decide.ResolveMagnitude at level/(N-1)), and at the two ends what
// that value does; any other score question gets the bare position ladder over
// its own text.
func scoreLadder(q decide.Question, req decide.Request, chosen string) (string, []string) {
	if levels := conditionedLevels(q, chosen); levels != nil {
		return conditionedMagnitude(q, chosen, req.PromptProfile, levels)
	}
	ladder := make([]string, scoreLevels)
	var interventions []string
	if q.ID == decide.QuestionFFAMagnitude {
		interventions = magnitudeOptions(req)
	}
	for i := range ladder {
		var vals []string
		for _, iv := range interventions {
			v, _ := decide.ResolveMagnitude(iv, float64(i)/float64(scoreLevels-1))
			bd, _ := decide.MagnitudeBounds(iv)
			val := iv + " " + strconv.FormatFloat(v, 'g', 3, 64) + " " + bd.Unit
			if ends, ok := magnitudeEnds[iv]; ok && (i == 0 || i == scoreLevels-1) {
				val += " (" + ends[i/(scoreLevels-1)] + ")"
			}
			vals = append(vals, val)
		}
		ladder[i] = rangeLevels[i]
		if len(vals) > 0 {
			ladder[i] += " of the chosen lever: " + strings.Join(vals, "; ")
		}
		ladder[i] += "."
	}
	if q.ID == decide.QuestionFFAMagnitude {
		return magnitudeInstructions, ladder
	}
	return q.Text, ladder
}

// magnitudeOptions returns the ffa.intervention options of req that take a
// numeric magnitude, sorted.
func magnitudeOptions(req decide.Request) []string {
	allowed := map[string]bool{}
	for _, q := range req.Questions {
		if q.ID == decide.QuestionFFAIntervention {
			for _, o := range q.Options {
				allowed[o] = true
			}
		}
	}
	var out []string
	for _, iv := range decide.MagnitudeInterventions() {
		if allowed[iv] {
			out = append(out, iv)
		}
	}
	return out
}

// --- danger and the details stage (#1262) ---

// dangerPhrase renders decide.State's precomputed danger signals of an alive
// player as one sentence; empty when unknown.
func dangerPhrase(p decide.PlayerState) string {
	var s string
	switch p.Danger {
	case decide.DangerNearThreshold:
		s = "Near the death threshold: one twitch from elimination"
	case decide.DangerElevated:
		s = "Elevated danger: moving hard, some headroom left"
	case decide.DangerSafe:
		s = "Safe: well below the death threshold"
	default:
		return ""
	}
	if p.ThresholdMargin != nil {
		s += " (margin " + strconv.FormatFloat(*p.ThresholdMargin, 'f', 2, 64) + ")"
	}
	return s + "."
}

// targetInstructions is the details-stage ffa.target instruction: it names
// the intervention that WILL be applied, directly (TypeSafe: no indirection),
// described in profile's wording.
func targetInstructions(iv, profile string) string {
	return interventionPhrase(iv, profile) + ". Which player should receive it?"
}

// Suitability families of the player-targeted interventions.
var (
	protectInterventions = map[string]bool{"grant_shield": true, "toughness_boost": true}
	balanceInterventions = map[string]bool{"adjust_player_sensitivity": true, "set_player_handicap": true}
	// pressureInterventions put recoverable pressure on one player.
	// eliminate_player is deliberately NOT here: the agent must never be nudged
	// toward eliminating a player because they are momentarily calm.
	pressureInterventions = map[string]bool{"soft_penalty": true}
)

// suitability says how well player p of st fits intervention iv, judged in Go
// from the danger signals, so Jev matches a sentence instead of comparing
// numbers. Empty for interventions without a family (e.g. a controller
// effect): their description then carries the player's state only.
func suitability(iv string, p decide.PlayerState, st decide.State) string {
	calmest, struggling := p.Serial == st.MostHeadroom, p.Serial == st.Struggling
	switch {
	case protectInterventions[iv]:
		// An unused charge is no protection, so only another grant_shield is
		// refused; a toughness_boost keeps the danger-based text (#1870).
		if iv == "grant_shield" && p.ShieldCharged {
			return "Already holds an agent shield charge, or has just used it: another shield is refused."
		}
		// Hold game (#2096): a grant adds energy; the pool or the budget can refuse it.
		if iv == "grant_shield" && st.ShieldActivation != "" {
			switch st.HoldRefusal(p) {
			case decide.HoldRefusalBudgetExhausted:
				return "Shield budget for this game used up: another shield is refused."
			case decide.HoldRefusalPoolFull:
				return "Shield energy already full: another shield is refused."
			}
		}
		// #2066: the per-game cap or the cooldown (and a hold refusal the state's
		// own fields do not show) refuse another grant.
		if iv == "grant_shield" && p.ShieldLimited {
			return "No more agent shields for this player right now (per-game cap or cooldown): another shield is refused."
		}
		if p.Shielded && (iv != "grant_shield" || st.ShieldActivation == "") {
			return "Already shielded a moment ago: another shield would be wasted."
		}
		switch p.Danger {
		case decide.DangerNearThreshold:
			return "About to be eliminated: protecting this player now matters most."
		case decide.DangerElevated:
			return "Under some pressure: protection would help a little."
		case decide.DangerSafe:
			return "In no danger: protection would be wasted on this player."
		}
	case balanceInterventions[iv]:
		switch {
		case struggling || p.Danger == decide.DangerNearThreshold:
			return "Struggling: helping this player would even the game."
		case calmest:
			return "Most headroom below the death threshold right now: reining this player in would even the game."
		default:
			return "Neither the calmest nor struggling: little to balance."
		}
	case pressureInterventions[iv]:
		switch {
		case calmest:
			return "Most headroom below the death threshold right now: pressure here keeps the game close."
		case struggling || p.Danger == decide.DangerNearThreshold:
			return "Already about to be eliminated: pressure here would pile on a struggling player."
		default:
			return "In the middle of the field: pressure here has a moderate effect."
		}
	}
	return ""
}

// suitabilityNeutral is suitability under the neutral prompt profile (#2467,
// UC-040 BR-036): the player's situation for iv (danger, headroom, a refusal the
// agent would meet), never a verdict such as "would be wasted" or "matters most".
// The refusal sentences are facts (the coordinator refuses the grant), so they
// match v1.
func suitabilityNeutral(iv string, p decide.PlayerState, st decide.State) string {
	calmest, struggling := p.Serial == st.MostHeadroom, p.Serial == st.Struggling
	switch {
	case protectInterventions[iv]:
		if iv == "grant_shield" && p.ShieldCharged {
			return "Already holds an agent shield charge, or has just used it: another shield is refused."
		}
		if iv == "grant_shield" && st.ShieldActivation != "" {
			switch st.HoldRefusal(p) {
			case decide.HoldRefusalBudgetExhausted:
				return "Shield budget for this game used up: another shield is refused."
			case decide.HoldRefusalPoolFull:
				return "Shield energy already full: another shield is refused."
			}
		}
		if iv == "grant_shield" && p.ShieldLimited {
			return "No more agent shields for this player right now (per-game cap or cooldown): another shield is refused."
		}
		switch p.Danger {
		case decide.DangerNearThreshold:
			return "Near the death threshold right now."
		case decide.DangerElevated:
			return "Under some pressure, some headroom left."
		case decide.DangerSafe:
			return "In no danger right now."
		}
	case balanceInterventions[iv], pressureInterventions[iv]:
		switch {
		case struggling || p.Danger == decide.DangerNearThreshold:
			return "Struggling: near the death threshold right now."
		case calmest:
			return "Most headroom below the death threshold right now."
		default:
			return "Neither the calmest nor struggling right now."
		}
	}
	return ""
}

// describeCandidates describes each alive player of st as a candidate for iv
// under the v1 profile.
func describeCandidates(st decide.State, iv string) map[string]string {
	return describeCandidatesFor(st, iv, decide.PromptProfileV1)
}

// describeCandidatesFor describes each alive player of st as a candidate for iv:
// danger, intensity rank, skill, then the suitability sentence in profile's
// wording.
func describeCandidatesFor(st decide.State, iv, profile string) map[string]string {
	suits := suitability
	if decide.NormalizePromptProfile(profile) == decide.PromptProfileNeutral {
		suits = suitabilityNeutral
	}
	ranked := st.RankedCount()
	out := map[string]string{}
	for _, p := range st.Players {
		if !p.Alive {
			continue
		}
		var b strings.Builder
		b.WriteString("Player " + p.Serial + ".")
		if d := dangerPhrase(p); d != "" {
			b.WriteString(" " + d)
		}
		if p.IntensityRank > 0 && ranked > 1 {
			b.WriteString(" Moving the " + rankWord(p.IntensityRank, ranked) + " of " + strconv.Itoa(ranked) + " alive players.")
		}
		switch p.Serial {
		case st.MostHeadroom:
			b.WriteString(" Most headroom of the alive players right now.")
		case st.Struggling:
			b.WriteString(" Currently struggling.")
		}
		if p.Shielded {
			b.WriteString(" Shielded a moment ago.")
		}
		if p.ShieldCharged {
			b.WriteString(" Holds an agent shield charge, or has just used it.")
		}
		b.WriteString(holdPhrase(p, st))
		b.WriteString(levelPhrase(p))
		if v := finiteOrNil(p.Skill); v != nil {
			b.WriteString(" Skill " + strconv.FormatFloat(*v, 'f', 2, 64) + ".")
		}
		if s := suits(iv, p, st); s != "" {
			b.WriteString(" " + s)
		}
		out[p.Serial] = b.String()
	}
	return out
}

// levelPhrase names the per-player factors the agent already applied (#2066), e.g.
// " Agent sensitivity factor 0.80 already applied.". Empty when none is set.
func levelPhrase(p decide.PlayerState) string {
	var s string
	if p.SensitivityFactor != nil {
		s += " Agent sensitivity factor " + strconv.FormatFloat(*p.SensitivityFactor, 'f', 2, 64) + " already applied."
	}
	if p.HandicapFactor != nil {
		s += " Agent handicap factor " + strconv.FormatFloat(*p.HandicapFactor, 'f', 2, 64) + " already applied."
	}
	return s
}

// rankWord is "most" for rank 1, "least" for the last rank, else "2nd most".
func rankWord(rank, n int) string {
	switch rank {
	case 1:
		return "most"
	case n:
		return "least"
	default:
		return ordinal(rank) + " most"
	}
}

// magnitudeLevels are intervention iv's own ACTING magnitude levels, low to
// high: scoreLevels evenly spaced values over its bounds
// (decide.ResolveMagnitude, so integer levers are whole numbers), fewer for an
// integer lever with a narrower range, so no two levels share a value. The
// neutral value ("no change") is never a level: an acting round must not spend
// an intervention on a no-op. nil without bounds.
func magnitudeLevels(iv string) []float64 {
	b, ok := decide.MagnitudeBounds(iv)
	if !ok {
		return nil
	}
	n := scoreLevels
	if b.Integer {
		n = min(n, int(b.Max-b.Min)+1)
	}
	if n < 2 {
		return []float64{b.Min, b.Max}
	}
	out := make([]float64, 0, n)
	for i := 0; i < n; i++ {
		v, _ := decide.ResolveMagnitude(iv, float64(i)/float64(n-1))
		if b.HasNeutral && math.Abs(v-b.Neutral) < 1e-9 {
			continue
		}
		out = append(out, v)
	}
	return out
}

// Two-way levers (#1262 review I1). For a per-player lever whose two ends do
// opposite things (protect vs handicap), Jev must not pick the target and the
// direction independently: it could pick the struggling player AND the
// handicap end. The DIRECTION is decided in Go from the chosen target's
// computed signals (direction), and Jev only answers the STRENGTH within that
// half of the lever. Both half-ladders are asked in the same details call (Jev
// answers questions independently anyway), and Decide keeps the one matching
// the chosen target's direction: no third call.

// Directions of a two-way lever.
const (
	directionHelp   = "help"
	directionReinIn = "rein_in"
)

// twoWay maps each two-way lever to whether its HELP side is the high end
// (set_player_handicap: 1.75 = big help) or the low end
// (adjust_player_sensitivity: 0.7 = most protected).
var twoWay = map[string]struct{ helpIsHigh bool }{
	"adjust_player_sensitivity": {helpIsHigh: false},
	"set_player_handicap":       {helpIsHigh: true},
}

// isTwoWay reports whether iv is a two-way per-player lever.
func isTwoWay(iv string) bool { _, ok := twoWay[iv]; return ok }

// halfLevels is the number of strength levels on each side of the neutral.
const halfLevels = 3

// strengthQuestionID is the internal details-stage question id of a
// two-way lever's strength in direction dir; it never leaves the backend.
func strengthQuestionID(dir string) string { return decide.QuestionFFAMagnitude + "." + dir }

// strengthDirection returns the direction of an internal strength question id
// ("" for any other id).
func strengthDirection(id string) string {
	for _, dir := range []string{directionHelp, directionReinIn} {
		if id == strengthQuestionID(dir) {
			return dir
		}
	}
	return ""
}

// direction decides, in Go, which side of a two-way lever the target gets:
// help for the struggling / near-threshold player, rein in for the player with
// the most headroom, help when ambiguous (fail-safe: helping never eliminates
// anyone).
func direction(st decide.State, target string) string {
	for _, p := range st.Players {
		if p.Serial != target {
			continue
		}
		switch {
		case p.Serial == st.Struggling || p.Danger == decide.DangerNearThreshold:
			return directionHelp
		case p.Serial == st.MostHeadroom:
			return directionReinIn
		}
	}
	return directionHelp
}

// strengthLevels are a two-way lever's values on side dir, weakest (nearest
// the neutral, which is excluded) to strongest (the bound), rounded to two
// decimals so they read like flag variants. nil for any other lever.
func strengthLevels(iv, dir string) []float64 {
	tw, ok := twoWay[iv]
	b, bok := decide.MagnitudeBounds(iv)
	if !ok || !bok || !b.HasNeutral {
		return nil
	}
	end := b.Min
	if tw.helpIsHigh == (dir == directionHelp) {
		end = b.Max
	}
	out := make([]float64, halfLevels)
	for k := 1; k <= halfLevels; k++ {
		v := b.Neutral + (end-b.Neutral)*float64(k)/halfLevels
		out[k-1] = math.Round(v*100) / 100
	}
	return out
}

// conditionedLevels returns the chosen intervention's levels when q is a
// details-stage magnitude question (ffa.magnitude, or a two-way lever's
// internal strength question), else nil.
func conditionedLevels(q decide.Question, chosen string) []float64 {
	if chosen == "" {
		return nil
	}
	if dir := strengthDirection(q.ID); dir != "" {
		return strengthLevels(chosen, dir)
	}
	if q.ID != decide.QuestionFFAMagnitude {
		return nil
	}
	return magnitudeLevels(chosen)
}

// interventionPhrase is "The agent will apply <iv> (<description>)", with the
// description in profile's wording.
func interventionPhrase(iv, profile string) string {
	s := "The agent will apply " + iv
	if d := descriptionsFor(profile)[iv]; d != "" {
		s += " (" + strings.TrimSuffix(d, ".") + ")"
	}
	return s
}

// conditionedMagnitude is the details-stage magnitude question for q: the
// chosen intervention's own levels, each saying what it does, or for a
// two-way lever's strength question the levels of one side, weakest first.
func conditionedMagnitude(q decide.Question, iv, profile string, levels []float64) (string, []string) {
	b, _ := decide.MagnitudeBounds(iv)
	if dir := strengthDirection(q.ID); dir != "" {
		effect := "help a struggling player (make them harder to eliminate)"
		if dir == directionReinIn {
			effect = "rein in the player with the most headroom (make them easier to eliminate)"
		}
		// Neutral (#2467, UC-040 BR-036): the effect on the target, no purpose.
		if decide.NormalizePromptProfile(profile) == decide.PromptProfileNeutral {
			effect = "make the target harder to eliminate"
			if dir == directionReinIn {
				effect = "make the target easier to eliminate"
			}
		}
		names := [halfLevels]string{"A slight change", "A moderate change", "The strongest change"}
		ladder := make([]string, len(levels))
		for i, v := range levels {
			ladder[i] = names[min(i, halfLevels-1)] + ": " + strconv.FormatFloat(v, 'g', 3, 64) + " " + b.Unit +
				" (neutral is " + strconv.FormatFloat(b.Neutral, 'g', 3, 64) + ")."
		}
		return interventionPhrase(iv, profile) + " to " + effect + ". How strong should the change be?", ladder
	}
	ends := magnitudeEnds[iv]
	n := len(levels)
	ladder := make([]string, n)
	for i, v := range levels {
		name := "A middle setting"
		switch {
		case i == 0:
			name = "The lowest setting"
		case i == n-1:
			name = "The highest setting"
		case 2*i < n-1:
			name = "A low setting"
		case 2*i > n-1:
			name = "A high setting"
		}
		text := name + ": " + strconv.FormatFloat(v, 'g', 3, 64) + " " + b.Unit
		switch {
		case i == 0 && ends[0] != "":
			text += " (" + ends[0] + ")"
		case i == n-1 && ends[1] != "":
			text += " (" + ends[1] + ")"
		case b.HasNeutral && b.Neutral > b.Min && v < b.Neutral:
			text += " (toward " + ends[0] + ")"
		case b.HasNeutral && b.Neutral > b.Min && v > b.Neutral:
			text += " (toward " + ends[1] + ")"
		}
		ladder[i] = text + "."
	}
	return interventionPhrase(iv, profile) + ". Which setting should it use?", ladder
}

// holdPhrase describes a hold-mode player's shield pool (#2096), e.g. " Shield energy
// 2.4 of 5 s, 9.1 s of shield budget left. Holding the shield now." Empty outside a
// hold game.
func holdPhrase(p decide.PlayerState, st decide.State) string {
	if st.ShieldActivation == "" {
		return ""
	}
	var s string
	if v := finiteOrNil(p.ShieldEnergyS); v != nil {
		s += " Shield energy " + strconv.FormatFloat(*v, 'f', 1, 64)
		if c := finiteOrNil(st.ShieldEnergyMaxS); c != nil {
			s += " of " + strconv.FormatFloat(*c, 'f', -1, 64)
		}
		s += " s"
		if b := finiteOrNil(p.ShieldBudgetRemainingS); b != nil {
			s += ", " + strconv.FormatFloat(*b, 'f', 1, 64) + " s of shield budget left"
		}
		s += "."
	}
	if p.ShieldActive {
		s += " Holding the shield now."
	}
	return s
}
