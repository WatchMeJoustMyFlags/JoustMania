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

	// history is the bounded per-partition rolling narrative (#916): a fixed-
	// capacity ring buffer of timestamped state deltas, eliminations, and phase
	// transitions. It is guarded by s.mu (no second lock) and lives ON the Store,
	// so it is automatically per-partition (#845) and evicted with the partition.
	// Reset on session-grace exit so a finished game's narrative never bleeds into
	// the next session on the same partition.
	history timeline

	// playerHistory is the bounded per-player movement TIME-SERIES (#1082): one
	// fixed-capacity ring per serial of bucketed (t, intensity[, variance]) samples,
	// guarded by s.mu like the #916 narrative. It lives ON the Store so it is
	// per-partition and is reset/evicted alongside the rest of the session-scoped
	// state. A player's ring is dropped when that player is TTL-evicted.
	playerHistory map[string]*playerHistory

	// speedTrack is the bounded session game-speed / death-threshold track (#1082):
	// per-tick (t, tempo, death_threshold) samples, the reference frame the per-
	// player proximity-to-death reads against. Session-scoped like the narrative,
	// reset on a new session and on grace exit.
	speedTrack speedTrack

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
	// playerTTLSource / sessionGraceSource, when non-nil, are the LIVE eviction-TTL
	// sources (#927): EvictStale reads them AT EVICTION TIME instead of the fixed
	// playerTTL/sessionGrace baked at construction, so a config-change to
	// lifecycle.player_ttl_seconds / lifecycle.session_grace_seconds takes effect on
	// the next eviction tick with no restart. They point at the shared
	// flags.LifecycleHolder (wired in main.go via SetTTLSources), so each read is a
	// lock-free atomic load — NOT a per-tick flagd evaluation. Nil (the construction
	// default, and all existing tests) keeps the fixed durations, preserving the
	// #766 F5 read-at-startup behavior. Eviction SEMANTICS are unchanged; only the
	// SOURCE of the two durations moves.
	playerTTLSource    func() time.Duration
	sessionGraceSource func() time.Duration
	now                func() time.Time

	// ownService is this agent's own OTEL service name. Telemetry from this
	// service is skipped by the extractors: the collector fans the agent's own
	// spans back to it (otlp/agent exporter), and while those spans carry no
	// recognized signals anyway, the skip is cheap defense-in-depth against a
	// self-ingestion feedback loop. Set once before serving; not mutex-guarded.
	ownService string

	// OnGameEnd, when non-nil, is fired EXACTLY ONCE on the GameActive true->false
	// transition (#844 post-game retrospective), with a pre-reset GameContext
	// snapshot — the EliminationSequence and SessionID are still intact. Set once
	// before serving; nil disables the hook (behavior-neutral). It is invoked
	// AFTER the store mutex is released (never under s.mu) so the callback may read
	// the store / build telemetry without a re-entrancy deadlock.
	//
	// NOTE (#845): this is the minimal additive hook the retrospective needs.
	// Issue #845 rewrites this file's lifecycle handling; keep this diff tiny.
	OnGameEnd func(GameContext)
}

// SetOwnService records the agent's own OTEL service name so the extractors
// can skip the agent's own telemetry. Must be called before the store starts
// receiving Apply* calls.
func (s *Store) SetOwnService(name string) {
	s.ownService = name
}

// SetTTLSources installs the LIVE eviction-TTL sources (#927): EvictStale reads
// playerTTL from playerSrc and sessionGrace from graceSrc at eviction time instead
// of the durations baked at construction, so a config-change to
// lifecycle.player_ttl_seconds / lifecycle.session_grace_seconds takes effect on
// the next eviction tick with no restart. main.go passes the shared
// flags.LifecycleHolder's PlayerTTL / SessionGrace (lock-free atomic loads), so the
// eviction path never evaluates flagd. A nil source falls back to that field's
// fixed construction value, so this is purely additive over the #766 F5 behavior.
// Set before serving; not safe to call concurrently with EvictStale.
func (s *Store) SetTTLSources(playerSrc, graceSrc func() time.Duration) {
	s.playerTTLSource = playerSrc
	s.sessionGraceSource = graceSrc
}

