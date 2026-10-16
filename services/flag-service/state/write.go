package state

import (
	"bytes"
	"encoding/json"
	"math"
	"time"

	"github.com/joustmania/flag-service/fserr"
	"github.com/joustmania/flag-service/registry"
)

// Scope is a write's scope properties (services.md §7.2).
type Scope struct {
	GameID          string `json:"game_id,omitempty"`
	Serial          string `json:"serial,omitempty"`
	ExperimentID    string `json:"experiment_id,omitempty"`
	Arm             string `json:"arm,omitempty"`
	GameKind        string `json:"game_kind,omitempty"`
	FractionPercent *int   `json:"fraction_percent,omitempty"`
}

// Name derives the registry scope a set of scope properties addresses, or ""
// when the combination fits no scope.
func (sc Scope) Name() string {
	hasGame, hasSerial := sc.GameID != "", sc.Serial != ""
	hasExp := sc.ExperimentID != "" || sc.Arm != ""
	hasKind, hasFrac := sc.GameKind != "", sc.FractionPercent != nil
	switch {
	case hasGame && hasSerial && !hasExp && !hasKind && !hasFrac:
		return registry.ScopeGameSerial
	case hasGame && !hasSerial && !hasExp && !hasKind && !hasFrac:
		return registry.ScopeGame
	case hasExp && !hasGame && !hasSerial && !hasKind && !hasFrac:
		if sc.ExperimentID == "" || sc.Arm == "" {
			return ""
		}
		return registry.ScopeExperiment
	case hasKind && !hasGame && !hasSerial && !hasExp && !hasFrac:
		if sc.GameKind != gameKindReal {
			return ""
		}
		return registry.ScopeReal
	case hasFrac && !hasGame && !hasSerial && !hasExp && !hasKind:
		return registry.ScopeFraction
	case !hasGame && !hasSerial && !hasExp && !hasKind && !hasFrac:
		return registry.ScopeStack
	}
	return ""
}

// WriteRequest is a PUT /{flagSetId}/{flagName}.
type WriteRequest struct {
	Set        string
	Flag       string
	Variant    string
	Value      json.RawMessage // nil when absent
	Scope      Scope
	TTLSeconds *float64
	Persist    bool
	Writer     string
	DecisionID string
}

// AppendRequest is a POST /{flagSetId}/{flagName} (an event).
type AppendRequest struct {
	Set        string
	Flag       string
	Scope      Scope
	Payload    map[string]any
	Writer     string
	DecisionID string
}

// ClearRequest is a DELETE /{flagSetId}/{flagName}. Persist clears the
// persisted admin value instead of the in-memory stack overlay.
type ClearRequest struct {
	Set     string
	Flag    string
	Scope   Scope
	Writer  string
	Persist bool
}

// WriteResult is the response of an accepted write, and the telemetry of a
// rejected one as far as it got (services.md §7.2 WriteResult; telemetry.md
// §10.1 flagservice.write attributes).
type WriteResult struct {
	FlagSet         string          `json:"flag_set"`
	Flag            string          `json:"flag"`
	Variant         string          `json:"variant,omitempty"`
	Value           json.RawMessage `json:"value,omitempty"`
	Seq             int64           `json:"seq,omitempty"`
	Epoch           string          `json:"epoch"`
	SnapshotVersion uint64          `json:"snapshot_version"`
	Persisted       bool            `json:"persisted"`
	// Telemetry only.
	Kind     string `json:"-"`
	ScopeKey string `json:"-"`
	GameKind string `json:"-"`
	Cleared  int    `json:"-"`
}

func checkIDs(sc Scope, writer string) *fserr.Error {
	for _, s := range []string{sc.GameID, sc.Serial, sc.ExperimentID, sc.Arm, sc.GameKind, writer} {
		if len(s) > MaxIDLength {
			return fserr.BadRequest("an id or label is longer than %d characters", MaxIDLength)
		}
	}
	return nil
}

// lookupLocked resolves (set, flag) and its policy: 404 for an unknown set or
// flag, 422 flag_read_only for a flag without a registry row.
func (s *State) lookupLocked(set, key string, res *WriteResult) (*flagState, *fserr.Error) {
	if s.base.Sets[set] == nil {
		return nil, fserr.NotFound(fserr.UnknownFlagSet, "no flag set %q", set)
	}
	f := s.flags[key]
	if f == nil || f.set != set {
		return nil, fserr.NotFound(fserr.UnknownFlag, "no flag %q in set %q", key, set)
	}
	if f.policy == nil {
		return nil, fserr.Unprocessable(fserr.FlagReadOnly, "%s/%s is read-only baseline (no registry row, flags.md §13)", set, key)
	}
	res.Kind = f.policy.Kind
	return f, nil
}

