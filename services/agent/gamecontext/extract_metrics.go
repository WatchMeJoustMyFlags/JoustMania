package gamecontext

import (
	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/pmetric"
)

// Metric name constants. "live" metrics are emitted by producers today;
// "proposed" metrics are forward-compatible mappings from the #722
// intervention-surface research (docs/research/722-intervention-surface.md) and
// will simply never match until producers ship them.
const (
	metricAccelMagnitude = "game_player_accel_magnitude" // live
	metricBatteryLevel   = "controller_battery_level"    // live (0-5 scale)
	metricPlaystyle      = "game_player_playstyle"       // live (0-3)
	metricGameAlive      = "game_player_alive"           // live
	metricConnected      = "controller_connected"        // live
	metricDuration       = "game_duration_seconds"       // live
	metricActivePlayers  = "game_active_players"         // live
	metricGameActive     = "game_active"                 // live
	metricGameMode       = "game_current_mode"           // live (coordinator emits game_current_mode; #848)
	metricDeathsTotal    = "game_player_deaths_total"    // live
	metricPeakAccel      = "game_player_peak_accel"      // live (whole-game aggregate + game_id carrier)

	// Trace-correlation signal (#1133, Phase 2 of #1088). A dedicated low-rate
	// per-game gauge (value always 1 while the game span is live) whose payload is its
	// labels: game_id + the game span's hex game_trace_id. The agent reads game_trace_id
	// here so the decision loop can add an OTel span LINK from agent.decision to the
	// originating game trace — the agent otherwise has no parent context (it consumes
	// game state only as metrics). Falls back gracefully (no link) when absent/unsampled.
	metricTraceCorrelation = "game_trace_correlation"

	// Game-speed / threshold reference frame (#1082). All three are SESSION-level
	// gauges the coordinator already emits (no serial label): game_music_tempo is
	// the applied game speed (1.0=slow..~1.3=fast); the effective thresholds are the
	// tempo-interpolated death/warning thresholds in g-force, so proximity-to-death
	// reads as "intensity vs threshold AT that tempo".
	metricMusicTempo        = "game_music_tempo"                 // live (Phase 70)
	metricEffectiveDeathThr = "game_effective_death_threshold"   // live (Phase 80)
	metricEffectiveWarnThr  = "game_effective_warning_threshold" // live (Phase 80)

	metricMovementVariance = "game_player_movement_variance" // live (coordinator emits at ~10Hz, #730/#1015)
	metricBatteryPct       = "controller_battery_pct"        // proposed
	metricSkillLevel       = "game_player_skill_level"       // live (coordinator emits at ~10Hz, #730/#1015)
	metricEliminationOrder = "game_player_elimination_order" // proposed

	// Whole-game RETAINED movement-variance aggregate (#1024). Emitted while alive
	// at ~1Hz; retained into the conclusion snapshot like skill_level so the
	// balanced-fitness spike-survival sub-check is meaningful post-game (vs. the
	// frozen-last-sample game_player_movement_variance).
	metricMovementVarianceAggregate = "game_player_movement_variance_aggregate" // live
)

// Attribute keys.
const (
	attrSerial   = "serial"
	attrMode     = "mode"
	attrGameID   = "game_id"
	attrGameKind = "game_kind"
	// attrGameTraceID is the coordinator root game-span trace_id carried on the
	// dedicated game_trace_correlation signal (#1133). Hex string; empty when the
	// game span was unsampled.
	attrGameTraceID = "game_trace_id"
	// attrGameTraceSpanID is the coordinator root game-span span_id carried on the
	// same game_trace_correlation signal (#1157). Hex string; empty when the game
	// span was unsampled.
	attrGameTraceSpanID = "game_trace_span_id"
	// Experiment attribution (#975, epic #982): finer-grained labels WITHIN a
	// shadow game. Carried on the LOW-RATE lifecycle metrics (game_active,
	// game_active_players, game_duration_seconds) the same way game_kind is —
	// NOT on the per-frame firehose. Empty when the signal predates / is not
	// part of an experiment.
	attrExperimentID = "experiment_id"
	attrArm          = "arm"
)