// effectivePlayerTTL / effectiveSessionGrace return the LIVE TTL when a source is
// wired and reports a positive duration, else the construction-time fixed value.
// A non-positive live value is ignored (treated as "unset") so a transient bad
// flag read can never collapse the TTL to zero and evict everything. Caller holds
// s.mu (EvictStale does).
func (s *Store) effectivePlayerTTL() time.Duration {
	if s.playerTTLSource != nil {
		if d := s.playerTTLSource(); d > 0 {
			return d
		}
	}
	return s.playerTTL
}

func (s *Store) effectiveSessionGrace() time.Duration {
	if s.sessionGraceSource != nil {
		if d := s.sessionGraceSource(); d > 0 {
			return d
		}
	}
	return s.sessionGrace
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
		history:       newTimeline(),
		playerHistory: make(map[string]*playerHistory),
		deathCounts:   make(map[string]float64),
		elimOrder:     make(map[string]int),
		disconnected:  make(map[string]bool),
		playerTTL:     playerTTL,
		sessionGrace:  sessionGrace,
		now:           now,
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

// recordStateDelta appends a periodic state-delta entry to the rolling narrative
// (#916), but ONLY when a tracked session aggregate (active player count or mean
// movement intensity) CHANGED versus the last recorded delta. Dedupe is the
// linchpin of the bound: live signals arrive at up to 60Hz, so appending one per
// signal would churn the 64-entry ring in a second and bury eliminations/phase
// changes. Recording only on change keeps the narrative a TREND log. Caller holds
// s.mu.
func (s *Store) recordStateDelta() {
	active := s.ctx.Session.ActivePlayerCount
	mean := s.meanIntensityLocked()

	if intPtrEqual(active, s.history.lastActivePlayers) && floatPtrEqual(mean, s.history.lastMeanIntensity) {
		return // nothing the narrative tracks changed; don't churn the ring
	}
	s.history.lastActivePlayers = active
	s.history.lastMeanIntensity = mean
	s.history.append(TimelineEvent{
		At:            s.now(),
		Kind:          EventStateDelta,
		ActivePlayers: active,
		MeanIntensity: mean,
	})
}

// meanIntensityLocked returns the mean of all observed player MovementIntensity
// values as the session's aggregate "energy", or nil when no player has reported
// intensity yet. It is the one fitness-style aggregate the Store can compute from
// what it already holds; richer fitness evaluations (skill gap, variance) are the
// decision engine's, and are layered in via AppendInterventionEvent's sibling
// seam if/when needed (#916). Caller holds s.mu.
func (s *Store) meanIntensityLocked() *float64 {
	var sum float64
	var n int
	for _, p := range s.ctx.Players {
		if p.MovementIntensity != nil {
			sum += *p.MovementIntensity
			n++
		}
	}
	if n == 0 {
		return nil
	}
	return ptr(sum / float64(n))
}

// intPtrEqual / floatPtrEqual compare two pointers by VALUE, treating two nils as
// equal and a nil-vs-set pair as unequal — the change test recordStateDelta needs.
func intPtrEqual(a, b *int) bool {
	if a == nil || b == nil {
		return a == b
	}
	return *a == *b
}

func floatPtrEqual(a, b *float64) bool {
	if a == nil || b == nil {
		return a == b
	}
	return *a == *b
}

// SetPlayerIntensity records movement intensity, creating the player on demand.
func (s *Store) SetPlayerIntensity(serial string, v float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p := s.player(serial)
	p.MovementIntensity = ptr(v)
	s.touch(p)
	// Append to the per-player movement TIME-SERIES (#1082), bucket-deduped to ~1Hz
	// so the 60Hz firehose never churns the ring. Carry the player's last-observed
	// variance alongside so the sample is self-describing.
	s.recordPlayerSample(serial, v, p.MovementVariance)
	// A movement-intensity change can move the session mean — record a state delta
	// (deduped) so the narrative captures the energy trend (#916).
	s.recordStateDelta()
}

// recordPlayerSample appends a bucketed (t, intensity, variance) sample to the
// player's movement ring (#1082), creating the ring on demand. Caller holds s.mu.
func (s *Store) recordPlayerSample(serial string, intensity float64, variance *float64) {
	h := s.playerHistory[serial]
	if h == nil {
		h = &playerHistory{}
		s.playerHistory[serial] = h
	}
	var vcopy *float64
	if variance != nil {
		v := *variance
		vcopy = &v
	}
	h.record(MovementSample{At: s.now(), Intensity: intensity, Variance: vcopy})
}

// SetPlayerVariance records movement variance, creating the player on demand.
func (s *Store) SetPlayerVariance(serial string, v float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p := s.player(serial)
	p.MovementVariance = ptr(v)
	s.touch(p)
}

// SetPlayerPeakAccel records the player's whole-game peak accel magnitude (a
// monotonic whole-game aggregate), creating the player on demand. Retained into
// the conclusion snapshot like SkillLevel so balanced-fitness spike-survival is
// meaningful post-game (#1024).
func (s *Store) SetPlayerPeakAccel(serial string, v float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p := s.player(serial)
	p.PeakAccel = ptr(v)
	s.touch(p)
}

// SetPlayerVarianceAggregate records the player's whole-game cumulative movement
// variance (a retained whole-game aggregate), creating the player on demand.
// Distinct from SetPlayerVariance, which records the frozen-at-conclusion
// rolling-window value (#1024).
func (s *Store) SetPlayerVarianceAggregate(serial string, v float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p := s.player(serial)
	p.MovementVarianceAggregate = ptr(v)
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
	s.recordStateDelta() // active-count change is a first-class narrative trend (#916)
}

// SetGameMode records the current game mode.
func (s *Store) SetGameMode(mode string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ctx.Session.GameMode = ptr(mode)
	s.touchSession()
}

// SetMusicTempo records the current applied game speed / music tempo (#1082) and
// appends it to the session speed track (the reference frame proximity-to-death
// reads against). Bucket-deduped to ~1Hz like the per-player samples.
func (s *Store) SetMusicTempo(v float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ctx.Session.MusicTempo = ptr(v)
	s.touchSession()
	s.speedTrack.record(SpeedSample{At: s.now(), Tempo: ptr(v)})
}

// SetDeathThreshold records the current effective (tempo-interpolated) death
// threshold (#1082) and appends it to the session speed track so proximity-to-
// death can read the threshold IN FORCE at each movement sample's time.
func (s *Store) SetDeathThreshold(v float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ctx.Session.DeathThreshold = ptr(v)
	s.touchSession()
	s.speedTrack.record(SpeedSample{At: s.now(), DeathThreshold: ptr(v)})
}

// SetWarningThreshold records the current effective warning threshold (#1082).
// It is a session signal only (no track sample) — the proximity track keys off the
// death threshold; the warning threshold is rendered as context in the header.
func (s *Store) SetWarningThreshold(v float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ctx.Session.WarningThreshold = ptr(v)
	s.touchSession()
}

// SetGameActive records game-active state and manages session lifecycle.
// A false->true transition starts a new session (bumps the sequence, assigns a
// synthetic SessionID, clears per-game state). A true->false transition opens
// the grace window by recording endedAt.
func (s *Store) SetGameActive(active bool) {
	// endSnapshot is captured under the lock on a true->false transition and the
	// OnGameEnd callback is fired with it AFTER the mutex is released (never under
	// s.mu — the callback must be free to read the store without re-entrancy).
	var endSnapshot *GameContext

	s.mu.Lock()
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
		// New session: clear the prior game's narrative and open the new one with a
		// "start" phase event (#916). Reset here (not only on grace exit) so a
		// back-to-back restart cannot carry the previous game's trend.
		s.history.reset()
		// Drop the prior game's per-player movement series + speed track so a back-
		// to-back restart cannot carry the previous game's trend (#1082), mirroring
		// the #916 narrative reset.
		s.playerHistory = make(map[string]*playerHistory)
		s.speedTrack.reset()
		s.history.append(TimelineEvent{At: s.now(), Kind: EventPhase, Detail: "start"})
	case prev && !active:
		s.endedAt = s.now()
		// Game end is the final phase transition; record it BEFORE the snapshot so
		// the OnGameEnd retro (#844) sees a narrative ending in "end" (#916).
		s.history.append(TimelineEvent{At: s.now(), Kind: EventPhase, Detail: "end"})
		if s.OnGameEnd != nil {
			snap := s.snapshotLocked() // pre-reset state: SessionID + sequence intact
			endSnapshot = &snap
		}
	}
	s.touchSession()
	s.mu.Unlock()

	if endSnapshot != nil {
		s.OnGameEnd(*endSnapshot)
	}
}

