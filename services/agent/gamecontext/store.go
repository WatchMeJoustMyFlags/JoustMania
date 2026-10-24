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

	// gameEndAt is when the game itself ended (#1494): the arrival of the
	// game_active 0, the game root span's end timestamp, or the last game_active
	// datapoint for a stale-ended game. It closes the start->end duration bracket
	// (effectiveDurationLocked), while endedAt (the time the agent learned of the
	// end) opens the grace window. Reset with endedAt.
	gameEndAt time.Time
	// gameActiveSeenAt is when the last game_active datapoint (any value) arrived
	// (#1494). EvictStale ends an active game whose game_active series has been
	// silent for gameActiveStaleAfter, so a dropped 0 cannot pin the partition.
	gameActiveSeenAt time.Time
	// endLatched latches once this partition's game ended (#1494): by any end
	// signal in a game_id partition (latchEnds), and by the game root span in any
	// partition. Game ids are unique per StartGame (game_{uuid12}) and never
	// reused, so a latched partition hosts exactly one game: a later game_active=1
	// (out-of-order delivery, or a series that resumes after a stale end) never
	// starts a second session that would end, and fire OnGameEnd, again. Never
	// reset: the partition is dropped once drained, and the Multiplexer
	// re-latches a recreated partition for an ended game id.
	endLatched bool
	// latchEnds is set by the Multiplexer for a game_id partition (not the
	// fallback, where unlabeled games share one store and a 0 -> 1 is a new game).
	latchEnds bool

	// durationAdvancedAt is when game_duration_seconds last CHANGED value (#1305).
	// The coordinator's observable gauge is re-exported every interval even when
	// the value is frozen, so arrival time alone cannot tell a live gauge from a
	// stuck one; the value advancing can.
	durationAdvancedAt time.Time
	// elapsedHighWater is the largest in-game elapsed value a snapshot has carried
	// for the current game (#1305), so elapsed never decreases within a game. Reset
	// when a new game starts and on session-grace exit.
	elapsedHighWater float64

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

	// elimOrder is the preferred elimination source (serial -> order index),
	// fed by game_player_elimination_order{serial,game_id}. The player_death span
	// event is the fallback. game_player_deaths_total is an aggregate
	// {mode,game_kind} counter with no serial, so it is not an elimination source.
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

	// OnGameEnd, when non-nil, is fired EXACTLY ONCE per game when the game ends
	// (#844 post-game retrospective), whichever end signal arrives first: the
	// game_active 1 -> 0 datapoint, the game root span (EndGameFromRootSpan) or a
	// stale game_active series (EvictStale) (#1494). It gets a pre-reset GameContext
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

// durationStaleAfter is how long game_duration_seconds may go without advancing
// before Snapshot() falls back to the time since StartedAt (#1305). The
// coordinator refreshes the gauge ~1 Hz while RUNNING, so 5 s tolerates a few
// lost exports without flapping to the fallback.
const durationStaleAfter = 5 * time.Second

// gameActiveStaleAfter is how long an active game's game_active series may stay
// silent before EvictStale ends the game (#1494, UC-041 A4). The coordinator
// re-exports the gauge every metric export interval (100 ms) while the game is
// live, and the game root span normally ends a game whose 0 was dropped within one
// span batch (about 5 s), so 60 s is only the backstop for a game that sent
// neither (coordinator crash, lost span batch, a collector restart). A stale end
// means the game's telemetry broke, so it is ended with EndReasonStale and the
// game-end consumers keep it out of experiments, retro, summary and proposer.
const gameActiveStaleAfter = 60 * time.Second

// SetSessionDuration records elapsed game time (UC-041 BR-003: the live gauge; it
// never steps back within a game).
func (s *Store) SetSessionDuration(v float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	prev := s.ctx.Session.DurationSeconds
	if prev == nil || *prev != v {
		s.durationAdvancedAt = s.now()
	}
	s.ctx.Session.DurationSeconds = ptr(v)
	s.touchSession()
}

