package decision

import (
	"fmt"
	"math"
	"sort"
	"strconv"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/gamecontext"
)

// LLM-only interventions (#800): adjust_volume, revive_player,
// adjust_global_sensitivity, adjust_global_difficulty, and set_pacing_profile
// are deliberately NOT emitted by any rule in this set. They need judgment a
// deterministic ruleset cannot encode (who deserves a revive; when reshaping
// session-wide difficulty/pacing is socially right) and are reserved for the
// M4 LLM path. Adding a rule for one of them is a product decision, not an
// oversight fix — see services/agent/README.md "LLM-only interventions".
//
// candidate is a potential decision produced by a rule, before objective
// weighting, policy filtering, and budget admission.
type candidate struct {
	decision  Decision
	objective string
	urgency   float64 // 0-1, how strongly the rule's signal fires
	cost      float64 // weighted budget cost (limiter.go)
	// difficulty marks interventions that change difficulty / movement demand
	// (tempo, sensitivity): they trigger the variance-baseline cooldown and
	// are blocked for low-battery players by the battery policy.
	difficulty bool
	// shieldCatch marks an R4 grant_shield candidate: rankLocked keeps only one of
	// them (pickShieldCatchUp, UC-040 A41, BR-030).
	shieldCatch bool
	// danger marks an R4 candidate of the danger path (UC-040 A43, BR-032): a player
	// in sustained danger before the first elimination, skill level optional.
	danger bool
	// rule is the id of the rule that proposed the candidate ("R1".."R9", "R4d"
	// for R4's danger path); the decide answer's prediction comes from it (#2479,
	// UC-040 A44, BR-034, rulesPrediction). Unexported and never serialized, so
	// the rules golden is unchanged.
	rule string
}

// statueVarianceEpsilon is the movement-variance level below which a player is
// considered to be gaming the EMA by holding still (R8).
const statueVarianceEpsilon = 0.05

// clamp01 clamps v to [0, 1].
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

// activePlayers returns the players marked Active, sorted by serial for
// deterministic iteration.
func activePlayers(c gamecontext.GameContext) []*gamecontext.PlayerSignals {
	out := make([]*gamecontext.PlayerSignals, 0, len(c.Players))
	for _, p := range c.Players {
		if p.Active != nil && *p.Active {
			out = append(out, p)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Serial < out[j].Serial })
	return out
}

// readingsFresh reports whether the player's movement readings are live at the
// context's UpdatedAt: R4d's signal (decide.FreshReadings), shared by R3, R4 and R8
// before they read a per-player skill level or movement variance (UC-040 A46,
// BR-037, #2511). No readings, a frozen value or an unknown context time is not
// fresh (fail closed).
func readingsFresh(c gamecontext.GameContext, p *gamecontext.PlayerSignals) bool {
	return decide.FreshReadings(p.History, c.UpdatedAt)
}

// freshPlayers returns the alive players (activePlayers order) whose readings are
// fresh: the target pool of R6, and the check R9 applies to its pick (UC-040 A46
// step 5, BR-037, #2517). A Fight
// Club or Tournament player who just went back to waiting keeps its last alive
// state until it is evicted, but its readings stop with its gauges, so it drops
// out of this pool within 3 s.
func freshPlayers(c gamecontext.GameContext) []*gamecontext.PlayerSignals {
	active := activePlayers(c)
	out := active[:0]
	for _, p := range active {
		if readingsFresh(c, p) {
			out = append(out, p)
		}
	}
	return out
}

// sessionDuration returns the session duration or -1 when unknown.
func sessionDuration(c gamecontext.GameContext) float64 {
	if c.Session.DurationSeconds == nil {
		return -1
	}
	return *c.Session.DurationSeconds
}

// elimFraction is the fraction of the game's players already eliminated, used to
// gate "players are dying fast" rules (R1, R2, R4's elimination path). Returns 0
// when nothing is known. The players in the game are the game_active_players
// roster (set at the start, waiting players included) when it is larger than
// eliminations + alive players: in Fight Club and Tournament a player waiting for
// its fight is evicted from the context but still in the game (UC-040 A49, BR-038,
// #2513). Without a roster value it is eliminations + alive players, as before.
func elimFraction(c gamecontext.GameContext, active int) float64 {
	elims := len(c.Session.EliminationSequence)
	if elims == 0 {
		return 0
	}
	inGame := elims + active
	if roster := c.Session.ActivePlayerCount; roster != nil && *roster > inGame {
		inGame = *roster
	}
	return float64(elims) / float64(inGame)
}

