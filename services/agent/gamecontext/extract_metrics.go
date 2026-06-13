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
	metricPeakAccel      = "game_player_peak_accel"      // live (game_id carrier)

	metricMovementVariance = "game_player_movement_variance" // proposed (#722 §7)
	metricBatteryPct       = "controller_battery_pct"        // proposed
	metricSkillLevel       = "game_player_skill_level"       // proposed
	metricEliminationOrder = "game_player_elimination_order" // proposed
)

// Attribute keys.
const (
	attrSerial   = "serial"
	attrMode     = "mode"
	attrGameID   = "game_id"
	attrGameKind = "game_kind"
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

// metricGameLabels resolves the game identity labels on a metric datapoint.
func metricGameLabels(attrs pcommon.Map) gameLabels {
	return gameLabels{
		GameID:       gameIDOf(attrs),
		GameKind:     gameKindOf(attrs),
		ExperimentID: experimentIDOf(attrs),
		Arm:          armOf(attrs),
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
		// Game-end carrier of game_id; serial not required here. adoptGame is the
		// only thing this datapoint contributes.
		if labels.GameID != "" || labels.GameKind != "" {
			s.adoptGame(labels)
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
	case metricGameMode:
		// game_current_mode is a primary/legacy signal: it NEVER carries game_id,
		// so it is deliberately not routed through adoptGame.
		if v != 0 {
			if mv, ok := attrs.Get(attrMode); ok {
				s.SetGameMode(mv.AsString())
				return true
			}
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
}