// effectiveDurationLocked returns the elapsed game time a snapshot should carry
// (#1305). While the game is active the game_duration_seconds gauge wins when it
// is fresh (advanced within durationStaleAfter); when it is missing or stale and a
// start was observed, the elapsed time is derived from StartedAt (never below a
// stale gauge value). A per-game high-water mark keeps the in-game value
// monotonic, so switching from the derived value back to a fresh gauge that lags
// it by an export interval never steps elapsed backwards.
//
// Once the game has ended, the value is the larger of the observed gauge and the
// agent's own start->end bracket (endedAt - StartedAt): the coordinator's final set
// can lose the race against game_active=0, leaving only the last live (short)
// value, and every game-end consumer (experiment conclusion, retro, summary,
// proposer) reads this one snapshot, so they all agree. Outside a bracketed game
// the observed gauge is returned unchanged. Caller holds s.mu.
func (s *Store) effectiveDurationLocked() *float64 {
	sess := s.ctx.Session
	if sess.StartedAt.IsZero() {
		return sess.DurationSeconds
	}
	active := sess.GameActive != nil && *sess.GameActive
	if !active {
		if s.endedAt.IsZero() {
			return sess.DurationSeconds
		}
		bracketEnd := s.gameEndAt
		if bracketEnd.IsZero() {
			bracketEnd = s.endedAt
		}
		bracket := bracketEnd.Sub(sess.StartedAt).Seconds()
		if bracket <= 0 || (sess.DurationSeconds != nil && *sess.DurationSeconds >= bracket) {
			return sess.DurationSeconds
		}
		return ptr(bracket)
	}

	now := s.now()
	var elapsed float64
	switch {
	case sess.DurationSeconds != nil && now.Sub(s.durationAdvancedAt) <= durationStaleAfter:
		elapsed = *sess.DurationSeconds
	default:
		elapsed = now.Sub(sess.StartedAt).Seconds()
		if sess.DurationSeconds != nil && *sess.DurationSeconds > elapsed {
			elapsed = *sess.DurationSeconds
		}
	}
	if elapsed < s.elapsedHighWater {
		elapsed = s.elapsedHighWater
	}
	if elapsed < 0 {
		elapsed = 0
	}
	s.elapsedHighWater = elapsed
	return ptr(elapsed)
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
// the grace window by recording endedAt. A game_active=1 for a partition whose
// game already ended (endLatched) is ignored (#1494).
func (s *Store) SetGameActive(active bool) {
	// endSnapshot is captured under the lock on a true->false transition and the
	// OnGameEnd callback is fired with it AFTER the mutex is released (never under
	// s.mu — the callback must be free to read the store without re-entrancy).
	var endSnapshot *GameContext

	s.mu.Lock()
	s.gameActiveSeenAt = s.now()
	if active && s.endLatched {
		s.mu.Unlock()
		return
	}
	prev := s.gameActiveLocked()
	s.ctx.Session.GameActive = ptr(active)

	switch {
	case !prev && active:
		s.sessionSeq++
		s.ctx.SessionID = fmt.Sprintf("session-%d", s.sessionSeq)
		s.ctx.Session.EliminationSequence = nil
		s.ctx.Session.InterventionCounts = nil
		s.ctx.Session.DurationSeconds = nil
		s.ctx.Session.StartedAt = s.now()
		s.durationAdvancedAt = time.Time{}
		s.elapsedHighWater = 0
		s.elimOrder = make(map[string]int)
		s.endedAt = time.Time{}
		s.gameEndAt = time.Time{}
		s.ctx.EndReason = ""
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
		endSnapshot = s.endGameLocked(s.now(), EndReasonZero)
	}
	s.touchSession()
	s.mu.Unlock()

	s.fireGameEnd(endSnapshot)
}

// setPartition marks the store as a game_id partition (#1494): every game end
// latches, and ended (the game id ended in an earlier, since removed partition)
// latches it from the start. Called by the Multiplexer before the store is
// published.
func (s *Store) setPartition(ended bool) {
	s.mu.Lock()
	s.latchEnds = true
	s.endLatched = s.endLatched || ended
	s.mu.Unlock()
}

// hasEnded reports whether this partition's game has ended (endLatched).
func (s *Store) hasEnded() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.endLatched
}

