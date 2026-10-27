package gamerunner

import (
	"context"
	"errors"
	"fmt"
	"io"
	"strings"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"

	mockpb "github.com/joustmania/agent/gen/controller_manager_mock"
	gcpb "github.com/joustmania/agent/gen/game_coordinator"
	"github.com/joustmania/agent/patterngame"
)

// ErrGameInProgress is the sentinel the coordinator's single-game cap surfaces:
// it rejects a concurrent start with "Game already in progress" (servicer.py).
// This is BACKPRESSURE — the coordinator runs one game at a time and the start
// should be retried later, NOT a genuine failure. The experiment registry (#998)
// matches it with errors.Is to back off instead of logging a per-spawn WARN.
var ErrGameInProgress = errors.New("coordinator at capacity: game already in progress")

// coordInProgressMarker is the substring the coordinator embeds in the
// game_start_error event data when its single-game cap rejects a start
// (servicer.py: `return False, "Game already in progress"`). Matched
// case-insensitively so a future wording tweak that keeps the phrase still maps
// to backpressure.
const coordInProgressMarker = "already in progress"

// reserveControllers adds N reserved+tagged mock controllers and returns their
// serials (step a). Reserved controllers are invisible to the menu (#777).
func (r *Runner) reserveControllers(ctx context.Context, cl *clients, spec Spec) ([]string, error) {
	rpcCtx, cancel := context.WithTimeout(ctx, r.cfg.RPCTimeout)
	defer cancel()

	resp, err := cl.mock.AddControllers(rpcCtx, &mockpb.AddControllersRequest{
		Count:    int32(spec.Players),
		Reserved: true,
		Tag:      spec.Tag(),
		// UC-046 step 3: the agent-pinned archetype (every shadow game has one).
		Archetype:       spec.Pattern.Name,
		ArchetypeParams: spec.Pattern.ParamsJSON,
	})
	if err != nil {
		return nil, fmt.Errorf("AddControllers: %w", err)
	}
	if !resp.GetSuccess() {
		return nil, fmt.Errorf("AddControllers rejected: %s", resp.GetError())
	}
	if spec.IsPattern() && resp.GetArchetype() != spec.Pattern.Name {
		return resp.GetSerials(), fmt.Errorf("AddControllers installed archetype %q, want %q",
			resp.GetArchetype(), spec.Pattern.Name)
	}
	serials := resp.GetSerials()
	if len(serials) != spec.Players {
		return serials, fmt.Errorf("AddControllers returned %d serials, want %d", len(serials), spec.Players)
	}
	// Reject DUPLICATE serials (#1013). The game roster is a serial-keyed map, so
	// two identical serials collapse to one player and FFA aborts with "Need at
	// least 2 players, got 1". A duplicate means the controller-manager handed back
	// a serial it had already minted (the len()-based naming colliding with a prior
	// game's not-yet-removed controller) — surface it here rather than start an
	// under-populated game.
	if dup := firstDuplicate(serials); dup != "" {
		return serials, fmt.Errorf("AddControllers returned duplicate serial %q (got %v): under-populated roster", dup, serials)
	}

	// Readiness gate (#1013): wait until every reserved serial is VISIBLE to the
	// mock service before the game is started. AddControllers is synchronous on the
	// adapter, but this guards against any propagation lag between reservation and
	// the coordinator reading the roster, so a spawned game reliably fields all
	// spec.Players controllers.
	if err := r.waitControllersReady(ctx, cl, serials); err != nil {
		return serials, err
	}
	return serials, nil
}

// firstDuplicate returns the first serial that appears more than once in the
// slice, or "" when every serial is unique.
func firstDuplicate(serials []string) string {
	seen := make(map[string]struct{}, len(serials))
	for _, s := range serials {
		if _, ok := seen[s]; ok {
			return s
		}
		seen[s] = struct{}{}
	}
	return ""
}

