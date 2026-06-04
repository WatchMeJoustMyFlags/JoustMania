package gamecontext

import (
	"fmt"
	"sort"
	"sync"
	"time"
)

// Store accumulates GameContext state from streamed signals. It is safe for
// concurrent use; all access is guarded by a single mutex.
//
// Invariant: setters always REPLACE pointer fields with fresh pointers and
// never mutate through an existing pointer. This lets Snapshot() shallow-copy
// each PlayerSignals struct (and thus share the pointers) without risk of a
// later setter mutating data already handed out in a snapshot.
type Store struct {
	mu sync.Mutex

	ctx        GameContext
	sessionSeq int
	endedAt    time.Time // when GameActive went true->false; starts the grace window

	// deathCounts tracks the last game_player_deaths_total per serial, so the
	// fallback elimination source can detect increases.
	deathCounts map[string]float64
	// elimOrder is the preferred elimination source (serial -> order index).
	// When non-empty it suppresses the deaths_total fallback entirely.
	elimOrder map[string]int
	// disconnected marks serials whose connection dropped, for eviction.
	disconnected map[string]bool

	playerTTL    time.Duration
	sessionGrace time.Duration
	now          func() time.Time

	// ownService is this agent's own OTEL service name. Telemetry from this
	// service is skipped by the extractors: the collector fans the agent's own
	// spans back to it (otlp/agent exporter), and while those spans carry no
	// recognized signals anyway, the skip is cheap defense-in-depth against a
	// self-ingestion feedback loop. Set once before serving; not mutex-guarded.
	ownService string
}

// SetOwnService records the agent's own OTEL service name so the extractors
// can skip the agent's own telemetry. Must be called before the store starts
// receiving Apply* calls.
func (s *Store) SetOwnService(name string) {
	s.ownService = name
}

// NewStore constructs a Store. playerTTL bounds how long a silent player is
// retained; sessionGrace bounds how long an ended session lingers before its
// session-scoped state is reset. now is injectable for tests; nil uses time.Now.
func NewStore(playerTTL, sessionGrace time.Duration, now func() time.Time) *Store {
	if now == nil {
		now = time.Now
	}
	return &Store{
		ctx: GameContext{
			Players: make(map[string]*PlayerSignals),
		},
		deathCounts:  make(map[string]float64),
		elimOrder:    make(map[string]int),
		disconnected: make(map[string]bool),
		playerTTL:    playerTTL,
		sessionGrace: sessionGrace,
		now:          now,
	}
}

// player returns the PlayerSignals for serial, creating it if missing.
// Caller must hold s.mu.
func (s *Store) player(serial string) *PlayerSignals {
	p := s.ctx.Players[serial]
	if p == nil {
		p = &PlayerSignals{Serial: serial}
		s.ctx.Players[serial] = p
	}
	return p
}

// touch stamps a player's and the context's update time. Caller holds s.mu.
func (s *Store) touch(p *PlayerSignals) {
	t := s.now()
	p.LastUpdate = t
	s.ctx.UpdatedAt = t
}

// touchSession stamps the session's and context's update time. Caller holds s.mu.
func (s *Store) touchSession() {
	t := s.now()
	s.ctx.Session.LastUpdate = t
	s.ctx.UpdatedAt = t
}

func ptr[T any](v T) *T { return &v }

// SetPlayerIntensity records movement intensity, creating the player on demand.
func (s *Store) SetPlayerIntensity(serial string, v float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p := s.player(serial)
	p.MovementIntensity = ptr(v)
	s.touch(p)
}

// SetPlayerVariance records movement variance, creating the player on demand.
func (s *Store) SetPlayerVariance(serial string, v float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p := s.player(serial)
	p.MovementVariance = ptr(v)
	s.touch(p)
}

// SetPlayerBattery records battery percentage. A fallback-source update is
// ignored once a preferred-source value has been seen for this player.
func (s *Store) SetPlayerBattery(serial string, pct float64, preferred bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p := s.player(serial)
	if !preferred && p.batteryPreferred {
		return
	}
	if preferred {
		p.batteryPreferred = true
	}
	p.BatteryPct = ptr(pct)
	s.touch(p)
}

// SetPlayerSkill records skill level. A fallback-source update is ignored once
// a preferred-source value has been seen for this player.
func (s *Store) SetPlayerSkill(serial string, v float64, preferred bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p := s.player(serial)
	if !preferred && p.skillPreferred {
		return
	}
	if preferred {
		p.skillPreferred = true
	}
	p.SkillLevel = ptr(v)
	s.touch(p)
}

// SetPlayerAlive is the preferred Active source. alive=true creates the player;
// alive=false only updates an existing player so that post-game stale gauges
// cannot resurrect an already-evicted player.
func (s *Store) SetPlayerAlive(serial string, alive bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !alive {
		p := s.ctx.Players[serial]
		if p == nil {
			return
		}
		p.activePreferred = true
		p.Active = ptr(false)
		s.touch(p)
		return
	}
	p := s.player(serial)
	p.activePreferred = true
	p.Active = ptr(true)
	s.touch(p)
}

// SetPlayerConnected is the fallback Active source and is only honored when the
// preferred (alive) source has not been seen for this player. connected=true
// creates the player and clears any eviction mark; connected=false only updates
// an existing player and marks it for eviction.
func (s *Store) SetPlayerConnected(serial string, connected bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !connected {
		p := s.ctx.Players[serial]
		if p == nil {
			return
		}
		s.disconnected[serial] = true
		if !p.activePreferred {
			p.Active = ptr(false)
		}
		s.touch(p)
		return
	}
	p := s.player(serial)
	delete(s.disconnected, serial)
	if !p.activePreferred {
		p.Active = ptr(true)
	}
	s.touch(p)
}

