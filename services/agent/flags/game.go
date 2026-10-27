package flags

import (
	"context"
	"math"
	"strings"
	"sync"
	"time"

	"github.com/open-feature/go-sdk/openfeature"
)

// GameDomain is the domain / flagSetId of the game set (services/flagd/game.json).
// The agent only READS two flags from it, for the charge ledger of UC-040 A31 step 1
// (#1837): shield.activation and shield.charge_expiry_seconds. It never writes them
// (C-034, agent_set_never_written_c034_test.go).
const GameDomain = "game"

// DefaultShieldChargeExpiry is the coordinator's default charge expiry
// (games/base.py DEFAULT_CHARGE_EXPIRY_SECONDS), used for a malformed value and when
// the game set cannot be read.
const DefaultShieldChargeExpiry = 20 * time.Second

// shieldChargeExpiryMax mirrors the coordinator's CHARGE_EXPIRY_MAX_SECONDS clamp.
const shieldChargeExpiryMax = 120 * time.Second

// gameUnreadableWarnEvery throttles the agent.shield_charge_mode_unreadable warning.
const gameUnreadableWarnEvery = 30 * time.Second

// gameReadWarn throttles the unreadable-game-set warning (UC-040 A31 step 1).
type gameReadWarn struct {
	mu   sync.Mutex
	last time.Time
}

func (w *gameReadWarn) allow(now time.Time) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	if !w.last.IsZero() && now.Sub(w.last) < gameUnreadableWarnEvery {
		return false
	}
	w.last = now
	return true
}

// shieldChargeContext is the agent's evaluation context for the two shield flags: gameId
// and game_kind=real. The coordinator reads them inside grant_shield with more: the
// InterventionManager runs the grant in the game's flag transaction context (#1970), so
// its read also carries targetingKey and game_mode (the mode), controller_count,
// sensitivity and the game's game_kind
// (services/game_coordinator/tests/test_shield_charge_flag_context.py). The agent still
// resolves what the coordinator does: it reads these flags only for a real FFA game
// (game_kind=real on both sides), and flags.md §4 restricts their targeting to gameId
// and game_kind (tests/contract/test_flagd_resolution.py and HANDLER_FLAG_READS in
// services/game_coordinator/handler_flag_reads.py), so the extra attributes never
// change the result.
//
// Keep in sync with _agent_context() in tests/contract/test_flagd_resolution.py, the
// live parity test that resolves both contexts against flagd.
func shieldChargeContext(gameID string) openfeature.EvaluationContext {
	return openfeature.NewEvaluationContext("", map[string]any{
		"game_kind": "real",
		"gameId":    gameID,
	})
}

// gameModeLatchTTL bounds how long the first shield.activation read of a game is kept: a
// game scope older than 1 h is dropped by the flag service as well (UC-053 A11).
const gameModeLatchTTL = time.Hour

// gameModeLatch is the first shield.activation the agent read per game (UC-040 A31 step 1,
// BR-027, #2096). The coordinator latches `hold` per game, and a game not latched to hold
// keeps the mode it latched for a later `hold` read; the agent cannot see that latch, so it
// keeps its own first read in its place.
type gameModeLatch struct {
	mu    sync.Mutex
	first map[string]gameModeSeen
}

type gameModeSeen struct {
	mode string
	at   time.Time
}

// latch returns the game's first mode, recording mode as that first mode when the game
// has none yet. Entries older than gameModeLatchTTL are dropped on the way.
func (l *gameModeLatch) latch(gameID, mode string, now time.Time) string {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.first == nil {
		l.first = map[string]gameModeSeen{}
	}
	for id, seen := range l.first {
		if now.Sub(seen.at) > gameModeLatchTTL {
			delete(l.first, id)
		}
	}
	if seen, ok := l.first[gameID]; ok {
		return seen.mode
	}
	l.first[gameID] = gameModeSeen{mode: mode, at: now}
	return mode
}

func (l *gameModeLatch) size() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return len(l.first)
}