// waitControllersReady polls ListMockControllers until every serial in want is
// present (registered/visible), or the readiness timeout elapses (#1013). It
// returns nil once all serials are visible, or an error describing which serials
// never appeared. The poll is cheap (an in-memory list on the mock service) and
// the happy path returns on the first check, so a normally-immediate registration
// costs one extra RPC.
func (r *Runner) waitControllersReady(ctx context.Context, cl *clients, want []string) error {
	deadline := time.Now().Add(r.cfg.ReadyTimeout)
	ticker := time.NewTicker(r.cfg.ReadyPollInterval)
	defer ticker.Stop()

	for {
		missing := r.missingControllers(ctx, cl, want)
		if len(missing) == 0 {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("reserved controllers not ready within %s: missing %v", r.cfg.ReadyTimeout, missing)
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("readiness wait cancelled: %w", ctx.Err())
		case <-ticker.C:
		}
	}
}

// missingControllers returns the subset of want that ListMockControllers does not
// currently report as present. A failed List leaves every serial "missing" so the
// caller retries until the deadline rather than starting a game on stale state.
func (r *Runner) missingControllers(ctx context.Context, cl *clients, want []string) []string {
	rpcCtx, cancel := context.WithTimeout(ctx, r.cfg.RPCTimeout)
	defer cancel()
	resp, err := cl.mock.ListMockControllers(rpcCtx, &mockpb.ListRequest{})
	if err != nil {
		r.log.Debug("ListMockControllers failed during readiness wait", "error", err)
		return append([]string(nil), want...)
	}
	present := make(map[string]struct{}, len(resp.GetSerials()))
	for _, s := range resp.GetSerials() {
		present[s] = struct{}{}
	}
	var missing []string
	for _, s := range want {
		if _, ok := present[s]; !ok {
			missing = append(missing, s)
		}
	}
	return missing
}

// removeControllers removes each serial best-effort. Cleanup must not fail the
// run, so errors are logged and swallowed. For pattern controllers it returns the
// replay records the controller manager answers with (UC-046 step 8).
func (r *Runner) removeControllers(ctx context.Context, cl *clients, serials []string) []patterngame.SlotRecord {
	var slots []patterngame.SlotRecord
	for _, serial := range serials {
		resp, err := cl.mock.RemoveController(ctx, &mockpb.RemoveControllerRequest{Serial: serial})
		if err != nil {
			r.log.Warn("shadow game cleanup: RemoveController failed", "serial", serial, "error", err)
			continue
		}
		if resp.GetArchetype() != "" {
			slots = append(slots, patterngame.SlotRecord{
				Slot:            int(resp.GetPatternSlot()),
				Serial:          serial,
				FramesDelivered: resp.GetFramesDelivered(),
				SignalHash:      resp.GetSignalHash(),
				Checkpoints:     resp.GetSignalCheckpoints(),
				FrameOverruns:   resp.GetFrameOverruns(),
			})
		}
	}
	return slots
}

// withPatternPlayers sets Players to the archetype's players for a pattern game.
func withPatternPlayers(spec Spec) Spec {
	if spec.IsPattern() && spec.Pattern.Players > 0 {
		spec.Players = spec.Pattern.Players
	}
	return spec
}

// registerPattern records a started pattern game with the tracker and returns the
// channel that force-ends it (kill switch, UC-046 A12).
func (r *Runner) registerPattern(spec Spec, gameID string, serials []string) (<-chan struct{}, patterngame.Info, bool) {
	if !spec.IsPattern() || r.tracker == nil {
		return nil, patterngame.Info{}, false
	}
	abort := make(chan struct{})
	var once sync.Once
	info := patterngame.Info{
		GameID:         gameID,
		Archetype:      spec.Pattern,
		MaxGameSeconds: patterngame.ClampCap(spec.MaxGameSeconds),
		Seed:           spec.Seed,
		Serials:        append([]string(nil), serials...),
		FlagDigest:     spec.FlagDigest,
		ExperimentID:   spec.ExperimentID,
		Arm:            spec.Arm,
		PairIndex:      spec.PairIndex,
		Abort:          func() { once.Do(func() { close(abort) }) },
	}
	r.tracker.Register(info)
	r.tracker.CheckConcurrency(gameID)
	if r.onStart != nil {
		r.onStart(info)
	}
	return abort, info, true
}

