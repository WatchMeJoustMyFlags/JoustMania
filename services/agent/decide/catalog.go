package decide

import "sort"

// Intervention names: exactly the interventions_allowed vocabulary of the flagd
// agent schema (services/flagd/agent.json). decision's Intervention* constants
// alias these, so a name is spelled once.
const (
	IntvPlayAudioCue            = "play_audio_cue"
	IntvSendControllerEffect    = "send_controller_effect"
	IntvAdjustVolume            = "adjust_volume"
	IntvAdjustMusicTempo        = "adjust_music_tempo"
	IntvAdjustPlayerSensitivity = "adjust_player_sensitivity"
	IntvSetPlayerHandicap       = "set_player_handicap"
	IntvGrantShield             = "grant_shield"
	IntvToughnessBoost          = "toughness_boost"
	IntvSoftPenalty             = "soft_penalty"
	IntvAdjustGlobalSensitivity = "adjust_global_sensitivity"
	IntvAdjustGlobalDifficulty  = "adjust_global_difficulty"
	IntvSetPacingProfile        = "set_pacing_profile"
	IntvRampTempo               = "ramp_tempo"
	IntvAutoRubberband          = "auto_rubberband"
	IntvEliminatePlayer         = "eliminate_player"
	IntvRevivePlayer            = "revive_player"
	IntvEndGame                 = "end_game"
	// IntvNoop is the synthetic do-nothing intervention; it is not in the catalog.
	IntvNoop = "noop"
)

// Intervention is one row of the catalog: everything the agent knows about an
// intervention by name.
type Intervention struct {
	Name string
	// PlayerTargeted: applied to ONE player (its ffa.target is an alive serial);
	// every other intervention is session-scoped and takes TargetNone. These are
	// the action sink's TargetSerial users (services/agent/actions/writer.go).
	PlayerTargeted bool
	// Weight is the cost against the per-minute budget (docs/research/722-intervention-surface.md
	// section 5): soft 0.5, medium 1, hard 2.
	Weight float64
	// Idempotent: the flag is of the registry kind state or level (flags.md 13.1),
	// so writing the same value again changes nothing at the coordinator. Grants
	// and events never are.
	Idempotent bool
	// Protective: a medium-cost intervention the send_controller_effect nudge
	// reserve keeps room for (BR-015).
	Protective bool
	// Difficulty: changes difficulty or movement demand; arms the variance
	// cooldown when dispatched.
	Difficulty bool
	// JevDescription is the lever's effect as the jev backend describes it to
	// the model (from the retired FFA impact model of llm/prompt.go, #1102).
	JevDescription string
}

