package state

import (
	"sort"
	"strings"

	"github.com/joustmania/flag-service/fserr"
)

// Reasons a game scope ends (telemetry.md §10.1 flagservice.game.end).
const (
	EndReasonEnded      = "ended"
	EndReasonReconciled = "reconciled"
	EndReasonExpired    = "expired"
)

// GameEnd describes one dropped game scope.
type GameEnd struct {
	GameID              string
	CoordinatorInstance string
	Reason              string
	DroppedValues       int
}

func validateGame(g *Game) *fserr.Error {
	if g.ID == "" {
		return fserr.BadRequest("game id is required")
	}
	if g.CoordinatorInstance == "" {
		return fserr.BadRequest("coordinator_instance is required")
	}
	if strings.Contains(g.ID, "/") || len(g.ID) > MaxIDLength || len(g.CoordinatorInstance) > MaxIDLength || len(g.Kind) > MaxIDLength {
		return fserr.BadRequest("game id, kind or coordinator_instance malformed")
	}
	if len(g.Serials) > MaxSerialsPerGame {
		return fserr.BadRequest("a game has at most %d serials", MaxSerialsPerGame)
	}
	for _, s := range g.Serials {
		if s == "" || len(s) > MaxIDLength {
			return fserr.BadRequest("malformed serial %q", s)
		}
	}
	return nil
}

// RegisterGame registers (or re-registers) a game scope (PUT /games/{gameId},
// FR-125). It returns already=true when the game was registered before; the
// stored record is then updated. An unspecified kind is stored as is and is
// never treated as real (FR-041): only the experiment rung reads the kind, and
// it compares with "real".
func (s *State) RegisterGame(g Game) (already bool, err *fserr.Error) {
	if err := validateGame(&g); err != nil {
		return false, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_, already = s.games[g.ID]
	s.registerLocked(g)
	return already, nil
}

func (s *State) registerLocked(g Game) {
	if old := s.games[g.ID]; old != nil {
		g.RegisteredAt = old.RegisteredAt
	} else {
		g.RegisteredAt = s.opts.Now()
	}
	serials := append([]string(nil), g.Serials...)
	sort.Strings(serials)
	g.Serials = serials
	s.games[g.ID] = &g
	// Registration changes no flag value; the snapshot is unchanged.
}

// EndGame drops a game scope and every value written for it (DELETE
// /games/{gameId}, FR-125, UC-053 step 7). found is false for an unknown game.
func (s *State) EndGame(gameID, reason string) (GameEnd, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.endLocked(gameID, reason)
}

func (s *State) endLocked(gameID, reason string) (GameEnd, bool) {
	g := s.games[gameID]
	if g == nil {
		return GameEnd{GameID: gameID, Reason: reason}, false
	}
	delete(s.games, gameID)
	end := GameEnd{GameID: gameID, CoordinatorInstance: g.CoordinatorInstance, Reason: reason}
	for _, f := range s.flags {
		end.DroppedValues += f.dropGame(gameID)
	}
	if end.DroppedValues > 0 {
		s.changed()
	}
	return end, true
}

// Reconcile makes instance's registered games exactly games: it registers or
// updates every listed game and drops the instance's other games. Games of
// other coordinator instances are untouched (PUT /coordinators/{instance}/games,
// UC-053 A10, review C3).
func (s *State) Reconcile(instance string, games []Game) ([]GameEnd, *fserr.Error) {
	if instance == "" {
		return nil, fserr.BadRequest("coordinator instance is required")
	}
	keep := map[string]bool{}
	for i := range games {
		if games[i].CoordinatorInstance == "" {
			games[i].CoordinatorInstance = instance
		}
		if games[i].CoordinatorInstance != instance {
			return nil, fserr.BadRequest("game %q names coordinator_instance %q, not %q", games[i].ID, games[i].CoordinatorInstance, instance)
		}
		if err := validateGame(&games[i]); err != nil {
			return nil, err
		}
		keep[games[i].ID] = true
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, g := range games {
		s.registerLocked(g)
	}
	var ends []GameEnd
	for _, id := range sortedKeys(s.games) {
		g := s.games[id]
		if g.CoordinatorInstance == instance && !keep[id] {
			end, _ := s.endLocked(id, EndReasonReconciled)
			ends = append(ends, end)
		}
	}
	return ends, nil
}

// ExpireGames drops game scopes older than the maximum game age (UC-053 A11).
func (s *State) ExpireGames() []GameEnd {
	s.mu.Lock()
	defer s.mu.Unlock()
	cutoff := s.opts.Now().Add(-s.opts.MaxGameAge)
	var ends []GameEnd
	for _, id := range sortedKeys(s.games) {
		if s.games[id].RegisteredAt.Before(cutoff) {
			end, _ := s.endLocked(id, EndReasonExpired)
			ends = append(ends, end)
		}
	}
	return ends
}

// GameView is a registered game with the number of values held for it.
type GameView struct {
	Game
	Values int `json:"values"`
}

// Games returns the registered game scopes, sorted by id.
func (s *State) Games() []GameView {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]GameView, 0, len(s.games))
	for _, id := range sortedKeys(s.games) {
		v := GameView{Game: *s.games[id]}
		for _, f := range s.flags {
			if _, ok := f.games[id]; ok {
				v.Values++
			}
			if _, ok := f.events[id]; ok {
				v.Values++
			}
			v.Values += len(f.serials[id])
		}
		out = append(out, v)
	}
	return out
}

// Game returns one registered game, or nil.
func (s *State) Game(id string) *Game {
	s.mu.Lock()
	defer s.mu.Unlock()
	if g := s.games[id]; g != nil {
		cp := *g
		return &cp
	}
	return nil
}
