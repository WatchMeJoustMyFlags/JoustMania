package patterngame

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"sort"
)

// fieldBound is one archetype field's bounds (UC-046 BR-003).
type fieldBound struct {
	low, high     float64
	highExclusive bool
}

// archetypeFields mirrors services/controller_manager/multiplexer/motion_patterns.py
// _FIELDS; the controller manager re-validates on install.
var archetypeFields = map[string]fieldBound{
	"players":        {2, 8, false},
	"stream_rate_hz": {15, 60, false},
	"base":           {0, 10, false},
	"drift":          {-1, 1, false},
	"amp":            {0, 10, false},
	"period_s":       {0, math.Inf(1), false},
	"phase":          {0, 1, true},
	"burst_h":        {0, 10, false},
	"burst_period_s": {0, math.Inf(1), false},
	"d_base":         {-1, 1, false},
	"d_drift":        {-1, 1, false},
	"d_amp":          {-1, 1, false},
	"d_phase":        {0, 1, true},
}

var archetypeNameRe = regexp.MustCompile(`^[a-z][a-z0-9_]*$`)

// Archetype is a resolved, validated archetype (entity MOTION_ARCHETYPE).
type Archetype struct {
	Name         string
	Params       map[string]float64
	ParamsJSON   string // canonical JSON (sorted keys, no whitespace), sent to the controller manager
	Digest       string // first 16 hex digits of SHA-256(ParamsJSON), UC-046 BR-009
	Players      int
	StreamRateHz int
}

// NewArchetype validates a parameter object (the flag variant value) against
// UC-046 BR-003 and returns the archetype, or the failed rules.
func NewArchetype(name string, raw any) (Archetype, []string) {
	var errs []string
	if !archetypeNameRe.MatchString(name) {
		errs = append(errs, fmt.Sprintf("name %q does not match [a-z][a-z0-9_]*", name))
	}
	obj, ok := raw.(map[string]any)
	if !ok {
		return Archetype{}, append(errs, "parameters are not a JSON object")
	}
	if len(obj) == 0 {
		return Archetype{}, append(errs, "parameters are empty (the off variant or an unknown archetype)")
	}
	var unknown []string
	for k := range obj {
		if _, known := archetypeFields[k]; !known {
			unknown = append(unknown, k)
		}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		errs = append(errs, fmt.Sprintf("unknown fields %v", unknown))
	}
	params := make(map[string]float64, len(archetypeFields))
	keys := make([]string, 0, len(archetypeFields))
	for k := range archetypeFields {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		b := archetypeFields[k]
		v, present := obj[k]
		if !present {
			errs = append(errs, k+" is missing")
			continue
		}
		f, isNum := toFloat(v)
		if !isNum || math.IsNaN(f) || math.IsInf(f, 0) {
			errs = append(errs, fmt.Sprintf("%s=%v is not a finite number", k, v))
			continue
		}
		tooHigh := f > b.high
		if b.highExclusive {
			tooHigh = f >= b.high
		}
		if f < b.low || tooHigh {
			errs = append(errs, fmt.Sprintf("%s=%v outside its bounds", k, f))
			continue
		}
		params[k] = f
	}
	if len(errs) > 0 {
		return Archetype{}, errs
	}
	if params["players"] != math.Trunc(params["players"]) {
		errs = append(errs, "players is not an integer")
	}
	switch params["stream_rate_hz"] {
	case 15, 30, 60:
	default:
		errs = append(errs, fmt.Sprintf("stream_rate_hz=%v is not one of [15 30 60]", params["stream_rate_hz"]))
	}
	if params["period_s"] <= 0 {
		errs = append(errs, "period_s must be > 0")
	}
	if params["burst_h"] > 0 && params["burst_period_s"] <= 0 {
		errs = append(errs, "burst_period_s must be > 0 when burst_h > 0")
	}
	players := int(params["players"])
	for i := 0; i < players && len(errs) == 0; i++ {
		base := params["base"] + float64(i)*params["d_base"]
		amp := params["amp"] + float64(i)*params["d_amp"]
		if base < 0 || base > 10 {
			errs = append(errs, fmt.Sprintf("slot %d base %.4f g outside 0..10", i, base))
		}
		if amp < 0 || amp > 10 {
			errs = append(errs, fmt.Sprintf("slot %d amp %.4f g outside 0..10", i, amp))
		}
	}
	if len(errs) > 0 {
		return Archetype{}, errs
	}
	canonical, err := json.Marshal(params) // map keys are emitted sorted
	if err != nil {
		return Archetype{}, []string{"parameters cannot be encoded: " + err.Error()}
	}
	sum := sha256.Sum256(canonical)
	return Archetype{
		Name:         name,
		Params:       params,
		ParamsJSON:   string(canonical),
		Digest:       hex.EncodeToString(sum[:])[:16],
		Players:      players,
		StreamRateHz: int(params["stream_rate_hz"]),
	}, nil
}

func toFloat(v any) (float64, bool) {
	switch n := v.(type) {
	case float64:
		return n, true
	case float32:
		return float64(n), true
	case int:
		return float64(n), true
	case int64:
		return float64(n), true
	case json.Number:
		f, err := n.Float64()
		return f, err == nil
	default:
		return 0, false
	}
}
