package gamecontext

import (
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
	metricGameMode       = "current_game_mode"           // live
	metricDeathsTotal    = "game_player_deaths_total"    // live
	metricPeakAccel      = "game_player_peak_accel"      // live (game_id carrier)

	metricMovementVariance = "game_player_movement_variance" // proposed (#722 §7)
	metricBatteryPct       = "controller_battery_pct"        // proposed
	metricSkillLevel       = "game_player_skill_level"       // proposed
	metricEliminationOrder = "game_player_elimination_order" // proposed
)

// Attribute keys.
const (
	attrSerial = "serial"
	attrMode   = "mode"
	attrGameID = "game_id"
)

// ApplyMetrics walks ResourceMetrics -> ScopeMetrics -> Metrics, decoding Gauge
// and Sum NumberDataPoints (Int or Double), and applies any recognized signal to
// the store. It returns true if at least one known signal was updated.
func (s *Store) ApplyMetrics(md pmetric.Metrics) bool {
	updated := false
	rms := md.ResourceMetrics()
	for i := 0; i < rms.Len(); i++ {
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

// applyDataPoint maps a single (metric name, data point) to a store setter.
func (s *Store) applyDataPoint(name string, dp pmetric.NumberDataPoint) bool {
	attrs := dp.Attributes()
	v := numberValue(dp)

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
			s.SetPlayerIntensity(serial, v)
			return true
		}
	case metricMovementVariance:
		if serial, ok := serialOf(); ok {
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
			s.SetPlayerSkill(serial, v, true)
			return true
		}
	case metricPlaystyle:
		if serial, ok := serialOf(); ok {
			s.SetPlayerSkill(serial, v/3, false)
			return true
		}
	case metricGameAlive:
		if serial, ok := serialOf(); ok {
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
			s.RecordDeathTotal(serial, v)
			return true
		}
	case metricEliminationOrder:
		if serial, ok := serialOf(); ok {
			s.SetEliminationOrder(serial, int(v))
			if gid, ok := attrs.Get(attrGameID); ok {
				s.AdoptSessionID(gid.AsString())
			}
			return true
		}
	case metricPeakAccel:
		// Only used to adopt the game_id; serial not required here.
		if gid, ok := attrs.Get(attrGameID); ok {
			s.AdoptSessionID(gid.AsString())
			return true
		}

	// --- session signals (no serial attr) ---
	case metricDuration:
		s.SetSessionDuration(v)
		return true
	case metricActivePlayers:
		s.SetActivePlayerCount(int(v))
		return true
	case metricGameActive:
		s.SetGameActive(v == 1)
		return true
	case metricGameMode:
		if v != 0 {
			if mv, ok := attrs.Get(attrMode); ok {
				s.SetGameMode(mv.AsString())
				return true
			}
		}
	}
	return false
}
