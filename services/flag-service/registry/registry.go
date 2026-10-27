// Package registry is the mutable-flag registry (FR-132, flags.md §13): the
// only flags that may change at runtime, their kind, scopes, bounds and
// whether a write may persist them. Every other flag is read-only baseline
// (C-032, C-033). registry.yaml encodes the flags.md §13 tables; a test keeps
// the two in step.
//
// There are no writer roles (architecture note D11): a write is validated
// only against this registry, the game scope and the sync stream.
package registry

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/joustmania/flag-service/baseline"
	"github.com/joustmania/flag-service/fserr"
)

// Kinds (flags.md §13).
const (
	KindState   = "state"
	KindLevel   = "level"
	KindGrant   = "grant"
	KindEvent   = "event"
	KindOverlay = "overlay"
)

// Scopes (flags.md §13).
const (
	ScopeStack      = "stack"
	ScopeGame       = "game"
	ScopeGameSerial = "game+serial"
	ScopeExperiment = "experiment+arm"
	ScopeReal       = "real"
	ScopeFraction   = "fraction"
)

// RealWriteNoLiveRealGame is the registry attribute (real_write) that makes the
// flag service refuse a write in scope real while a registered game has kind
// real (409 real_game_live, flags.md §13, #2096): a promotion never changes a
// value a running real game reads.
const RealWriteNoLiveRealGame = "no_live_real_game"

var validKinds = map[string]bool{KindState: true, KindLevel: true, KindGrant: true, KindEvent: true, KindOverlay: true}
var validScopes = map[string]bool{
	ScopeStack: true, ScopeGame: true, ScopeGameSerial: true, ScopeExperiment: true, ScopeReal: true, ScopeFraction: true,
}

// MaxFreeString is the longest value a `free string` bound accepts.
const MaxFreeString = 512

// soundIDPattern is the syntax of an audio_cue sound id. The service cannot
// see the audio container's assets; the audio service resolves the id
// (flags.md §13.1).
var soundIDPattern = regexp.MustCompile(`^[A-Za-z0-9_.\-]{1,64}$`)

// Bounds says which values a write may carry. Exactly one of the fields is
// set; none set means "every declared variant". A declared variant name is
// always accepted by `variant` (the variants are the reviewed baseline),
// within the Variants subset when one is given.
type Bounds struct {
	// Variants restricts a write to these declared variant names.
	Variants []string `yaml:"variants,omitempty" json:"variants,omitempty"`
	// Range accepts a number within [min, max] (inclusive).
	Range []float64 `yaml:"range,omitempty" json:"range,omitempty"`
	// Integer requires a Range value to be a whole number.
	Integer bool `yaml:"integer,omitempty" json:"integer,omitempty"`
	// Pattern accepts a string made of ':'-separated segments; each
	// alternative is a list of segment specs: "enum a|b", "int lo hi",
	// "float lo hi".
	Pattern [][]string `yaml:"pattern,omitempty" json:"pattern,omitempty"`
	// FreeString accepts any string up to MaxFreeString characters.
	FreeString bool `yaml:"free_string,omitempty" json:"free_string,omitempty"`
	// Payload is the schema of an event payload: field -> rule. Rules:
	// "serial" (a serial of the game), "serial_or_empty", "sound", or an
	// "enum a|b" list.
	Payload map[string]string `yaml:"payload,omitempty" json:"payload,omitempty"`
	// Fraction is the allowed fraction_percent range for a fraction write.
	Fraction []int `yaml:"fraction,omitempty" json:"fraction,omitempty"`
	// Shape accepts a JSON object with exactly the keys of the flag's baseline
	// default variant, each value of the same JSON kind (an object-valued
	// experiment catalog flag such as `windows`).
	Shape bool `yaml:"shape,omitempty" json:"shape,omitempty"`
}