// SetGameKind records the game kind ("real"/"shadow") from the game_kind
// datapoint label or the game.kind span attribute (#845). An empty kind is
// ignored so an unlabeled signal never clobbers a previously observed kind.
func (s *Store) SetGameKind(kind string) {
	if kind == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ctx.GameKind = kind
	s.touchSession()
}

// SetExperimentID records the experiment this game belongs to (#975) from the
// experiment_id datapoint label or the experiment.id span attribute. An empty
// id is ignored so an unlabeled signal never clobbers a previously observed
// experiment id — exactly like SetGameKind.
func (s *Store) SetExperimentID(id string) {
	if id == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ctx.ExperimentID = id
	s.touchSession()
}

// SetArm records the experiment arm ("experimental"/"control", #975) from the
// arm datapoint label or the experiment.arm span attribute. An empty arm is
// ignored so an unlabeled signal never clobbers a previously observed arm.
func (s *Store) SetArm(arm string) {
	if arm == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ctx.Arm = arm
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
	_, inOrderMap := s.elimOrder[serial]
	s.elimOrder[serial] = order
	// Narrate each serial's elimination exactly ONCE — but across BOTH dedupe
	// stores. This preferred source dedupes on the elimOrder map (inOrderMap); the
	// death/span fallback path (appendElimination) narrates through
	// EliminationSequence membership. The two never cross-check, so without the
	// sequence guard below a serial the death/span path already narrated would get
	// a SECOND elimination line here (#916). Latent today — the
	// game_player_elimination_order producer is unshipped, so only the death/span
	// path fires — but armed the moment that metric ships alongside the death/span
	// signals, so we close it now. The reverse order is already safe: once this
	// method rebuilds EliminationSequence below, a later appendElimination finds the
	// serial present and returns early. Record with the reported 1-based order so
	// the narrative matches the rebuilt sequence.
	alreadyNarrated := inOrderMap
	if !alreadyNarrated {
		for _, existing := range s.ctx.Session.EliminationSequence {
			if existing == serial {
				alreadyNarrated = true // the death/span path already narrated it
				break
			}
		}
	}
	if !alreadyNarrated {
		s.history.append(TimelineEvent{
			At:     s.now(),
			Kind:   EventElimination,
			Serial: serial,
			Order:  order,
		})
	}
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

// AppendInterventionEvent records a dispatched OR blocked intervention in the
// rolling narrative (#916). It is the CLEAN SEAM the decision Loop can call after
// it decides an intervention's outcome (runDecision knows dispatched vs blocked),
// since the Loop and the Store are separate objects: the receiver pipeline holds
// both (mux.Snapshot's Store and loops.For's Loop), so a follow-up can thread the
// per-game Store's AppendInterventionEvent into the action path without a second
// global structure.
//
// DEFERRED (#916): the first cut wires only what the Store itself observes (state
// deltas, eliminations, phase). Intervention-event capture needs the receiver to
// hand the partition's Store to the Loop (or the action sink) so this method is
// reached on each decision; that plumbing is left for a follow-up so this PR stays
// a tight, well-tested first cut. The seam is intentionally present and unit-tested
// so the follow-up is a wiring change, not a redesign.
func (s *Store) AppendInterventionEvent(intervention, serial string, blocked bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.history.append(TimelineEvent{
		At:      s.now(),
		Kind:    EventIntervention,
		Detail:  intervention,
		Serial:  serial,
		Blocked: blocked,
	})
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
	// Record the elimination in the rolling narrative with its 1-based order (#916),
	// so the timeline carries WHEN each player dropped out, not just the final order.
	s.history.append(TimelineEvent{
		At:     s.now(),
		Kind:   EventElimination,
		Serial: serial,
		Order:  len(s.ctx.Session.EliminationSequence),
	})
}

// Snapshot returns a deep-enough copy of the context: a fresh Players map with
// copied structs and a copied EliminationSequence. Safe to read after later
// store mutations because setters never mutate through shared pointers.
func (s *Store) Snapshot() GameContext {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.snapshotLocked()
}

// snapshotLocked is the body of Snapshot; the caller must hold s.mu. It is shared
// by Snapshot() and by SetGameActive's game-end hook so both produce an identical
// deep-enough copy without re-acquiring the mutex.
func (s *Store) snapshotLocked() GameContext {
	out := s.ctx
	out.Players = make(map[string]*PlayerSignals, len(s.ctx.Players))
	for serial, p := range s.ctx.Players {
		cp := *p
		// Attach a fresh, oldest-first copy of this player's movement series (#1082):
		// samples() allocates a new slice sharing no backing array with the ring, so a
		// later record never mutates a handed-out snapshot — the same shared-nothing
		// guarantee Players / Timeline give.
		if h := s.playerHistory[serial]; h != nil {
			cp.History = h.samples()
		}
		out.Players[serial] = &cp
	}
	if s.ctx.Session.EliminationSequence != nil {
		seq := make([]string, len(s.ctx.Session.EliminationSequence))
		copy(seq, s.ctx.Session.EliminationSequence)
		out.Session.EliminationSequence = seq
	}
	// Carry a fresh, oldest-first copy of the rolling narrative on the snapshot
	// (#916). events() allocates a new slice sharing no backing array with the
	// ring, so a later append never mutates a handed-out snapshot — the same
	// shared-nothing guarantee the Players/EliminationSequence copies give.
	out.Timeline = s.history.events()
	// Carry a fresh, oldest-first copy of the session speed/threshold track (#1082),
	// shared-nothing with the live ring like the timeline above.
	out.SpeedTrack = s.speedTrack.samples()
	return out
}

// isDrained reports whether the store holds no live state worth keeping: it has
// no players and its session is neither active nor inside a grace window. The
// Multiplexer (#845 PR B) uses this — AFTER calling EvictStale, which TTL-evicts
// stale players and resets an expired session — to decide a non-fallback
// partition can be dropped entirely. A partition that still has fresh players, an
// active game, or an unexpired grace window is NOT drained and is retained.
func (s *Store) isDrained() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.ctx.Players) > 0 {
		return false
	}
	if !s.endedAt.IsZero() {
		return false // grace window still open
	}
	if s.ctx.Session.GameActive != nil && *s.ctx.Session.GameActive {
		return false // game still active
	}
	return true
}

