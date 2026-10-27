package state

import (
	"bytes"
	"encoding/json"
	"fmt"
	"sort"
)

// Evaluation-context keys the generated targeting reads. They must match the
// readers: gameId (services/game_coordinator/interventions.py
// _session_context), targetingKey (the serial), and the experiment keys of
// lib/feature_flags.py / services/agent/experiment/targeting.go.
const (
	ctxGameID       = "gameId"
	ctxTargetingKey = "targetingKey"
	ctxGameKind     = "game_kind"
	ctxExperimentID = "experiment_id"
	ctxArm          = "arm"
	gameKindReal    = "real"
	gameKindShadow  = "shadow"
)

// renderedFlag is one flag as flagd reads it.
type renderedFlag struct {
	State          string                     `json:"state"`
	Variants       map[string]json.RawMessage `json:"variants"`
	DefaultVariant string                     `json:"defaultVariant"`
	Targeting      json.RawMessage            `json:"targeting,omitempty"`
	Metadata       map[string]any             `json:"metadata"`
}

type renderedConfig struct {
	Flags    map[string]*renderedFlag `json:"flags"`
	Metadata map[string]any           `json:"metadata,omitempty"`
}

func eq(varName string, v any) map[string]any {
	return map[string]any{"==": []any{map[string]any{"var": varName}, v}}
}

// effectiveDefault is the defaultVariant after the stack-wide substitutions
// (D13): the in-memory stack overlay wins over the persisted admin value,
// which wins over a promotion (real) overlay, which wins over the baseline
// default.
func (f *flagState) effectiveDefault() string {
	switch {
	case f.stack != nil:
		return f.stack.Variant
	case f.admin != nil:
		return f.admin.Variant
	case f.real != nil:
		return f.real.Variant
	}
	return f.base.DefaultVariant
}

// render builds the flag's flagd definition (architecture note §10):
//
//   - stack and real overlays substitute defaultVariant, so baseline targeting
//     still wins;
//   - game+serial, game (values and event lists) and experiment+arm values are
//     prepended as targeting rungs, in that order, with the baseline targeting
//     as the final else (null when there is none);
//   - a fraction overlay replaces the final else with a fractional split
//     between its variant and the effective default.
func (f *flagState) render(epoch string) (*renderedFlag, error) {
	out := &renderedFlag{
		State:          f.base.State,
		Variants:       make(map[string]json.RawMessage, len(f.base.Variants)),
		DefaultVariant: f.effectiveDefault(),
		Metadata:       map[string]any{},
	}
	for name, raw := range f.base.Variants {
		out.Variants[name] = raw
	}
	for k, v := range f.base.Metadata {
		out.Metadata[k] = v
	}
	out.Metadata["flagSetId"] = f.set

	addGenerated := func(v *value) {
		if v != nil && v.generated() {
			out.Variants[v.Variant] = v.Value
		}
	}
	addGenerated(f.stack)
	addGenerated(f.admin)
	addGenerated(f.real)
	addGenerated(f.fraction)

	var rungs []any
	for _, gameID := range sortedKeys(f.serials) {
		bySerial := f.serials[gameID]
		for _, serial := range sortedKeys(bySerial) {
			v := bySerial[serial]
			addGenerated(v)
			cond := map[string]any{"and": []any{eq(ctxGameID, gameID), eq(ctxTargetingKey, serial)}}
			rungs = append(rungs, cond, v.Variant)
		}
	}
	gameIDs := map[string]bool{}
	for id := range f.games {
		gameIDs[id] = true
	}
	for id := range f.events {
		gameIDs[id] = true
	}
	for _, gameID := range sortedKeys(gameIDs) {
		if v := f.games[gameID]; v != nil {
			addGenerated(v)
			rungs = append(rungs, eq(ctxGameID, gameID), v.Variant)
		}
		if l := f.events[gameID]; l != nil {
			raw, err := json.Marshal(map[string]any{"epoch": epoch, "seq": l.Seq, "events": l.Events})
			if err != nil {
				return nil, fmt.Errorf("render %s event list: %w", f.key, err)
			}
			out.Variants[l.Variant] = raw
			rungs = append(rungs, eq(ctxGameID, gameID), l.Variant)
		}
	}
	for _, expID := range sortedKeys(f.experiments) {
		byArm := f.experiments[expID]
		for _, arm := range sortedKeys(byArm) {
			v := byArm[arm]
			addGenerated(v)
			cond := map[string]any{"and": []any{
				map[string]any{"!=": []any{map[string]any{"var": ctxGameKind}, gameKindReal}},
				eq(ctxExperimentID, expID),
				eq(ctxArm, arm),
			}}
			rungs = append(rungs, cond, v.Variant)
		}
	}

	var elseBranch json.RawMessage
	switch {
	case f.fraction != nil:
		frac := map[string]any{"fractional": []any{
			[]any{f.fraction.Variant, f.fraction.Fraction},
			[]any{out.DefaultVariant, 100 - f.fraction.Fraction},
		}}
		raw, err := json.Marshal(frac)
		if err != nil {
			return nil, err
		}
		elseBranch = raw
	case f.base.HasTargeting():
		elseBranch = f.base.Targeting
	}

	switch {
	case len(rungs) == 0:
		out.Targeting = elseBranch
	default:
		if elseBranch == nil {
			elseBranch = json.RawMessage("null")
		}
		parts := make([]json.RawMessage, 0, len(rungs)+1)
		for _, r := range rungs {
			raw, err := json.Marshal(r)
			if err != nil {
				return nil, err
			}
			parts = append(parts, raw)
		}
		parts = append(parts, elseBranch)
		var buf bytes.Buffer
		buf.WriteString(`{"if":[`)
		for i, p := range parts {
			if i > 0 {
				buf.WriteByte(',')
			}
			buf.Write(p)
		}
		buf.WriteString(`]}`)
		out.Targeting = buf.Bytes()
	}
	return out, nil
}

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// Snapshot stamp (UC-053 A9, #1552): flag metadata naming the snapshot a flag
// was rendered from. The game coordinator reads the interventions set and the
// agent allow-list on separate sync streams that reconnect in no fixed order,
// and applies an intervention only once the allow-list it holds is from the
// same epoch and at least as new as the interventions set it holds.
const (
	MetaSnapshotEpoch   = "snapshotEpoch"
	MetaSnapshotVersion = "snapshotVersion"
)