// liveChecksLocked rejects a write flagd could not see or that names a game
// that is not live (FR-123).
func (s *State) liveChecksLocked(f *flagState, sc Scope, scopeName string, res *WriteResult) *fserr.Error {
	if !s.served[f.set] {
		return fserr.Conflict(fserr.SetNotServed, "flag set %q is not served by the flag service yet (still a file source)", f.set)
	}
	if scopeName == registry.ScopeGame || scopeName == registry.ScopeGameSerial {
		g := s.games[sc.GameID]
		if g == nil {
			return fserr.Conflict(fserr.GameNotLive, "game %q is not registered or has ended", sc.GameID)
		}
		res.GameKind = g.Kind
		if scopeName == registry.ScopeGameSerial && !g.HasSerial(sc.Serial) {
			return fserr.Unprocessable(fserr.SerialNotInGame, "serial %q is not a player of game %q", sc.Serial, sc.GameID)
		}
	}
	return nil
}

func (s *State) streamCheckLocked(set string) *fserr.Error {
	if !s.hasStreamForLocked(set) {
		return fserr.Unavailable(fserr.NoSyncStream, "flagd has no open sync stream for set %q; the write would not reach readers", set)
	}
	return nil
}

// Put sets a value in a scope (PUT /{flagSetId}/{flagName}).
func (s *State) Put(req WriteRequest) (WriteResult, *fserr.Error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	res := WriteResult{FlagSet: req.Set, Flag: req.Flag, Epoch: s.opts.Epoch, ScopeKey: req.Scope.Name()}

	f, err := s.lookupLocked(req.Set, req.Flag, &res)
	if err != nil {
		return res, err
	}
	p := f.policy
	if p.Kind == registry.KindEvent {
		return res, fserr.BadRequest("%s is an event flag: append with POST", req.Flag)
	}
	if (req.Variant == "") == (req.Value == nil) {
		return res, fserr.BadRequest("exactly one of variant and value")
	}
	if err := checkIDs(req.Scope, req.Writer); err != nil {
		return res, err
	}
	scopeName := res.ScopeKey
	if scopeName == "" || !p.AllowsScope(scopeName) {
		return res, fserr.BadRequest("the scope does not fit %s (allowed: %v)", req.Flag, p.Scopes)
	}
	stackLike := scopeName == registry.ScopeStack || scopeName == registry.ScopeReal || scopeName == registry.ScopeFraction
	var ttl time.Duration
	if req.TTLSeconds != nil {
		if !stackLike || *req.TTLSeconds <= 0 || math.IsInf(*req.TTLSeconds, 0) || math.IsNaN(*req.TTLSeconds) {
			return res, fserr.BadRequest("ttl_seconds must be positive and applies to stack overlays only")
		}
		ttl = time.Duration(*req.TTLSeconds * float64(time.Second))
	}
	if req.Persist {
		switch {
		case !p.Persist || scopeName != registry.ScopeStack:
			return res, fserr.BadRequest("%s cannot persist in scope %s (registry persist: %v)", req.Flag, scopeName, p.Persist)
		case req.TTLSeconds != nil:
			return res, fserr.BadRequest("persist and ttl_seconds exclude each other")
		}
	}
	if err := s.liveChecksLocked(f, req.Scope, scopeName, &res); err != nil {
		return res, err
	}
	checked, err := registry.CheckValue(p, f.base, req.Variant, req.Value)
	if err != nil {
		return res, err
	}
	if scopeName == registry.ScopeFraction {
		pct := *req.Scope.FractionPercent
		if pct < p.Bounds.Fraction[0] || pct > p.Bounds.Fraction[1] {
			return res, fserr.Unprocessable(fserr.OutOfBounds, "fraction_percent %d outside %v", pct, p.Bounds.Fraction)
		}
	}
	if err := s.streamCheckLocked(f.set); err != nil {
		return res, err
	}

	now := s.opts.Now()
	v := &value{Writer: req.Writer, DecisionID: req.DecisionID, WrittenAt: now, Persisted: req.Persist}
	if ttl > 0 {
		v.ExpiresAt = now.Add(ttl)
	}
	if stackLike && checked.Variant != "" {
		v.Variant = checked.Variant // D13: point defaultVariant at the declared variant
	} else {
		prefix := "r"
		if stackLike {
			prefix = "o"
		}
		v.Variant = s.nextVariant(prefix)
		v.Value = checked.Value
	}
	res.Value = checked.Value

	switch scopeName {
	case registry.ScopeStack:
		if req.Persist {
			// The host's setting replaces any in-memory overlay, so it takes
			// effect now, and is kept across restarts (A2).
			f.admin, f.stack = v, nil
			s.saveAdminLocked()
		} else {
			f.stack = v
		}
	case registry.ScopeReal:
		f.real = v
	case registry.ScopeFraction:
		v.Fraction = *req.Scope.FractionPercent
		f.fraction = v
	case registry.ScopeGame:
		f.games[req.Scope.GameID] = v
	case registry.ScopeGameSerial:
		bySerial := f.serials[req.Scope.GameID]
		if bySerial == nil {
			bySerial = map[string]*value{}
			f.serials[req.Scope.GameID] = bySerial
		}
		bySerial[req.Scope.Serial] = v
	case registry.ScopeExperiment:
		byArm := f.experiments[req.Scope.ExperimentID]
		if byArm == nil {
			byArm = map[string]*value{}
			f.experiments[req.Scope.ExperimentID] = byArm
		}
		byArm[req.Scope.Arm] = v
	}
	s.changed()
	res.Variant = v.Variant
	res.SnapshotVersion = s.version
	res.Persisted = req.Persist && s.lastPersistOK
	return res, nil
}