// gameLabels carries the per-datapoint/per-span game identity resolved from the
// signal's labels: the game_id / game_kind datapoint labels (metrics) or the
// game.id / game.kind span attributes. Either field may be empty when the source
// signal does not carry that label (legacy / primary-context signals such as
// game_current_mode never carry game_id).
//
// This struct is the routing key. The Multiplexer (multiplexer.go, #845 PR B)
// selects the per-game Store partition on the resolved GameID (gameIDOf /
// spanGameIDOf) BEFORE dispatching the datapoint/span to that store's
// applyDataPoint / applySpan; within the selected partition the labels are then
// used to enrich it (SetGameKind + AdoptSessionID via adoptGame). Signals with an
// empty GameID route to the fallback partition (FallbackGameID).
type gameLabels struct {
	GameID   string
	GameKind string
	// ExperimentID / Arm carry the experiment attribution (#975) when the signal
	// is part of an agent experiment — empty otherwise. They are partition
	// ENRICHMENT, not a routing key: the Multiplexer still partitions on GameID;
	// a cohort is reconstructed by grouping partitions that share an ExperimentID.
	ExperimentID string
	Arm          string
	// GameTraceID carries the coordinator's root game-span trace_id (#1133) when the
	// signal is the dedicated game_trace_correlation gauge — empty on every other
	// signal. Like ExperimentID/Arm it is partition ENRICHMENT, not a routing key.
	GameTraceID string
	// GameTraceSpanID carries the coordinator's root game-span span_id (#1157),
	// likewise only on the game_trace_correlation gauge and likewise enrichment.
	GameTraceSpanID string
}

// gameIDOf reads the game_id datapoint label; empty when absent.
func gameIDOf(attrs pcommon.Map) string {
	if v, ok := attrs.Get(attrGameID); ok {
		return v.AsString()
	}
	return ""
}

// gameKindOf reads the game_kind datapoint label; empty when absent.
func gameKindOf(attrs pcommon.Map) string {
	if v, ok := attrs.Get(attrGameKind); ok {
		return v.AsString()
	}
	return ""
}

// experimentIDOf reads the experiment_id datapoint label; empty when absent.
func experimentIDOf(attrs pcommon.Map) string {
	if v, ok := attrs.Get(attrExperimentID); ok {
		return v.AsString()
	}
	return ""
}

// armOf reads the arm datapoint label; empty when absent.
func armOf(attrs pcommon.Map) string {
	if v, ok := attrs.Get(attrArm); ok {
		return v.AsString()
	}
	return ""
}

// gameTraceIDOf reads the game_trace_id datapoint label (#1133); empty when absent.
func gameTraceIDOf(attrs pcommon.Map) string {
	if v, ok := attrs.Get(attrGameTraceID); ok {
		return v.AsString()
	}
	return ""
}

// gameTraceSpanIDOf reads the game_trace_span_id datapoint label (#1157); empty when absent.
func gameTraceSpanIDOf(attrs pcommon.Map) string {
	if v, ok := attrs.Get(attrGameTraceSpanID); ok {
		return v.AsString()
	}
	return ""
}

// metricGameLabels resolves the game identity labels on a metric datapoint.
func metricGameLabels(attrs pcommon.Map) gameLabels {
	return gameLabels{
		GameID:          gameIDOf(attrs),
		GameKind:        gameKindOf(attrs),
		ExperimentID:    experimentIDOf(attrs),
		Arm:             armOf(attrs),
		GameTraceID:     gameTraceIDOf(attrs),
		GameTraceSpanID: gameTraceSpanIDOf(attrs),
	}
}