// enduranceTempo is R1's adjust_music_tempo value: the fast end of the lever's
// bounds (1.3, the `fast` variant of music_tempo_override). The coordinator's
// death threshold slides from SLOW_MAX to FAST_MAX with the tempo, so the fast
// end tolerates the most movement and slows the eliminations, which is what
// endurance wants (#2487, UC-040 A51, BR-040). It mirrors the `slow` variant
// (1.0) that R1 was documented to write: the same 0.15 step from the 1.15
// default, in the other direction.
var enduranceTempo = func() string {
	b, _ := decide.MagnitudeBounds(InterventionAdjustMusicTempo)
	return strconv.FormatFloat(b.Max, 'g', -1, 64)
}()

// enduranceCandidates implements R1 (speed the music up, raising the death
// threshold, while the session is young and players are dying fast) and R2 (a
// calming audio cue, same window, lower urgency).
func enduranceCandidates(c gamecontext.GameContext, fit FitnessThresholds) []candidate {
	dur := sessionDuration(c)
	if dur < 0 || dur >= fit.EnduranceMinSessionSeconds {
		return nil
	}
	active := activePlayers(c)
	ef := elimFraction(c, len(active))
	if ef == 0 {
		return nil // nobody eliminated yet — the session is not at risk
	}
	timeUrgency := clamp01((fit.EnduranceMinSessionSeconds - dur) / fit.EnduranceMinSessionSeconds)
	urgency := clamp01(timeUrgency * 2 * ef)
	if urgency == 0 {
		return nil
	}
	fitness := map[string]float64{
		"session_duration":    dur,
		"min_session_seconds": fit.EnduranceMinSessionSeconds,
		"eliminations":        float64(len(c.Session.EliminationSequence)),
	}
	return []candidate{
		{
			decision: Decision{
				Intervention:    InterventionAdjustMusicTempo,
				Value:           enduranceTempo,
				Reason:          fmt.Sprintf("session at %.0fs of %.0fs endurance target with %d eliminations: speed up the tempo to raise the death threshold", dur, fit.EnduranceMinSessionSeconds, len(c.Session.EliminationSequence)),
				ObjectiveServed: ObjectiveEndurance,
				Fitness:         fitness,
			},
			objective:  ObjectiveEndurance,
			urgency:    urgency,
			cost:       costOf(InterventionAdjustMusicTempo),
			difficulty: true,
			rule:       "R1",
		},
		{
			decision: Decision{
				Intervention:    InterventionPlayAudioCue,
				Reason:          "calming cue while the field shrinks early in the session",
				ObjectiveServed: ObjectiveEndurance,
				Fitness: map[string]float64{
					"session_duration":    dur,
					"min_session_seconds": fit.EnduranceMinSessionSeconds,
				},
			},
			objective: ObjectiveEndurance,
			urgency:   clamp01(urgency * 0.5),
			cost:      costOf(InterventionPlayAudioCue),
			rule:      "R2",
		},
	}
}