// gameActiveLocked reports whether the session's game is active. Caller holds s.mu.
func (s *Store) gameActiveLocked() bool {
	return s.ctx.Session.GameActive != nil && *s.ctx.Session.GameActive
}

// endGameLocked ends the active game (#844, #1494): it is the single game-end
// transition every end signal (game_active 0, game root span, stale game_active)
// goes through, so OnGameEnd fires exactly once per game whichever signal arrives
// first; the later ones find the game inactive, and in a game_id partition the
// end latches so no later game_active=1 restarts it. gameEnd is when the game
// itself ended: it closes the duration bracket and stamps the timeline's "end"
// phase event, so the start -> end bracket game-end consumers derive from the
// timeline (experiment_loop.go withEffectiveDuration) agrees with it; the grace
// window opens now. reason is recorded as EndReason. It returns the
// pre-reset snapshot for OnGameEnd (nil when no hook is wired). Caller holds s.mu
// and must have checked that the game is active.
func (s *Store) endGameLocked(gameEnd time.Time, reason string) *GameContext {
	s.ctx.Session.GameActive = ptr(false)
	s.ctx.EndReason = reason
	s.endedAt = s.now()
	s.gameEndAt = gameEnd
	if s.latchEnds {
		s.endLatched = true
	}
	// Game end is the final phase transition; record it BEFORE the snapshot so
	// the OnGameEnd retro (#844) sees a narrative ending in "end" (#916).
	s.history.append(TimelineEvent{At: gameEnd, Kind: EventPhase, Detail: "end"})
	if s.OnGameEnd == nil {
		return nil
	}
	snap := s.snapshotLocked() // pre-reset state: SessionID + sequence intact
	return &snap
}

// fireGameEnd invokes OnGameEnd with snap outside the store mutex; nil is a no-op.
func (s *Store) fireGameEnd(snap *GameContext) {
	if snap != nil {
		s.OnGameEnd(*snap)
	}
}

// EndGameFromRootSpan is the second game-end signal (#1494): the coordinator
// exports the game root span when the game's session ends, natural end and
// force-end alike, so its arrival ends a game whose game_active 0 datapoint was
// never exported (retire removed the series first). spanEnd is the span's end
// timestamp (zero when absent); it closes the duration bracket, clamped to
// [StartedAt, now]. A game that already ended (or was never seen active) is left
// alone, and the partition is latched so a late game_active=1 cannot restart it.
func (s *Store) EndGameFromRootSpan(spanEnd time.Time) {
	var endSnapshot *GameContext

	s.mu.Lock()
	s.endLatched = true
	if s.gameActiveLocked() {
		now := s.now()
		gameEnd := spanEnd
		switch {
		case gameEnd.IsZero() || gameEnd.After(now):
			gameEnd = now
		case gameEnd.Before(s.ctx.Session.StartedAt):
			gameEnd = s.ctx.Session.StartedAt
		}
		endSnapshot = s.endGameLocked(gameEnd, EndReasonRootSpan)
		s.touchSession()
	}
	s.mu.Unlock()

	s.fireGameEnd(endSnapshot)
}