// ledgerTrigger maps the mode read now and the game's first read to the charge ledger's
// trigger mode: `hold` stays trigger only in a game first read as trigger (a flip to hold
// never changes a running game's mode), and a game first read as `hold` is a hold game
// in which the ledger never applies (fail open; the coordinator decides).
func ledgerTrigger(mode, first string) bool {
	switch mode {
	case "trigger":
		return first != "hold"
	case "hold":
		return first == "trigger"
	default:
		return false
	}
}

// LatchShieldMode records the game's first shield.activation when the agent first sees the
// game (main.go calls it from the per-game Loop factory, on the game's first signal), so a
// later flip to `hold` before the first grant_shield decision does not become the game's
// first mode (UC-040 A31 step 1, BR-027, #2096). Quiet: an unreadable flag latches nothing
// and logs at debug level; ShieldCharge warns when it really needs the flag.
func (f *Flags) LatchShieldMode(ctx context.Context, gameID string) {
	if f == nil || f.game == nil || gameID == "" {
		return
	}
	mode, err := f.game.StringValue(ctx, "shield.activation", "instant", shieldChargeContext(gameID))
	if err != nil {
		f.log.Debug("flags shield.activation unreadable at the game's first signal", "game_id", gameID, "error", err)
		return
	}
	clock := f.now
	if clock == nil {
		clock = time.Now
	}
	f.gameModes.latch(gameID, strings.ToLower(strings.TrimSpace(mode)), clock())
}

// ShieldCharge reads the game's shield activation mode and charge expiry from the game
// set for the agent's charge ledger (UC-040 A31 step 1). The mode, trimmed and
// lower-cased (as resolve_shield_activation in games/base.py), counts as trigger when it
// is "trigger" in a game the agent did not first read as "hold", or "hold" in a game the
// agent first read as "trigger" (ledgerTrigger, #2096). expiry follows
// resolve_charge_expiry: NaN or negative gives 20 s, anything else is clamped to
// 0..120 s (0 = never).
//
// Fail open: a nil game client, an unready domain or any evaluation error gives
// (false, 20 s), so the ledger is inert and the coordinator decides (BR-004). An
// unreadable activation flag logs a warning at most once per 30 s. Callers read it only
// for a targeted grant_shield in a real FFA game, so the warning means a trigger-mode
// check really went without the flag.
func (f *Flags) ShieldCharge(ctx context.Context, gameID string) (trigger bool, expiry time.Duration) {
	if f == nil || f.game == nil {
		return false, DefaultShieldChargeExpiry
	}
	evalCtx := shieldChargeContext(gameID)
	mode, err := f.game.StringValue(ctx, "shield.activation", "instant", evalCtx)
	if err != nil {
		if f.gameWarn.allow(time.Now()) {
			f.log.Warn("agent.shield_charge_mode_unreadable", "game_id", gameID, "error", err)
		}
		return false, DefaultShieldChargeExpiry
	}
	mode = strings.ToLower(strings.TrimSpace(mode))
	clock := f.now
	if clock == nil {
		clock = time.Now
	}
	if !ledgerTrigger(mode, f.gameModes.latch(gameID, mode, clock())) {
		return false, DefaultShieldChargeExpiry
	}
	secs, err := f.game.FloatValue(ctx, "shield.charge_expiry_seconds", DefaultShieldChargeExpiry.Seconds(), evalCtx)
	if err != nil {
		f.log.Debug("flags shield.charge_expiry_seconds fell back to default", "error", err)
		return true, DefaultShieldChargeExpiry
	}
	return true, chargeExpiry(secs)
}

// chargeExpiry mirrors resolve_charge_expiry (games/base.py).
func chargeExpiry(secs float64) time.Duration {
	if math.IsNaN(secs) || secs < 0 {
		return DefaultShieldChargeExpiry
	}
	if secs >= shieldChargeExpiryMax.Seconds() {
		return shieldChargeExpiryMax
	}
	return time.Duration(secs * float64(time.Second))
}
