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

// Strip removes one experiment's shadow-scoped targeting from the game flag file
// — the teardown inverse of Apply (design §10). It drops the experiment's
// chained-if branch via stripShadowBranch (un-nesting only THIS experiment's
// experiment_id condition, leaving K-1 other experiments' branches and the
// original pre-experiment targeting verbatim) AND removes its reserved
// agent_experiment__<id> variant. A missing branch/variant is a no-op, so Strip
// is idempotent — the registry calls it on every terminal status, possibly more
// than once.
//
// Invariant-safe by construction: removing a shadow branch only ever affects
// shadow games (the same argument as buildShadowTargeting's structural proof), so
// the real-resolution invariant the Gate protects is untouched. Teardown does not
// go through the Gate — the Gate validates ADDING an experiment; removing one can
// only ever restore a strictly more-real-protective rule.
//
// An unknown flag (already removed from game.json, or never present) is a no-op,
// not an error: teardown must not fail because the surface it scoped is gone.
func (w *Writer) Strip(flagKey, experimentID string) error {
	return w.edit(func(doc *document) error { return stripProposal(doc, flagKey, experimentID) })
}

// stripProposal mutates the in-memory document to remove experimentID's branch +
// variant from flagKey. It is the pure core of Strip (no I/O). A missing flag or
// a flag without this experiment's branch/variant leaves the document unchanged.
func stripProposal(doc *document, flagKey, experimentID string) error {
	if !doc.hasFlag(flagKey) {
		return nil // already gone — idempotent teardown.
	}
	f, err := doc.flag(flagKey)
	if err != nil {
		return err
	}

	if existing, ok := f.raw("targeting"); ok {
		stripped := stripShadowBranch(experimentID, existing)
		if len(stripped) == 0 || string(stripped) == "null" {
			// The experiment's branch was the only/outermost targeting; drop the key
			// so the flag returns to "no targeting" (defaultVariant resolves), the
			// pre-experiment state for a flag that had none.
			f.del("targeting")
		} else {
			f.setRaw("targeting", stripped)
		}
	}

	// Remove this experiment's reserved variant. del is a no-op if it is absent
	// (idempotent). Other experiments' variants and game-authored variants are
	// untouched (Strip only ever names experimentVariantName(experimentID)).
	variants := map[string]json.RawMessage{}
	if _, err := f.get("variants", &variants); err != nil {
		return err
	}
	if _, ok := variants[experimentVariantName(experimentID)]; ok {
		delete(variants, experimentVariantName(experimentID))
		if err := f.set("variants", variants); err != nil {
			return err
		}
	}

	return doc.putFlag(flagKey, f)
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
	variants[experimentVariantName(p.ExperimentID)] = valRaw
	if err := f.set("variants", variants); err != nil {
		return err
	}

	// Compose THIS proposal's branch over the pre-existing targeting so real games
	// (and OTHER experiments' games) keep their prior behaviour. We read the
	// targeting present BEFORE we touched it and strip only the branch matching
	// THIS proposal's experiment id — so re-experimenting the same id is idempotent
	// (replace, don't stack) while DIFFERENT experiments accumulate as a chain of
	// ifs (each keyed on its own experiment_id). The remaining chain becomes the
	// else of the freshly-built outermost branch.
	existing, _ := f.raw("targeting")
	existing = stripShadowBranch(p.ExperimentID, existing)
	targeting, err := buildShadowTargeting(p.ExperimentID, existing)
	if err != nil {
		return err
	}
	f.setRaw("targeting", targeting)

	return doc.putFlag(p.FlagKey, f)
}

// stripShadowBranch removes the chained-if branch produced by a PRIOR write for
// experimentID, returning the targeting with only that branch elided (its else
// spliced in where it sat). Other experiments' branches and the original
// (pre-experiment) targeting are preserved verbatim. A targeting that is not one
// of ours, or carries no branch for experimentID, is returned unchanged. This
// keeps re-experimenting one id idempotent (replace, not stack) without
// disturbing the K-1 other experiments on the same flag.
func stripShadowBranch(experimentID string, targeting json.RawMessage) json.RawMessage {
	if len(targeting) == 0 {
		return nil
	}
	arms, ok := decodeIfArms(targeting)
	if !ok {
		return targeting // not a {"if":[cond,then,else]} — not ours, leave it.
	}
	// Is THIS the branch for experimentID? cond must match the proposal's shadow
	// condition AND then must select that proposal's variant.
	if conditionMatchesExperiment(arms[0], experimentID) && variantNameIs(arms[1], experimentVariantName(experimentID)) {
		// Drop this branch: its else becomes the result (recursing in case a stale
		// duplicate of the same id was somehow chained deeper — defensive).
		stripped := stripShadowBranch(experimentID, arms[2])
		if len(stripped) == 0 || string(stripped) == "null" {
			return nil
		}
		return stripped
	}
	// A different experiment's (or the legacy) branch: keep it, recurse into the
	// else so a deeper branch for experimentID is still found and removed.
	newElse := stripShadowBranch(experimentID, arms[2])
	if jsonEqual(orNull(newElse), orNull(arms[2])) {
		return targeting // nothing changed below — return original bytes verbatim.
	}
	return rebuildIf(arms[0], arms[1], orNull(newElse))
}

// decodeIfArms returns the three arms of a single-key {"if":[cond,then,else]}
// block, or (nil,false) if raw is not exactly that shape.
func decodeIfArms(raw json.RawMessage) ([]json.RawMessage, bool) {
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(raw, &obj); err != nil || len(obj) != 1 {
		return nil, false
	}
	ifRaw, ok := obj["if"]
	if !ok {
		return nil, false
	}
	var arms []json.RawMessage
	if err := json.Unmarshal(ifRaw, &arms); err != nil || len(arms) != 3 {
		return nil, false
	}
	return arms, true
}

// conditionMatchesExperiment reports whether cond is the shadow condition for
// experimentID — the legacy game_kind!=real condition when id is empty, or the
// experiment_id==id AND arm==experimental condition otherwise.
func conditionMatchesExperiment(cond json.RawMessage, experimentID string) bool {
	if experimentID == "" {
		return isShadowCondition(cond)
	}
	got, ok := experimentIDFromCondition(cond)
	return ok && got == experimentID
}

// variantNameIs reports whether raw is the JSON string equal to want.
func variantNameIs(raw json.RawMessage, want string) bool {
	var s string
	if err := json.Unmarshal(raw, &s); err != nil {
		return false
	}
	return s == want
}

// rebuildIf reassembles {"if":[cond,then,else]} from raw arms, preserving each
// arm's bytes (so untouched sibling branches round-trip).
func rebuildIf(cond, then, elseBranch json.RawMessage) json.RawMessage {
	var buf []byte
	buf = append(buf, []byte(`{"if":[`)...)
	buf = append(buf, cond...)
	buf = append(buf, ',')
	buf = append(buf, then...)
	buf = append(buf, ',')
	buf = append(buf, elseBranch...)
	buf = append(buf, []byte(`]}`)...)
	return json.RawMessage(buf)
}

// orNull returns raw, or the JSON literal null when raw is empty.
func orNull(raw json.RawMessage) json.RawMessage {
	if len(raw) == 0 {
		return json.RawMessage("null")
	}
	return raw
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
