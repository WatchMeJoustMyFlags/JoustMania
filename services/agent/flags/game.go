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

// ShieldCharge reads the game's shield activation mode and charge expiry from the game
// set for the agent's charge ledger (UC-040 A31 step 1). trigger is true only when
// shield.activation, trimmed and lower-cased, is "trigger" (as resolve_shield_activation
// in games/base.py). expiry follows resolve_charge_expiry: NaN or negative gives 20 s,
// anything else is clamped to 0..120 s (0 = never).
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
	if strings.ToLower(strings.TrimSpace(mode)) != "trigger" {
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
