package promote

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/joustmania/agent/flagservice"
)

// realdefault.go is the autonomous mode's REAL default change — the SEPARATE,
// higher-privilege write the safety rail isolates from the shadow
// experiment.Writer (W5, epic #1407).
//
// WHY A SEPARATE WRITER: experiment.Writer only ever writes an experiment-arm
// value, which the flag service renders for non-real games of that experiment
// only, so it CANNOT change what a real game resolves. Autonomous promotion
// deliberately DOES change what real games resolve, so it is a DIFFERENT,
// explicitly-gated mechanism: PUT /game/{flag} with scope.game_kind=real (a
// "real" overlay the flag service renders as a defaultVariant substitution).
//
// The overlay lives in the flag service's memory: a flag-service restart drops
// it. The durable path for a promoted default is the promotion PR (FR-064,
// C-007, FR-138). A revert is DELETE /game/{flag}?game_kind=real, which returns
// the flag to whatever the baseline (and any admin or stage overlay) says — the
// exact pre-promotion state, without the value snapshots the file writer needed
// (#1065). The target is always the game flag set (#1293: no configurable path).
//
// It is built ONLY by NewRealDefaultWriterFromEnv behind the env gate, and it is
// invoked only on the autonomous path, which is additionally behind the agent
// kill-switch (Config.Enabled, checked in promoteAutonomous). Two independent gates
// (env + kill-switch) guard every real-default change.

// gameFlagSet is the only flag set a promotion writes.
const gameFlagSet = "game"

// promotionWriter is the informational writer label on promotion writes (D11).
const promotionWriter = "promotion"

// realDefaultService is the part of the flag-service client the writer uses.
type realDefaultService interface {
	Put(ctx context.Context, set, flag string, w flagservice.Write) (flagservice.Result, error)
	Delete(ctx context.Context, set, flag string, query url.Values) error
	GetFlag(ctx context.Context, set, flag string) (flagservice.FlagView, error)
	GetSet(ctx context.Context, set string) (flagservice.SetView, error)
}

// serviceRealDefaultWriter writes and reverts the real default of a game flag
// through the flag service. It implements RealDefaultWriter and
// RealDefaultReverter.
type serviceRealDefaultWriter struct {
	svc realDefaultService
	log *slog.Logger
	mu  sync.Mutex
	// snaps is the durable store of the post-revert cooldown stamps (#1065 item 3,
	// #1076), so thrash protection survives an agent restart. nil when the store
	// directory was unreadable at construction (logged).
	snaps *snapStore
	clock func() time.Time
}

// PersistedRevertTimes exposes the durable post-revert cooldown stamps so the
// Promoter can seed its in-memory lastRevert map after a restart (#1076 item 3).
func (w *serviceRealDefaultWriter) PersistedRevertTimes() map[string]time.Time {
	if w.snaps == nil {
		return nil
	}
	return w.snaps.loadLastRevert()
}

// NewRealDefaultWriterFromEnv builds the real-default writer, or (nil, nil) when
// the autonomous path is not enabled (the fail-closed default; the caller then
// treats autonomous as a recorded no-op). This is the ONLY constructor for a
// real-default writer.
//
// Gate conditions:
//   - AGENT_CODE_IMPROVEMENT_ENABLED == "true"        (explicit opt-in)
//   - AGENT_AUTONOMOUS_ENABLED == "true"               (a SECOND opt-in specific to
//     the real-default change — autonomous is the most privileged path)
//
// The flag service is FLAG_SERVICE_URL; the flag set is always `game` (#1293).
func NewRealDefaultWriterFromEnv(log *slog.Logger) (*serviceRealDefaultWriter, error) {
	if log == nil {
		log = slog.Default()
	}
	if !strings.EqualFold(strings.TrimSpace(os.Getenv("AGENT_CODE_IMPROVEMENT_ENABLED")), "true") {
		return nil, nil
	}
	if !strings.EqualFold(strings.TrimSpace(os.Getenv("AGENT_AUTONOMOUS_ENABLED")), "true") {
		log.Info("code_improvement real-default writer NOT built: AGENT_AUTONOMOUS_ENABLED != true (autonomous stays a recorded no-op)")
		return nil, nil
	}
	dir := snapStoreDir()
	snaps, err := newSnapStore(dir)
	if err != nil {
		log.Warn("code_improvement real-default cooldown store could not be loaded; the post-revert cooldown starts empty",
			"dir", dir, "error", err)
	}
	log.Warn("code_improvement AUTONOMOUS real-default writer ENABLED (#936): autonomous promotions WILL change the REAL default for live players (flag service, in memory until the promotion PR merges)")
	return newServiceRealDefaultWriter(flagservice.NewFromEnv(), snaps, log), nil
}