// finishPattern turns the removed controllers' records into the game's replay record.
func (r *Runner) finishPattern(spec Spec, gameID string, outcome Outcome, events patterngame.GameEvents, slots []patterngame.SlotRecord) (patterngame.Record, bool) {
	if !spec.IsPattern() || r.tracker == nil || gameID == "" {
		return patterngame.Record{}, false
	}
	return r.tracker.Finish(gameID, patternOutcome(outcome), events, slots)
}

// livenessTick returns the once-per-second UC-046 BR-008 check: is another game
// live while this pattern game runs?
func (r *Runner) livenessTick(gameID string) func() {
	if r.tracker == nil {
		return nil
	}
	return func() { r.tracker.CheckConcurrency(gameID) }
}

// Game events the elimination order is read from (UC-046 BR-014, games/ffa.py).
const (
	eventPlayerDeath = "player_death"
	eventGameWinner  = "game_winner"
	eventGameTie     = "game_tie"
)

// eventWatcher records deaths, the winner and a tie from the event stream.
type eventWatcher struct {
	mu     sync.Mutex
	events patterngame.GameEvents
}

func (w *eventWatcher) observe(ev *gcpb.GameEvent) {
	w.mu.Lock()
	defer w.mu.Unlock()
	switch ev.GetEventType() {
	case eventPlayerDeath:
		if serial := ev.GetData()["serial"]; serial != "" {
			w.events.Deaths = append(w.events.Deaths, serial)
		}
	case eventGameWinner:
		w.events.Winner = ev.GetData()["serial"]
	case eventGameTie:
		w.events.Tie = true
	}
}

func (w *eventWatcher) get() patterngame.GameEvents {
	w.mu.Lock()
	defer w.mu.Unlock()
	return patterngame.GameEvents{
		Deaths: append([]string(nil), w.events.Deaths...),
		Winner: w.events.Winner,
		Tie:    w.events.Tie,
	}
}

// PatternGameAttributes are the attributes of experiment.pattern_game (telemetry.md §9).
func PatternGameAttributes(info patterngame.Info) []attribute.KeyValue {
	return []attribute.KeyValue{
		attribute.String("game.id", info.GameID),
		attribute.String("experiment.arm", info.Arm),
		attribute.Int("experiment.pair_index", info.PairIndex),
		attribute.String("pattern.archetype", info.Archetype.Name),
		attribute.String("pattern.params_digest", info.Archetype.Digest),
		attribute.String("game.rng_seed", fmt.Sprintf("%d", info.Seed)),
		attribute.Int("pattern.stream_rate_hz", info.Archetype.StreamRateHz),
		attribute.StringSlice("pattern.slot_serials", info.Serials),
		attribute.Int("pattern.max_game_seconds", info.MaxGameSeconds),
		attribute.String("pattern.flag_digest", info.FlagDigest),
	}
}

// addPatternEndedEvent stamps experiment.pattern_game_ended on the run span.
func addPatternEndedEvent(span trace.Span, rec patterngame.Record) {
	span.AddEvent("experiment.pattern_game_ended", trace.WithAttributes(PatternEndedAttributes(rec)...))
}

// PatternEndedAttributes are the attributes of experiment.pattern_game_ended (telemetry.md §9).
func PatternEndedAttributes(rec patterngame.Record) []attribute.KeyValue {
	return []attribute.KeyValue{
		attribute.String("game.id", rec.GameID),
		attribute.String("pattern.archetype", rec.Archetype.Name),
		attribute.String("pattern.params_digest", rec.Archetype.Digest),
		attribute.String("pattern.outcome", rec.Outcome),
		attribute.IntSlice("pattern.elimination_order", rec.EliminationOrder),
		attribute.Int("pattern.winner_slot", rec.WinnerSlot),
		attribute.StringSlice("pattern.signal_hashes", rec.SignalHashes()),
		attribute.Bool("pattern.replay_exact", rec.Exact()),
		attribute.StringSlice("pattern.non_exact_reasons", rec.NonExactReasons),
		attribute.StringSlice("pattern.decisions", rec.DecisionSequence()),
		attribute.String("pattern.flag_digest", rec.FlagDigest),
	}
}

