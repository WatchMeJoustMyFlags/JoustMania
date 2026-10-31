package flags

import (
	"context"
	"errors"
	"math"
	"strings"
	"sync"
	"time"

	"github.com/open-feature/go-sdk/openfeature"

	"github.com/joustmania/agent/decide"
)

// GameDomain is the domain / flagSetId of the game set (services/flagd/game.json).
// The agent only READS flags from it: shield.activation and
// shield.charge_expiry_seconds for the charge ledger of UC-040 A31 step 1 (#1837),
// and the shadow start-config keys of an experiment arm (ShadowGameInt, UC-046
// BR-019, #2392). It never writes them (C-034, agent_set_never_written_c034_test.go).
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

// GameAttrs is a game's kind and experiment attribution as the agent saw them on its
// telemetry (gamecontext Session.GameKind, ExperimentID, Arm).
type GameAttrs struct {
	GameKind     string
	ExperimentID string
	Arm          string
}

// SetGameAttrs wires the per-game attribute lookup for the shield reads (#2096,
// UC-043 A17): in a shadow game of an experiment the agent then resolves the arm's
// value, as the coordinator does in the game's flag context.
func (f *Flags) SetGameAttrs(lookup func(gameID string) GameAttrs) {
	if f != nil {
		f.gameAttrs = lookup
	}
}

// GameContext is the agent's per-game evaluation context (#2467 PR 5, generalised
// from the shield reads): gameId, the game's game_kind (real when unknown) and, for
// a game of an experiment, experiment_id and arm, from the lookup SetGameAttrs wired.
// The shield flags (ShieldCharge, LatchShieldMode) and decision.prompt_profile
// (PromptProfile) are read with it; see shieldChargeContext for why the
// coordinator's richer context resolves the same shield values.
func (f *Flags) GameContext(gameID string) openfeature.EvaluationContext {
	var attrs GameAttrs
	if f != nil && f.gameAttrs != nil {
		attrs = f.gameAttrs(gameID)
	}
	return shieldChargeContext(gameID, attrs)
}

// keyDecisionPromptProfile is decision.prompt_profile (agent set, flags.md §3).
const keyDecisionPromptProfile = "decision.prompt_profile"

// PromptProfile reads decision.prompt_profile for one game from the agent set, in
// the game's context (GameContext), and normalizes it (decide-contract.md §3,
// UC-040 BR-036): "neutral" is neutral, anything else is v1. A nil wrapper, a nil
// client or any evaluation error gives v1, the shipped prompt (debug log only: the
// remote path reads it per request). The coordinator never reads this flag, and
// flags.md allows its targeting only game_kind, experiment_id and arm
// (tests/contract/test_flagd_resolution.py).
func (f *Flags) PromptProfile(ctx context.Context, gameID string) string {
	if f == nil || f.client == nil {
		return decide.PromptProfileV1
	}
	v, err := f.client.StringValue(ctx, keyDecisionPromptProfile, decide.PromptProfileV1, f.GameContext(gameID))
	if err != nil {
		f.log.Debug("flags decision.prompt_profile unreadable, using v1", "game_id", gameID, "error", err)
		return decide.PromptProfileV1
	}
	return decide.NormalizePromptProfile(v)
}

// shieldChargeContext is the agent's evaluation context for the two shield flags: gameId
// and the game's game_kind (real when unknown), plus experiment_id and arm for a game of
// an experiment (#2096: shield.activation has an experiment rung since FR-162). The coordinator reads them inside grant_shield with more: the
// InterventionManager runs the grant in the game's flag transaction context (#1970), so
// its read also carries targetingKey and game_mode (the mode), controller_count,
// sensitivity and the game's game_kind
// (services/game_coordinator/tests/test_shield_charge_flag_context.py). The agent still
// resolves what the coordinator does: it reads these flags only for a real FFA game
// (game_kind=real on both sides) or, in a shadow game, with the game's kind and arm, and
// flags.md §4 restricts their targeting to gameId, game_kind, experiment_id and arm
// (tests/contract/test_flagd_resolution.py and HANDLER_FLAG_READS in
// services/game_coordinator/handler_flag_reads.py), so the extra attributes never
// change the result.
//
// Keep in sync with _agent_context() in tests/contract/test_flagd_resolution.py, the
// live parity test that resolves both contexts against flagd.
func shieldChargeContext(gameID string, attrs GameAttrs) openfeature.EvaluationContext {
	kind := attrs.GameKind
	if kind == "" {
		kind = "real"
	}
	evalCtx := map[string]any{
		"game_kind": kind,
		"gameId":    gameID,
	}
	if attrs.ExperimentID != "" {
		evalCtx["experiment_id"] = attrs.ExperimentID
	}
	if attrs.Arm != "" {
		evalCtx["arm"] = attrs.Arm
	}
	return openfeature.NewEvaluationContext("", evalCtx)
}

// ShadowGameInt resolves one integer game flag for a shadow game of an experiment
// arm, with the context the game coordinator gives that game's own flag reads:
// game_kind shadow, the game mode, experiment_id and arm (UC-046 BR-019, #2392). The
// shadow spawner puts the result into the game's StartGameConfig, so an experiment
// on a start-config key (sensitivity) gives each arm its own value. An evaluation
// error, a nil game client or an unready domain is returned as an error: the caller
// refuses the spawn rather than start an arm with a value the other arm may not
// share.
func (f *Flags) ShadowGameInt(ctx context.Context, key, mode, experimentID, arm string) (int64, error) {
	if f == nil || f.game == nil {
		return 0, errors.New("game flag set not wired")
	}
	evalCtx := openfeature.NewEvaluationContext("", map[string]any{
		"game_kind":     "shadow",
		"game_mode":     mode,
		"experiment_id": experimentID,
		"arm":           arm,
	})
	return f.game.IntValue(ctx, key, 0, evalCtx)
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
	mode, err := f.game.StringValue(ctx, "shield.activation", "instant", f.GameContext(gameID))
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
	evalCtx := f.GameContext(gameID)
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