// Policy is one registry row (entity MUTABLE_FLAG_POLICY).
type Policy struct {
	Set     string   `yaml:"set" json:"flag_set"`
	Key     string   `yaml:"key" json:"flag"`
	Kind    string   `yaml:"kind" json:"kind"`
	Scopes  []string `yaml:"scope" json:"scope"`
	Bounds  Bounds   `yaml:"bounds" json:"bounds"`
	Persist bool     `yaml:"persist" json:"persist"`
	// RealWrite restricts writes in scope real: "" (none) or
	// RealWriteNoLiveRealGame.
	RealWrite string `yaml:"real_write,omitempty" json:"real_write,omitempty"`
}

// RealWriteNeedsNoRealGame reports whether a real-scope write of this flag is
// refused while a real game is registered.
func (p *Policy) RealWriteNeedsNoRealGame() bool {
	return p.RealWrite == RealWriteNoLiveRealGame
}

// AllowsScope reports whether scope is one of the policy's scopes.
func (p *Policy) AllowsScope(scope string) bool {
	for _, s := range p.Scopes {
		if s == scope {
			return true
		}
	}
	return false
}

// Registry is the loaded registry.
type Registry struct {
	policies map[string]*Policy // key: set + "/" + key
}

type fileDoc struct {
	Flags []*Policy `yaml:"flags"`
}

func id(set, key string) string { return set + "/" + key }

// Get returns the policy of (set, key), or nil when the flag is read-only.
func (r *Registry) Get(set, key string) *Policy {
	if r == nil {
		return nil
	}
	return r.policies[id(set, key)]
}

// Policies returns every policy, sorted by set and key.
func (r *Registry) Policies() []*Policy {
	out := make([]*Policy, 0, len(r.policies))
	for _, p := range r.policies {
		out = append(out, p)
	}
	sort.Slice(out, func(i, j int) bool { return id(out[i].Set, out[i].Key) < id(out[j].Set, out[j].Key) })
	return out
}

// LoadFile reads and validates the registry at path against the baseline.
func LoadFile(path string, b *baseline.Baseline) (*Registry, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("registry: read %s: %w", path, err)
	}
	return Parse(raw, b)
}

// Parse decodes and validates a registry document against the baseline.
func Parse(raw []byte, b *baseline.Baseline) (*Registry, error) {
	var doc fileDoc
	dec := yaml.NewDecoder(bytes.NewReader(raw))
	dec.KnownFields(true)
	if err := dec.Decode(&doc); err != nil {
		return nil, fmt.Errorf("registry: parse: %w", err)
	}
	r := &Registry{policies: map[string]*Policy{}}
	for _, p := range doc.Flags {
		if err := validatePolicy(p, b); err != nil {
			return nil, fmt.Errorf("registry: %s/%s: %w", p.Set, p.Key, err)
		}
		if _, dup := r.policies[id(p.Set, p.Key)]; dup {
			return nil, fmt.Errorf("registry: %s/%s declared twice", p.Set, p.Key)
		}
		r.policies[id(p.Set, p.Key)] = p
	}
	return r, nil
}

