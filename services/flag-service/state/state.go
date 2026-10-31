// Package state holds the flag service's runtime flag state in memory (C-030):
// the baseline per flag, stack overlays (with persisted admin settings), game
// scopes and their values, experiment arms and per-game event lists. It renders
// the merged flagd configuration (architecture note §10) and notifies the sync
// streams on every change.
//
// All mutation goes through State's methods under one mutex; rendering is
// cached per snapshot version.
package state

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"sort"
	"sync"
	"time"

	"github.com/joustmania/flag-service/baseline"
	"github.com/joustmania/flag-service/registry"
)

// Defaults (services.md §7, "Config").
const (
	DefaultEventListLen = 16
	DefaultMaxGameAge   = time.Hour
	// DefaultExperimentIdle drops an experiment's arm values once no registered
	// game has carried its experiment id, and nothing wrote them, for this long
	// (NFR-051): values a game can no longer read do not pile up.
	DefaultExperimentIdle = 2 * time.Hour
	// DefaultMaxGames caps live game scopes (NFR-051): the coordinators run at
	// most max_concurrent_games (8) real and shadow games, so 64 leaves room for
	// several coordinators while bounding what an unauthenticated caller can add.
	DefaultMaxGames = 64
	// MaxSerialsPerGame bounds a game scope (NFR-051: at most 64 KiB per game).
	MaxSerialsPerGame = 64
	// MaxIDLength bounds game ids, serials, experiment ids, arms and writer labels.
	MaxIDLength = 80
)

// Options configures a State.
type Options struct {
	// Epoch is the boot id; it is part of every generated variant name and
	// every event list (FR-137, review Important 2).
	Epoch string
	// Served is the set of flag-set ids included in the sync payload
	// (FLAG_SERVICE_SETS). Writes to other sets are 409 set_not_served.
	Served []string
	// EventListLen is K, the number of events an event list keeps.
	EventListLen int
	// MaxGameAge expires a game scope not seen (registered or reconciled) for
	// this long, i.e. whose end was never reported (UC-053 A11).
	MaxGameAge time.Duration
	// MaxGames caps the number of live game scopes (NFR-051).
	MaxGames int
	// ExperimentIdle expires experiment-arm values no registered game references.
	ExperimentIdle time.Duration
	// StateFile is the admin state file; "" disables persistence.
	StateFile string
	// Now is the clock (tests inject one).
	Now func() time.Time
	// Logger receives warnings (dropped state entries, persist failures).
	Logger *slog.Logger
}

// Game is one registered game scope (entity GAME_SCOPE).
type Game struct {
	ID                  string    `json:"game_id"`
	CoordinatorInstance string    `json:"coordinator_instance"`
	Kind                string    `json:"game_kind"`
	Serials             []string  `json:"serials"`
	ExperimentID        string    `json:"experiment_id,omitempty"`
	Arm                 string    `json:"arm,omitempty"`
	RegisteredAt        time.Time `json:"registered_at"`
	// LastSeenAt is refreshed by every registration and every reconcile entry;
	// game expiry counts from it (UC-053 A11, #2217).
	LastSeenAt time.Time `json:"last_seen_at"`
}

// HasSerial reports whether serial is a player of the game.
func (g *Game) HasSerial(serial string) bool {
	for _, s := range g.Serials {
		if s == serial {
			return true
		}
	}
	return false
}

// value is one runtime value (entity FLAG_OVERLAY).
type value struct {
	// Variant is the variant name the value renders as: a declared variant of
	// the baseline (stack overlays only) or a generated opaque name.
	Variant string
	// Value is set for a generated variant; nil when Variant is declared.
	Value      json.RawMessage
	Writer     string
	DecisionID string
	WrittenAt  time.Time
	ExpiresAt  time.Time // zero: no TTL
	Persisted  bool      // an admin value (the persisted layer)
	Fraction   int       // fraction overlays: percent of serials
}

func (v *value) generated() bool { return v.Value != nil }

type event struct {
	Seq     int64          `json:"seq"`
	Payload map[string]any `json:"payload"`
}

type eventList struct {
	Variant string
	Seq     int64
	Events  []event
}

