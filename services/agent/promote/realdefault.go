package promote

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"sync"
	"syscall"
	"time"
)

// realdefault.go is the autonomous mode's REAL defaultVariant mutation — the
// SEPARATE, higher-privilege write the safety rail isolates from the shadow
// experiment.Writer.
//
// WHY A SEPARATE WRITER (the safety rail's core distinction): experiment.Writer is
// structurally shadow-only — it ONLY ever emits a `game_kind != "real"` targeting
// rule and ADDS a variant, so a real (`game_kind="real"`) evaluation always falls
// through to the untouched defaultVariant. That writer CANNOT change a real
// resolution by construction (the Gate enforces the invariant). Autonomous
// promotion deliberately DOES change what real games resolve, so it must be a
// DIFFERENT, explicitly-gated mechanism — never the shadow Writer "tricked" into a
// real change. This writer changes the flag's defaultVariant (and the variant's
// value), which is exactly the real-affecting write the shadow path forbids.
//
// It is built ONLY by NewRealDefaultWriterFromEnv behind the env gate, and it is
// invoked only on the autonomous path, which is additionally behind the agent
// kill-switch (Config.Enabled, checked in promoteAutonomous). Two independent gates
// (env + kill-switch) guard every real-default mutation.

// fileRealDefaultWriter writes the REAL defaultVariant of a flag in a flagd JSON
// file in place. It mirrors the in-place write discipline of experiment.Writer
// (O_NOFOLLOW, no temp+rename — EBUSY on the bind mount flagd watches) but writes
// the REAL default, not a shadow rule.
//
// It also implements RealDefaultReverter: SetRealDefault SNAPSHOTS the pre-write
// {defaultVariant name + its concrete value} at write time, and RevertRealDefault
// RESTORES that exact prior state. Snapshotting the VALUE (not just the variant
// name) is the safety-critical property (#1065): pointing defaultVariant back at a
// remembered NAME is unsafe if an intervening promotion overwrote that variant's
// value (e.g. a second autonomous write clobbers variants["promoted"]). Restoring
// the captured value reconstructs exactly what real games resolved before the
// promotion, regardless of what happened to the slot in between.
type fileRealDefaultWriter struct {
	path string
	log  *slog.Logger
	mu   sync.Mutex
	// snapshots holds the pre-promotion state per flagKey, captured inside
	// SetRealDefault before the mutation. RevertRealDefault restores from it. Keyed by
	// flagKey so concurrent promotions of different flags each remember their own prior
	// state. A re-promotion of the SAME flag is intentionally NOT re-snapshotted while a
	// snapshot is already held (see SetRealDefault) so a revert always rolls back to the
	// state BEFORE the agent first touched the flag, never to an intermediate
	// agent-written value.
	snapshots map[string]defaultSnapshot
	// snaps is the DURABLE backing for snapshots + the post-revert cooldown stamps
	// (#1076). Without it, a restart between an autonomous apply and a later revert
	// strands the regression (RevertRealDefault finds no snapshot in memory). It is
	// loaded on construction and written through on first-touch snapshot + on
	// successful revert. nil only when the persistence dir was unreadable at
	// construction (logged); the in-memory map then still works for the live process.
	snaps *snapStore
	// clock is an injectable time source for the persisted post-revert cooldown stamp
	// (#1076). nil → time.Now (production). Tests set it to drive the stamp.
	clock func() time.Time
}

// PersistedRevertTimes exposes the durable post-revert cooldown stamps so the
// Promoter can seed its in-memory lastRevert map after a restart (#1076 item 3),
// preserving post-revert thrash protection across the restart boundary. Empty when
// no flag has been reverted or the store is unavailable.
func (w *fileRealDefaultWriter) PersistedRevertTimes() map[string]time.Time {
	if w.snaps == nil {
		return nil
	}
	return w.snaps.loadLastRevert()
}

// defaultSnapshot is the exact pre-promotion real-default state for one flag: the
// defaultVariant NAME that was in effect and the concrete VALUE that name resolved
// to. RevertRealDefault writes both back, so even if the named variant's value was
// later overwritten the prior real resolution is reconstructed verbatim.
type defaultSnapshot struct {
	// hadDefault is false when the flag had NO defaultVariant before the promotion (a
	// malformed/edge flag); revert then removes the agent-introduced default rather
	// than inventing one.
	hadDefault bool
	// variant is the defaultVariant name in effect pre-promotion.
	variant string
	// value is the concrete JSON value that `variant` resolved to pre-promotion. Stored
	// raw so it round-trips byte-for-byte. nil when the variant had no value entry.
	value json.RawMessage
}