// ApplyMetrics walks ResourceMetrics -> ScopeMetrics -> Metrics, decoding Gauge
// and Sum NumberDataPoints (Int or Double), and applies any recognized signal to
// the store. It returns true if at least one known signal was updated.
func (s *Store) ApplyMetrics(md pmetric.Metrics) bool {
	updated := false
	rms := md.ResourceMetrics()
	for i := 0; i < rms.Len(); i++ {
		if s.isOwnResource(rms.At(i).Resource()) {
			continue
		}
		sms := rms.At(i).ScopeMetrics()
		for j := 0; j < sms.Len(); j++ {
			ms := sms.At(j).Metrics()
			for k := 0; k < ms.Len(); k++ {
				if s.applyMetric(ms.At(k)) {
					updated = true
				}
			}
		}
	}
	return updated
}

// dataPoints returns the NumberDataPointSlice for Gauge/Sum metrics, or an
// empty slice for other types.
func dataPoints(m pmetric.Metric) pmetric.NumberDataPointSlice {
	switch m.Type() {
	case pmetric.MetricTypeGauge:
		return m.Gauge().DataPoints()
	case pmetric.MetricTypeSum:
		return m.Sum().DataPoints()
	default:
		return pmetric.NewNumberDataPointSlice()
	}
}

func numberValue(dp pmetric.NumberDataPoint) float64 {
	switch dp.ValueType() {
	case pmetric.NumberDataPointValueTypeInt:
		return float64(dp.IntValue())
	case pmetric.NumberDataPointValueTypeDouble:
		return dp.DoubleValue()
	default:
		return 0
	}
}

// applyMetric handles one metric's data points. Returns true if anything applied.
func (s *Store) applyMetric(m pmetric.Metric) bool {
	name := m.Name()
	dps := dataPoints(m)
	updated := false
	for i := 0; i < dps.Len(); i++ {
		if s.applyDataPoint(name, dps.At(i)) {
			updated = true
		}
	}
	return updated
}