// flagState is the runtime state of one baseline flag.
type flagState struct {
	set    string
	key    string
	base   *baseline.Flag
	policy *registry.Policy // nil: read-only baseline
	// stack is the in-memory stack overlay (stage scripts, tests, operator);
	// admin is the persisted admin setting beneath it (D10, §10.1). A stage
	// overlay therefore never erases the host's setting: clearing it shows the
	// admin value again.
	stack    *value
	admin    *value
	real     *value
	fraction *value
	// games: game-scoped values, by game id.
	games map[string]*value
	// serials: game+serial values, by game id then serial.
	serials map[string]map[string]*value
	// experiments: experiment+arm values, by experiment id then arm.
	experiments map[string]map[string]*value
	// events: per-game event lists, by game id.
	events map[string]*eventList
}

func newFlagState(set, key string, base *baseline.Flag, policy *registry.Policy) *flagState {
	return &flagState{
		set: set, key: key, base: base, policy: policy,
		games:       map[string]*value{},
		serials:     map[string]map[string]*value{},
		experiments: map[string]map[string]*value{},
		events:      map[string]*eventList{},
	}
}

// scopeValueCount counts the game-scoped and experiment values the flag holds.
func (f *flagState) scopeValueCount() int {
	n := len(f.games) + len(f.events)
	for _, m := range f.serials {
		n += len(m)
	}
	for _, m := range f.experiments {
		n += len(m)
	}
	return n
}

// dropGame removes every value written for gameID and returns how many.
func (f *flagState) dropGame(gameID string) int {
	n := 0
	if _, ok := f.games[gameID]; ok {
		delete(f.games, gameID)
		n++
	}
	if m, ok := f.serials[gameID]; ok {
		n += len(m)
		delete(f.serials, gameID)
	}
	if _, ok := f.events[gameID]; ok {
		delete(f.events, gameID)
		n++
	}
	return n
}

// State is the flag service's runtime state.
type State struct {
	mu     sync.Mutex
	opts   Options
	base   *baseline.Baseline
	reg    *registry.Registry
	served map[string]bool
	flags  map[string]*flagState // by flag key (keys are unique across sets)
	games  map[string]*Game
	// expSeen: when a registered game last carried each experiment id.
	expSeen map[string]time.Time

	counter uint64 // generated-variant counter
	version uint64 // snapshot version

	cacheVersion uint64
	cache        map[string][]byte // selector set ("" = all) -> payload

	subs map[*Subscription]struct{}

	persistErrors int64
	lastPersistOK bool
	// persistBlocked: the state file exists but could not be read (not a parse
	// error); writing it would destroy it, so persistence stays off.
	persistBlocked bool
}

// LoadReport says what New restored from the admin state file.
type LoadReport struct {
	AdminValues int
	Dropped     []string
	Corrupt     bool
	CorruptPath string
	// FutureVersion: the file was written by a newer flag service; it is kept
	// untouched and admin writes stay in memory (UC-053 A12).
	FutureVersion bool
}

// New builds the state from the baseline and the registry, and restores the
// persisted admin settings from opts.StateFile (FR-130, C-030).
func New(b *baseline.Baseline, r *registry.Registry, opts Options) (*State, LoadReport, error) {
	if opts.Epoch == "" {
		return nil, LoadReport{}, fmt.Errorf("state: empty epoch")
	}
	if opts.EventListLen <= 0 {
		opts.EventListLen = DefaultEventListLen
	}
	if opts.MaxGames <= 0 {
		opts.MaxGames = DefaultMaxGames
	}
	if opts.MaxGameAge <= 0 {
		opts.MaxGameAge = DefaultMaxGameAge
	}
	if opts.ExperimentIdle <= 0 {
		opts.ExperimentIdle = DefaultExperimentIdle
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.Logger == nil {
		opts.Logger = slog.Default()
	}
	s := &State{
		opts:    opts,
		base:    b,
		reg:     r,
		served:  map[string]bool{},
		flags:   map[string]*flagState{},
		games:   map[string]*Game{},
		expSeen: map[string]time.Time{},
		cache:   map[string][]byte{},
		subs:    map[*Subscription]struct{}{},
	}
	for _, set := range opts.Served {
		if b.Sets[set] == nil {
			return nil, LoadReport{}, fmt.Errorf("state: served set %q has no baseline file", set)
		}
		s.served[set] = true
	}
	// The coordinator gates every intervention on the allow-list's snapshot
	// stamp (UC-053 A9, #1552): an interventions set served without the agent
	// set would be deferred forever, so refuse that configuration at start.
	if s.served["interventions"] && !s.served["agent"] {
		return nil, LoadReport{}, fmt.Errorf("state: served set %q needs %q (its interventions_allowed allow-list)", "interventions", "agent")
	}
	for _, setID := range b.Order {
		set := b.Sets[setID]
		for key, f := range set.Flags {
			s.flags[key] = newFlagState(setID, key, f, r.Get(setID, key))
		}
	}
	report := s.loadAdmin()
	s.version = 1
	return s, report, nil
}

// Epoch returns the boot id.
func (s *State) Epoch() string { return s.opts.Epoch }

// Served reports whether set is in the sync payload.
func (s *State) Served(set string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.served[set]
}

// ServedSets returns the served set ids, sorted.
func (s *State) ServedSets() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.servedSetsLocked()
}