// EvictStale removes players that are disconnected-marked or silent past the TTL
// (also clearing their death baseline so a later re-observation cannot fake an
// elimination), and resets the session once its grace window has elapsed. Players
// persist across session reset so the lobby can continue between games.
func (s *Store) EvictStale() {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()

	// Read the TTLs LIVE (#927): a config-change to lifecycle.player_ttl_seconds /
	// lifecycle.session_grace_seconds is honored on this very tick. Read once per
	// EvictStale so all players this tick are judged against one consistent TTL.
	playerTTL := s.effectivePlayerTTL()
	sessionGrace := s.effectiveSessionGrace()

	for serial, p := range s.ctx.Players {
		if s.disconnected[serial] || now.Sub(p.LastUpdate) > playerTTL {
			delete(s.ctx.Players, serial)
			delete(s.disconnected, serial)
			delete(s.deathCounts, serial)
			delete(s.playerHistory, serial) // drop the evicted player's movement ring (#1082)
		}
	}

	if !s.endedAt.IsZero() && now.Sub(s.endedAt) > sessionGrace {
		s.ctx.Session = SessionSignals{GameActive: ptr(false)}
		s.ctx.SessionID = ""
		s.ctx.GameKind = ""
		s.ctx.ExperimentID = ""
		s.ctx.Arm = ""
		s.elimOrder = make(map[string]int)
		s.endedAt = time.Time{}
		s.ctx.UpdatedAt = now
		// Drop the finished game's narrative along with the rest of its
		// session-scoped state (#916): the ring buffer is evicted with the session
		// so a new game on this partition starts with an empty timeline.
		s.history.reset()
		// Likewise drop the per-player movement series + the session speed track
		// (#1082) so the next game on this partition starts with empty tracks.
		s.playerHistory = make(map[string]*playerHistory)
		s.speedTrack.reset()
	}
}