// applyDataPoint maps a single (metric name, data point) to a store setter. The
// game identity labels (game_id / game_kind) are resolved up front and threaded
// to every dispatch site via labels, so PR B's per-game multiplexer can route on
// labels.GameID without re-plumbing this switch.
func (s *Store) applyDataPoint(name string, dp pmetric.NumberDataPoint) bool {
	attrs := dp.Attributes()
	v := numberValue(dp)
	labels := metricGameLabels(attrs)

	serialOf := func() (string, bool) {
		sv, ok := attrs.Get(attrSerial)
		if !ok {
			return "", false
		}
		return sv.AsString(), true
	}

	switch name {
	// --- per-player signals (require a serial attr) ---
	case metricAccelMagnitude:
		if serial, ok := serialOf(); ok {
			s.adoptGame(labels)
			s.SetPlayerIntensity(serial, v)
			return true
		}
	case metricMovementVariance:
		if serial, ok := serialOf(); ok {
			s.adoptGame(labels)
			s.SetPlayerVariance(serial, v)
			return true
		}
	case metricBatteryPct:
		if serial, ok := serialOf(); ok {
			s.SetPlayerBattery(serial, v, true)
			return true
		}
	case metricBatteryLevel:
		if serial, ok := serialOf(); ok {
			s.SetPlayerBattery(serial, v*20, false)
			return true
		}
	case metricSkillLevel:
		if serial, ok := serialOf(); ok {
			s.adoptGame(labels)
			s.SetPlayerSkill(serial, v, true)
			return true
		}
	case metricPlaystyle:
		if serial, ok := serialOf(); ok {
			s.adoptGame(labels)
			s.SetPlayerSkill(serial, v/3, false)
			return true
		}
	case metricGameAlive:
		if serial, ok := serialOf(); ok {
			s.adoptGame(labels)
			s.SetPlayerAlive(serial, v == 1)
			return true
		}
	case metricConnected:
		if serial, ok := serialOf(); ok {
			s.SetPlayerConnected(serial, v == 1)
			return true
		}
	case metricDeathsTotal:
		if serial, ok := serialOf(); ok {
			s.adoptGame(labels)
			s.RecordDeathTotal(serial, v)
			return true
		}
	case metricEliminationOrder:
		if serial, ok := serialOf(); ok {
			s.adoptGame(labels)
			s.SetEliminationOrder(serial, int(v))
			return true
		}
	case metricPeakAccel:
		// Whole-game PEAK accel aggregate (#1024): when it carries a serial, retain
		// the per-player value (used by balanced-fitness spike-survival at
		// conclusion). It also carries game_id, so adoptGame regardless.
		if labels.GameID != "" || labels.GameKind != "" {
			s.adoptGame(labels)
		}
		if serial, ok := serialOf(); ok {
			s.SetPlayerPeakAccel(serial, v)
			return true
		}
		// Serial-less datapoint still usefully contributed the game_id.
		return labels.GameID != "" || labels.GameKind != ""
	case metricMovementVarianceAggregate:
		if serial, ok := serialOf(); ok {
			s.adoptGame(labels)
			s.SetPlayerVarianceAggregate(serial, v)
			return true
		}

	// --- session signals (no serial attr; carry game_id + game_kind) ---
	case metricDuration:
		s.adoptGame(labels)
		s.SetSessionDuration(v)
		return true
	case metricActivePlayers:
		s.adoptGame(labels)
		s.SetActivePlayerCount(int(v))
		return true
	case metricGameActive:
		// SetGameActive first: a false->true transition assigns a synthetic
		// SessionID, so adoptGame must run AFTER to override it with the real
		// game_id (otherwise the synthetic id would clobber the adopted one).
		s.SetGameActive(v == 1)
		s.adoptGame(labels)
		return true
	case metricTraceCorrelation:
		// Dedicated trace-correlation signal (#1133): value is always 1; the payload is
		// the labels (game_id + game_trace_id). Route through adoptGame so the
		// game_trace_id enriches the same partition the multiplexer selected on game_id;
		// SetGameTraceID ignores an empty id, so a malformed datapoint is a safe no-op.
		s.adoptGame(labels)
		return labels.GameTraceID != "" || labels.GameID != ""
	case metricGameMode:
		// game_current_mode is a primary/legacy signal: it NEVER carries game_id,
		// so it is deliberately not routed through adoptGame.
		if v != 0 {
			if mv, ok := attrs.Get(attrMode); ok {
				s.SetGameMode(mv.AsString())
				return true
			}
		}

	// --- game-speed / threshold reference frame (#1082; session-level, no serial) ---
	// These gauges carry no game_id label (like game_current_mode), so they are not
	// routed through adoptGame; they record onto whatever partition the multiplexer
	// selected. v==0 is the coordinator's "no game" sentinel for tempo/thresholds, so
	// a zero reading is skipped rather than recorded as a real 0.0 speed/threshold.
	case metricMusicTempo:
		if v != 0 {
			s.SetMusicTempo(v)
			return true
		}
	case metricEffectiveDeathThr:
		if v != 0 {
			s.SetDeathThreshold(v)
			return true
		}
	case metricEffectiveWarnThr:
		if v != 0 {
			s.SetWarningThreshold(v)
			return true
		}
	}
	return false
}

// adoptGame enriches the selected partition's store from a signal's resolved game
// identity labels: it adopts the game_id as the SessionID (much earlier than the
// pre-#845 end-of-game peak_accel path) and records the game_kind. Both setters
// no-op on an empty value, so an unlabeled signal leaves the store unchanged.
//
// With the Multiplexer (#845 PR B), label-based partition selection happens BEFORE
// this call (the Multiplexer routes on GameID); adoptGame stays the per-store
// enrichment step within the already-selected partition.
func (s *Store) adoptGame(labels gameLabels) {
	s.AdoptSessionID(labels.GameID)
	s.SetGameKind(labels.GameKind)
	s.SetExperimentID(labels.ExperimentID)
	s.SetArm(labels.Arm)
	s.SetGameTraceID(labels.GameTraceID)         // #1133: empty on all but game_trace_correlation
	s.SetGameTraceSpanID(labels.GameTraceSpanID) // #1157: ditto
}
