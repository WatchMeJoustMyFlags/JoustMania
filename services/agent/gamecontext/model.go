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
	// This is a FROZEN last-sample at conclusion (the windowed value at the last
	// live frame before elimination) and is therefore only meaningful mid-game.
	MovementVariance *float64

	// PeakAccel is the player's whole-game PEAK accel magnitude (g-force), a
	// monotonically-increasing whole-game aggregate.
	// Source: game_player_peak_accel{serial} (live; coordinator emits it per-player
	// at ~1Hz). RETAINED into the OnGameEnd snapshot like SkillLevel, so it stays
	// meaningful at conclusion. Used by balanced-fitness spike-survival (#1024).
	PeakAccel *float64

	// MovementVarianceAggregate is the whole-game CUMULATIVE variance of accel
	// magnitude (g^2, Welford over every frame), in contrast to MovementVariance's
	// frozen rolling-window last-sample.
	// Source: game_player_movement_variance_aggregate{serial} (live; coordinator
	// emits it per-player at ~1Hz). RETAINED into the OnGameEnd snapshot like
	// SkillLevel, so balanced-fitness spike-survival is meaningful post-game (#1024).
	MovementVarianceAggregate *float64

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

	// History is the bounded per-player movement TIME-SERIES (#1082): an oldest-
	// first slice of bucketed (t, intensity[, variance]) samples, a SNAPSHOT copy
	// of the Store's per-player ring (shares no backing array with the live ring,
	// so it is safe to render after later Store mutations). nil until the first
	// intensity sample for this player arrives. The llm prompt / retro and the
	// game summary render it as a compact sparkline + a proximity-to-death track —
	// never as raw floats — via RenderSparkline / ComputeProximity / ClassifyActivity.
	History []MovementSample

	// Hold-mode shield (#2096, UC-040 A37, UC-042 BR-007; nil / empty outside a hold
	// game). Sources: game_player_shield_energy_seconds,
	// game_player_shield_budget_remaining_seconds, game_player_shield_active and
	// game_player_shield_total{total} (live; set at every hold transition and at least
	// every 2 s, so up to one export interval old). ShieldTotals is keyed by the total
	// label: granted_seconds, recharged_seconds, used_seconds, activations, empties.
	ShieldEnergy          *float64
	ShieldBudgetRemaining *float64
	ShieldActive          *bool
	ShieldTotals          map[string]float64

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

// InterventionCount is one intervention type's per-game tally.
//
// Dispatched and Blocked split the decisions at the permission chain (allow-list,
// battery, rate limit). Applied and GatedOff split the dispatched ones by how the
// write ended (#1771): Applied = written, GatedOff = the actuation gate
// (interventions_enabled) turned it into a no-op. A failed write is Dispatched
// only. The game summary reports Applied as applied and Blocked + GatedOff as
// blocked by a gate.
type InterventionCount struct {
	Dispatched int
	Blocked    int
	Applied    int
	GatedOff   int
}

// ApplyOutcome is how a dispatched intervention's write ended (#1771).
type ApplyOutcome string

const (
	// ApplyOutcomeApplied: the actuation gate was on and the write succeeded.
	ApplyOutcomeApplied ApplyOutcome = "applied"
	// ApplyOutcomeGatedOff: the actuation gate interventions_enabled was off (or
	// unreadable, fail closed), so nothing was written.
	ApplyOutcomeGatedOff ApplyOutcome = "gated_off"
	// ApplyOutcomeWriteFailed: the write was attempted and returned an error.
	ApplyOutcomeWriteFailed ApplyOutcome = "write_failed"
)

