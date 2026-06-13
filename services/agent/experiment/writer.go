package experiment

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"sync"
	"syscall"

	"github.com/joustmania/agent/flagset"
)

// DefaultGamePath is the in-container game flag file path. Override with
// GAME_FLAG_PATH. The Writer is HARDCODED to the game flagset — there is NO
// constructor that accepts an arbitrary path the agent could redirect to
// agent.json (that file is additionally `:ro`-mounted as the kernel backstop).
// NewWriter takes a path only so tests can point at a t.TempDir copy; production
// uses NewWriterFromEnv → GAME_FLAG_PATH → this default.
const DefaultGamePath = "/etc/flagd/game.json"

// Writer applies a Proposal to the game flag file by adding a new experimental
// variant and a shadow-scoped (game_kind != "real") targeting rule. It MIRRORS
// services/agent/actions/writer.go (#730): order-preserving read-modify-write
// in place under a process mutex, so untouched flags round-trip byte-for-byte
// and the write avoids temp+rename (rename triggers EBUSY on the docker bind
// mount flagd is watching; same semantics as lib/flag_config_writer.py).
//
// The Writer never validates the invariant itself — that is the Gate's job, run
// BEFORE Apply. Apply assumes the Gate has accepted the proposal; callers that
// skip the Gate are a bug (the Gate is the only sanctioned entry point — see
// Gate.Review).
type Writer struct {
	path string
	log  *slog.Logger

	mu sync.Mutex
}

// NewWriter builds a Writer for the game flag file at path. path "" uses
// DefaultGamePath. log nil uses slog.Default().
func NewWriter(path string, log *slog.Logger) *Writer {
	if path == "" {
		path = DefaultGamePath
	}
	if log == nil {
		log = slog.Default()
	}
	return &Writer{path: path, log: log}
}

// NewWriterFromEnv builds a Writer for the active game flag file via the single
// source of truth (flagset.ResolvePath, issue #959): the GAME_FLAG_PATH override
// still wins, otherwise the active game flag file is the plain join
// $FLAGD_FLAG_DIR/game.json. In production (flag dir /etc/flagd) this is
// DefaultGamePath, unchanged.
func NewWriterFromEnv(log *slog.Logger) *Writer {
	return NewWriter(flagset.ResolvePath("GAME_FLAG_PATH", "game"), log)
}

// Path returns the file the Writer operates on (the Gate reads the same file to
// validate before/after; exposing the path keeps them pointed at one target).
func (w *Writer) Path() string { return w.path }

// Apply writes the proposal's shadow-scoped experiment into the game flag file.
// It ADDS ExperimentVariant=ExperimentalValue (never mutating an existing
// variant or the defaultVariant) and sets a targeting rule selecting that
// variant for shadow games only. Safe for concurrent use (the agent is the sole
// writer of the game flag file).
func (w *Writer) Apply(p Proposal) error {
	return w.edit(func(doc *document) error { return applyProposal(doc, p) })
}

// edit runs one read-modify-write cycle under the process mutex: read the file,
// apply fn to the order-preserving document, and write it back in place.
func (w *Writer) edit(fn func(*document) error) error {
	w.mu.Lock()
	defer w.mu.Unlock()

	raw, err := os.ReadFile(w.path)
	if err != nil {
		return fmt.Errorf("read game flag file %q: %w", w.path, err)
	}
	doc, err := parseDoc(raw)
	if err != nil {
		return err
	}
	if err := fn(doc); err != nil {
		return err
	}
	out, err := doc.marshal()
	if err != nil {
		return err
	}
	return w.writeInPlace(out)
}