// Catalog is the single table of interventions; every per-intervention set in
// the agent (target scope, budget weight, idempotency, reserve, difficulty,
// jev descriptions) derives from it. Do not mutate.
var Catalog = []Intervention{
	{
		Name:   IntvPlayAudioCue,
		Weight: 0.5,
		JevDescription: "Play an audio cue or announcement to build tension or excitement. No effect on who " +
			"gets eliminated.",
	},
	{
		Name:           IntvSendControllerEffect,
		PlayerTargeted: true,
		Weight:         0.5,
		JevDescription: "Flash or rumble one player's controller as feedback. No effect on who gets " +
			"eliminated.",
	},
	{
		Name:           IntvAdjustVolume,
		Weight:         0.5,
		Idempotent:     true,
		JevDescription: "Change the music volume. Atmosphere only: no effect on who gets eliminated.",
	},
	{
		Name:       IntvAdjustMusicTempo,
		Weight:     1,
		Idempotent: true,
		Difficulty: true,
		JevDescription: "Change the music tempo for everyone. Faster music raises everyone's death " +
			"threshold (more movement tolerated, fewer eliminations); slower music lowers it (eliminations come faster).",
	},
	{
		Name:           IntvAdjustPlayerSensitivity,
		PlayerTargeted: true,
		Weight:         1,
		Idempotent:     true,
		Protective:     true,
		Difficulty:     true,
		JevDescription: "Change one player's sensitivity, protecting them (harder to eliminate) " +
			"or handicapping them (easier to eliminate).",
	},
	{
		Name:           IntvSetPlayerHandicap,
		PlayerTargeted: true,
		Weight:         1,
		Idempotent:     true,
		Protective:     true,
		Difficulty:     true,
		JevDescription: "Help one player (harder to eliminate) or rein in one player (easier to " +
			"eliminate), to balance a lopsided game.",
	},
	{
		Name:           IntvGrantShield,
		PlayerTargeted: true,
		Weight:         1,
		Protective:     true,
		JevDescription: "Make one player immune to elimination for a few seconds. Saves a player who is about " +
			"to be eliminated unfairly.",
	},
	{
		Name:           IntvToughnessBoost,
		PlayerTargeted: true,
		Weight:         1,
		Protective:     true,
		JevDescription: "Make one player much harder, but not impossible, to eliminate for a few seconds; " +
			"weaker than grant_shield.",
	},
	{
		Name:           IntvSoftPenalty,
		PlayerTargeted: true,
		Weight:         1,
		JevDescription: "Put pressure on one player: a warning cue, or briefly making them easier to " +
			"eliminate. Recoverable, never an instant elimination.",
	},
	{
		Name:       IntvAdjustGlobalSensitivity,
		Weight:     2,
		Idempotent: true,
		Difficulty: true,
		JevDescription: "Change the sensitivity level for all players at once. A higher level makes " +
			"everyone much harder to eliminate, a lower level much easier; one step is a sizable jump.",
	},
	{
		Name:       IntvAdjustGlobalDifficulty,
		Weight:     1,
		Idempotent: true,
		Difficulty: true,
		JevDescription: "Scale the difficulty for all players at once, making eliminations come " +
			"faster (harder) or slower (easier) for everyone.",
	},
	{
		Name:       IntvSetPacingProfile,
		Weight:     1,
		Idempotent: true,
		Difficulty: true,
		JevDescription: "Switch the session's pacing profile (how the music tempo evolves over the round), " +
			"speeding up or slowing down eliminations for everyone.",
	},
	{
		Name:       IntvRampTempo,
		Weight:     1,
		Idempotent: true,
		Difficulty: true,
		JevDescription: "Ramp the music tempo smoothly over several seconds instead of one step, gradually " +
			"making the game more forgiving or more dangerous for everyone.",
	},
	{
		Name:       IntvAutoRubberband,
		Weight:     1,
		Idempotent: true,
		JevDescription: "Automatically help the trailing players (harder to eliminate) to keep a lopsided " +
			"game close, without ever inverting the standings.",
	},
	{
		Name:           IntvEliminatePlayer,
		PlayerTargeted: true,
		Weight:         2,
		JevDescription: "Immediately and permanently eliminate one player. Maximal and irreversible.",
	},
	{
		Name:           IntvRevivePlayer,
		PlayerTargeted: true,
		Weight:         2,
		JevDescription: "Bring an eliminated player back into the game.",
	},
	{
		Name:           IntvEndGame,
		Weight:         2,
		JevDescription: "End the round now. Maximal and irreversible; the last player alive wins.",
	},
}

// ByName returns the catalog row of name.
func ByName(name string) (Intervention, bool) {
	for _, iv := range Catalog {
		if iv.Name == name {
			return iv, true
		}
	}
	return Intervention{}, false
}

// Names returns the catalog names for which keep is true (nil keeps all), sorted.
func Names(keep func(Intervention) bool) []string {
	var out []string
	for _, iv := range Catalog {
		if keep == nil || keep(iv) {
			out = append(out, iv.Name)
		}
	}
	sort.Strings(out)
	return out
}

// Set returns the names for which keep is true as a lookup set.
func Set(keep func(Intervention) bool) map[string]bool {
	out := make(map[string]bool, len(Catalog))
	for _, iv := range Catalog {
		if keep(iv) {
			out[iv.Name] = true
		}
	}
	return out
}