// SetSessionDuration records elapsed game time.
func (s *Store) SetSessionDuration(v float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ctx.Session.DurationSeconds = ptr(v)
	s.touchSession()
}

// SetActivePlayerCount records the active player count.
func (s *Store) SetActivePlayerCount(n int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ctx.Session.ActivePlayerCount = ptr(n)
	s.touchSession()
}

// SetGameMode records the current game mode.
func (s *Store) SetGameMode(mode string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ctx.Session.GameMode = ptr(mode)
	s.touchSession()
}

// SetGameActive records game-active state and manages session lifecycle.
// A false->true transition starts a new session (bumps the sequence, assigns a
// synthetic SessionID, clears per-game state). A true->false transition opens
// the grace window by recording endedAt.
func (s *Store) SetGameActive(active bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	prev := false
	if s.ctx.Session.GameActive != nil {
		prev = *s.ctx.Session.GameActive
	}
	s.ctx.Session.GameActive = ptr(active)

	switch {
	case !prev && active:
		s.sessionSeq++
		s.ctx.SessionID = fmt.Sprintf("session-%d", s.sessionSeq)
		s.ctx.Session.EliminationSequence = nil
		s.ctx.Session.DurationSeconds = nil
		s.elimOrder = make(map[string]int)
		s.endedAt = time.Time{}
	case prev && !active:
		s.endedAt = s.now()
	}
	s.touchSession()
}

// AdoptSessionID overrides the synthetic SessionID with a real game_id label.
func (s *Store) AdoptSessionID(id string) {
	if id == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ctx.SessionID = id
	s.touchSession()
}

// RecordDeathTotal is the fallback elimination source from
// game_player_deaths_total{serial}. The first observation per serial is a
// baseline (no append). A subsequent increase appends the serial to the
// elimination sequence exactly once (respawn-mode repeat deaths are not tracked
// in this scaffold). It is ignored entirely while the preferred elimOrder source
// is populated.
func (s *Store) RecordDeathTotal(serial string, total float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.elimOrder) > 0 {
		return
	}
	prev, seen := s.deathCounts[serial]
	s.deathCounts[serial] = total
	if !seen {
		return // baseline
	}
	if total > prev {
		s.appendElimination(serial)
	}
}

// SetEliminationOrder is the preferred elimination source (future
// game_player_elimination_order{serial,game_id}). It records the order and
// rebuilds EliminationSequence sorted by order.
func (s *Store) SetEliminationOrder(serial string, order int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.elimOrder[serial] = order
	serials := make([]string, 0, len(s.elimOrder))
	for k := range s.elimOrder {
		serials = append(serials, k)
	}
	sort.Slice(serials, func(i, j int) bool {
		return s.elimOrder[serials[i]] < s.elimOrder[serials[j]]
	})
	s.ctx.Session.EliminationSequence = serials
	s.touchSession()
}

// RecordElimination is the span-event confirmation path; it appends the serial
// with the same dedupe as the deaths fallback.
func (s *Store) RecordElimination(serial string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.appendElimination(serial)
}

// appendElimination appends serial to the elimination sequence unless already
// present. Caller holds s.mu.
func (s *Store) appendElimination(serial string) {
	for _, existing := range s.ctx.Session.EliminationSequence {
		if existing == serial {
			return
		}
	}
	s.ctx.Session.EliminationSequence = append(s.ctx.Session.EliminationSequence, serial)
	s.touchSession()
}

// Snapshot returns a deep-enough copy of the context: a fresh Players map with
// copied structs and a copied EliminationSequence. Safe to read after later
// store mutations because setters never mutate through shared pointers.
func (s *Store) Snapshot() GameContext {
	s.mu.Lock()
	defer s.mu.Unlock()

	out := s.ctx
	out.Players = make(map[string]*PlayerSignals, len(s.ctx.Players))
	for serial, p := range s.ctx.Players {
		cp := *p
		out.Players[serial] = &cp
	}
	if s.ctx.Session.EliminationSequence != nil {
		seq := make([]string, len(s.ctx.Session.EliminationSequence))
		copy(seq, s.ctx.Session.EliminationSequence)
		out.Session.EliminationSequence = seq
	}
	return out
}

// EvictStale removes players that are disconnected-marked or silent past the TTL
// (also clearing their death baseline so a later re-observation cannot fake an
// elimination), and resets the session once its grace window has elapsed. Players
// persist across session reset so the lobby can continue between games.
func (s *Store) EvictStale() {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()

	for serial, p := range s.ctx.Players {
		if s.disconnected[serial] || now.Sub(p.LastUpdate) > s.playerTTL {
			delete(s.ctx.Players, serial)
			delete(s.disconnected, serial)
			delete(s.deathCounts, serial)
		}
	}

	if !s.endedAt.IsZero() && now.Sub(s.endedAt) > s.sessionGrace {
		s.ctx.Session = SessionSignals{GameActive: ptr(false)}
		s.ctx.SessionID = ""
		s.elimOrder = make(map[string]int)
		s.endedAt = time.Time{}
		s.ctx.UpdatedAt = now
	}
}
