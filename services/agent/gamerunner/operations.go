package gamerunner

import (
	"context"
	"errors"
	"fmt"
	"io"
	"time"

	"google.golang.org/grpc"

	mockpb "github.com/joustmania/agent/gen/controller_manager_mock"
	gcpb "github.com/joustmania/agent/gen/game_coordinator"
)

// reserveControllers adds N reserved+tagged mock controllers and returns their
// serials (step a). Reserved controllers are invisible to the menu (#777).
func (r *Runner) reserveControllers(ctx context.Context, cl *clients, spec Spec) ([]string, error) {
	rpcCtx, cancel := context.WithTimeout(ctx, r.cfg.RPCTimeout)
	defer cancel()

	resp, err := cl.mock.AddControllers(rpcCtx, &mockpb.AddControllersRequest{
		Count:    int32(spec.Players),
		Reserved: true,
		Tag:      spec.Tag(),
	})
	if err != nil {
		return nil, fmt.Errorf("AddControllers: %w", err)
	}
	if !resp.GetSuccess() {
		return nil, fmt.Errorf("AddControllers rejected: %s", resp.GetError())
	}
	serials := resp.GetSerials()
	if len(serials) != spec.Players {
		return serials, fmt.Errorf("AddControllers returned %d serials, want %d", len(serials), spec.Players)
	}
	return serials, nil
}

// removeControllers removes each serial best-effort. Cleanup must not fail the
// run, so errors are logged and swallowed.
func (r *Runner) removeControllers(ctx context.Context, cl *clients, serials []string) {
	for _, serial := range serials {
		_, err := cl.mock.RemoveController(ctx, &mockpb.RemoveControllerRequest{Serial: serial})
		if err != nil {
			r.log.Warn("shadow game cleanup: RemoveController failed", "serial", serial, "error", err)
		}
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
			return nil, "", fmt.Errorf("coordinator rejected start: %v", event.GetData())
		}
		if id := event.GetGameId(); id != "" {
			return stream, id, nil
		}
	}
}

// driveGame keeps the game alive and pushes it toward its win condition (step
// d). It periodically sends SimulateMovement to every controller for liveliness
// and, on a slower cadence, sends SimulateDeath to one player at a time until
// all but one are dead — reaching the last-player-standing condition for the
// elimination modes.
//
// SimulateDeath holds death-level acceleration for ~1s in the mock, so kills are
// paced by KillInterval (> the hold) to avoid overlapping a held death with the
// next kill and to let the post-death grace window pass. driveGame returns when
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

// simulateMovement sends a small liveliness jitter to one controller.
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
) (terminal string, eventsSeen int, outcome Outcome) {
	deadline := time.NewTimer(r.cfg.GameTimeout)
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
	for {
		select {
		case <-ctx.Done():
			return terminal, eventsSeen, OutcomeError
		case <-deadline.C:
			if forced {
				// Already force-ended and still no terminal event; give up.
				return terminal, eventsSeen, OutcomeForceEnded
			}
			r.log.Warn("shadow game timed out, force-ending", "game_id", gameID,
				"timeout", r.cfg.GameTimeout)
			r.forceEnd(ctx, cl, gameID)
			forced = true
			// Allow a short grace for the force-end terminal event to arrive.
			deadline.Reset(r.cfg.RPCTimeout)
		case res := <-events:
			if res.err == io.EOF {
				// Stream closed without a terminal event; treat a prior
				// force-end as the outcome, else error.
				if forced {
					return terminal, eventsSeen, OutcomeForceEnded
				}
				return terminal, eventsSeen, OutcomeError
			}
			if res.err != nil {
				if forced {
					return terminal, eventsSeen, OutcomeForceEnded
				}
				return terminal, eventsSeen, OutcomeError
			}
			eventsSeen++
			et := res.event.GetEventType()
			if _, isTerminal := terminalEvents[et]; isTerminal {
				terminal = et
				switch {
				case forced:
					return terminal, eventsSeen, OutcomeForceEnded
				case et == eventGameError:
					return terminal, eventsSeen, OutcomeError
				default:
					return terminal, eventsSeen, OutcomeCompleted
				}
			}
		}
	}
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