func validatePolicy(p *Policy, b *baseline.Baseline) error {
	flag := b.Flag(p.Set, p.Key)
	if flag == nil {
		return fmt.Errorf("no such flag in the baseline")
	}
	if !validKinds[p.Kind] {
		return fmt.Errorf("unknown kind %q", p.Kind)
	}
	if len(p.Scopes) == 0 {
		return fmt.Errorf("no scope")
	}
	for _, s := range p.Scopes {
		if !validScopes[s] {
			return fmt.Errorf("unknown scope %q", s)
		}
	}
	// real_write is checked for its value only, not against the scopes: a row
	// may carry it before its scope is widened to real (the guard is then inert).
	if p.RealWrite != "" && p.RealWrite != RealWriteNoLiveRealGame {
		return fmt.Errorf("unknown real_write %q (allowed: %s)", p.RealWrite, RealWriteNoLiveRealGame)
	}
	if p.RealWrite != "" && p.Kind != KindOverlay {
		return fmt.Errorf("real_write needs kind overlay")
	}
	if p.Persist && (p.Kind != KindOverlay || !p.AllowsScope(ScopeStack)) {
		return fmt.Errorf("persist needs kind overlay with scope stack")
	}
	if p.Kind == KindEvent {
		if len(p.Scopes) != 1 || p.Scopes[0] != ScopeGame || p.Bounds.Payload == nil {
			return fmt.Errorf("an event flag has scope [game] and a payload schema")
		}
		for field, rule := range p.Bounds.Payload {
			if err := validPayloadRule(rule); err != nil {
				return fmt.Errorf("payload field %q: %w", field, err)
			}
		}
	}
	if p.Kind == KindGrant && !p.AllowsScope(ScopeGameSerial) {
		return fmt.Errorf("a grant has scope game+serial")
	}
	for _, v := range p.Bounds.Variants {
		if _, ok := flag.Variants[v]; !ok {
			return fmt.Errorf("bound variant %q is not declared", v)
		}
	}
	if r := p.Bounds.Range; r != nil && (len(r) != 2 || r[0] > r[1]) {
		return fmt.Errorf("range must be [min, max]")
	}
	for _, alt := range p.Bounds.Pattern {
		for _, seg := range alt {
			if _, err := parseSegment(seg); err != nil {
				return err
			}
		}
	}
	if p.Bounds.Shape {
		var obj map[string]json.RawMessage
		if err := json.Unmarshal(flag.Variants[flag.DefaultVariant], &obj); err != nil || obj == nil {
			return fmt.Errorf("a shape bound needs an object-valued default variant")
		}
	}
	if p.AllowsScope(ScopeFraction) {
		if f := p.Bounds.Fraction; len(f) != 2 || f[0] < 1 || f[1] > 100 || f[0] > f[1] {
			return fmt.Errorf("a fraction scope needs bounds.fraction [lo, hi] within 1..100")
		}
	}
	return nil
}

func validPayloadRule(rule string) error {
	switch {
	case rule == "serial", rule == "serial_or_empty", rule == "sound":
		return nil
	case strings.HasPrefix(rule, "enum "):
		return nil
	}
	return fmt.Errorf("unknown payload rule %q", rule)
}

// Checked is a validated value for a non-event write.
type Checked struct {
	// Variant is the declared variant the write selects, or "" when the value
	// is not a declared variant (the service generates an opaque variant).
	Variant string
	// Value is the JSON value the variant resolves to.
	Value json.RawMessage
}

// CheckValue validates a PUT's variant or value against the policy bounds.
// Exactly one of variant and value is set (the caller checks that).
func CheckValue(p *Policy, flag *baseline.Flag, variant string, value json.RawMessage) (*Checked, *fserr.Error) {
	if variant != "" {
		raw, ok := flag.Variants[variant]
		if !ok {
			return nil, fserr.Unprocessable(fserr.OutOfBounds, "%q is not a declared variant of %s", variant, p.Key)
		}
		if len(p.Bounds.Variants) > 0 && !contains(p.Bounds.Variants, variant) {
			return nil, fserr.Unprocessable(fserr.OutOfBounds, "variant %q is not allowed for %s (allowed: %s)", variant, p.Key, strings.Join(p.Bounds.Variants, ", "))
		}
		return &Checked{Variant: variant, Value: raw}, nil
	}
	value = bytes.TrimSpace(value)
	// A value equal to an allowed declared variant selects that variant.
	if name := matchVariant(p, flag, value); name != "" {
		return &Checked{Variant: name, Value: flag.Variants[name]}, nil
	}
	switch {
	case p.Bounds.Range != nil:
		if err := checkRange(p, value); err != nil {
			return nil, err
		}
	case p.Bounds.Pattern != nil:
		s, ok := asString(value)
		if !ok || !matchPattern(p.Bounds.Pattern, s) {
			return nil, fserr.Unprocessable(fserr.OutOfBounds, "%s does not match the format of %s", value, p.Key)
		}
	case p.Bounds.Shape:
		if !sameShape(flag.Variants[flag.DefaultVariant], value) {
			return nil, fserr.Unprocessable(fserr.OutOfBounds, "%s takes an object with the keys and value types of its declared variants, got %s", p.Key, value)
		}
	case p.Bounds.FreeString:
		s, ok := asString(value)
		if !ok || len(s) > MaxFreeString {
			return nil, fserr.Unprocessable(fserr.OutOfBounds, "%s takes a string of at most %d characters", p.Key, MaxFreeString)
		}
	default:
		return nil, fserr.Unprocessable(fserr.OutOfBounds, "%s accepts only its declared variants; %s is none of them", p.Key, value)
	}
	return &Checked{Value: value}, nil
}