// buildStartConfig assembles the StartGameConfig for the spec's serials (step
// b). Players are assigned alternating teams so team modes have both teams
// populated; the matching mode-specific oneof is attached where the factory
// requires one (mirrors the integration helpers' build_start_config).
func buildStartConfig(spec Spec, serials []string) *gcpb.StartGameConfig {
	players := make([]*gcpb.Player, len(serials))
	for i, serial := range serials {
		players[i] = &gcpb.Player{
			Serial: serial,
			Team:   int32(i % 2),
			Alive:  true,
			Score:  0,
		}
	}
	cfg := &gcpb.StartGameConfig{
		GameName:    spec.GameName,
		Players:     players,
		Sensitivity: int32(spec.Sensitivity),
	}
	// Experiment-cohort binding (#976): an experiment game is started with
	// origin=AGENT (always classified shadow coordinator-side) and its
	// experiment_id/arm so the started session's eval-context + telemetry carry the
	// cohort attribution from the first frame. An unbound shadow game (the #778
	// one-shot runner, ExperimentID == "") sets neither, leaving origin UNSPECIFIED
	// — still shadow, byte-unchanged from before.
	if spec.ExperimentID != "" {
		cfg.Origin = gcpb.GameOrigin_GAME_ORIGIN_AGENT
		cfg.ExperimentId = spec.ExperimentID
		cfg.Arm = spec.Arm
		// Paired CRN seed (#1003): bind the experiment-derived seed AT SPAWN so
		// the started shadow session builds its per-instance RNG from it. 0 means
		// entropy (today's behavior); the coordinator honors it only for a shadow
		// session (same real-protection guard as experiment_id/arm).
		cfg.RngSeed = spec.Seed
	}
	// A one-shot pattern game (UC-046 BR-006) carries its seed too, so a replay
	// with the same SHADOW_GAME_SEED faces the same game RNG. Honored
	// coordinator-side only for a shadow session, like the experiment seed.
	if spec.IsPattern() && spec.Seed != 0 {
		cfg.RngSeed = spec.Seed
	}
	// UC-046 BR-018 (#2096): the game honours trigger and hold only when its
	// controllers press the trigger by themselves. The flag comes from the same
	// spec.Pattern whose parameters reserveControllers installs, so the start and
	// the installed archetype always agree. Shadow-only coordinator-side.
	cfg.PatternTriggerPolicy = spec.IsPattern() && spec.Pattern.HasTriggerPolicy()
	switch spec.GameName {
	case "JoustFFA":
		cfg.GameConfig = &gcpb.StartGameConfig_FfaConfig{FfaConfig: &gcpb.FFAConfig{}}
	case "JoustTeams":
		cfg.GameConfig = &gcpb.StartGameConfig_TeamsConfig{
			TeamsConfig: &gcpb.TeamsConfig{NumTeams: 2, RandomAssignment: false},
		}
	case "JoustRandomTeams":
		cfg.GameConfig = &gcpb.StartGameConfig_RandomTeamsConfig{
			RandomTeamsConfig: &gcpb.RandomTeamsConfig{NumTeams: 2},
		}
	case "Swapper":
		cfg.GameConfig = &gcpb.StartGameConfig_SwapperConfig{SwapperConfig: &gcpb.SwapperConfig{}}
	case "Werewolf":
		cfg.GameConfig = &gcpb.StartGameConfig_WerewolfConfig{
			WerewolfConfig: &gcpb.WerewolfConfig{RevealTimeSeconds: 35.0},
		}
	case "Zombies":
		cfg.GameConfig = &gcpb.StartGameConfig_ZombieConfig{ZombieConfig: &gcpb.ZombieConfig{}}
	case "NonStop":
		cfg.GameConfig = &gcpb.StartGameConfig_NonstopConfig{
			NonstopConfig: &gcpb.NonstopConfig{TimeLimitSeconds: 0},
		}
	}
	return cfg
}

