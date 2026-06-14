// Package gamecontext holds the accumulated, denormalized view of a JoustMania
// game session that the agent builds up from streamed OTel metrics and spans.
//
// Pointer fields are used deliberately to distinguish "never observed" (nil)
// from a genuine zero value (e.g. a battery reading of 0%). Each signal documents
// the source metric it is derived from. Some sources are live today; others are
// proposed by the #722 intervention-surface research and are wired in here for
// forward compatibility (they stay nil until producers ship them).
package gamecontext

import "time"

// PlayerSignals is the per-controller view the agent reasons over.
type PlayerSignals struct {
	Serial string

	// MovementIntensity is the player's current accel magnitude.
	// Source: game_player_accel_magnitude{serial} (live).
	MovementIntensity *float64

	// MovementVariance is the variance of recent movement.
	// Source: game_player_movement_variance{serial} (live; the coordinator emits
	// it per-player at ~10Hz, #730 / #1015). nil until the first reading arrives.
	MovementVariance *float64

	// BatteryPct is the controller battery percentage (0-100).
	// Preferred source: controller_battery_pct{serial} (proposed).
	// Fallback source: controller_battery_level{serial} on a 0-5 scale, * 20.
	BatteryPct *float64

	// SkillLevel is a normalized 0.0-1.0 skill estimate.
	// Preferred source: game_player_skill_level{serial} (live; the coordinator
	// emits it per-player at ~10Hz, #730 / #1015).
	// Fallback proxy: game_player_playstyle{serial} (0-3) / 3.
	SkillLevel *float64

	// Active indicates the player is currently participating.
	// Preferred source: game_player_alive{serial} == 1.
	// Fallback source: controller_connected{serial} == 1.
	Active *bool

	// LastUpdate is the last time any signal for this player changed.
	LastUpdate time.Time

	// batteryPreferred/skillPreferred/activePreferred record whether a
	// preferred-source value has been seen for the corresponding signal. Once
	// set, fallback-source updates for that signal are ignored so the better
	// source always wins regardless of arrival order.
	batteryPreferred bool
	skillPreferred   bool
	activePreferred  bool
}

// SessionSignals is the per-game (session) view the agent reasons over.
type SessionSignals struct {
	// DurationSeconds is the elapsed game time.
	// Source: game_duration_seconds (live).
	DurationSeconds *float64

	// ActivePlayerCount is the number of players still in the game.
	// Source: game_active_players (live).
	ActivePlayerCount *int

	// EliminationSequence is the ordered list of eliminated serials,
	// first eliminated first. Derived from the elimination sources below.
	EliminationSequence []string

	// GameActive indicates a game is in progress.
	// Source: game_active == 1 (live).
	GameActive *bool

	// GameMode is the current game mode.
	// Source: game_current_mode{mode} label (value != 0) (live), or span
	// attribute game.mode.
	GameMode *string

	// LastUpdate is the last time any session signal changed.
	LastUpdate time.Time
}

// GameContext is the full accumulated view: one session plus its players.
type GameContext struct {
	// SessionID is a synthetic id ("session-<n>") or the adopted game_id label.
	SessionID string
	// GameKind is the kind of game this session is: "real" (a live, player-facing
	// game), "shadow" (a parallel evaluation game), or "" (unknown — not yet
	// observed on any labeled signal). Source: the game_kind datapoint label on
	// game_active / game_active_players / game_duration_seconds, or the game.kind
	// attribute on the game-session / player_lifecycle spans (#845).
	GameKind string
	// ExperimentID / Arm are the experiment attribution within a shadow session
	// (#975, epic #982): which experiment this game belongs to ("exp_<id>" or "" —
	// not part of any experiment) and which arm ("experimental" / "control" / "").
	// Source: the experiment_id / arm datapoint labels on the lifecycle metrics
	// (game_active / game_active_players / game_duration_seconds), or the
	// experiment.id / experiment.arm attributes on the game-session span. They are
	// partition enrichment, not a routing key — the cohort aggregator (#979) groups
	// partitions by ExperimentID; the Multiplexer still partitions on game_id.
	ExperimentID string
	Arm          string
	Session      SessionSignals
	Players      map[string]*PlayerSignals
	UpdatedAt    time.Time

	// Timeline is a bounded, oldest-first slice of the partition's recent rolling
	// narrative (#916): periodic state deltas, eliminations, and session phase
	// transitions, each timestamped. It is a SNAPSHOT copy of the Store's ring
	// buffer taken in Snapshot(), so it shares no backing array with the live ring
	// and is safe to render after later Store mutations. nil when nothing has been
	// recorded yet. The llm package renders it into a compact timeline section in
	// both the in-game prompt (#739) and the retro (#844) via RenderTimeline.
	Timeline []TimelineEvent
}