// sameShape reports whether value is a JSON object with exactly the keys of
// want, each value of the same JSON kind.
func sameShape(want, value json.RawMessage) bool {
	var w, v map[string]json.RawMessage
	if json.Unmarshal(want, &w) != nil || json.Unmarshal(value, &v) != nil || v == nil || len(w) != len(v) {
		return false
	}
	for k, wv := range w {
		vv, ok := v[k]
		if !ok || kindOf(wv) != kindOf(vv) {
			return false
		}
	}
	return true
}

func kindOf(raw json.RawMessage) string {
	var x any
	if json.Unmarshal(raw, &x) != nil {
		return "invalid"
	}
	switch x.(type) {
	case float64:
		return "number"
	case string:
		return "string"
	case bool:
		return "bool"
	case []any:
		return "array"
	case map[string]any:
		return "object"
	}
	return "null"
}

func matchVariant(p *Policy, flag *baseline.Flag, value json.RawMessage) string {
	allowed := p.Bounds.Variants
	if len(allowed) == 0 {
		allowed = flag.VariantNames()
	}
	for _, name := range allowed {
		if jsonEqual(flag.Variants[name], value) {
			return name
		}
	}
	return ""
}

func checkRange(p *Policy, value json.RawMessage) *fserr.Error {
	if len(value) == 0 || value[0] == '"' {
		return fserr.Unprocessable(fserr.OutOfBounds, "%s takes a number, got %s", p.Key, value)
	}
	var n json.Number
	dec := json.NewDecoder(bytes.NewReader(value))
	dec.UseNumber()
	if err := dec.Decode(&n); err != nil {
		return fserr.Unprocessable(fserr.OutOfBounds, "%s takes a number, got %s", p.Key, value)
	}
	f, err := n.Float64()
	if err != nil {
		return fserr.Unprocessable(fserr.OutOfBounds, "%s takes a number, got %s", p.Key, value)
	}
	if p.Bounds.Integer {
		if _, err := n.Int64(); err != nil {
			return fserr.Unprocessable(fserr.OutOfBounds, "%s takes a whole number, got %s", p.Key, value)
		}
	}
	lo, hi := p.Bounds.Range[0], p.Bounds.Range[1]
	if f < lo || f > hi {
		return fserr.Unprocessable(fserr.OutOfBounds, "%s must be within %v to %v, got %s", p.Key, lo, hi, value)
	}
	return nil
}

type segment struct {
	kind   string // enum, int, float
	enum   []string
	lo, hi float64
}

func parseSegment(spec string) (*segment, error) {
	fields := strings.Fields(spec)
	if len(fields) == 0 {
		return nil, fmt.Errorf("empty pattern segment")
	}
	switch fields[0] {
	case "enum":
		if len(fields) != 2 {
			return nil, fmt.Errorf("pattern segment %q: enum a|b", spec)
		}
		return &segment{kind: "enum", enum: strings.Split(fields[1], "|")}, nil
	case "int", "float":
		if len(fields) != 3 {
			return nil, fmt.Errorf("pattern segment %q: %s lo hi", spec, fields[0])
		}
		lo, err1 := strconv.ParseFloat(fields[1], 64)
		hi, err2 := strconv.ParseFloat(fields[2], 64)
		if err1 != nil || err2 != nil || lo > hi {
			return nil, fmt.Errorf("pattern segment %q: bad bounds", spec)
		}
		return &segment{kind: fields[0], lo: lo, hi: hi}, nil
	}
	return nil, fmt.Errorf("pattern segment %q: unknown kind", spec)
}

