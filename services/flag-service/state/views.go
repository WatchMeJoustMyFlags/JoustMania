package state

import (
	"encoding/json"
	"time"

	"github.com/joustmania/flag-service/fserr"
	"github.com/joustmania/flag-service/registry"
)

// ValueView is one runtime value as the read endpoints show it.
type ValueView struct {
	Variant    string          `json:"variant"`
	Value      json.RawMessage `json:"value"`
	Writer     string          `json:"writer,omitempty"`
	DecisionID string          `json:"decision_id,omitempty"`
	WrittenAt  time.Time       `json:"written_at"`
	ExpiresAt  *time.Time      `json:"expires_at,omitempty"`
	Fraction   int             `json:"fraction_percent,omitempty"`
}

// EventListView is one game's event list.
type EventListView struct {
	Variant string          `json:"variant"`
	Seq     int64           `json:"seq"`
	Events  json.RawMessage `json:"events"`
}

// FlagView is one flag's effective state (GET /{flagSetId}[/{flagName}]).
type FlagView struct {
	Flag             string                          `json:"flag"`
	Mutable          bool                            `json:"mutable"`
	Policy           *registry.Policy                `json:"policy,omitempty"`
	BaselineDefault  string                          `json:"baseline_default"`
	EffectiveDefault string                          `json:"effective_default"`
	Variants         []string                        `json:"variants"`
	VariantValues    map[string]json.RawMessage      `json:"variant_values"`
	StackOverlay     *ValueView                      `json:"stack_overlay,omitempty"`
	AdminValue       *ValueView                      `json:"admin_value,omitempty"`
	RealOverlay      *ValueView                      `json:"real_overlay,omitempty"`
	FractionOverlay  *ValueView                      `json:"fraction_overlay,omitempty"`
	Games            map[string]*ValueView           `json:"games,omitempty"`
	Serials          map[string]map[string]ValueView `json:"serials,omitempty"`
	Experiments      map[string]map[string]ValueView `json:"experiments,omitempty"`
	Events           map[string]*EventListView       `json:"events,omitempty"`
	Rendered         json.RawMessage                 `json:"rendered,omitempty"`
}

// SetView is one flag set (GET /{flagSetId}).
type SetView struct {
	FlagSet string     `json:"flag_set"`
	Served  bool       `json:"served"`
	Flags   []FlagView `json:"flags"`
}

// SetSummary is one row of GET /.
type SetSummary struct {
	FlagSet string `json:"flag_set"`
	Flags   int    `json:"flags"`
	Mutable int    `json:"mutable"`
	Served  bool   `json:"served"`
}

func (f *flagState) view(v *value) *ValueView {
	if v == nil {
		return nil
	}
	out := &ValueView{
		Variant: v.Variant, Value: f.jsonValue(v), Writer: v.Writer, DecisionID: v.DecisionID,
		WrittenAt: v.WrittenAt, Fraction: v.Fraction,
	}
	if !v.ExpiresAt.IsZero() {
		t := v.ExpiresAt
		out.ExpiresAt = &t
	}
	return out
}

func (f *flagState) flagView() FlagView {
	fv := FlagView{
		Flag:             f.key,
		Mutable:          f.policy != nil,
		Policy:           f.policy,
		BaselineDefault:  f.base.DefaultVariant,
		EffectiveDefault: f.effectiveDefault(),
		Variants:         f.base.VariantNames(),
		VariantValues:    f.base.Variants,
		StackOverlay:     f.view(f.stack),
		AdminValue:       f.view(f.admin),
		RealOverlay:      f.view(f.real),
		FractionOverlay:  f.view(f.fraction),
	}
	if len(f.games) > 0 {
		fv.Games = map[string]*ValueView{}
		for id, v := range f.games {
			fv.Games[id] = f.view(v)
		}
	}
	if len(f.serials) > 0 {
		fv.Serials = map[string]map[string]ValueView{}
		for id, m := range f.serials {
			fv.Serials[id] = map[string]ValueView{}
			for serial, v := range m {
				fv.Serials[id][serial] = *f.view(v)
			}
		}
	}
	if len(f.experiments) > 0 {
		fv.Experiments = map[string]map[string]ValueView{}
		for id, m := range f.experiments {
			fv.Experiments[id] = map[string]ValueView{}
			for arm, v := range m {
				fv.Experiments[id][arm] = *f.view(v)
			}
		}
	}
	if len(f.events) > 0 {
		fv.Events = map[string]*EventListView{}
		for id, l := range f.events {
			raw, _ := json.Marshal(l.Events)
			fv.Events[id] = &EventListView{Variant: l.Variant, Seq: l.Seq, Events: raw}
		}
	}
	return fv
}

// Sets summarises every flag set (GET /).
func (s *State) Sets() []SetSummary {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]SetSummary, 0, len(s.base.Order))
	for _, id := range s.base.Order {
		set := s.base.Sets[id]
		sum := SetSummary{FlagSet: id, Flags: len(set.Flags), Served: s.served[id]}
		for key := range set.Flags {
			if s.flags[key].policy != nil {
				sum.Mutable++
			}
		}
		out = append(out, sum)
	}
	return out
}

// SetView describes every flag of a set (GET /{flagSetId}).
func (s *State) SetView(set string) (*SetView, *fserr.Error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	bs := s.base.Sets[set]
	if bs == nil {
		return nil, fserr.NotFound(fserr.UnknownFlagSet, "no flag set %q", set)
	}
	out := &SetView{FlagSet: set, Served: s.served[set], Flags: []FlagView{}}
	for _, key := range bs.FlagKeys() {
		out.Flags = append(out.Flags, s.flags[key].flagView())
	}
	return out, nil
}

// FlagView describes one flag, with its rendered flagd JSON (GET
// /{flagSetId}/{flagName}).
func (s *State) FlagView(set, key string) (*FlagView, *fserr.Error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.base.Sets[set] == nil {
		return nil, fserr.NotFound(fserr.UnknownFlagSet, "no flag set %q", set)
	}
	f := s.flags[key]
	if f == nil || f.set != set {
		return nil, fserr.NotFound(fserr.UnknownFlag, "no flag %q in set %q", key, set)
	}
	fv := f.flagView()
	rf, err := f.render(s.opts.Epoch)
	if err != nil {
		return nil, fserr.As(err)
	}
	fv.Rendered, _ = json.Marshal(rf)
	return &fv, nil
}