// SetGameKind records the game kind ("real"/"shadow") from the game_kind
// datapoint label or the game.kind span attribute (#845). The kind is normalised
// (NormalizeGameKind: telemetry "primary" -> "real", #1249) so GameContext.GameKind
// is always in the flag-evaluation vocabulary. An empty kind is ignored so an
// unlabeled signal never clobbers a previously observed kind.
func (s *Store) SetGameKind(kind string) {
	kind = NormalizeGameKind(kind)
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

// SetGameTraceID records the coordinator's root game-span trace_id (#1133) from
// the game_trace_id label on the game_trace_correlation signal. An empty id is
// ignored so an unlabeled signal never clobbers a previously observed trace id —
// exactly like SetGameKind. The decision loop reads this to add a span Link from
// agent.decision to the originating game trace; it is validated there, so an
// invalid value stored here simply yields no Link (graceful fallback).
func (s *Store) SetGameTraceID(id string) {
	if id == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ctx.GameTraceID = id
	s.touchSession()
}

// SetGameTraceSpanID records the coordinator's root game-span span_id (#1157) from
// the game_trace_span_id label on the game_trace_correlation signal. Mirrors
// SetGameTraceID exactly: an empty id is ignored so an unlabeled signal never
// clobbers a previously observed span id. The decision/retro loop reads this to
// set the SpanID on its game-trace Link so Jaeger highlights the actual game-start
// span; an empty value there simply yields no Link (graceful fallback).
func (s *Store) SetGameTraceSpanID(id string) {
	if id == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ctx.GameTraceSpanID = id
	s.touchSession()
}

// SetGameplayPhaseSpanID records the coordinator's gameplay_phase sub-span span_id
// (#1195, refines #1187) from the gameplay_phase_span_id label on the
// game_trace_correlation signal. Mirrors SetGameTraceSpanID exactly: an empty id is
// ignored so an unlabeled signal (or a not-yet-open gameplay_phase span) never
// clobbers a previously observed id — the agent then keeps re-parenting under the
// game root via GameTraceSpanID. The decision/async loops read this to re-parent the
// in-game decision chain under gameplay_phase, with two-tier fallback to the game
// root; an empty value there simply keeps the #1187 behavior (graceful fallback).
func (s *Store) SetGameplayPhaseSpanID(id string) {
	if id == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ctx.GameplayPhaseSpanID = id
	s.touchSession()
}

// SetGameCycleSpanID records the coordinator's game_cycle span_id (#1325, UC-041
// BR-004) from the game_cycle_span_id label on the game_trace_correlation signal.
// Mirrors SetGameplayPhaseSpanID: an empty id is ignored so an unlabeled signal (or a
// not-yet-open game_cycle) never clobbers a previously observed id.
func (s *Store) SetGameCycleSpanID(id string) {
	if id == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ctx.GameCycleSpanID = id
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

// SetEliminationOrder is the preferred elimination source
// (game_player_elimination_order{serial,game_id}). It records the order and
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
	// a SECOND elimination line here (#916). Both sources are live: the
	// coordinator emits game_player_elimination_order next to the player_death
	// span event, so either can arrive first. The reverse order is safe: once this
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

// RecordInterventionApply adds how a dispatched intervention's write ended to the
// game's per-type tally (#1771): Applied for a write, GatedOff for an
// actuation-gate no-op; a failed write changes nothing (it stays Dispatched only).
// The timeline is not touched: AppendInterventionEvent already recorded the
// dispatch.
func (s *Store) RecordInterventionApply(intervention string, outcome ApplyOutcome) {
	if outcome != ApplyOutcomeApplied && outcome != ApplyOutcomeGatedOff {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.ctx.Session.InterventionCounts == nil {
		s.ctx.Session.InterventionCounts = make(map[string]InterventionCount)
	}
	tally := s.ctx.Session.InterventionCounts[intervention]
	if outcome == ApplyOutcomeApplied {
		tally.Applied++
	} else {
		tally.GatedOff++
	}
	s.ctx.Session.InterventionCounts[intervention] = tally
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
	if s.ctx.Session.InterventionCounts == nil {
		s.ctx.Session.InterventionCounts = make(map[string]InterventionCount)
	}
	tally := s.ctx.Session.InterventionCounts[intervention]
	if blocked {
		tally.Blocked++
	} else {
		tally.Dispatched++
	}
	s.ctx.Session.InterventionCounts[intervention] = tally
	s.history.append(TimelineEvent{
		At:      s.now(),
		Kind:    EventIntervention,
		Detail:  intervention,
		Serial:  serial,
		Blocked: blocked,
	})
}

// AppendInterventionInPlace records in the rolling narrative an intervention the
// agent did not write because the same value was already applied (UC-040 A35). It
// reads "already in place", not "blocked": nothing stopped the agent, so the
// narrative must not invite a retry. The dispatched / blocked tally is unchanged.
func (s *Store) AppendInterventionInPlace(intervention, serial string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.history.append(TimelineEvent{
		At:      s.now(),
		Kind:    EventIntervention,
		Detail:  intervention,
		Serial:  serial,
		InPlace: true,
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
	// Live elapsed time with the StartedAt fallback while the game is active (#1305).
	out.Session.DurationSeconds = s.effectiveDurationLocked()
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
	if s.ctx.Session.InterventionCounts != nil {
		counts := make(map[string]InterventionCount, len(s.ctx.Session.InterventionCounts))
		for k, v := range s.ctx.Session.InterventionCounts {
			counts[k] = v
		}
		out.Session.InterventionCounts = counts
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
	if s.gameActiveLocked() {
		return false // game still active
	}
	return true
}

// EvictStale removes players that are disconnected-marked or silent past the TTL
// (also clearing their death baseline so a later re-observation cannot fake an
// elimination), and resets the session once its grace window has elapsed. Players
// persist across session reset so the lobby can continue between games.
//
// A stale-ended game's OnGameEnd chain (experiment conclusion, retro, summary,
// proposer) runs synchronously on the caller's goroutine, the eviction ticker's
// (server.go startEvictionLoop), after s.mu is released; the Multiplexer calls
// this outside m.mu too, so the chain may read the store and the multiplexer.
func (s *Store) EvictStale() {
	var endSnapshot *GameContext
	s.mu.Lock()
	now := s.now()

	// Backstop game end (#1494): an active game whose game_active series has been
	// silent past gameActiveStaleAfter ended without a 0 or a root span reaching the
	// agent. The bracket closes at the last datapoint; the grace window opens now.
	if s.gameActiveLocked() && !s.gameActiveSeenAt.IsZero() && now.Sub(s.gameActiveSeenAt) > gameActiveStaleAfter {
		endSnapshot = s.endGameLocked(s.gameActiveSeenAt, EndReasonStale)
		s.touchSession()
	}

	// Read the TTLs LIVE (#927): a config-change to lifecycle.player_ttl_seconds /
	// lifecycle.session_grace_seconds is honored on this very tick. Read once per
	// EvictStale so all players this tick are judged against one consistent TTL.
	playerTTL := s.effectivePlayerTTL()
	sessionGrace := s.effectiveSessionGrace()

	for serial, p := range s.ctx.Players {
		if s.disconnected[serial] || now.Sub(p.LastUpdate) > playerTTL {
			delete(s.ctx.Players, serial)
			delete(s.disconnected, serial)
			delete(s.playerHistory, serial) // drop the evicted player's movement ring (#1082)
		}
	}

	if !s.endedAt.IsZero() && now.Sub(s.endedAt) > sessionGrace {
		s.ctx.Session = SessionSignals{GameActive: ptr(false)}
		s.durationAdvancedAt = time.Time{}
		s.elapsedHighWater = 0
		s.ctx.SessionID = ""
		s.ctx.GameKind = ""
		s.ctx.ExperimentID = ""
		s.ctx.Arm = ""
		s.ctx.GameTraceID = ""         // drop the finished game's trace link (#1133)
		s.ctx.GameTraceSpanID = ""     // ditto, the linked span_id (#1157)
		s.ctx.GameplayPhaseSpanID = "" // ditto, the gameplay_phase span_id (#1195)
		s.ctx.GameCycleSpanID = ""     // ditto, the game_cycle span_id (#1325)
		s.elimOrder = make(map[string]int)
		s.endedAt = time.Time{}
		s.gameEndAt = time.Time{}
		s.ctx.EndReason = ""
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
	s.mu.Unlock()

	s.fireGameEnd(endSnapshot)
}