func matchPattern(alternatives [][]string, s string) bool {
	parts := strings.Split(s, ":")
	for _, alt := range alternatives {
		if len(alt) == len(parts) && matchSegments(alt, parts) {
			return true
		}
	}
	return false
}

func matchSegments(alt []string, parts []string) bool {
	for i, spec := range alt {
		seg, err := parseSegment(spec)
		if err != nil || !seg.match(parts[i]) {
			return false
		}
	}
	return true
}

func (s *segment) match(part string) bool {
	switch s.kind {
	case "enum":
		return contains(s.enum, part)
	case "int":
		n, err := strconv.Atoi(part)
		return err == nil && float64(n) >= s.lo && float64(n) <= s.hi
	case "float":
		f, err := strconv.ParseFloat(part, 64)
		return err == nil && f >= s.lo && f <= s.hi
	}
	return false
}

// PayloadCheck validates an event payload. inGame reports whether a serial
// belongs to the game the event is appended to.
func PayloadCheck(p *Policy, payload map[string]any, inGame func(string) bool) *fserr.Error {
	schema := p.Bounds.Payload
	for field := range payload {
		if _, ok := schema[field]; !ok {
			return fserr.Unprocessable(fserr.OutOfBounds, "%s payload has no field %q", p.Key, field)
		}
	}
	fields := make([]string, 0, len(schema))
	for f := range schema {
		fields = append(fields, f)
	}
	sort.Strings(fields)
	for _, field := range fields {
		if err := checkPayloadField(p.Key, field, schema[field], payload[field], inGame); err != nil {
			return err
		}
	}
	return nil
}

func checkPayloadField(key, field, rule string, raw any, inGame func(string) bool) *fserr.Error {
	s, isString := raw.(string)
	if raw != nil && !isString {
		return fserr.Unprocessable(fserr.OutOfBounds, "%s payload field %q must be a string", key, field)
	}
	switch {
	case rule == "serial":
		if s == "" {
			return fserr.BadRequest("%s payload needs %q", key, field)
		}
		if !inGame(s) {
			return fserr.Unprocessable(fserr.SerialNotInGame, "%s: serial %q is not a player of the game", key, s)
		}
	case rule == "serial_or_empty":
		if s != "" && !inGame(s) {
			return fserr.Unprocessable(fserr.SerialNotInGame, "%s: serial %q is not a player of the game", key, s)
		}
	case rule == "sound":
		if !soundIDPattern.MatchString(s) {
			return fserr.Unprocessable(fserr.OutOfBounds, "%s: %q is not a sound id", key, s)
		}
	case strings.HasPrefix(rule, "enum "):
		if !contains(strings.Split(strings.TrimPrefix(rule, "enum "), "|"), s) {
			return fserr.Unprocessable(fserr.OutOfBounds, "%s payload field %q: %q is not allowed", key, field, s)
		}
	}
	return nil
}

func contains(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}

func asString(raw json.RawMessage) (string, bool) {
	var s string
	if err := json.Unmarshal(raw, &s); err != nil {
		return "", false
	}
	return s, true
}

// jsonEqual compares two JSON values semantically (numbers by value).
func jsonEqual(a, b json.RawMessage) bool {
	var va, vb any
	if json.Unmarshal(a, &va) != nil || json.Unmarshal(b, &vb) != nil {
		return false
	}
	ja, _ := json.Marshal(va)
	jb, _ := json.Marshal(vb)
	return bytes.Equal(ja, jb)
}