func newServiceRealDefaultWriter(svc realDefaultService, snaps *snapStore, log *slog.Logger) *serviceRealDefaultWriter {
	return &serviceRealDefaultWriter{svc: svc, log: log, snaps: snaps}
}

// SetRealDefault makes real games resolve value for flagKey: a "real" overlay
// on the flag service. The registry bounds the value (a declared variant's
// value); a value whose JSON kind differs from the current default is refused
// here first (#936 review note 2), as the last line before real players.
func (w *serviceRealDefaultWriter) SetRealDefault(ctx context.Context, flagKey string, value any) error {
	w.mu.Lock()
	defer w.mu.Unlock()

	view, err := w.svc.GetFlag(ctx, gameFlagSet, flagKey)
	if err != nil {
		return fmt.Errorf("read %s/%s before promotion: %w", gameFlagSet, flagKey, err)
	}
	valRaw, err := json.Marshal(value)
	if err != nil {
		return fmt.Errorf("marshal value: %w", err)
	}
	if curRaw, ok := view.VariantValues[view.EffectiveDefault]; ok {
		if ck, nk := jsonKind(curRaw), jsonKind(valRaw); ck != "" && nk != "" && ck != nk {
			return fmt.Errorf("refusing real-default write for %q: value kind %q != current default %q kind %q", flagKey, nk, view.EffectiveDefault, ck)
		}
	}
	res, err := w.svc.Put(ctx, gameFlagSet, flagKey, flagservice.Write{
		Value:  value,
		Scope:  flagservice.Scope{GameKind: "real"},
		Writer: promotionWriter,
		Reason: "autonomous_promotion",
	})
	if err != nil {
		return fmt.Errorf("promote %s/%s: %w", gameFlagSet, flagKey, err)
	}
	w.log.Warn("code_improvement.autonomous changing the REAL default (#936)",
		"flag", flagKey, "variant", res.Variant, "epoch", res.Epoch)
	return nil
}

// RevertRealDefault removes the promotion's real overlay, so real games resolve
// the pre-promotion default again (#1065), and stamps the revert time for the
// post-revert cooldown. With no promotion overlay present (a flag-service
// restart dropped it) real games already resolve the pre-promotion default: the
// revert writes nothing, stamps the cooldown and succeeds (PR #1417 review).
//
// COMPARE-AND-DELETE (#1877, closes the #1753 window): the DELETE carries
// ?if_writer=promotion, so the flag service clears the real overlay only while
// its writer is still "promotion", atomically under its lock. Another writer's
// overlay set between the read and the delete is refused with 412
// writer_mismatch and left in place; the revert is then dropped like the
// "no promotion overlay" case (logged once, cooldown stamped, nil returned).
// The kill switch uses the same path: every overlay this agent writes carries
// writer "promotion", so the precondition never blocks clearing one of them.
func (w *serviceRealDefaultWriter) RevertRealDefault(ctx context.Context, flagKey string) error {
	return w.revert(ctx, flagKey, false)
}

// RevertRealDefaultUnlessRealGame is RevertRealDefault with the guarded DELETE
// (?unless_real_game=true, #2236): for a flag marked real_write:
// no_live_real_game the flag service refuses it with 409 real_game_live while a
// non-shadow game is registered, checked under its registration lock. The watch
// and re-watch reverts of hold keys use it; the kill switch never does.
func (w *serviceRealDefaultWriter) RevertRealDefaultUnlessRealGame(ctx context.Context, flagKey string) error {
	return w.revert(ctx, flagKey, true)
}

// ErrSupersededByAnotherWriter is returned by a revert whose DELETE the flag service
// refused with 412 writer_mismatch (#1877): nothing was cleared, the cooldown is
// stamped, and the revert counts as done. Callers treat it as success; the kill
// switch adds SupersededReasonSuffix to the journaled reason so "the agent cleared
// it" and "another writer owns it" can be told apart.
var ErrSupersededByAnotherWriter = errors.New("real overlay superseded by another writer (writer_mismatch); nothing removed")

// SupersededReasonSuffix is appended to the kill-switch journal reason when the
// revert was dropped on a writer_mismatch.
const SupersededReasonSuffix = "; superseded by another writer"