// SessionSignals is the per-game (session) view the agent reasons over.
type SessionSignals struct {
	// DurationSeconds is the elapsed game time in seconds since the game was
	// started. Source: game_duration_seconds (live: the coordinator refreshes it
	// ~1 Hz while the game is RUNNING and sets the final value at game end, #1305).
	// While the game is active, Snapshot() falls back to the time since StartedAt
	// when the gauge is missing or has not advanced for durationStaleAfter (see
	// Store.effectiveDurationLocked), so in-game consumers never see a stuck 0;
	// the in-game value never decreases within a game. In the game-end snapshot it
	// is the larger of the gauge and the agent's own start->end bracket, so every
	// game-end consumer sees the same duration.
	DurationSeconds *float64

	// StartedAt is when the agent saw this session's game_active go 0 -> 1 (agent
	// clock). It is the fallback base for DurationSeconds while the game is
	// active (#1305). Zero when no start has been observed for this session.
	StartedAt time.Time

	// ActivePlayerCount is the number of players still in the game.
	// Source: game_active_players (live).
	ActivePlayerCount *int

	// EliminationSequence is the ordered list of eliminated serials,
	// first eliminated first. Derived from the elimination sources below.
	EliminationSequence []string

	// InterventionCounts tallies this game's agent interventions by type
	// (dispatched / blocked). Unlike the 64-entry Timeline it is unbounded per game,
	// so game-end aggregates count every intervention (#1368). nil until the first
	// intervention; reset at game start.
	InterventionCounts map[string]InterventionCount

	// GameActive indicates a game is in progress.
	// Source: game_active == 1 (live).
	GameActive *bool

	// GameMode is the current game mode.
	// Source: game_current_mode{mode, game_id} label (value != 0) (live), or span
	// attribute game.mode.
	GameMode *string

	// MusicTempo is the current applied game speed / music tempo — the reference
	// frame the per-player intensity tracks must be read against (#1082): the same
	// intensity is near-death at slow tempo and barely trying at fast tempo.
	// Source: game_music_tempo{game_id} (live; 1.0=slow .. ~1.3=fast). nil until
	// observed. The accelerate rules (R5/R6/R7) and adjust_music_tempo interventions
	// ramp it over the game, so the Store also keeps a per-tick SpeedTrack.
	MusicTempo *float64

	// DeathThreshold / WarningThreshold are the current effective (tempo-
	// interpolated) elimination / warning thresholds in g-force (#1082).
	// Source: game_effective_death_threshold / game_effective_warning_threshold
	// (live, one series per game_id). These are SESSION-level (one effective
	// value, after the slow<->fast LERP), not per-player: they exclude per-player
	// sensitivity_factor, handicap, soft_penalty and shields, and the
	// per-sensitivity tables stay internal to the coordinator. Proximity-to-death is each player's MovementIntensity read
	// against DeathThreshold. nil until observed.
	DeathThreshold   *float64
	WarningThreshold *float64

	// ShieldActivation is the shield activation mode the game latched: "instant",
	// "trigger" or "hold" (#2096). Source: game_shield_activation_mode{mode} (value 1).
	// nil until observed. ShieldSettings holds a hold game's latched values keyed by
	// flag key (shield.energy_max_seconds, ...). Source:
	// game_shield_hold_setting{setting}. Both are read from the gauges, never from
	// flagd, so an experiment arm or a latched promotion reads right.
	ShieldActivation *string
	ShieldSettings   map[string]float64

	// LastUpdate is the last time any session signal changed.
	LastUpdate time.Time
}

