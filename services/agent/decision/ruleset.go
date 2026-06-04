package decision

import (
	"fmt"
	"sort"

	"github.com/joustmania/agent/gamecontext"
)

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

// sessionDuration returns the session duration or -1 when unknown.
func sessionDuration(c gamecontext.GameContext) float64 {
	if c.Session.DurationSeconds == nil {
		return -1
	}
	return *c.Session.DurationSeconds
}

// elimFraction is the fraction of known players already eliminated, used to
// gate "players are dying fast" rules. Returns 0 when nothing is known.
func elimFraction(c gamecontext.GameContext, active int) float64 {
	elims := len(c.Session.EliminationSequence)
	if elims == 0 || elims+active == 0 {
		return 0
	}
	return float64(elims) / float64(elims+active)
}

// enduranceCandidates implements R1 (slow the music while the session is young
// and players are dying fast) and R2 (a calming audio cue, same window, lower
// urgency).
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
	timeURgency := clamp01((fit.EnduranceMinSessionSeconds - dur) / fit.EnduranceMinSessionSeconds)
	urgency := clamp01(timeURgency * 2 * ef)
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
				Reason:          fmt.Sprintf("session at %.0fs of %.0fs endurance target with %d eliminations: slow the tempo", dur, fit.EnduranceMinSessionSeconds, len(c.Session.EliminationSequence)),
				ObjectiveServed: ObjectiveEndurance,
				Fitness:         fitness,
			},
			objective:  ObjectiveEndurance,
			urgency:    urgency,
			cost:       costOf(InterventionAdjustMusicTempo),
			difficulty: true,
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
		},
	}
}

// balancedCandidates implements R3 (handicap the skill outlier when the spread
// exceeds the fitness gap) and R4 (shield the weakest player while the field
// shrinks).
func balancedCandidates(c gamecontext.GameContext, fit FitnessThresholds) []candidate {
	active := activePlayers(c)
	var out []candidate

	// R3: skill gap.
	skilled := make([]*gamecontext.PlayerSignals, 0, len(active))
	for _, p := range active {
		if p.SkillLevel != nil {
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
			})
		}
	}

	// R4: shield the weakest while the field shrinks.
	if len(skilled) >= 2 {
		if ef := elimFraction(c, len(active)); ef > 0 {
			weakest := skilled[0]
			for _, p := range skilled[1:] {
				if *p.SkillLevel < *weakest.SkillLevel {
					weakest = p
				}
			}
			urgency := clamp01((1 - *weakest.SkillLevel) * 2 * ef)
			if urgency > 0 {
				out = append(out, candidate{
					decision: Decision{
						Intervention:    InterventionGrantShield,
						TargetSerial:    weakest.Serial,
						Reason:          fmt.Sprintf("shield the weakest player (skill %.2f) while the field shrinks", *weakest.SkillLevel),
						ObjectiveServed: ObjectiveBalanced,
						Fitness: map[string]float64{
							"weakest_skill":  *weakest.SkillLevel,
							"active_players": float64(len(active)),
						},
					},
					objective: ObjectiveBalanced,
					urgency:   urgency,
					cost:      costOf(InterventionGrantShield),
				})
			}
		}
	}
	return out
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
	active := activePlayers(c)
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
	}}

	// R6: eliminate the least-moving player.
	if dur > 1.5*target && len(active) > 2 {
		var lowest *gamecontext.PlayerSignals
		for _, p := range active {
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
		})
	}
	return out
}

// accelerateDominant reports whether accelerate is the strict argmax of the
// weights. A tie is NOT dominance — a tie must never silently end a game.
func accelerateDominant(weights map[string]float64) bool {
	acc, ok := weights[ObjectiveAccelerate]
	if !ok {
		return false
	}
	for name, w := range weights {
		if name != ObjectiveAccelerate && w >= acc {
			return false
		}
	}
	return true
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
			if p.MovementVariance == nil || *p.MovementVariance >= statueVarianceEpsilon {
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
		})
	}
	return out
}
