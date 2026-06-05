package decision

import (
	"sort"
	"strconv"
	"strings"

	"github.com/joustmania/agent/infracontext"
)

// Infrastructure fitness (#735) scores the live Bluetooth-transport context
// (infracontext.InfraContext, #733) against three flag-sourced thresholds. It is
// the infra-domain parallel to the game fitness functions (#731): it turns "is
// the Bluetooth transport healthy enough to expand the rollout?" into an
// observable, runtime-tunable pass/fail with named violations.
//
// The thresholds come from the fitness.bluetooth.* flags (never code) and are
// read on EVERY evaluation, so they can be tuned live on stage. This PR provides
// the evaluation, the flag plumbing, and a fitness-source seam only — no rollout
// expansion (#734) or rollback (#736) decisions are made here; those land in
// stacked follow-ups that consume EvaluateInfraFitness.
//
// Three fitness functions:
//   - event_gap_ms: violated when the window max inter-frame gap exceeds
//     MaxEventGapMs (transport is stalling).
//   - dropped_events_pct: violated when the window drop ratio exceeds
//     MaxDroppedEventsPct (transport is lossy).
//   - movement_update_hz: violated when an update rate falls below
//     MinMovementUpdateHz. Evaluated at BOTH window granularity (the window min
//     per-serial rate) AND per-controller granularity, so a violation can name
//     the offending serial(s).
//
// Missing INDIVIDUAL signals are skipped, not treated as failures (mirroring how
// game fitness skips missing signals): a nil window EventGapMs contributes no
// event_gap violation rather than a false one. Only a context with NO window
// signals at all (every window pointer nil) is reported as Evaluated=false.

// Comparators used in violations and the violation string. A ">" violation fired
// because the observed value exceeded a max threshold; "<" because it fell below
// a min threshold.
const (
	ComparatorGreater = ">"
	ComparatorLess    = "<"
)

// Violation signal names. Stable identifiers used in InfraViolation.Signal and in
// the ViolationsString rendering PR F/G lifts onto the fitness.violations span
// attribute.
const (
	SignalEventGapMs       = "event_gap_ms"
	SignalDroppedEventsPct = "dropped_events_pct"
	SignalMovementUpdateHz = "movement_update_hz"
)

// BluetoothThresholds holds the three fitness.bluetooth.* thresholds the infra
// fitness functions evaluate against. They are read from flags on every cycle.
type BluetoothThresholds struct {
	// MaxEventGapMs: a window event gap above this fails fitness
	// (fitness.bluetooth.max_event_gap_ms, default 50).
	MaxEventGapMs float64
	// MaxDroppedEventsPct: a window drop ratio above this fails fitness
	// (fitness.bluetooth.max_dropped_events_pct, default 0.02).
	MaxDroppedEventsPct float64
	// MinMovementUpdateHz: an update rate below this fails fitness
	// (fitness.bluetooth.min_movement_update_hz, default 10).
	MinMovementUpdateHz float64
}

// DefaultBluetoothThresholds returns the defaults from the flagd agent schema
// (services/flagd/agent.json fitness.bluetooth.* defaultVariants), so the
// evaluator has sane values before the first flag publication.
func DefaultBluetoothThresholds() BluetoothThresholds {
	return BluetoothThresholds{
		MaxEventGapMs:       50,
		MaxDroppedEventsPct: 0.02,
		MinMovementUpdateHz: 10,
	}
}

// InfraViolation is one failed fitness check. Serial is the offending controller
// for a per-controller violation, and empty ("") for a window-level violation.
type InfraViolation struct {
	// Signal is the fitness function that failed (one of the Signal* constants).
	Signal string
	// Serial names the offending controller for per-controller violations; "" for
	// window-level violations.
	Serial string
	// Observed is the value that breached the threshold.
	Observed float64
	// Threshold is the flag-sourced limit that was breached.
	Threshold float64
	// Comparator is the relation that makes this a violation: ">" (observed >
	// threshold, a max breach) or "<" (observed < threshold, a min breach).
	Comparator string
}

// InfraFitnessResult is the structured outcome of one infra fitness evaluation.
type InfraFitnessResult struct {
	// Evaluated is false only when the context had no window signals at all
	// (every window pointer nil) — there was nothing to evaluate. True means the
	// evaluation ran; Passing and Violations are then meaningful.
	Evaluated bool
	// Passing is true when no fitness function was violated. Only meaningful when
	// Evaluated.
	Passing bool
	// Violations lists every failed check, deterministically ordered (window
	// before per-controller, then by signal, then by serial).
	Violations []InfraViolation
	// Values holds the thresholds in effect plus the observed signals, under
	// dotted keys for the span (e.g. "bluetooth.event_gap_ms",
	// "bluetooth.max_event_gap_ms"). PR F/G lifts these onto the decision span.
	Values map[string]float64
}