func (w *serviceRealDefaultWriter) revert(ctx context.Context, flagKey string, unlessRealGame bool) error {
	w.mu.Lock()
	defer w.mu.Unlock()

	view, err := w.svc.GetFlag(ctx, gameFlagSet, flagKey)
	if err != nil {
		return fmt.Errorf("read %s/%s before revert: %w", gameFlagSet, flagKey, err)
	}
	if view.RealOverlay == nil || view.RealOverlay.Writer != promotionWriter {
		// No overlay of ours: a flag-service restart dropped it, or another writer's
		// real overlay replaced it. That overlay is not the promotion's to remove
		// (#1704): nothing is deleted.
		w.log.Warn("code_improvement revert: no promotion overlay; nothing to remove",
			"flag", flagKey, "real_overlay_writer", realOverlayWriter(view))
		w.stampRevert(flagKey)
		return nil
	}
	query := url.Values{"game_kind": {"real"}, "writer": {promotionWriter}, "if_writer": {promotionWriter}}
	if unlessRealGame {
		query.Set("unless_real_game", "true")
	}
	if err := w.svc.Delete(ctx, gameFlagSet, flagKey, query); err != nil {
		if flagservice.IsReason(err, flagservice.ReasonWriterMismatch) {
			// Another writer replaced the overlay after the read (#1877): it owns
			// the real default now, so the revert is dropped, not retried.
			w.log.Warn("code_improvement revert dropped: another writer owns the real overlay now (writer_mismatch); nothing removed",
				"flag", flagKey)
			w.stampRevert(flagKey)
			return ErrSupersededByAnotherWriter
		}
		return fmt.Errorf("revert %s/%s: %w", gameFlagSet, flagKey, err)
	}
	// 204 also when the overlay vanished between the read and the DELETE (a
	// flag-service restart): the promotion's overlay is gone either way.
	w.log.Warn("code_improvement.autonomous REVERTED: the promotion's real-default overlay is gone (cleared now, or already absent); real games resolve the default beneath it (#1065)", "flag", flagKey)
	w.stampRevert(flagKey)
	return nil
}

// PromotedFlags lists the game flags whose real overlay a promotion wrote
// (writer "promotion"), read from the flag service, so the kill switch reverts
// them even after an agent restart (#1704). Overlays of other writers are never
// listed.
func (w *serviceRealDefaultWriter) PromotedFlags(ctx context.Context) ([]string, error) {
	set, err := w.svc.GetSet(ctx, gameFlagSet)
	if err != nil {
		return nil, fmt.Errorf("list %s overlays: %w", gameFlagSet, err)
	}
	var out []string
	for _, f := range set.Flags {
		if f.RealOverlay != nil && f.RealOverlay.Writer == promotionWriter {
			out = append(out, f.Flag)
		}
	}
	return out, nil
}

// PromotionOverlay reads the value of flagKey's real overlay when a promotion wrote
// it (ok false otherwise), so a revert restored after a restart deletes only its
// own value (UC-043 A17).
func (w *serviceRealDefaultWriter) PromotionOverlay(ctx context.Context, flagKey string) (json.RawMessage, bool, error) {
	view, err := w.svc.GetFlag(ctx, gameFlagSet, flagKey)
	if err != nil {
		return nil, false, err
	}
	if view.RealOverlay == nil || view.RealOverlay.Writer != promotionWriter {
		return nil, false, nil
	}
	return view.RealOverlay.Value, true, nil
}

// realOverlayWriter is the writer of view's real overlay, or "" when none.
func realOverlayWriter(view flagservice.FlagView) string {
	if view.RealOverlay == nil {
		return ""
	}
	return view.RealOverlay.Writer
}

// stampRevert records the revert time (#1076) so the cooldown survives a
// restart. A failure is logged, NOT returned: the revert itself succeeded.
func (w *serviceRealDefaultWriter) stampRevert(flagKey string) {
	if w.snaps != nil {
		if err := w.snaps.markReverted(flagKey, w.now()); err != nil {
			w.log.Warn("code_improvement real-default revert succeeded but the durable cooldown stamp failed; the cooldown may not survive a restart (#1076)",
				"flag", flagKey, "error", err)
		}
	}
}

func (w *serviceRealDefaultWriter) now() time.Time {
	if w.clock != nil {
		return w.clock()
	}
	return time.Now()
}

// jsonKind classifies a raw JSON value as number/string/bool/array/object, or ""
// for null/unparseable (kindless — imposes no type constraint). All JSON numerics
// are one "number" kind, so an int promoted value never mismatches a float default.
func jsonKind(raw json.RawMessage) string {
	var v any
	if json.Unmarshal(raw, &v) != nil {
		return ""
	}
	switch v.(type) {
	case float64:
		return "number"
	case string:
		return "string"
	case bool:
		return "bool"
	case []any:
		return "array"
	case map[string]any:
		return "object"
	default:
		return "" // null
	}
}
