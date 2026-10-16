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
		// UC-046 step 3: the agent-pinned archetype; "" = random noise (A1).
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
// channel that force-ends it (kill switch, UC-046 A12). nil for a noise game.
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
func (r *Runner) finishPattern(spec Spec, gameID string, outcome Outcome, slots []patterngame.SlotRecord) (patterngame.Record, bool) {
	if !spec.IsPattern() || r.tracker == nil || gameID == "" {
		return patterngame.Record{}, false
	}
	return r.tracker.Finish(gameID, patternOutcome(outcome), slots)
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

// driveGame keeps the game alive and pushes it toward its win condition (step
// d). It periodically sends SimulateMovement(0.2, 0.2, 1.0) to every controller,
// which the mock now HOLDS as a constant ~1.04g (UC-003 BR-002, #1279) — well
// under every death threshold — and, on a slower cadence, sends SimulateDeath to one player at a time until
// all but one are dead — reaching the last-player-standing condition for the
// elimination modes.
//
// Because the hold persists, the periodic resend is redundant (the first call
// would suffice); it is kept as a cheap safeguard, e.g. against a controller
// reset mid-game. SimulateDeath latches death-level acceleration for 2
// DELIVERED gameplay frames (#926), after which the held ~1.04g vector resumes;
// kills are paced by KillInterval so each death lands and its post-death grace
// window passes before the next player is killed. driveGame returns when
// ctx is cancelled (the await loop saw a terminal event) or it runs out of
// players to kill.
func (r *Runner) driveGame(ctx context.Context, cl *clients, serials []string) {
	moveTicker := time.NewTicker(r.cfg.MovementInterval)
	defer moveTicker.Stop()
	killTicker := time.NewTicker(r.cfg.KillInterval)
	defer killTicker.Stop()

	// Kill all but the last player; the final survivor triggers the win
	// condition. nextKill walks the serial list.
	nextKill := 0
	lastToKill := len(serials) - 1

	for {
		select {
		case <-ctx.Done():
			return
		case <-moveTicker.C:
			for _, serial := range serials {
				r.simulateMovement(ctx, cl, serial)
			}
		case <-killTicker.C:
			if nextKill >= lastToKill {
				continue // leave one survivor; await loop ends the run
			}
			r.simulateDeath(ctx, cl, serials[nextKill])
			nextKill++
		}
	}
}

// simulateMovement (re)sends the held ~1.04g resting vector to one controller.
// The mock holds it until changed (#1279), so the resend is redundant but kept.
func (r *Runner) simulateMovement(ctx context.Context, cl *clients, serial string) {
	rpcCtx, cancel := context.WithTimeout(ctx, r.cfg.RPCTimeout)
	defer cancel()
	_, err := cl.mock.SimulateMovement(rpcCtx, &mockpb.MovementRequest{
		Serial: serial,
		AccelX: 0.2,
		AccelY: 0.2,
		AccelZ: 1.0, // ~1g gravity baseline, well below the death threshold
	})
	if err != nil && ctx.Err() == nil {
		r.log.Debug("SimulateMovement failed", "serial", serial, "error", err)
	}
}

// simulateDeath triggers a held death-level spike on one controller.
func (r *Runner) simulateDeath(ctx context.Context, cl *clients, serial string) {
	rpcCtx, cancel := context.WithTimeout(ctx, r.cfg.RPCTimeout)
	defer cancel()
	_, err := cl.mock.SimulateDeath(rpcCtx, &mockpb.DeathRequest{Serial: serial})
	if err != nil && ctx.Err() == nil {
		r.log.Debug("SimulateDeath failed", "serial", serial, "error", err)
		return
	}
	r.log.Debug("shadow game kill", "serial", serial)
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
) (terminal string, eventsSeen int, outcome Outcome) {
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()

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
	for {
		select {
		case <-ctx.Done():
			return terminal, eventsSeen, OutcomeError
		case <-deadline.C:
			if forced {
				// Already force-ended and still no terminal event; give up.
				return terminal, eventsSeen, forcedOutcome(abort != nil || aborted, aborted)
			}
			r.log.Warn("shadow game timed out, force-ending", "game_id", gameID,
				"timeout", timeout)
			r.forceEnd(ctx, cl, gameID)
			forced = true
			// Allow a short grace for the force-end terminal event to arrive.
			deadline.Reset(r.cfg.RPCTimeout)
		case <-abort:
			// Kill switch (UC-046 A12): force-end now; a nil channel never fires.
			abort = nil
			if !forced {
				aborted = true
				r.log.Info("pattern game aborted, force-ending", "game_id", gameID)
				r.forceEnd(ctx, cl, gameID)
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
			if res.err == io.EOF {
				// Stream closed without a terminal event; treat a prior
				// force-end as the outcome, else error.
				if forced {
					return terminal, eventsSeen, forcedOutcome(abort != nil || aborted, aborted)
				}
				return terminal, eventsSeen, OutcomeError
			}
			if res.err != nil {
				if forced {
					return terminal, eventsSeen, forcedOutcome(abort != nil || aborted, aborted)
				}
				return terminal, eventsSeen, OutcomeError
			}
			eventsSeen++
			et := res.event.GetEventType()
			if _, isTerminal := terminalEvents[et]; isTerminal {
				terminal = et
				switch {
				case forced:
					return terminal, eventsSeen, forcedOutcome(abort != nil || aborted, aborted)
				case et == eventGameError:
					return terminal, eventsSeen, OutcomeError
				default:
					return terminal, eventsSeen, OutcomeCompleted
				}
			}
		}
	}
}

// forcedOutcome classifies a force-ended game. A pattern game (pattern = true)
// force-ended by the safety cap is a valid timeout (UC-046 BR-013); one ended by
// the kill switch is force_ended (aborted). Noise games keep force_ended.
func forcedOutcome(pattern, aborted bool) Outcome {
	if pattern && !aborted {
		return OutcomeTimeout
	}
	return OutcomeForceEnded
}

// forceEnd force-ends the game by id, best-effort.
func (r *Runner) forceEnd(ctx context.Context, cl *clients, gameID string) {
	rpcCtx, cancel := context.WithTimeout(ctx, r.cfg.RPCTimeout)
	defer cancel()
	gid := gameID
	_, err := cl.coord.ForceEndGame(rpcCtx, &gcpb.ForceEndGameRequest{
		Reason: "shadow_game_timeout",
		GameId: &gid,
	})
	if err != nil {
		r.log.Warn("ForceEndGame failed", "game_id", gameID, "error", err)
	}
}