// GameContext is the full accumulated view: one session plus its players.
type GameContext struct {
	// SessionID is a synthetic id ("session-<n>") or the adopted game_id label.
	SessionID string
	// EndReason is which signal ended the game (UC-041 A4, #1494): EndReasonZero,
	// EndReasonRootSpan or EndReasonStale. Empty while the game runs; set in the
	// OnGameEnd snapshot, so game-end consumers can tell a game whose telemetry
	// stopped (stale) from one that ended normally.
	EndReason string
	// GameKind is the kind of game this session is: "real" (a live, player-facing
	// game), "shadow" (a parallel evaluation game), or "" (unknown — not yet
	// observed on any labeled signal). Source: the game_kind datapoint label on
	// game_active / game_active_players / game_duration_seconds, or the game.kind
	// attribute on the game-session / player_lifecycle spans (#845). Always in the
	// flag-evaluation vocabulary: the coordinator's telemetry "primary" is
	// normalised to "real" at ingest (NormalizeGameKind, #1249).
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
	// GameTraceID is the hex trace_id of the coordinator's root game span for this
	// session (#1133, Phase 2 of #1088). Source: the game_trace_id datapoint label on
	// the dedicated game_trace_correlation gauge the coordinator emits while the game
	// span is live. The agent has no parent trace context (it consumes game state only
	// as metrics), so this id is what lets the decision loop add an OTel span LINK from
	// agent.decision to the originating game trace — navigable in Jaeger, not merely a
	// shared game.id attribute (Phase 1). Empty when not yet observed or the game span
	// was unsampled; an empty value means "no link" (graceful fallback, no error).
	GameTraceID string
	// GameTraceSpanID is the hex span_id of the coordinator's root game span for
	// this session (#1157, follow-up to #1133). Source: the game_trace_span_id
	// datapoint label on the same game_trace_correlation gauge. Carried alongside
	// GameTraceID so the decision/retro Link references the actual game-start span
	// (Jaeger highlights it via uiFind) rather than an all-zero span id under the
	// trace. Empty when not yet observed or the game span was unsampled; an empty
	// trace_id OR span_id means "no link" (graceful fallback, no error).
	GameTraceSpanID string
	// GameplayPhaseSpanID is the hex span_id of the coordinator's gameplay_phase
	// sub-span for this session (#1195, refines #1187). Source: the
	// gameplay_phase_span_id datapoint label on the same game_trace_correlation
	// gauge. gameplay_phase is the STABLE active-play window WITHIN the game trace;
	// the agent re-parents its in-game decision chain (signal_received -> decision
	// -> action) under THIS span instead of the game root, so a reader sees
	// interventions nested where they actually occurred. Same trace as GameTraceID.
	// Two-tier fallback: when empty (gameplay_phase not yet open / unsampled) the
	// re-parent uses GameTraceSpanID (the game root, #1187 behavior); when that is
	// also empty the span stays own-root. Never breaks span emission.
	GameplayPhaseSpanID string
	// GameCycleSpanID is the hex span_id of the coordinator's game_cycle span (a
	// child of gameplay_phase) for this session (#1325, UC-041 BR-004). Source: the
	// game_cycle_span_id label on the same game_trace_correlation gauge. It is the
	// PREFERRED remote parent of the in-game agent spans (UC-040 BR-013); when empty
	// (not yet open, unsampled, or a mode without game_cycle) the parent falls back to
	// GameplayPhaseSpanID, then GameTraceSpanID, then own root.
	GameCycleSpanID string
	Session         SessionSignals
	Players         map[string]*PlayerSignals
	UpdatedAt       time.Time

	// Timeline is a bounded, oldest-first slice of the partition's recent rolling
	// narrative (#916): periodic state deltas, eliminations, and session phase
	// transitions, each timestamped. It is a SNAPSHOT copy of the Store's ring
	// buffer taken in Snapshot(), so it shares no backing array with the live ring
	// and is safe to render after later Store mutations. nil when nothing has been
	// recorded yet. The llm package renders it into a compact timeline section in
	// both the in-game prompt (#739) and the retro (#844) via RenderTimeline.
	Timeline []TimelineEvent

	// SpeedTrack is a bounded, oldest-first SNAPSHOT of the session game-speed /
	// death-threshold track (#1082): per-tick (t, tempo, death_threshold) samples,
	// the reference frame the per-player proximity-to-death is read against (the
	// same intensity is near-death at slow tempo, barely-trying at fast tempo). It
	// shares no backing array with the live ring (safe to render after later Store
	// mutations). nil until the first tempo/threshold reading arrives. Rendered as a
	// compact tempo sparkline (RenderSpeedTrack) alongside the per-player sparklines.
	SpeedTrack []SpeedSample
}

// Game end reasons (GameContext.EndReason, UC-041 A4, #1494).
const (
	// EndReasonZero: the game_active 1 -> 0 datapoint ended the game.
	EndReasonZero = "zero"
	// EndReasonRootSpan: the game root span arrived before any 0.
	EndReasonRootSpan = "root_span"
	// EndReasonStale: the game's game_active series stayed silent for
	// gameActiveStaleAfter. The agent's telemetry for the game broke, so the game's
	// data is incomplete: experiments and the retro, summary and proposer skip it.
	EndReasonStale = "stale"
)

// Hold-mode setting keys (game_shield_hold_setting{setting}, the flag keys; #2096).
const (
	ShieldSettingEnergyMax     = "shield.energy_max_seconds"
	ShieldSettingEnergyBudget  = "shield.energy_budget_seconds"
	ShieldSettingEnergyInitial = "shield.energy_initial_seconds"
	ShieldSettingRecharge      = "shield.recharge_per_second"
	ShieldSettingMinBurst      = "shield.min_burst_seconds"
)

// ShieldActivationHold is the game_shield_activation_mode value of a hold game.
const ShieldActivationHold = "hold"

// IsShieldHold reports whether the game latched shield.activation=hold (#2096).
func (s SessionSignals) IsShieldHold() bool {
	return s.ShieldActivation != nil && *s.ShieldActivation == ShieldActivationHold
}

// ShieldSetting returns a latched hold value by flag key, nil when not observed.
func (s SessionSignals) ShieldSetting(key string) *float64 {
	v, ok := s.ShieldSettings[key]
	if !ok {
		return nil
	}
	return &v
}
