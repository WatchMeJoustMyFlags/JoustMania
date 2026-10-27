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

// TriggerPolicyKey is the optional, non-numeric archetype field of UC-046 BR-018.
const TriggerPolicyKey = "trigger_policy"

// Trigger policy kinds (UC-046 BR-018).
const (
	TriggerPolicyNone      = "none"
	TriggerPolicyBurst     = "burst"
	TriggerPolicyThreshold = "threshold"
)

// Threshold policy bounds (UC-046 BR-018). hold_frames starts at 2 so a press
// always spans two delivered frames and one coalesced frame cannot drop it.
const (
	policyLevelMax      = 10.0
	policyReactionMax   = 60
	policyHoldFramesMin = 2
	policyHoldFramesMax = 120
)

// TriggerPolicy is how a pattern controller presses its trigger by itself
// (UC-046 BR-018). Kind "" or "none" means it never presses.
type TriggerPolicy struct {
	Kind           string
	Level          float64
	ReactionFrames int
	HoldFrames     int
}

// HasTriggerPolicy reports whether the archetype's controllers press the
// trigger (a policy other than none, UC-046 BR-018).
func (a Archetype) HasTriggerPolicy() bool {
	return a.TriggerPolicy.Kind == TriggerPolicyBurst || a.TriggerPolicy.Kind == TriggerPolicyThreshold
}

// canonical returns the policy as it goes into the canonical JSON, or nil for none.
func (p TriggerPolicy) canonical() map[string]any {
	switch p.Kind {
	case TriggerPolicyBurst:
		return map[string]any{"kind": TriggerPolicyBurst}
	case TriggerPolicyThreshold:
		return map[string]any{
			"kind":            TriggerPolicyThreshold,
			"level":           p.Level,
			"reaction_frames": p.ReactionFrames,
			"hold_frames":     p.HoldFrames,
		}
	default:
		return nil
	}
}

// parseTriggerPolicy validates the trigger_policy object of UC-046 BR-018.
func parseTriggerPolicy(raw any) (TriggerPolicy, []string) {
	obj, ok := raw.(map[string]any)
	if !ok {
		return TriggerPolicy{}, []string{"trigger_policy is not a JSON object"}
	}
	kind, _ := obj["kind"].(string)
	allowed := map[string]bool{"kind": true}
	switch kind {
	case TriggerPolicyNone, TriggerPolicyBurst:
	case TriggerPolicyThreshold:
		allowed["level"], allowed["reaction_frames"], allowed["hold_frames"] = true, true, true
	default:
		return TriggerPolicy{}, []string{fmt.Sprintf("trigger_policy.kind=%v is not one of [none burst threshold]", obj["kind"])}
	}
	var errs []string
	var unknown []string
	for k := range obj {
		if !allowed[k] {
			unknown = append(unknown, k)
		}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		errs = append(errs, fmt.Sprintf("trigger_policy has unknown fields %v", unknown))
	}
	p := TriggerPolicy{Kind: kind}
	if kind != TriggerPolicyThreshold {
		return p, errs
	}
	level, isNum := toFloat(obj["level"])
	if !isNum || math.IsNaN(level) || math.IsInf(level, 0) || level < 0 || level > policyLevelMax {
		errs = append(errs, fmt.Sprintf("trigger_policy.level=%v is not a number in 0..10", obj["level"]))
	}
	reaction, rOK := wholeNumber(obj["reaction_frames"], 0, policyReactionMax)
	if !rOK {
		errs = append(errs, fmt.Sprintf("trigger_policy.reaction_frames=%v is not a whole number in 0..%d",
			obj["reaction_frames"], policyReactionMax))
	}
	hold, hOK := wholeNumber(obj["hold_frames"], policyHoldFramesMin, policyHoldFramesMax)
	if !hOK {
		errs = append(errs, fmt.Sprintf("trigger_policy.hold_frames=%v is not a whole number in %d..%d",
			obj["hold_frames"], policyHoldFramesMin, policyHoldFramesMax))
	}
	p.Level, p.ReactionFrames, p.HoldFrames = level, reaction, hold
	return p, errs
}

func wholeNumber(v any, low, high int) (int, bool) {
	f, ok := toFloat(v)
	if !ok || math.IsNaN(f) || math.IsInf(f, 0) || f != math.Trunc(f) || f < float64(low) || f > float64(high) {
		return 0, false
	}
	return int(f), true
}

// Archetype is a resolved, validated archetype (entity MOTION_ARCHETYPE).
type Archetype struct {
	Name   string
	Params map[string]float64
	// TriggerPolicy is the optional simulated trigger (UC-046 BR-018); the zero value is none.
	TriggerPolicy TriggerPolicy
	ParamsJSON    string // canonical JSON (sorted keys, no whitespace), sent to the controller manager
	Digest        string // first 16 hex digits of SHA-256(ParamsJSON), UC-046 BR-009
	Players       int
	StreamRateHz  int
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
		if _, known := archetypeFields[k]; !known && k != TriggerPolicyKey {
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
	var policy TriggerPolicy
	if raw, present := obj[TriggerPolicyKey]; present {
		var perrs []string
		policy, perrs = parseTriggerPolicy(raw)
		errs = append(errs, perrs...)
	}
	if len(errs) > 0 {
		return Archetype{}, errs
	}
	if policy.Kind == TriggerPolicyBurst && params["burst_h"] <= 0 {
		errs = append(errs, "trigger_policy burst needs burst_h > 0 (it would never press)")
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
	// The canonical object holds the numeric fields plus the policy unless it is
	// none, so the digest of an archetype without a policy is unchanged (BR-018).
	object := make(map[string]any, len(params)+1)
	for k, v := range params {
		object[k] = v
	}
	if c := policy.canonical(); c != nil {
		object[TriggerPolicyKey] = c
	}
	canonical, err := json.Marshal(object) // map keys (also nested) are emitted sorted
	if err != nil {
		return Archetype{}, []string{"parameters cannot be encoded: " + err.Error()}
	}
	sum := sha256.Sum256(canonical)
	return Archetype{
		Name:          name,
		Params:        params,
		TriggerPolicy: policy,
		ParamsJSON:    string(canonical),
		Digest:        hex.EncodeToString(sum[:])[:16],
		Players:       players,
		StreamRateHz:  int(params["stream_rate_hz"]),
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