// EvaluateInfraFitness scores the Bluetooth-transport context against the
// thresholds. It is a pure, deterministic function with no I/O.
//
// hz dedup decision: when the window min movement rate AND one or more specific
// controllers both violate the hz floor, we emit ONE per-serial violation for
// each offending controller and SKIP the redundant window-level hz violation —
// the per-serial violations already name every controller responsible, so a
// window-level duplicate would double-count the same failure. The window-level
// hz check only fires when the window min is below the floor but NO per-serial
// rate is available to attribute it to (per-controller signals missing): then a
// single window-level hz violation with empty Serial preserves the signal rather
// than dropping it.
func EvaluateInfraFitness(infra infracontext.InfraContext, th BluetoothThresholds) InfraFitnessResult {
	w := infra.Window
	// Thresholds in effect are recorded under their own max/min keys; the observed
	// signals land under the bluetooth.* keys below only when actually present
	// (a missing signal records no observed value, so the span never fabricates one).
	values := map[string]float64{
		"bluetooth.max_event_gap_ms":       th.MaxEventGapMs,
		"bluetooth.max_dropped_events_pct": th.MaxDroppedEventsPct,
		"bluetooth.min_movement_update_hz": th.MinMovementUpdateHz,
	}

	// Evaluated=false only when there is no window observation at all.
	if w.EventGapMs == nil && w.DroppedEventsPct == nil && w.MovementUpdateHz == nil {
		return InfraFitnessResult{Evaluated: false, Values: values}
	}

	var violations []InfraViolation

	// event_gap_ms (window max): skipped when unobserved.
	if w.EventGapMs != nil {
		values["bluetooth.event_gap_ms"] = *w.EventGapMs
		if *w.EventGapMs > th.MaxEventGapMs {
			violations = append(violations, InfraViolation{
				Signal:     SignalEventGapMs,
				Observed:   *w.EventGapMs,
				Threshold:  th.MaxEventGapMs,
				Comparator: ComparatorGreater,
			})
		}
	}

	// dropped_events_pct (window ratio): skipped when unobserved.
	if w.DroppedEventsPct != nil {
		values["bluetooth.dropped_events_pct"] = *w.DroppedEventsPct
		if *w.DroppedEventsPct > th.MaxDroppedEventsPct {
			violations = append(violations, InfraViolation{
				Signal:     SignalDroppedEventsPct,
				Observed:   *w.DroppedEventsPct,
				Threshold:  th.MaxDroppedEventsPct,
				Comparator: ComparatorGreater,
			})
		}
	}

	// movement_update_hz: per-controller first, then window-level only if no
	// per-controller rate attributed the failure (see dedup decision above).
	perSerial := perControllerHzViolations(infra.Controllers, th.MinMovementUpdateHz)
	violations = append(violations, perSerial...)

	if w.MovementUpdateHz != nil {
		values["bluetooth.movement_update_hz"] = *w.MovementUpdateHz
		if *w.MovementUpdateHz < th.MinMovementUpdateHz && len(perSerial) == 0 {
			violations = append(violations, InfraViolation{
				Signal:     SignalMovementUpdateHz,
				Observed:   *w.MovementUpdateHz,
				Threshold:  th.MinMovementUpdateHz,
				Comparator: ComparatorLess,
			})
		}
	}

	sortViolations(violations)
	return InfraFitnessResult{
		Evaluated:  true,
		Passing:    len(violations) == 0,
		Violations: violations,
		Values:     values,
	}
}

// perControllerHzViolations returns one violation per controller whose observed
// movement rate is below the floor. Controllers with no observed rate (nil) are
// skipped. The result is sorted by serial for determinism.
func perControllerHzViolations(controllers map[string]*infracontext.ControllerHealth, minHz float64) []InfraViolation {
	var out []InfraViolation
	for serial, ch := range controllers {
		if ch == nil || ch.MovementUpdateHz == nil {
			continue
		}
		if *ch.MovementUpdateHz < minHz {
			out = append(out, InfraViolation{
				Signal:     SignalMovementUpdateHz,
				Serial:     serial,
				Observed:   *ch.MovementUpdateHz,
				Threshold:  minHz,
				Comparator: ComparatorLess,
			})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Serial < out[j].Serial })
	return out
}

// sortViolations orders violations deterministically: window-level (empty
// serial) before per-controller, then by signal name, then by serial.
func sortViolations(vs []InfraViolation) {
	sort.SliceStable(vs, func(i, j int) bool {
		a, b := vs[i], vs[j]
		if a.Signal != b.Signal {
			return a.Signal < b.Signal
		}
		return a.Serial < b.Serial
	})
}

// ViolationsString renders the violations as a stable, compact string for the
// fitness.violations span attribute (PR F/G). Each violation is
// "<signal><[serial]> <observed><comparator><threshold>", joined by "; ".
// Per-controller violations carry a "[serial]" qualifier; window-level ones do
// not. Empty when Passing (or not evaluated). Example:
//
//	"event_gap_ms 87.5>50; movement_update_hz[AA:BB] 8.3<10"
func (r InfraFitnessResult) ViolationsString() string {
	if len(r.Violations) == 0 {
		return ""
	}
	parts := make([]string, 0, len(r.Violations))
	for _, v := range r.Violations {
		var b strings.Builder
		b.WriteString(v.Signal)
		if v.Serial != "" {
			b.WriteByte('[')
			b.WriteString(v.Serial)
			b.WriteByte(']')
		}
		b.WriteByte(' ')
		b.WriteString(formatFloat(v.Observed))
		b.WriteString(v.Comparator)
		b.WriteString(formatFloat(v.Threshold))
		parts = append(parts, b.String())
	}
	return strings.Join(parts, "; ")
}

// formatFloat renders a float compactly without a trailing ".0" for integers
// (e.g. 50, not 50.0; 8.3, not 8.300000), keeping the violation string stable.
func formatFloat(f float64) string {
	return strconv.FormatFloat(f, 'g', -1, 64)
}