// Append adds one event to a game's event list (POST /{flagSetId}/{flagName},
// FR-127, UC-053 BR-006). The list keeps the last K events.
func (s *State) Append(req AppendRequest) (WriteResult, *fserr.Error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	res := WriteResult{FlagSet: req.Set, Flag: req.Flag, Epoch: s.opts.Epoch, ScopeKey: req.Scope.Name()}

	f, err := s.lookupLocked(req.Set, req.Flag, &res)
	if err != nil {
		return res, err
	}
	if f.policy.Kind != registry.KindEvent {
		return res, fserr.BadRequest("%s is not an event flag: set it with PUT", req.Flag)
	}
	if err := checkIDs(req.Scope, req.Writer); err != nil {
		return res, err
	}
	if res.ScopeKey != registry.ScopeGame {
		return res, fserr.BadRequest("an event is appended in a game scope (scope.game_id only)")
	}
	if err := s.liveChecksLocked(f, req.Scope, res.ScopeKey, &res); err != nil {
		return res, err
	}
	payload := req.Payload
	if payload == nil {
		payload = map[string]any{}
	}
	game := s.games[req.Scope.GameID]
	if err := registry.PayloadCheck(f.policy, payload, game.HasSerial); err != nil {
		return res, err
	}
	if err := s.streamCheckLocked(f.set); err != nil {
		return res, err
	}

	list := f.events[req.Scope.GameID]
	if list == nil {
		list = &eventList{Events: []event{}}
		f.events[req.Scope.GameID] = list
	}
	list.Seq++
	list.Events = append(list.Events, event{Seq: list.Seq, Payload: payload})
	if k := s.opts.EventListLen; len(list.Events) > k {
		list.Events = append([]event(nil), list.Events[len(list.Events)-k:]...)
	}
	list.Variant = s.nextVariant("r")
	s.changed()
	raw, _ := json.Marshal(payload)
	res.Value = raw
	res.Variant = list.Variant
	res.Seq = list.Seq
	res.SnapshotVersion = s.version
	return res, nil
}