// startAndCapture opens the event stream with start_config (headless start,
// step b/c) and reads forward until it learns the game_id (or a rejection). It
// returns the live stream so the caller continues consuming lifecycle events.
//
// The coordinator stamps GameEvent.game_id on every event for the started
// session (#776); the first event carrying a non-empty game_id yields the id. A
// game_start_error event means the start was rejected.
func (r *Runner) startAndCapture(
	ctx context.Context, cl *clients, spec Spec, serials []string,
) (grpc.ServerStreamingClient[gcpb.GameEvent], string, error) {
	cfg := buildStartConfig(spec, serials)
	// Spawn-traceparent propagation (#1182, epic #1181): for an experiment-bound
	// shadow spawn carrying a valid experiment SpanContext, inject it as the
	// OUTGOING traceparent so the coordinator's game-lifecycle span is created as
	// a CHILD of the experiment span. A real game / unbound shadow game (invalid
	// SpanContext) injects nothing and stays own-rooted.
	ctx = injectExperimentTraceparent(ctx, spec.ExperimentSpanContext)
	stream, err := cl.coord.StreamGameEvents(ctx, &gcpb.StreamEventsRequest{StartConfig: cfg})
	if err != nil {
		return nil, "", fmt.Errorf("StreamGameEvents: %w", err)
	}

	for {
		event, err := stream.Recv()
		if err == io.EOF {
			return nil, "", errors.New("event stream closed before game_id was assigned")
		}
		if err != nil {
			return nil, "", fmt.Errorf("recv game start event: %w", err)
		}
		if event.GetEventType() == eventGameStartError {
			data := event.GetData()
			// The coordinator's single-game cap ("Game already in progress") is
			// backpressure, not a failure: wrap the sentinel so the registry (#998)
			// backs off and retries on the next tick instead of WARNing per spawn.
			if startRejectedInProgress(data) {
				return nil, "", fmt.Errorf("coordinator rejected start (%v): %w", data, ErrGameInProgress)
			}
			return nil, "", fmt.Errorf("coordinator rejected start: %v", data)
		}
		if id := event.GetGameId(); id != "" {
			return stream, id, nil
		}
	}
}

// injectExperimentTraceparent injects the experiment ROOT span context (#1182,
// epic #1181) as the W3C traceparent on the outgoing gRPC metadata, so the
// coordinator's server interceptor extracts it and parents the spawned game's
// lifecycle span under the experiment span (causal parent-child, not a Link).
//
// It is deliberately TARGETED rather than a blanket client stats-handler: only
// an experiment-bound spawn carrying a VALID SpanContext propagates a parent. A
// real game or an unbound shadow game has the zero (invalid) SpanContext and
// injects NOTHING — the coordinator then sees no incoming traceparent and keeps
// the game own-rooted (#1157/#1164). This is the single cross-service hop that
// stitches the experiment trace together, so it is explicit and gated here.
//
// The SpanContext is wrapped as a REMOTE parent (NonRecordingSpan) and run
// through the globally-configured TextMapPropagator into a metadata carrier,
// which is merged onto any existing outgoing metadata on ctx.
func injectExperimentTraceparent(ctx context.Context, sc trace.SpanContext) context.Context {
	if !sc.IsValid() {
		return ctx
	}
	carrier := propagation.MapCarrier{}
	parentCtx := trace.ContextWithRemoteSpanContext(ctx, sc)
	otel.GetTextMapPropagator().Inject(parentCtx, carrier)
	if len(carrier) == 0 {
		// No propagator configured (e.g. telemetry off): nothing to inject, so the
		// game stays own-rooted exactly as before.
		return ctx
	}
	md := metadata.MD{}
	if existing, ok := metadata.FromOutgoingContext(ctx); ok {
		md = existing.Copy()
	}
	for k, v := range carrier {
		md.Set(k, v)
	}
	return metadata.NewOutgoingContext(ctx, md)
}

// startRejectedInProgress reports whether a game_start_error event's data is the
// coordinator's single-game-cap rejection ("Game already in progress"). The
// coordinator publishes the reason under the "error" key (servicer.py); we scan
// every value so a future key/wording change that keeps the phrase still maps to
// backpressure rather than a hard failure.
func startRejectedInProgress(data map[string]string) bool {
	for _, v := range data {
		if strings.Contains(strings.ToLower(v), coordInProgressMarker) {
			return true
		}
	}
	return false
}