// NewRealDefaultWriterFromEnv returns the REAL-default writer IFF the autonomous
// env gate is satisfied, else nil (autonomous then degrades to a recorded no-op —
// promoteAutonomous handles the nil). This is the ONLY constructor for a
// real-default writer; a test cannot build one (it would have to set the env gate
// AND supply a path), so no test mutates a real default.
//
// Gate conditions:
//   - AGENT_CODE_IMPROVEMENT_ENABLED == "true"        (explicit opt-in)
//   - AGENT_AUTONOMOUS_ENABLED == "true"               (a SECOND opt-in specific to
//     the real-default mutation — autonomous is the most privileged path, so it
//     requires its own switch beyond the shared code-improvement gate)
//   - AGENT_REAL_DEFAULT_FLAG_PATH points at a flagd file
//
// Returns (nil, nil) when not gated (the fail-closed default), (nil, err) on a
// gated-but-misconfigured path.
func NewRealDefaultWriterFromEnv(log *slog.Logger) (*fileRealDefaultWriter, error) {
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
	path := strings.TrimSpace(os.Getenv("AGENT_REAL_DEFAULT_FLAG_PATH"))
	if path == "" {
		log.Info("code_improvement real-default writer NOT built: AGENT_REAL_DEFAULT_FLAG_PATH unset (fail-closed)")
		return nil, nil
	}
	if _, err := os.Stat(path); err != nil {
		return nil, fmt.Errorf("AGENT_REAL_DEFAULT_FLAG_PATH=%q not readable: %w", path, err)
	}
	// Load the DURABLE snapshot store (#1076) from the persisted volume so a revert
	// after an agent restart still finds the pre-promotion snapshot. A missing file
	// is a clean first run; a corrupt/unreadable file is logged and the agent starts
	// with an empty in-memory map rather than refusing to boot — the durable copy is
	// a safety net, not a hard prerequisite for the (default-off) autonomous path.
	dir := snapStoreDir()
	snaps, err := newSnapStore(dir)
	if err != nil {
		log.Warn("code_improvement real-default snapshot store could not be loaded; starting with empty in-memory snapshots (a revert of a PRE-restart promotion may be unavailable until re-snapshotted)",
			"dir", dir, "error", err)
	}
	loaded := snaps.loadSnapshots()
	if len(loaded) > 0 {
		log.Warn("code_improvement real-default reverter restored pre-promotion snapshots from durable store (#1076): a post-restart revert can roll these back",
			"flags", len(loaded))
	}
	log.Warn("code_improvement AUTONOMOUS real-default writer ENABLED (#936): autonomous promotions WILL change the REAL defaultVariant for live players",
		"path", path)
	return &fileRealDefaultWriter{path: path, log: log, snapshots: loaded, snaps: snaps}, nil
}