// applyProposal mutates the in-memory document: add the experimental variant and
// the shadow-scoped targeting rule for p.FlagKey. It is the pure core of Apply
// (no I/O) so the Gate can run it against a copy for its before/after eval.
func applyProposal(doc *document, p Proposal) error {
	f, err := doc.flag(p.FlagKey)
	if err != nil {
		return err
	}

	// Add (or overwrite) the single reserved experimental variant. We MERGE into
	// the existing variants map so every game-authored variant is preserved
	// untouched — the Gate's "no existing variant changed" check depends on this.
	variants := map[string]json.RawMessage{}
	if _, err := f.get("variants", &variants); err != nil {
		return err
	}
	valRaw, err := json.Marshal(p.ExperimentalValue)
	if err != nil {
		return fmt.Errorf("marshal experimental value for %q: %w", p.FlagKey, err)
	}
	variants[ExperimentVariant] = valRaw
	if err := f.set("variants", variants); err != nil {
		return err
	}

	// Compose the shadow branch over any pre-existing targeting so real games
	// keep their prior behaviour. We read the targeting that was present BEFORE
	// we touched it; on re-experimentation the prior block already starts with
	// our shadow branch, so we strip it first to avoid nesting it indefinitely.
	existing, _ := f.raw("targeting")
	existing = stripShadowBranch(existing)
	targeting, err := buildShadowTargeting(existing)
	if err != nil {
		return err
	}
	f.setRaw("targeting", targeting)

	return doc.putFlag(p.FlagKey, f)
}

// stripShadowBranch returns the ELSE branch of a targeting block previously
// produced by buildShadowTargeting, or the block unchanged if it is not one of
// ours. This makes re-experimenting on the same flag idempotent: we re-wrap the
// original (pre-experiment) targeting instead of stacking shadow branches.
func stripShadowBranch(targeting json.RawMessage) json.RawMessage {
	if len(targeting) == 0 {
		return nil
	}
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(targeting, &obj); err != nil {
		return targeting
	}
	ifRaw, ok := obj["if"]
	if !ok || len(obj) != 1 {
		return targeting
	}
	var arms []json.RawMessage
	if err := json.Unmarshal(ifRaw, &arms); err != nil || len(arms) != 3 {
		return targeting
	}
	// Confirm arm[0] is our shadow condition and arm[1] selects our variant.
	if !isShadowCondition(arms[0]) || !isExperimentVariant(arms[1]) {
		return targeting
	}
	if string(arms[2]) == "null" {
		return nil
	}
	return arms[2]
}

// isShadowCondition reports whether raw is exactly {"!=":[{"var":"game_kind"},"real"]}.
func isShadowCondition(raw json.RawMessage) bool {
	want, err := json.Marshal(map[string]any{
		"!=": []any{map[string]any{"var": GameKindVar}, GameKindReal},
	})
	if err != nil {
		return false
	}
	return jsonEqual(raw, want)
}

// isExperimentVariant reports whether raw is the JSON string ExperimentVariant.
func isExperimentVariant(raw json.RawMessage) bool {
	var s string
	if err := json.Unmarshal(raw, &s); err != nil {
		return false
	}
	return s == ExperimentVariant
}

// writeInPlace overwrites the file at the same path WITHOUT temp+rename. Rename
// over a docker bind mount that flagd is watching fails with EBUSY; an in-place
// truncate+write is the proven admin-mode pattern (actions/writer.go,
// lib/flag_config_writer.py).
func (w *Writer) writeInPlace(data []byte) error {
	// O_NOFOLLOW: refuse to write THROUGH a symlink. The agent's write surface is
	// pinned to the game flagset; without this, a symlink at w.path (e.g. game.json
	// -> agent.json) would let a write escape into the agent's own governance file.
	// The Gate's isGameFlagPath rejects symlinks up front; O_NOFOLLOW is the kernel
	// backstop that holds even against a TOCTOU swap between the check and this open.
	f, err := os.OpenFile(w.path, os.O_WRONLY|os.O_TRUNC|os.O_CREATE|syscall.O_NOFOLLOW, 0o644)
	if err != nil {
		return fmt.Errorf("open game flag file for write: %w", err)
	}
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		return fmt.Errorf("write game flag file: %w", err)
	}
	if err := f.Close(); err != nil {
		return fmt.Errorf("close game flag file: %w", err)
	}
	return nil
}
