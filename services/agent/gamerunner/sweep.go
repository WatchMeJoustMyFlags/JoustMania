package gamerunner

import (
	"context"
	"fmt"
	"strings"

	mockpb "github.com/joustmania/agent/gen/controller_manager_mock"
	gcpb "github.com/joustmania/agent/gen/game_coordinator"
)

// SweepResult reports what an orphan sweep removed.
type SweepResult struct {
	// Removed are the serials of orphaned reserved controllers that were
	// removed.
	Removed []string
	// Live are the serials that were left alone because a live game still
	// references them.
	Live []string
}

// SweepOrphans removes reserved mock controllers whose tag begins with
// tagPrefix but which no live game references — the backstop for an agent crash
// mid-run, which would otherwise leak reserved controllers.
//
// It lists mock controllers (ListMockControllers) and the live games
// (ListGames), then removes any reserved controller whose tag has the prefix
// and whose serial does not appear in any live game's player roster. Call it
// before starting a new run so a previous crashed run does not accumulate
// phantom controllers.
//
// tagPrefix is matched as a literal prefix (e.g. "agent:"); an empty prefix is
// rejected to avoid sweeping unrelated reserved controllers.
func (r *Runner) SweepOrphans(ctx context.Context, tagPrefix string) (SweepResult, error) {
	var result SweepResult
	if tagPrefix == "" {
		return result, fmt.Errorf("gamerunner: SweepOrphans requires a non-empty tagPrefix")
	}

	cl, err := r.connect()
	if err != nil {
		return result, err
	}
	defer cl.close()

	// Serials referenced by any live game are protected from the sweep.
	live, err := r.liveSerials(ctx, cl)
	if err != nil {
		return result, err
	}

	listCtx, cancel := context.WithTimeout(ctx, r.cfg.RPCTimeout)
	defer cancel()
	listing, err := cl.mock.ListMockControllers(listCtx, &mockpb.ListRequest{})
	if err != nil {
		return result, fmt.Errorf("ListMockControllers: %w", err)
	}

	for _, info := range listing.GetControllers() {
		if !info.GetReserved() || !strings.HasPrefix(info.GetTag(), tagPrefix) {
			continue
		}
		if _, referenced := live[info.GetSerial()]; referenced {
			result.Live = append(result.Live, info.GetSerial())
			continue
		}
		result.Removed = append(result.Removed, info.GetSerial())
	}

	r.removeControllers(ctx, cl, result.Removed)
	if len(result.Removed) > 0 {
		r.log.Info("swept orphaned shadow-game controllers",
			"prefix", tagPrefix, "removed", len(result.Removed), "live", len(result.Live))
	}
	return result, nil
}

// liveSerials returns the set of controller serials referenced by any live
// game session, so the orphan sweep never removes an in-use controller.
func (r *Runner) liveSerials(ctx context.Context, cl *clients) (map[string]struct{}, error) {
	rpcCtx, cancel := context.WithTimeout(ctx, r.cfg.RPCTimeout)
	defer cancel()
	resp, err := cl.coord.ListGames(rpcCtx, &gcpb.ListGamesRequest{})
	if err != nil {
		return nil, fmt.Errorf("ListGames: %w", err)
	}
	if !resp.GetSuccess() {
		return nil, fmt.Errorf("ListGames rejected: %s", resp.GetError())
	}
	live := make(map[string]struct{})
	for _, game := range resp.GetGames() {
		for _, player := range game.GetPlayers() {
			live[player.GetSerial()] = struct{}{}
		}
	}
	return live, nil
}
