// Package fake is an in-memory stand-in for the flag service (epic #1407) for
// the agent's tests: it serves one or more flag sets from baseline JSON,
// accepts the writes the agent sends (PUT stack / real / experiment+arm
// values, DELETE) and renders them the way services/flag-service does
// (experiment rung: game_kind != "real" AND experiment_id AND arm; a real
// overlay substitutes defaultVariant), so the experiment Gate's JSONLogic
// checks run against realistic rendered flags.
//
// It is a test double: it implements the read/write subset the agent uses and
// a registry given as a map of flag -> allowed scopes.
package fake

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"sort"
	"sync"

	"gopkg.in/yaml.v3"

	"github.com/joustmania/agent/flagservice"
)

type flag struct {
	State          string                     `json:"state"`
	Variants       map[string]json.RawMessage `json:"variants"`
	DefaultVariant string                     `json:"defaultVariant"`
	Targeting      json.RawMessage            `json:"targeting,omitempty"`
	order          []string
}

// Policy is the part of a registry row the fake enforces: the allowed scopes
// and a numeric range (a value equal to a declared variant's value is always
// accepted). A flag without a policy is read-only.
type Policy struct {
	Scopes  []string
	Range   []float64
	Integer bool
	// Shape accepts an object with the default variant's keys and value kinds.
	Shape bool
	// Description is served in the view's policy (flags.md §13, #2345).
	Description string
}

type armKey struct{ exp, arm string }

type value struct {
	variant string
	raw     json.RawMessage
	writer  string
}

type flagState struct {
	base     *flag
	policy   Policy
	scopes   map[string]bool
	stack    *value
	real     *value
	arms     map[armKey]*value
	armOrder []armKey
}

// Service is the fake flag service.
type Service struct {
	mu      sync.Mutex
	sets    map[string]map[string]*flagState
	counter int
	// Calls records every write as "PUT set/flag scope" / "DELETE set/flag query".
	Calls []string
	// FailPut makes every PUT fail with this error (nil: accept).
	FailPut error
	epoch   int
}

// Epoch implements GET /healthz's boot epoch.
func (s *Service) Epoch(context.Context) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return fmt.Sprintf("fake%d", s.epoch), nil
}

// Restart simulates a flag-service restart: a new epoch, and every in-memory
// value (stack and real overlays, experiment arms) is gone.
func (s *Service) Restart() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.epoch++
	for _, flags := range s.sets {
		for _, f := range flags {
			f.stack, f.real = nil, nil
			f.arms = map[armKey]*value{}
			f.armOrder = nil
		}
	}
}

// New builds an empty fake.
func New() *Service { return &Service{sets: map[string]map[string]*flagState{}} }

// LoadSet loads a flag set from a baseline file with only scopes (no range):
// scopes maps a flag to the registry scopes it allows.
func (s *Service) LoadSet(set, path string, scopes map[string][]string) error {
	policies := map[string]Policy{}
	for k, sc := range scopes {
		policies[k] = Policy{Scopes: sc}
	}
	return s.LoadSetWithPolicies(set, path, policies)
}