// balancedCandidates implements R3 (handicap the skill outlier when the spread
// exceeds the fitness gap) and R4 (shield a struggling player while the field
// shrinks).
// R4 yields one candidate per player; rankLocked keeps one (pickShieldCatchUp).
func balancedCandidates(c gamecontext.GameContext, fit FitnessThresholds) []candidate {
	active := activePlayers(c)
	var out []candidate

	// R3: skill gap. R3 and R4's elimination path read only the players whose
	// movement readings are fresh (UC-040 A46, BR-037, #2511): a stalled or frozen
	// player's skill gauge is re-exported with its last value, so it is neither a
	// target nor part of the spread, the mean or the strongest player.
	skilled := make([]*gamecontext.PlayerSignals, 0, len(active))
	for _, p := range active {
		if p.SkillLevel != nil && readingsFresh(c, p) {
			skilled = append(skilled, p)
		}
	}
	if len(skilled) >= 2 {
		minP, maxP := skilled[0], skilled[0]
		for _, p := range skilled[1:] {
			if *p.SkillLevel < *minP.SkillLevel {
				minP = p
			}
			if *p.SkillLevel > *maxP.SkillLevel {
				maxP = p
			}
		}
		gap := *maxP.SkillLevel - *minP.SkillLevel
		if gap > fit.BalancedMaxSkillGap {
			out = append(out, candidate{
				decision: Decision{
					Intervention:    InterventionAdjustPlayerSensitivity,
					TargetSerial:    maxP.Serial,
					Reason:          fmt.Sprintf("skill gap %.2f exceeds %.2f: handicap the outlier", gap, fit.BalancedMaxSkillGap),
					ObjectiveServed: ObjectiveBalanced,
					Fitness: map[string]float64{
						"skill_gap":     gap,
						"max_skill_gap": fit.BalancedMaxSkillGap,
						"outlier_skill": *maxP.SkillLevel,
					},
				},
				objective:  ObjectiveBalanced,
				urgency:    clamp01((gap - fit.BalancedMaxSkillGap) / (1 - fit.BalancedMaxSkillGap)),
				cost:       costOf(InterventionAdjustPlayerSensitivity),
				difficulty: true,
				rule:       "R3",
			})
		}
	}

	// R4: shield the weak while the field shrinks. The candidates are the struggling
	// players: alive, with a skill level strictly below the mean skill of the alive
	// skilled field, and never the strongest alive player (so never R3's outlier, and
	// never the stronger player of a duel). Each has their own urgency; after the
	// filters and minScore the rules keep one R4 candidate: the one with the fewest
	// agent shields this game, the weakest on a tie (catch-up, UC-040 A41, BR-030;
	// pickShieldCatchUp).
	if len(skilled) >= 2 {
		if ef := elimFraction(c, len(active)); ef > 0 {
			for _, p := range shieldCandidates(skilled) {
				urgency := clamp01((1 - *p.SkillLevel) * 2 * ef)
				if urgency <= 0 {
					continue
				}
				out = append(out, candidate{
					decision: Decision{
						Intervention:    InterventionGrantShield,
						TargetSerial:    p.Serial,
						Reason:          fmt.Sprintf("shield a struggling player (skill %.2f) while the field shrinks", *p.SkillLevel),
						ObjectiveServed: ObjectiveBalanced,
						Fitness: map[string]float64{
							"target_skill":   *p.SkillLevel,
							"active_players": float64(len(active)),
						},
					},
					objective:   ObjectiveBalanced,
					urgency:     urgency,
					cost:        costOf(InterventionGrantShield),
					shieldCatch: true,
					rule:        "R4",
				})
			}
		}
	}
	// R4 before the first elimination: shield a player in sustained danger (UC-040
	// A43, BR-032). The elimination path above needs ef > 0, so the two never both
	// propose, and after the first elimination R4 is exactly the path above.
	if len(c.Session.EliminationSequence) == 0 {
		out = append(out, dangerShieldCandidates(c, active, fit)...)
	}
	return out
}

// dangerShieldCandidates is R4's danger path (UC-040 A43, BR-032; #2461): with no
// elimination yet and at least two alive players, one grant_shield candidate per
// alive player whose newest readings stay at or above the clamped
// policy.shield_danger_ratio of the death threshold for policy.shield_danger_samples
// consecutive readings (decide.SustainedDanger, fail closed on missing or stale
// data). Urgency is the window's lowest ratio, at most 1. The candidates are
// shieldCatch, so they pass the same filters and rotation as the elimination path.
func dangerShieldCandidates(c gamecontext.GameContext, active []*gamecontext.PlayerSignals, fit FitnessThresholds) []candidate {
	if len(active) < 2 || fit.ShieldDangerOff {
		return nil
	}
	level, n := shieldDangerParams(fit)
	var out []candidate
	for _, p := range active {
		ratio, ok := decide.SustainedDanger(p.History, c.SpeedTrack, c.Session.DeathThreshold, c.UpdatedAt, level, n)
		if !ok {
			continue
		}
		urgency := clamp01(ratio)
		fitness := map[string]float64{
			"danger_ratio":   urgency,
			"danger_samples": float64(n),
			"active_players": float64(len(active)),
		}
		if p.SkillLevel != nil {
			fitness["target_skill"] = *p.SkillLevel
		}
		out = append(out, candidate{
			decision: Decision{
				Intervention:    InterventionGrantShield,
				TargetSerial:    p.Serial,
				Reason:          fmt.Sprintf("shield a player in sustained danger (%d readings at or above %.2f of the death threshold) before the first elimination", n, level),
				ObjectiveServed: ObjectiveBalanced,
				Fitness:         fitness,
			},
			objective:   ObjectiveBalanced,
			urgency:     urgency,
			cost:        costOf(InterventionGrantShield),
			shieldCatch: true,
			danger:      true,
			rule:        "R4d",
		})
	}
	return out
}