// Clear removes the value in a scope (DELETE /{flagSetId}/{flagName}). No
// scope clears the stack overlay; a persisted overlay also leaves the state
// file. Clearing a value that is not there is not an error.
func (s *State) Clear(req ClearRequest) (WriteResult, *fserr.Error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	res := WriteResult{FlagSet: req.Set, Flag: req.Flag, Epoch: s.opts.Epoch, ScopeKey: req.Scope.Name()}

	f, err := s.lookupLocked(req.Set, req.Flag, &res)
	if err != nil {
		return res, err
	}
	scopeName := res.ScopeKey
	if scopeName == registry.ScopeGameSerial && req.Scope.Serial == "" {
		scopeName = ""
	}
	if scopeName == "" || !f.policy.AllowsScope(scopeName) {
		return res, fserr.BadRequest("the scope does not fit %s (allowed: %v)", req.Flag, f.policy.Scopes)
	}
	if !s.served[f.set] {
		return res, fserr.Conflict(fserr.SetNotServed, "flag set %q is not served by the flag service yet", f.set)
	}
	if err := s.streamCheckLocked(f.set); err != nil {
		return res, err
	}
	if req.Persist && scopeName != registry.ScopeStack {
		return res, fserr.BadRequest("persist=true clears a stack-wide admin value only")
	}
	sc := req.Scope
	switch scopeName {
	case registry.ScopeStack:
		switch {
		case req.Persist && f.admin != nil:
			f.admin, res.Cleared = nil, 1
			res.Persisted = true
			s.saveAdminLocked()
		case !req.Persist && f.stack != nil:
			f.stack, res.Cleared = nil, 1
		}
	case registry.ScopeReal:
		if f.real != nil {
			f.real, res.Cleared = nil, 1
		}
	case registry.ScopeFraction:
		if f.fraction != nil {
			f.fraction, res.Cleared = nil, 1
		}
	case registry.ScopeGame:
		if f.policy.Kind == registry.KindEvent {
			if _, ok := f.events[sc.GameID]; ok {
				delete(f.events, sc.GameID)
				res.Cleared = 1
			}
		} else if _, ok := f.games[sc.GameID]; ok {
			delete(f.games, sc.GameID)
			res.Cleared = 1
		}
	case registry.ScopeGameSerial:
		if m := f.serials[sc.GameID]; m != nil {
			if _, ok := m[sc.Serial]; ok {
				delete(m, sc.Serial)
				res.Cleared = 1
				if len(m) == 0 {
					delete(f.serials, sc.GameID)
				}
			}
		}
	case registry.ScopeExperiment:
		if m := f.experiments[sc.ExperimentID]; m != nil {
			if _, ok := m[sc.Arm]; ok {
				delete(m, sc.Arm)
				res.Cleared = 1
				if len(m) == 0 {
					delete(f.experiments, sc.ExperimentID)
				}
			}
		}
	}
	if res.Cleared > 0 {
		s.changed()
	}
	res.SnapshotVersion = s.version
	return res, nil
}

// ClearWriter removes every stack-wide overlay (stack, real, fraction) that
// writer set in a flag set (DELETE /{flagSetId}?writer=, the talk reset).
// It returns how many overlays it removed.
func (s *State) ClearWriter(set, writer string) (int, *fserr.Error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.base.Sets[set] == nil {
		return 0, fserr.NotFound(fserr.UnknownFlagSet, "no flag set %q", set)
	}
	if writer == "" {
		return 0, fserr.BadRequest("writer is required")
	}
	if !s.served[set] {
		return 0, fserr.Conflict(fserr.SetNotServed, "flag set %q is not served by the flag service yet", set)
	}
	if err := s.streamCheckLocked(set); err != nil {
		return 0, err
	}
	n, persistedGone := 0, false
	for _, f := range s.flags {
		if f.set != set {
			continue
		}
		if f.stack != nil && f.stack.Writer == writer {
			f.stack = nil
			n++
		}
		if f.admin != nil && f.admin.Writer == writer {
			f.admin = nil
			persistedGone = true
			n++
		}
		if f.real != nil && f.real.Writer == writer {
			f.real = nil
			n++
		}
		if f.fraction != nil && f.fraction.Writer == writer {
			f.fraction = nil
			n++
		}
	}
	if persistedGone {
		s.saveAdminLocked()
	}
	if n > 0 {
		s.changed()
	}
	return n, nil
}

// ExpireOverlays drops stack-wide overlays whose TTL ran out and returns how
// many.
func (s *State) ExpireOverlays() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.opts.Now()
	n := 0
	expired := func(v *value) bool { return v != nil && !v.ExpiresAt.IsZero() && !now.Before(v.ExpiresAt) }
	for _, f := range s.flags {
		if expired(f.stack) {
			f.stack = nil
			n++
		}
		if expired(f.real) {
			f.real = nil
			n++
		}
		if expired(f.fraction) {
			f.fraction = nil
			n++
		}
	}
	if n > 0 {
		s.changed()
	}
	return n
}

// jsonValue renders a value for views: the declared variant's value or the
// generated one.
func (f *flagState) jsonValue(v *value) json.RawMessage {
	if v.Value != nil {
		return v.Value
	}
	return bytes.TrimSpace(f.base.Variants[v.Variant])
}