// SetRealDefault changes the REAL defaultVariant for flagKey to value: it ensures a
// variant equal to value exists (adding a reserved "promoted" variant if needed)
// and points defaultVariant at it, so a `game_kind="real"` evaluation now resolves
// value. This is the one sanctioned real-player-affecting write.
func (w *fileRealDefaultWriter) SetRealDefault(_ context.Context, flagKey string, value any) error {
	w.mu.Lock()
	defer w.mu.Unlock()

	raw, err := os.ReadFile(w.path)
	if err != nil {
		return fmt.Errorf("read flag file %q: %w", w.path, err)
	}
	var doc struct {
		Metadata json.RawMessage            `json:"metadata,omitempty"`
		Flags    map[string]json.RawMessage `json:"flags"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return fmt.Errorf("parse flag file: %w", err)
	}
	flagRaw, ok := doc.Flags[flagKey]
	if !ok {
		return fmt.Errorf("flag %q not present", flagKey)
	}
	var flag map[string]json.RawMessage
	if err := json.Unmarshal(flagRaw, &flag); err != nil {
		return fmt.Errorf("parse flag %q: %w", flagKey, err)
	}

	var variants map[string]json.RawMessage
	if vr, ok := flag["variants"]; ok {
		if err := json.Unmarshal(vr, &variants); err != nil {
			return fmt.Errorf("parse variants of %q: %w", flagKey, err)
		}
	} else {
		variants = map[string]json.RawMessage{}
	}

	valRaw, err := json.Marshal(value)
	if err != nil {
		return fmt.Errorf("marshal value: %w", err)
	}
	// Defense-in-depth (#936 review note 2): this is the LAST line before a write
	// real players resolve, so refuse a value whose JSON kind differs from the flag's
	// CURRENT defaultVariant value. A mistyped value (e.g. a string on a numeric flag)
	// would make flagd TYPE_MISMATCH at resolution and fall back to ITS default —
	// degrading the flag for everyone. The experiment value reaching here is already
	// type-correct upstream (the #955 gate), but this guards independently, mirroring
	// that gate for the real-default path.
	if curDef, ok := flag["defaultVariant"]; ok {
		var curName string
		if json.Unmarshal(curDef, &curName) == nil {
			if curRaw, present := variants[curName]; present {
				if ck, nk := jsonKind(curRaw), jsonKind(valRaw); ck != "" && nk != "" && ck != nk {
					return fmt.Errorf("refusing real-default write for %q: value kind %q != current default %q kind %q", flagKey, nk, curName, ck)
				}
			}
		}
	}
	// SNAPSHOT the pre-promotion state (#1065) BEFORE mutating, so RevertRealDefault
	// can restore the EXACT prior {defaultVariant name + value}. We snapshot only the
	// FIRST time we touch a flag (no snapshot held yet): a re-promotion of the same
	// flag must still revert to the state before the agent EVER touched it, not to an
	// intermediate agent-written value. The snapshot captures the VALUE, not just the
	// name, so a later promotion overwriting the named slot cannot corrupt the revert.
	if w.snapshots == nil {
		w.snapshots = map[string]defaultSnapshot{}
	}
	if _, held := w.snapshots[flagKey]; !held {
		snap := defaultSnapshot{}
		if curDef, ok := flag["defaultVariant"]; ok {
			var curName string
			if json.Unmarshal(curDef, &curName) == nil {
				snap.hadDefault = true
				snap.variant = curName
				if curRaw, present := variants[curName]; present {
					// Copy the raw bytes so a later in-place mutation of `variants` cannot
					// reach into the snapshot.
					snap.value = append(json.RawMessage(nil), curRaw...)
				}
			}
		}
		w.snapshots[flagKey] = snap
		// Persist the snapshot DURABLY before the mutating write (#1076), so a revert
		// after an agent restart can still roll back. Surface a persistence failure as
		// an error and ABORT the promotion: applying a real-default change whose revert
		// snapshot did not reach disk would reintroduce the exact strand-on-restart bug
		// this issue closes (the flag file would hold the promoted value with no durable
		// way to undo it after a restart). Drop the in-memory snapshot too so state stays
		// consistent with the un-mutated file.
		if w.snaps != nil {
			if err := w.snaps.putSnapshot(flagKey, snap); err != nil {
				delete(w.snapshots, flagKey)
				return fmt.Errorf("persist pre-promotion snapshot for %q (refusing to promote without a durable revert path): %w", flagKey, err)
			}
		}
	}

	// Point at an existing variant if one already equals value (keeps the file tidy),
	// else add a reserved "promoted" variant. Either way defaultVariant moves.
	variantName := "promoted"
	for name, vr := range variants {
		if jsonEqual(vr, valRaw) {
			variantName = name
			break
		}
	}
	variants[variantName] = valRaw

	vrOut, _ := json.Marshal(variants)
	flag["variants"] = vrOut
	dvOut, _ := json.Marshal(variantName)
	flag["defaultVariant"] = dvOut

	flagOut, _ := json.Marshal(flag)
	doc.Flags[flagKey] = flagOut
	out, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal flag file: %w", err)
	}

	w.log.Warn("code_improvement.autonomous changing REAL defaultVariant (#936)",
		"flag", flagKey, "variant", variantName)
	return w.writeInPlace(append(out, '\n'))
}

// RevertRealDefault restores the EXACT pre-promotion real default for flagKey from
// the snapshot SetRealDefault captured at write time (#1065). It rewrites both the
// defaultVariant name AND the concrete value that name resolved to before the
// promotion, so even if an intervening write overwrote the slot, real games resolve
// the prior value verbatim after the revert. After a successful revert the snapshot
// is dropped (a fresh promotion re-snapshots). It is the concrete RealDefaultReverter
// the autonomous safety net rolls back through.
func (w *fileRealDefaultWriter) RevertRealDefault(_ context.Context, flagKey string) error {
	w.mu.Lock()
	defer w.mu.Unlock()

	snap, held := w.snapshots[flagKey]
	if !held {
		// Nothing to revert to: the agent never wrote this flag (or already reverted).
		// Refuse rather than guess — a revert with no captured prior state could only
		// fabricate one, which is exactly the unsafe behavior #1065 calls out.
		return fmt.Errorf("revert real default %q: no pre-promotion snapshot captured", flagKey)
	}

	raw, err := os.ReadFile(w.path)
	if err != nil {
		return fmt.Errorf("read flag file %q: %w", w.path, err)
	}
	var doc struct {
		Metadata json.RawMessage            `json:"metadata,omitempty"`
		Flags    map[string]json.RawMessage `json:"flags"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return fmt.Errorf("parse flag file: %w", err)
	}
	flagRaw, ok := doc.Flags[flagKey]
	if !ok {
		return fmt.Errorf("flag %q not present", flagKey)
	}
	var flag map[string]json.RawMessage
	if err := json.Unmarshal(flagRaw, &flag); err != nil {
		return fmt.Errorf("parse flag %q: %w", flagKey, err)
	}

	if !snap.hadDefault {
		// The flag had no defaultVariant before the agent touched it; remove the one the
		// promotion introduced rather than inventing a default.
		delete(flag, "defaultVariant")
	} else {
		var variants map[string]json.RawMessage
		if vr, ok := flag["variants"]; ok {
			if err := json.Unmarshal(vr, &variants); err != nil {
				return fmt.Errorf("parse variants of %q: %w", flagKey, err)
			}
		} else {
			variants = map[string]json.RawMessage{}
		}
		// Restore the EXACT prior value into the prior variant name (it may have been
		// overwritten by an intervening promotion), then point defaultVariant back at it.
		if snap.value != nil {
			variants[snap.variant] = append(json.RawMessage(nil), snap.value...)
		}
		vrOut, _ := json.Marshal(variants)
		flag["variants"] = vrOut
		dvOut, _ := json.Marshal(snap.variant)
		flag["defaultVariant"] = dvOut
	}

	flagOut, _ := json.Marshal(flag)
	doc.Flags[flagKey] = flagOut
	out, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal flag file: %w", err)
	}

	w.log.Warn("code_improvement.autonomous REVERTING REAL defaultVariant to pre-promotion state (#1065)",
		"flag", flagKey, "variant", snap.variant)
	if err := w.writeInPlace(append(out, '\n')); err != nil {
		return err
	}
	// Drop the snapshot so a subsequent promotion re-captures fresh prior state.
	delete(w.snapshots, flagKey)
	// Durably drop the snapshot AND stamp the revert time (#1076), so (a) a restart
	// does not resurrect a stale snapshot for an already-reverted flag, and (b) the
	// post-revert cooldown (#1065 item 3) survives a restart — the Promoter seeds
	// lastRevert from this stamp on boot. Persistence failure here is logged, NOT
	// returned: the revert itself ALREADY succeeded (the file holds the prior value),
	// so failing the call would wrongly imply the rollback did not happen. The only
	// cost of a lost stamp is a missed cooldown across a restart that coincides with a
	// store-write failure — strictly less dangerous than a stranded regression.
	if w.snaps != nil {
		if err := w.snaps.markReverted(flagKey, w.now()); err != nil {
			w.log.Warn("code_improvement real-default revert succeeded but durable snapshot/cooldown update failed; cooldown may not survive a restart (#1076)",
				"flag", flagKey, "error", err)
		}
	}
	return nil
}