// snapshotStamped reports whether a flag carries the snapshot stamp: every
// flag of the interventions set and the agent allow-list. Other flags stay
// unstamped, so a write does not mark them changed on every stream.
func snapshotStamped(set, key string) bool {
	return set == "interventions" || (set == "agent" && key == "interventions_allowed")
}

// renderLocked returns the merged configuration for a selector: "" = every
// served set, otherwise the one set. Cached per snapshot version. Callers hold
// s.mu.
func (s *State) renderLocked(selectorSet string) ([]byte, error) {
	if s.cacheVersion != s.version {
		s.cache = map[string][]byte{}
		s.cacheVersion = s.version
	}
	if cached, ok := s.cache[selectorSet]; ok {
		return cached, nil
	}
	cfg := renderedConfig{Flags: map[string]*renderedFlag{}}
	if selectorSet != "" {
		cfg.Metadata = map[string]any{"flagSetId": selectorSet}
	}
	for key, f := range s.flags {
		if !s.served[f.set] || (selectorSet != "" && f.set != selectorSet) {
			continue
		}
		rf, err := f.render(s.opts.Epoch)
		if err != nil {
			return nil, err
		}
		if snapshotStamped(f.set, key) {
			rf.Metadata[MetaSnapshotEpoch] = s.opts.Epoch
			rf.Metadata[MetaSnapshotVersion] = s.version
		}
		cfg.Flags[key] = rf
	}
	payload, err := json.Marshal(cfg)
	if err != nil {
		return nil, fmt.Errorf("render: %w", err)
	}
	s.cache[selectorSet] = payload
	return payload, nil
}

// Snapshot returns the merged configuration for a selector set ("" = every
// served set) and its snapshot version. An unserved set is an error.
func (s *State) Snapshot(selectorSet string) ([]byte, uint64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if selectorSet != "" && !s.served[selectorSet] {
		return nil, 0, fmt.Errorf("flag set %q is not served", selectorSet)
	}
	payload, err := s.renderLocked(selectorSet)
	return payload, s.version, err
}

// RenderFlag returns one flag's rendered flagd JSON (GET /{set}/{flag}).
func (s *State) RenderFlag(set, key string) (json.RawMessage, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	f := s.flags[key]
	if f == nil || f.set != set {
		return nil, fmt.Errorf("no flag %s/%s", set, key)
	}
	rf, err := f.render(s.opts.Epoch)
	if err != nil {
		return nil, err
	}
	return json.Marshal(rf)
}