// LoadSetWithPolicies loads a flag set from a baseline file; policies maps a
// flag to its registry policy (a flag absent from it is read-only).
func (s *Service) LoadSetWithPolicies(set, path string, policies map[string]Policy) error {
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	var doc struct {
		Flags map[string]json.RawMessage `json:"flags"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	flags := map[string]*flagState{}
	for key, rawFlag := range doc.Flags {
		var f flag
		if err := json.Unmarshal(rawFlag, &f); err != nil {
			return fmt.Errorf("%s: %w", key, err)
		}
		f.order = variantOrder(rawFlag)
		allowed := map[string]bool{}
		for _, sc := range policies[key].Scopes {
			allowed[sc] = true
		}
		flags[key] = &flagState{base: &f, policy: policies[key], scopes: allowed, arms: map[armKey]*value{}}
	}
	s.sets[set] = flags
	return nil
}

func variantOrder(rawFlag json.RawMessage) []string {
	var withVariants struct {
		Variants json.RawMessage `json:"variants"`
	}
	if json.Unmarshal(rawFlag, &withVariants) != nil {
		return nil
	}
	dec := json.NewDecoder(bytes.NewReader(withVariants.Variants))
	if _, err := dec.Token(); err != nil {
		return nil
	}
	var keys []string
	for dec.More() {
		tok, err := dec.Token()
		if err != nil {
			return keys
		}
		keys = append(keys, tok.(string))
		var skip json.RawMessage
		_ = dec.Decode(&skip)
	}
	return keys
}

func (s *Service) lookup(set, key string) (*flagState, error) {
	flags, ok := s.sets[set]
	if !ok {
		return nil, &flagservice.Error{Status: 404, Reason: "unknown_flag_set"}
	}
	f, ok := flags[key]
	if !ok {
		return nil, &flagservice.Error{Status: 404, Reason: "unknown_flag"}
	}
	return f, nil
}

// Put implements the PUT subset the agent uses.
func (s *Service) Put(_ context.Context, set, key string, w flagservice.Write) (flagservice.Result, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	scope := "stack"
	switch {
	case w.Scope.ExperimentID != "" || w.Scope.Arm != "":
		scope = "experiment+arm"
	case w.Scope.GameKind == "real":
		scope = "real"
	case w.Scope.GameID != "":
		scope = "game"
	}
	method := "PUT"
	if w.DryRun {
		method = "DRYRUN"
	}
	s.Calls = append(s.Calls, fmt.Sprintf("%s %s/%s %s", method, set, key, scope))
	if s.FailPut != nil {
		return flagservice.Result{}, s.FailPut
	}
	f, err := s.lookup(set, key)
	if err != nil {
		return flagservice.Result{}, err
	}
	if len(f.scopes) == 0 {
		return flagservice.Result{}, &flagservice.Error{Status: 422, Reason: "flag_read_only"}
	}
	if !f.scopes[scope] {
		return flagservice.Result{}, &flagservice.Error{Status: 400, Reason: "malformed", Detail: "scope does not fit"}
	}
	if w.IfWriterIn != nil {
		// ?if_writer_in= (#2260): the write precondition, like the flag service.
		if scope != "real" {
			return flagservice.Result{}, &flagservice.Error{Status: 400, Reason: "malformed", Detail: "if_writer_in outside scope real"}
		}
	}
	var v value
	if w.Variant != "" {
		raw, ok := f.base.Variants[w.Variant]
		if !ok {
			return flagservice.Result{}, &flagservice.Error{Status: 422, Reason: "out_of_bounds"}
		}
		v = value{variant: w.Variant, raw: raw, writer: w.Writer}
	} else {
		raw, _ := json.Marshal(w.Value)
		match := ""
		for _, name := range f.base.order {
			if jsonEqual(f.base.Variants[name], raw) {
				match = name
				break
			}
		}
		if match == "" && !f.inBounds(raw) {
			return flagservice.Result{}, &flagservice.Error{Status: 422, Reason: "out_of_bounds"}
		}
		s.counter++
		v = value{variant: fmt.Sprintf("rfake_%d", s.counter), raw: raw, writer: w.Writer}
	}
	// Checked last, after the 422 value/bounds checks, like the real service.
	if w.IfWriterIn != nil && !realWriterAllowed(f.real, w.IfWriterIn) {
		return flagservice.Result{}, &flagservice.Error{Status: 412, Reason: flagservice.ReasonWriterMismatch, Detail: "fake: real overlay owned by another writer"}
	}
	var before json.RawMessage
	var restore func()
	if w.DryRun {
		before = f.view(key).Rendered
		oldStack, oldReal := f.stack, f.real
		oldArms := make(map[armKey]*value, len(f.arms))
		for k, a := range f.arms {
			oldArms[k] = a
		}
		oldOrder := append([]armKey(nil), f.armOrder...)
		restore = func() { f.stack, f.real, f.arms, f.armOrder = oldStack, oldReal, oldArms, oldOrder }
	}
	switch scope {
	case "stack":
		f.stack = &v
	case "real":
		f.real = &v
	case "experiment+arm":
		k := armKey{w.Scope.ExperimentID, w.Scope.Arm}
		if _, ok := f.arms[k]; !ok {
			f.armOrder = append(f.armOrder, k)
		}
		f.arms[k] = &v
	}
	res := flagservice.Result{FlagSet: set, Flag: key, Variant: v.variant, Epoch: "fake"}
	if w.DryRun {
		res.DryRun, res.RenderedBefore, res.Rendered = true, before, f.view(key).Rendered
		restore()
	}
	return res, nil
}

// Post is not used by the experiment and promotion paths.
func (s *Service) Post(context.Context, string, string, flagservice.Event) (flagservice.Result, error) {
	return flagservice.Result{}, fmt.Errorf("fake: POST not supported")
}

// Delete implements the DELETE subset the agent uses.
func (s *Service) Delete(_ context.Context, set, key string, q url.Values) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.Calls = append(s.Calls, fmt.Sprintf("DELETE %s/%s %s", set, key, q.Encode()))
	f, err := s.lookup(set, key)
	if err != nil {
		return err
	}
	switch {
	case q.Get("experiment_id") != "":
		k := armKey{q.Get("experiment_id"), q.Get("arm")}
		delete(f.arms, k)
		for i, o := range f.armOrder {
			if o == k {
				f.armOrder = append(f.armOrder[:i], f.armOrder[i+1:]...)
				break
			}
		}
	case q.Get("game_kind") == "real":
		// ?if_writer= (#1877): compare-and-delete, like the flag service.
		if iw := q.Get("if_writer"); iw != "" && f.real != nil && f.real.writer != iw {
			return &flagservice.Error{Status: 412, Reason: flagservice.ReasonWriterMismatch, Detail: "fake: real overlay written by " + f.real.writer}
		}
		f.real = nil
	default:
		f.stack = nil
	}
	return nil
}

// realWriterAllowed mirrors the flag service: "" matches no overlay, a label
// matches an overlay with that writer; an unlabeled overlay matches nothing.
func realWriterAllowed(cur *value, allowed []string) bool {
	for _, w := range allowed {
		if (cur == nil && w == "") || (cur != nil && w != "" && cur.writer == w) {
			return true
		}
	}
	return false
}

func (f *flagState) inBounds(raw json.RawMessage) bool {
	p := f.policy
	switch {
	case len(p.Range) == 2:
		var n float64
		if json.Unmarshal(raw, &n) != nil || n < p.Range[0] || n > p.Range[1] {
			return false
		}
		return !p.Integer || n == float64(int64(n))
	case p.Shape:
		var want, got map[string]json.RawMessage
		if json.Unmarshal(f.base.Variants[f.base.DefaultVariant], &want) != nil || json.Unmarshal(raw, &got) != nil || got == nil || len(want) != len(got) {
			return false
		}
		for k := range want {
			if _, ok := got[k]; !ok {
				return false
			}
		}
		return true
	}
	return false
}

func (f *flagState) effectiveDefault() string {
	switch {
	case f.stack != nil:
		return f.stack.variant
	case f.real != nil:
		return f.real.variant
	}
	return f.base.DefaultVariant
}

func (f *flagState) rendered() json.RawMessage {
	variants := map[string]json.RawMessage{}
	for k, v := range f.base.Variants {
		variants[k] = v
	}
	for _, v := range []*value{f.stack, f.real} {
		if v != nil {
			variants[v.variant] = v.raw
		}
	}
	out := map[string]any{"state": "ENABLED", "variants": variants, "defaultVariant": f.effectiveDefault()}
	var rungs []any
	keys := append([]armKey(nil), f.armOrder...)
	sort.Slice(keys, func(i, j int) bool { return keys[i].exp+keys[i].arm < keys[j].exp+keys[j].arm })
	for _, k := range keys {
		v := f.arms[k]
		variants[v.variant] = v.raw
		cond := map[string]any{"and": []any{
			map[string]any{"!=": []any{map[string]any{"var": "game_kind"}, "real"}},
			map[string]any{"==": []any{map[string]any{"var": "experiment_id"}, k.exp}},
			map[string]any{"==": []any{map[string]any{"var": "arm"}, k.arm}},
		}}
		rungs = append(rungs, cond, v.variant)
	}
	var elseBranch any
	if len(f.base.Targeting) > 0 && string(f.base.Targeting) != "{}" {
		elseBranch = f.base.Targeting
	}
	if len(rungs) > 0 {
		out["targeting"] = map[string]any{"if": append(rungs, elseBranch)}
	} else if elseBranch != nil {
		out["targeting"] = elseBranch
	}
	raw, _ := json.Marshal(out)
	return raw
}

func (f *flagState) view(key string) flagservice.FlagView {
	scopes := make([]string, 0, len(f.scopes))
	for sc := range f.scopes {
		scopes = append(scopes, sc)
	}
	sort.Strings(scopes)
	v := flagservice.FlagView{
		Flag:             key,
		Mutable:          len(scopes) > 0,
		BaselineDefault:  f.base.DefaultVariant,
		EffectiveDefault: f.effectiveDefault(),
		Variants:         append([]string(nil), f.base.order...),
		VariantValues:    f.base.Variants,
		Rendered:         f.rendered(),
	}
	if len(scopes) > 0 {
		v.Policy = &flagservice.Policy{
			Kind: "overlay", Scopes: scopes, Description: f.policy.Description,
			Bounds: flagservice.Bounds{Range: f.policy.Range, Integer: f.policy.Integer},
		}
	}
	if f.stack != nil {
		v.StackOverlay = &flagservice.ValueView{Variant: f.stack.variant, Value: f.stack.raw, Writer: f.stack.writer}
	}
	if f.real != nil {
		v.RealOverlay = &flagservice.ValueView{Variant: f.real.variant, Value: f.real.raw, Writer: f.real.writer}
	}
	return v
}

// GetFlag implements GET /{set}/{flag}.
func (s *Service) GetFlag(_ context.Context, set, key string) (flagservice.FlagView, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	f, err := s.lookup(set, key)
	if err != nil {
		return flagservice.FlagView{}, err
	}
	return f.view(key), nil
}

// GetSet implements GET /{set}.
func (s *Service) GetSet(_ context.Context, set string) (flagservice.SetView, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	flags, ok := s.sets[set]
	if !ok {
		return flagservice.SetView{}, &flagservice.Error{Status: 404, Reason: "unknown_flag_set"}
	}
	out := flagservice.SetView{FlagSet: set, Served: true}
	keys := make([]string, 0, len(flags))
	for k := range flags {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		v := flags[k].view(k)
		v.Rendered = nil
		out.Flags = append(out.Flags, v)
	}
	return out, nil
}

// Rendered returns a flag's rendered form (for test asserts): the JSON
// document flagd would receive for it.
func (s *Service) Rendered(set, key string) json.RawMessage {
	s.mu.Lock()
	defer s.mu.Unlock()
	f, err := s.lookup(set, key)
	if err != nil {
		return nil
	}
	return f.rendered()
}

// ArmValues returns the experiment-arm values of a flag keyed "<experiment>|<arm>".
func (s *Service) ArmValues(set, key string) map[string]json.RawMessage {
	s.mu.Lock()
	defer s.mu.Unlock()
	f, err := s.lookup(set, key)
	if err != nil {
		return nil
	}
	out := map[string]json.RawMessage{}
	for k, v := range f.arms {
		out[k.exp+"|"+k.arm] = v.raw
	}
	return out
}

// RealValue returns the real overlay of a flag, if any.
func (s *Service) RealValue(set, key string) (json.RawMessage, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	f, err := s.lookup(set, key)
	if err != nil || f.real == nil {
		return nil, false
	}
	return f.real.raw, true
}

func jsonEqual(a, b json.RawMessage) bool {
	var av, bv any
	if json.Unmarshal(a, &av) != nil || json.Unmarshal(b, &bv) != nil {
		return false
	}
	am, _ := json.Marshal(av)
	bm, _ := json.Marshal(bv)
	return string(am) == string(bm)
}

// RegistryPolicies reads services/flag-service/registry.yaml and returns the
// policies of one flag set, the argument LoadSetWithPolicies takes, so a test
// runs against the real registry's catalog and bounds.
func RegistryPolicies(path, set string) (map[string]Policy, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var doc struct {
		Flags []struct {
			Set         string   `yaml:"set"`
			Key         string   `yaml:"key"`
			Scope       []string `yaml:"scope"`
			Description string   `yaml:"description"`
			Bounds      struct {
				Range   []float64 `yaml:"range"`
				Integer bool      `yaml:"integer"`
				Shape   bool      `yaml:"shape"`
			} `yaml:"bounds"`
		} `yaml:"flags"`
	}
	if err := yaml.Unmarshal(raw, &doc); err != nil {
		return nil, err
	}
	out := map[string]Policy{}
	for _, f := range doc.Flags {
		if f.Set == set {
			out[f.Key] = Policy{Scopes: f.Scope, Range: f.Bounds.Range, Integer: f.Bounds.Integer, Shape: f.Bounds.Shape, Description: f.Description}
		}
	}
	return out, nil
}