// shieldCandidates returns R4's candidates among the alive skilled players (UC-040
// A41 step 2, BR-030): skill strictly below the field's mean and below the strongest
// player's skill. The second check is not implied in floating point: with equal
// skills the computed mean can land above them. An even field has no candidate.
func shieldCandidates(skilled []*gamecontext.PlayerSignals) []*gamecontext.PlayerSignals {
	sum, strongest := 0.0, *skilled[0].SkillLevel
	for _, p := range skilled {
		sum += *p.SkillLevel
		strongest = max(strongest, *p.SkillLevel)
	}
	mean := sum / float64(len(skilled))
	out := make([]*gamecontext.PlayerSignals, 0, len(skilled))
	for _, p := range skilled {
		if *p.SkillLevel < mean && *p.SkillLevel < strongest {
			out = append(out, p)
		}
	}
	return out
}

// pickShieldCatchUp keeps one R4 candidate in ranked (UC-040 A41, BR-030; #2395):
// among the R4 candidates that survived the filters (charge, cap, cooldown, pool,
// battery) and minScore, the one with the fewest agent shields in this game (grants,
// Loop.ShieldGrants), ties to the weakest, then the serial. A player who is not a
// candidate never holds the others back. The kept candidate takes the best R4
// candidate's score and place (the rotation picks the target, not the priority), so
// it never drops out of the emission where the best R4 candidate would have gone
// out. When a weaker R4 candidate was passed over, its reason says catch-up and its
// fitness carries shield_grants. nil grants: every count is 0, so the weakest
// surviving candidate.
func pickShieldCatchUp(ranked []scoredCandidate, c gamecontext.GameContext, grants map[string]int) []scoredCandidate {
	// A danger candidate may have no skill level (A43): it sorts after the skilled
	// ones on a tie.
	skill := func(sc scoredCandidate) float64 {
		if p := c.Players[sc.decision.TargetSerial]; p != nil && p.SkillLevel != nil {
			return *p.SkillLevel
		}
		return math.Inf(1)
	}
	best, weakest, top := -1, -1, -1
	for i, sc := range ranked {
		if !sc.shieldCatch {
			continue
		}
		if top < 0 {
			top = i // ranked is sorted: the first R4 candidate has the best score
		}
		if weakest < 0 || skill(sc) < skill(ranked[weakest]) ||
			(skill(sc) == skill(ranked[weakest]) && sc.decision.TargetSerial < ranked[weakest].decision.TargetSerial) {
			weakest = i
		}
		if best < 0 || fewerShields(sc, ranked[best], grants, skill) {
			best = i
		}
	}
	if best < 0 {
		return ranked
	}
	kept := ranked[best]
	kept.score = ranked[top].score
	out := make([]scoredCandidate, 0, len(ranked))
	for i, sc := range ranked {
		if sc.shieldCatch && i != top {
			continue
		}
		if i == top {
			sc = kept
		}
		if best != weakest && sc.shieldCatch {
			d := sc.decision
			n := grants[d.TargetSerial]
			if sc.danger {
				d.Reason = fmt.Sprintf("catch-up: shield the player in sustained danger with the fewest agent shields (%d shields) before the first elimination", n)
			} else {
				d.Reason = fmt.Sprintf("catch-up: shield the player with the fewest agent shields (skill %.2f, %d shields) while the field shrinks", skill(sc), n)
			}
			fit := make(map[string]float64, len(d.Fitness)+1)
			for k, v := range d.Fitness {
				fit[k] = v
			}
			fit["shield_grants"] = float64(n)
			d.Fitness = fit
			sc.decision = d
		}
		out = append(out, sc)
	}
	return out
}

// fewerShields orders R4 candidates for pickShieldCatchUp: fewer agent shields this
// game first, then the weaker player, then the lower serial.
func fewerShields(a, b scoredCandidate, grants map[string]int, skill func(scoredCandidate) float64) bool {
	ga, gb := grants[a.decision.TargetSerial], grants[b.decision.TargetSerial]
	if ga != gb {
		return ga < gb
	}
	if skill(a) != skill(b) {
		return skill(a) < skill(b)
	}
	return a.decision.TargetSerial < b.decision.TargetSerial
}