// awaitTerminal consumes the event stream until a terminal event arrives or the
// game timeout elapses (step e). On timeout it force-ends the game by id and
// keeps reading for the resulting terminal event. It returns the terminal event
// type, the count of events seen, and the classified outcome.
func (r *Runner) awaitTerminal(
	ctx context.Context,
	cl *clients,
	stream grpc.ServerStreamingClient[gcpb.GameEvent],
	gameID string,
	timeout time.Duration,
	abort <-chan struct{},
	onEvent func(*gcpb.GameEvent),
	tick func(),
) (terminal string, eventsSeen int, outcome Outcome) {
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()
	// UC-046 BR-008: once per second, check whether another game is live.
	var tickC <-chan time.Time
	if tick != nil {
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		tickC = ticker.C
	}

	// Recv blocks, so run it on a goroutine and select against the timeout.
	type recvResult struct {
		event *gcpb.GameEvent
		err   error
	}
	events := make(chan recvResult, 1)
	recvCtx, cancelRecv := context.WithCancel(ctx)
	defer cancelRecv()
	go func() {
		for {
			event, err := stream.Recv()
			select {
			case events <- recvResult{event, err}:
			case <-recvCtx.Done():
				return
			}
			if err != nil {
				return
			}
		}
	}()

	forced := false
	aborted := false
	// endedPending is set by a game_ended without an outcome field (a coordinator
	// older than #1328): the game is over, but a game_force_ended may still follow
	// (UC-046 BR-016). graceC fires when the wait for it is over.
	endedPending := false
	var graceC <-chan time.Time
	for {
		select {
		case <-ctx.Done():
			return terminal, eventsSeen, OutcomeError
		case <-graceC:
			// No game_force_ended followed the outcome-less game_ended: the game
			// ended on its own.
			return terminal, eventsSeen, OutcomeCompleted
		case <-deadline.C:
			if endedPending {
				// The game already ended; never force-end it at the cap.
				continue
			}
			if forced {
				// Already force-ended and still no terminal event; give up.
				return terminal, eventsSeen, forcedOutcome(aborted)
			}
			r.log.Warn("shadow game timed out, force-ending", "game_id", gameID,
				"timeout", timeout)
			r.forceEnd(ctx, cl, gameID, agentForceEndReason)
			forced = true
			// Allow a short grace for the force-end terminal event to arrive.
			deadline.Reset(r.cfg.RPCTimeout)
		case <-tickC:
			tick()
		case <-abort:
			// Kill switch (UC-046 A12): force-end now; a nil channel never fires.
			abort = nil
			if !forced && !endedPending {
				aborted = true
				r.log.Info("pattern game aborted, force-ending", "game_id", gameID)
				r.forceEnd(ctx, cl, gameID, killSwitchForceEndReason)
				forced = true
				if !deadline.Stop() {
					select {
					case <-deadline.C:
					default:
					}
				}
				deadline.Reset(r.cfg.RPCTimeout)
			}
		case res := <-events:
			if res.err != nil {
				// Stream closed (EOF) or failed without a (further) terminal event.
				switch {
				case endedPending:
					return terminal, eventsSeen, OutcomeCompleted
				case forced:
					return terminal, eventsSeen, forcedOutcome(aborted)
				default:
					return terminal, eventsSeen, OutcomeError
				}
			}
			eventsSeen++
			if onEvent != nil {
				onEvent(res.event)
			}
			et := res.event.GetEventType()
			if _, isTerminal := terminalEvents[et]; !isTerminal {
				continue
			}
			terminal = et
			outcome, wait := classifyTerminal(res.event, forced, aborted)
			if !wait {
				return terminal, eventsSeen, outcome
			}
			if !endedPending {
				endedPending = true
				grace := time.NewTimer(r.endedGrace)
				defer grace.Stop()
				graceC = grace.C
			}
		}
	}
}