func (s *State) servedSetsLocked() []string {
	out := make([]string, 0, len(s.served))
	for set := range s.served {
		out = append(out, set)
	}
	sort.Strings(out)
	return out
}

// Version returns the current snapshot version.
func (s *State) Version() uint64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.version
}

// nextVariant returns a fresh opaque variant name (r<epoch>_<n>, or o<epoch>_<n>
// for stack overlays) that is never reused within or across epochs.
func (s *State) nextVariant(prefix string) string {
	s.counter++
	return fmt.Sprintf("%s%s_%d", prefix, s.opts.Epoch, s.counter)
}

// changed bumps the snapshot version and wakes every subscription. Callers
// hold s.mu.
func (s *State) changed() {
	s.version++
	for sub := range s.subs {
		sub.wake()
	}
}

// Stats is the state's size, for GET /state and the gauges (telemetry.md §10.2).
type Stats struct {
	Epoch           string         `json:"epoch"`
	SnapshotVersion uint64         `json:"snapshot_version"`
	ServedSets      []string       `json:"served_sets"`
	SyncStreams     int            `json:"sync_streams"`
	LiveGames       int            `json:"live_games"`
	LiveGamesByKind map[string]int `json:"live_games_by_kind"`
	Overlays        int            `json:"overlays"`
	PersistedValues int            `json:"persisted_overlays"`
	ScopeValues     int            `json:"scope_values"`
	// OverlaysBySet and ScopeValuesBySet feed the per-set gauges.
	OverlaysBySet          map[string]int `json:"-"`
	PersistedOverlaysBySet map[string]int `json:"-"`
	ScopeValuesBySet       map[string]int `json:"-"`
	SnapshotBytes          int            `json:"snapshot_bytes"`
	PersistErrors          int64          `json:"admin_persist_errors"`
}

// Stats returns the current sizes.
func (s *State) Stats() Stats {
	s.mu.Lock()
	defer s.mu.Unlock()
	st := Stats{
		Epoch:                  s.opts.Epoch,
		SnapshotVersion:        s.version,
		ServedSets:             s.servedSetsLocked(),
		SyncStreams:            len(s.subs),
		LiveGames:              len(s.games),
		LiveGamesByKind:        map[string]int{},
		OverlaysBySet:          map[string]int{},
		PersistedOverlaysBySet: map[string]int{},
		ScopeValuesBySet:       map[string]int{},
		PersistErrors:          s.persistErrors,
	}
	for _, g := range s.games {
		st.LiveGamesByKind[g.Kind]++
	}
	for _, f := range s.flags {
		for _, v := range []*value{f.stack, f.admin, f.real, f.fraction} {
			if v == nil {
				continue
			}
			st.Overlays++
			if v.Persisted {
				st.PersistedValues++
				st.PersistedOverlaysBySet[f.set]++
			} else {
				st.OverlaysBySet[f.set]++
			}
		}
		n := f.scopeValueCount()
		st.ScopeValues += n
		st.ScopeValuesBySet[f.set] += n
	}
	if payload, err := s.renderLocked(""); err == nil {
		st.SnapshotBytes = len(payload)
	}
	return st
}
