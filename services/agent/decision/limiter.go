package decision

import (
	"sync"
	"time"
)

// Intervention identifiers — exactly the interventions_allowed vocabulary from
// the flagd agent schema (services/flagd/agent.json) and the #722 research §6.
const (
	InterventionPlayAudioCue            = "play_audio_cue"
	InterventionSendControllerEffect    = "send_controller_effect"
	InterventionAdjustVolume            = "adjust_volume"
	InterventionAdjustMusicTempo        = "adjust_music_tempo"
	InterventionAdjustPlayerSensitivity = "adjust_player_sensitivity"
	InterventionGrantShield             = "grant_shield"
	InterventionAdjustGlobalSensitivity = "adjust_global_sensitivity"
	InterventionEliminatePlayer         = "eliminate_player"
	InterventionRevivePlayer            = "revive_player"
	InterventionEndGame                 = "end_game"
)

// interventionCost weights the per-minute budget per the #722 research §5:
// a physical party game tolerates ambient nudges far better than visible
// state changes, so soft actions cost 0.5, reversible tunables 1, and hard
// state changes 2. Unknown interventions default to the hard cost (fail safe).
var interventionCost = map[string]float64{
	InterventionPlayAudioCue:            0.5,
	InterventionSendControllerEffect:    0.5,
	InterventionAdjustVolume:            0.5,
	InterventionAdjustMusicTempo:        1,
	InterventionAdjustPlayerSensitivity: 1,
	InterventionGrantShield:             1,
	InterventionAdjustGlobalSensitivity: 2,
	InterventionEliminatePlayer:         2,
	InterventionRevivePlayer:            2,
	InterventionEndGame:                 2,
}

// costOf returns the budget cost for an intervention, defaulting to the hard
// cost for unknown identifiers.
func costOf(intervention string) float64 {
	if c, ok := interventionCost[intervention]; ok {
		return c
	}
	return 2
}

// rateWindow is the sliding window over which the weighted budget applies.
const rateWindow = time.Minute

// windowLimiter enforces the weighted interventions-per-minute budget
// (policy.max_interventions_per_minute) over a sliding window. Safe for
// concurrent use.
//
// Budget semantics: the engine charges for every decision it EMITS, including
// decisions the Loop's Permissions layer later blocks. The engine cannot see
// permissions (that seam lives in the Loop, after Evaluate returns), the §5
// model rate-limits attempts, and the game coordinator's flag-application
// layer (#730) is the authoritative backstop — charging attempts keeps
// emission bounded even under a misconfigured allowed-list.
type windowLimiter struct {
	mu      sync.Mutex
	entries []limiterEntry
}

type limiterEntry struct {
	at   time.Time
	cost float64
}

// admit reports whether a decision of the given cost fits the budget at time
// now, charging it when admitted.
func (l *windowLimiter) admit(now time.Time, cost, budget float64) bool {
	l.mu.Lock()
	defer l.mu.Unlock()

	cutoff := now.Add(-rateWindow)
	kept := l.entries[:0]
	spent := 0.0
	for _, e := range l.entries {
		if e.at.After(cutoff) {
			kept = append(kept, e)
			spent += e.cost
		}
	}
	l.entries = kept

	if spent+cost > budget {
		return false
	}
	l.entries = append(l.entries, limiterEntry{at: now, cost: cost})
	return true
}