// Coordinator vocabulary of the game_ended payload (games/ffa.py, #1328), the
// reasons the runner passes to ForceEndGame, and the force-end reasons of other
// callers the runner tells apart (UC-046 BR-016).
const (
	endedDataOutcome      = "outcome"
	endedDataReason       = "reason"
	gameOutcomeForceEnded = "force_ended"
	// agentForceEndReason is the runner's force-end at the safety cap (BR-013).
	agentForceEndReason = "shadow_game_timeout"
	// killSwitchForceEndReason is the runner's force-end for the kill switch (A12),
	// distinct from the cap so coordinator telemetry does not count it as a timeout.
	killSwitchForceEndReason = "pattern_kill_switch"
	// agentEndGameReason is the coordinator's reason for the agent's own end_game
	// intervention (interventions.py _handle_end_game, UC-046 A14).
	agentEndGameReason = "agent_intervention"
	// preemptedReason is the coordinator's reason when a real game preempts the
	// shadow games (servicer.py, UC-043 A2, UC-046 A8).
	preemptedReason = "preempted_by_real_game"
)

// DefaultEndedGrace bounds the wait for a game_force_ended after a game_ended
// without an outcome field (UC-046 BR-016): the coordinator publishes
// game_force_ended only after joining the game thread for up to 2 s
// (game_session.py force_end), plus a margin.
const DefaultEndedGrace = 3 * time.Second

// classifyTerminal classifies a terminal event (UC-046 BR-016). forced reports
// whether the runner force-ended the game itself (aborted: by the kill switch
// rather than the safety cap). wait is true for a game_ended without an outcome
// field: the caller waits briefly for a following game_force_ended before
// classifying the game as completed.
func classifyTerminal(event *gcpb.GameEvent, forced, aborted bool) (outcome Outcome, wait bool) {
	et := event.GetEventType()
	data := event.GetData()
	forceEnd := et == eventGameForceEnded ||
		(et == eventGameEnded && data[endedDataOutcome] == gameOutcomeForceEnded)
	reason := data[endedDataReason]
	switch {
	case forceEnd && aborted:
		// UC-046 A12: once the kill switch force-ended the game, it is aborted
		// whatever reason the coordinator names.
		return OutcomeForceEnded, false
	case forceEnd && reason == agentEndGameReason:
		// UC-046 A14: the agent's own end_game is a right-censored sample.
		return OutcomeAgentEnded, false
	case forceEnd && foreignForceEnd(reason, forced):
		// UC-046 A8: a force-end the agent did not request is an abort: released
		// without a sample. A real-game preemption keeps its reason so the rerun
		// does not use up the pair's budget (BR-015).
		if reason == preemptedReason {
			return OutcomePreempted, false
		}
		return OutcomeForceEnded, false
	case forced:
		return forcedOutcome(aborted), false
	case et == eventGameError:
		return OutcomeError, false
	case et == eventGameEnded && data[endedDataOutcome] == "":
		return OutcomeCompleted, true
	default:
		// game_ended with outcome winner / tie / completed: the outcome was frozen
		// when the game loop ended, so a force-end that follows does not change it.
		return OutcomeCompleted, false
	}
}

// foreignForceEnd reports whether a force-end with this reason was not the
// runner's own: the runner did not request one, or the coordinator names another
// reason (e.g. preempted_by_real_game won the race against the runner's request).
func foreignForceEnd(reason string, forced bool) bool {
	// The kill switch's own pattern_kill_switch never reaches here: classifyTerminal
	// handles every force-end after a kill-switch request first (UC-046 BR-016).
	return !forced || (reason != "" && reason != agentForceEndReason)
}

// forcedOutcome classifies a game the agent force-ended. Ended by the safety cap
// it is a valid timeout (UC-046 BR-013); ended by the kill switch it is
// force_ended (aborted).
func forcedOutcome(aborted bool) Outcome {
	if aborted {
		return OutcomeForceEnded
	}
	return OutcomeTimeout
}

// forceEnd force-ends the game by id with the given reason, best-effort. The
// coordinator records the reason as the game's game.end_reason.
func (r *Runner) forceEnd(ctx context.Context, cl *clients, gameID, reason string) {
	rpcCtx, cancel := context.WithTimeout(ctx, r.cfg.RPCTimeout)
	defer cancel()
	gid := gameID
	_, err := cl.coord.ForceEndGame(rpcCtx, &gcpb.ForceEndGameRequest{
		Reason: reason,
		GameId: &gid,
	})
	if err != nil {
		r.log.Warn("ForceEndGame failed", "game_id", gameID, "error", err)
	}
}
