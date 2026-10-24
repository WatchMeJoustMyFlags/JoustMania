// Package baseline loads the read-only flag files (services/flagd/<set>.json)
// that the flag service serves as its baseline (FR-130, UC-053).
//
// The files are read once at start from an explicit list (never a glob, so a
// stray file in the directory is never served) and never written. Each file is one flag set; its flags are later merged into
// one flagd configuration in which every flag carries metadata.flagSetId
// (architecture note §5.2). A duplicate flag key across sets, or two files
// declaring the same set, refuses the start.
package baseline

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// DefaultFiles is the explicit baseline file list (one per flag set).
var DefaultFiles = []string{
	"system.json",
	"controller.json",
	"observability.json",
	"game.json",
	"user.json",
	"agent.json",
	"interventions.json",
	"rollout.json",
	"operator.json",
}

// ReservedSetIDs cannot be flag-set ids: they are HTTP API paths (services.md §7.2).
var ReservedSetIDs = map[string]bool{"games": true, "coordinators": true, "state": true, "healthz": true}

// Flag is one flagd flag definition as read from a baseline file.
type Flag struct {
	State          string                     `json:"state"`
	Variants       map[string]json.RawMessage `json:"variants"`
	DefaultVariant string                     `json:"defaultVariant"`
	Targeting      json.RawMessage            `json:"targeting,omitempty"`
	Metadata       map[string]any             `json:"metadata,omitempty"`
	// order is the declared order of the variants in the baseline file (the
	// menu cycles admin settings in this order).
	order []string
}

// HasTargeting reports whether the flag declares a non-empty targeting rule.
func (f *Flag) HasTargeting() bool {
	t := bytes.TrimSpace(f.Targeting)
	return len(t) > 0 && !bytes.Equal(t, []byte("null")) && !bytes.Equal(t, []byte("{}"))
}

// VariantNames returns the declared variant names in the order the baseline
// file declares them.
func (f *Flag) VariantNames() []string {
	if len(f.order) == len(f.Variants) {
		return append([]string(nil), f.order...)
	}
	names := make([]string, 0, len(f.Variants))
	for name := range f.Variants {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

// objectKeys returns the keys of a JSON object in document order.
func objectKeys(raw json.RawMessage) ([]string, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	tok, err := dec.Token()
	if err != nil {
		return nil, err
	}
	if d, ok := tok.(json.Delim); !ok || d != '{' {
		return nil, fmt.Errorf("not a JSON object")
	}
	var keys []string
	for dec.More() {
		tok, err := dec.Token()
		if err != nil {
			return nil, err
		}
		key, _ := tok.(string)
		keys = append(keys, key)
		var skip json.RawMessage
		if err := dec.Decode(&skip); err != nil {
			return nil, err
		}
	}
	return keys, nil
}

// Set is one flag set (one baseline file).
type Set struct {
	ID       string
	File     string
	Metadata map[string]any
	Flags    map[string]*Flag
}

// FlagKeys returns the set's flag keys, sorted.
func (s *Set) FlagKeys() []string {
	keys := make([]string, 0, len(s.Flags))
	for k := range s.Flags {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// Baseline is every loaded flag set.
type Baseline struct {
	Sets map[string]*Set
	// Order is the load order of the set ids (the file list order).
	Order []string
	// setOf maps a flag key to its set id (keys are unique across sets).
	setOf map[string]string
}

// SetOf returns the set id that declares key, or "" when no set does.
func (b *Baseline) SetOf(key string) string { return b.setOf[key] }

// Flag returns the baseline flag (set, key) or nil.
func (b *Baseline) Flag(setID, key string) *Flag {
	s := b.Sets[setID]
	if s == nil {
		return nil
	}
	return s.Flags[key]
}

type fileDoc struct {
	Schema   string                     `json:"$schema,omitempty"`
	Metadata map[string]any             `json:"metadata"`
	Flags    map[string]json.RawMessage `json:"flags"`
}

// Load reads every file of files (names relative to dir) and merges them.
// It fails on an unreadable or malformed file, a file without flags, a set
// id that is reserved or declared twice, a flag without its defaultVariant
// among its variants, and a flag key declared by two sets.
func Load(dir string, files []string) (*Baseline, error) {
	if len(files) == 0 {
		return nil, fmt.Errorf("baseline: empty file list")
	}
	b := &Baseline{Sets: map[string]*Set{}, setOf: map[string]string{}}
	for _, name := range files {
		path := filepath.Join(dir, name)
		raw, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("baseline: read %s: %w", path, err)
		}
		set, err := parseSet(name, raw)
		if err != nil {
			return nil, fmt.Errorf("baseline: %s: %w", path, err)
		}
		if _, dup := b.Sets[set.ID]; dup {
			return nil, fmt.Errorf("baseline: flag set %q declared by two files (%s and %s)", set.ID, b.Sets[set.ID].File, name)
		}
		for _, key := range set.FlagKeys() {
			if other, dup := b.setOf[key]; dup {
				return nil, fmt.Errorf("baseline: duplicate flag key %q in sets %q and %q: one sync payload holds each key once (architecture note §5.2)", key, other, set.ID)
			}
			b.setOf[key] = set.ID
		}
		b.Sets[set.ID] = set
		b.Order = append(b.Order, set.ID)
	}
	return b, nil
}

func parseSet(fileName string, raw []byte) (*Set, error) {
	var doc fileDoc
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&doc); err != nil {
		return nil, fmt.Errorf("parse: %w", err)
	}
	if len(doc.Flags) == 0 {
		return nil, fmt.Errorf("no flags")
	}
	id := strings.TrimSuffix(filepath.Base(fileName), filepath.Ext(fileName))
	if declared, ok := doc.Metadata["flagSetId"].(string); ok && declared != "" {
		if declared != id {
			return nil, fmt.Errorf("metadata.flagSetId %q does not match the file name %q", declared, id)
		}
	}
	if ReservedSetIDs[id] {
		return nil, fmt.Errorf("flag set id %q is reserved", id)
	}
	flags := make(map[string]*Flag, len(doc.Flags))
	for key, rawFlag := range doc.Flags {
		var f *Flag
		dec := json.NewDecoder(bytes.NewReader(rawFlag))
		dec.UseNumber()
		if err := dec.Decode(&f); err != nil {
			return nil, fmt.Errorf("flag %q: %w", key, err)
		}
		if f == nil {
			return nil, fmt.Errorf("flag %q is null", key)
		}
		var withVariants struct {
			Variants json.RawMessage `json:"variants"`
		}
		if err := json.Unmarshal(rawFlag, &withVariants); err == nil && len(withVariants.Variants) > 0 {
			f.order, _ = objectKeys(withVariants.Variants)
		}
		flags[key] = f
		if len(f.Variants) == 0 {
			return nil, fmt.Errorf("flag %q has no variants", key)
		}
		if _, ok := f.Variants[f.DefaultVariant]; !ok {
			return nil, fmt.Errorf("flag %q: defaultVariant %q is not a declared variant", key, f.DefaultVariant)
		}
		if f.State == "" {
			f.State = "ENABLED"
		}
	}
	meta := doc.Metadata
	if meta == nil {
		meta = map[string]any{}
	}
	return &Set{ID: id, File: fileName, Metadata: meta, Flags: flags}, nil
}