// now returns the writer's time source. Production uses time.Now; tests can swap it
// to drive the persisted cooldown stamp deterministically.
func (w *fileRealDefaultWriter) now() time.Time {
	if w.clock != nil {
		return w.clock()
	}
	return time.Now()
}

// writeInPlace overwrites the file at the same path WITHOUT temp+rename (EBUSY on
// the flagd bind mount) and refuses to follow a symlink (O_NOFOLLOW), mirroring
// experiment.Writer.writeInPlace.
func (w *fileRealDefaultWriter) writeInPlace(data []byte) error {
	f, err := os.OpenFile(w.path, os.O_WRONLY|os.O_TRUNC|os.O_CREATE|syscall.O_NOFOLLOW, 0o644)
	if err != nil {
		return fmt.Errorf("open flag file for write: %w", err)
	}
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		return fmt.Errorf("write flag file: %w", err)
	}
	if err := f.Close(); err != nil {
		return fmt.Errorf("close flag file: %w", err)
	}
	return nil
}

// jsonEqual reports whether two raw JSON messages are semantically equal (so a
// re-promotion to an already-present variant value reuses it).
func jsonEqual(a, b json.RawMessage) bool {
	var av, bv any
	if json.Unmarshal(a, &av) != nil || json.Unmarshal(b, &bv) != nil {
		return false
	}
	am, _ := json.Marshal(av)
	bm, _ := json.Marshal(bv)
	return string(am) == string(bm)
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