// accelerateCandidates implements R5 (speed up past the target duration),
// R6 (eliminate the least-moving player when far past target), and R7
// (end the game when way past target — only when accelerate strictly
// dominates the weights; ties never end a game).
func accelerateCandidates(c gamecontext.GameContext, fit FitnessThresholds, weights map[string]float64) []candidate {
	dur := sessionDuration(c)
	target := fit.AccelerateTargetSessionSeconds
	if dur < 0 || target <= 0 || dur <= target {
		return nil
	}
	fresh := freshPlayers(c)
	durFit := map[string]float64{
		"session_duration":       dur,
		"target_session_seconds": target,
	}
	out := []candidate{{
		decision: Decision{
			Intervention:    InterventionAdjustMusicTempo,
			Reason:          fmt.Sprintf("session at %.0fs past the %.0fs accelerate target: speed up", dur, target),
			ObjectiveServed: ObjectiveAccelerate,
			Fitness:         durFit,
		},
		objective:  ObjectiveAccelerate,
		urgency:    clamp01((dur - target) / target),
		cost:       costOf(InterventionAdjustMusicTempo),
		difficulty: true,
		rule:       "R5",
	}}

	// R6: eliminate the least-moving player, among the players with fresh readings.
	if dur > 1.5*target && len(fresh) > 2 {
		var lowest *gamecontext.PlayerSignals
		for _, p := range fresh {
			if p.MovementIntensity == nil {
				continue
			}
			if lowest == nil || *p.MovementIntensity < *lowest.MovementIntensity {
				lowest = p
			}
		}
		if lowest != nil {
			fitness := map[string]float64{
				"session_duration":       dur,
				"target_session_seconds": target,
				"lowest_intensity":       *lowest.MovementIntensity,
			}
			out = append(out, candidate{
				decision: Decision{
					Intervention:    InterventionEliminatePlayer,
					TargetSerial:    lowest.Serial,
					Reason:          fmt.Sprintf("session 1.5x past target: eliminate the least active player (%.2fg)", *lowest.MovementIntensity),
					ObjectiveServed: ObjectiveAccelerate,
					Fitness:         fitness,
				},
				objective: ObjectiveAccelerate,
				urgency:   clamp01((dur - 1.5*target) / target),
				cost:      costOf(InterventionEliminatePlayer),
				rule:      "R6",
			})
		}
	}

	// R7: end the game — terminal, only when accelerate strictly dominates.
	if dur > 2*target && accelerateDominant(weights) {
		out = append(out, candidate{
			decision: Decision{
				Intervention:    InterventionEndGame,
				Reason:          fmt.Sprintf("session 2x past the %.0fs target with accelerate dominant: end the game", target),
				ObjectiveServed: ObjectiveAccelerate,
				Fitness:         durFit,
			},
			objective: ObjectiveAccelerate,
			urgency:   clamp01((dur - 2*target) / target),
			cost:      costOf(InterventionEndGame),
			rule:      "R7",
		})
	}
	return out
}

// accelerateDominant reports whether accelerate is the strict argmax of the
// weights. A tie is NOT dominance — a tie must never silently end a game.
func accelerateDominant(weights map[string]float64) bool {
	return objectiveDominant(weights, ObjectiveAccelerate)
}

// chaosCandidates implements R8 (rumble a "statue" player whose movement
// variance shows them gaming the EMA — no-ops until producers ship the
// variance metric) and R9 (a periodic random controller effect, driven by the
// engine's rng roll and pre-picked target).
func chaosCandidates(c gamecontext.GameContext, pol Policy, roll float64, rollTarget string) []candidate {
	var out []candidate
	dur := sessionDuration(c)

	// R8: statue detection — requires the variance window to be filled.
	if dur >= pol.MovementVarianceWindow.Seconds() {
		for _, p := range activePlayers(c) {
			if p.MovementVariance == nil || *p.MovementVariance >= statueVarianceEpsilon || !readingsFresh(c, p) {
				continue
			}
			out = append(out, candidate{
				decision: Decision{
					Intervention:    InterventionSendControllerEffect,
					TargetSerial:    p.Serial,
					Reason:          fmt.Sprintf("near-zero movement variance (%.3f): nudge the statue", *p.MovementVariance),
					ObjectiveServed: ObjectiveChaos,
					Fitness: map[string]float64{
						"movement_variance": *p.MovementVariance,
						"variance_epsilon":  statueVarianceEpsilon,
					},
				},
				objective: ObjectiveChaos,
				urgency:   clamp01(1 - *p.MovementVariance/statueVarianceEpsilon),
				cost:      costOf(InterventionSendControllerEffect),
				rule:      "R8",
			})
		}
	}

	// R9: periodic random effect.
	if rollTarget != "" {
		out = append(out, candidate{
			decision: Decision{
				Intervention:    InterventionSendControllerEffect,
				TargetSerial:    rollTarget,
				Reason:          "chaos: random controller effect",
				ObjectiveServed: ObjectiveChaos,
				Fitness:         map[string]float64{"chaos_roll": roll},
			},
			objective: ObjectiveChaos,
			urgency:   clamp01(0.6 * roll),
			cost:      costOf(InterventionSendControllerEffect),
			rule:      "R9",
		})
	}
	return out
}
