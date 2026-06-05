package actions

import (
	"fmt"
	"log/slog"
	"os"
	"sync"
)

// RolloutWriter is the agent's writer for the flagd `rollout` flag file
// (services/flagd/rollout.json). It is the infrastructure-domain parallel to
// Writer: the progressive-rollout controller (#734) expands the Bluetooth
// backend controller-by-controller by flipping current_controller_count's
// defaultVariant along the stage ladder, and PR F rolls it back the same way.
//
// It reuses the same order-preserving read-modify-write-in-place machinery as
// Writer (document.go / writeInPlace): untouched flags round-trip byte-for-byte,
// and the write avoids temp+rename so it does not hit EBUSY on the flagd bind
// mount. It carries its OWN mutex (separate file from interventions.json → a
// separate lock); the agent is the sole writer of rollout.json.
type RolloutWriter struct {
	path string
	log  *slog.Logger

	mu sync.Mutex
}

// flagCurrentControllerCount is the rollout flag whose defaultVariant the agent
// flips to advance/retreat the rollout stage.
const flagCurrentControllerCount = "current_controller_count"

// DefaultRolloutPath is the in-container rollout flag file path. Override with
// ROLLOUT_FLAG_PATH.
const DefaultRolloutPath = "/etc/flagd/rollout.json"

// Rollout stage variant names and their controller-count values. These mirror
// the current_controller_count variants in services/flagd/rollout.json. flagd
// flips by VARIANT NAME (not value), so the agent must map the ladder value it
// reasons about back to the variant name before writing — this mapping is the
// sharp edge the loop depends on.
const (
	StageNoneVariant  = "none"
	StageOneVariant   = "one"
	StageThreeVariant = "three"
	StageSixVariant   = "six"
	StageAllVariant   = "all"

	StageNoneValue  = 0
	StageOneValue   = 1
	StageThreeValue = 3
	StageSixValue   = 6
	StageAllValue   = 99
)

// rolloutLadder is the ordered expansion ladder: none(0) → one(1) → three(3) →
// six(6) → all(99). It is the single source of truth for both NextStage and
// StageVariantForValue, so the value↔variant mapping can never drift.
var rolloutLadder = []struct {
	variant string
	value   int
}{
	{StageNoneVariant, StageNoneValue},
	{StageOneVariant, StageOneValue},
	{StageThreeVariant, StageThreeValue},
	{StageSixVariant, StageSixValue},
	{StageAllVariant, StageAllValue},
}

// NewRolloutWriter builds a RolloutWriter for the rollout flag file at path.
// path "" uses DefaultRolloutPath. log nil uses slog.Default().
func NewRolloutWriter(path string, log *slog.Logger) *RolloutWriter {
	if path == "" {
		path = DefaultRolloutPath
	}
	if log == nil {
		log = slog.Default()
	}
	return &RolloutWriter{path: path, log: log}
}

// NewRolloutWriterFromEnv builds a RolloutWriter using ROLLOUT_FLAG_PATH (default
// DefaultRolloutPath), mirroring NewWriterFromEnv.
func NewRolloutWriterFromEnv(log *slog.Logger) *RolloutWriter {
	return NewRolloutWriter(os.Getenv("ROLLOUT_FLAG_PATH"), log)
}

// SetControllerCount flips current_controller_count's defaultVariant to the
// named variant. The variant MUST already exist in the document's variants map
// (the agent never invents variants — the schema file ships them all); an
// unknown variant is an error and the file is left untouched. Safe for
// concurrent use.
func (w *RolloutWriter) SetControllerCount(variant string) error {
	if err := w.edit(func(doc *orderedDoc) error {
		f, err := doc.flag(flagCurrentControllerCount)
		if err != nil {
			return err
		}
		// Validate the variant exists before flipping, so a typo never points
		// defaultVariant at a missing variant (flagd would then serve no value).
		variants := map[string]any{}
		if ok, err := f.get("variants", &variants); err != nil {
			return err
		} else if !ok {
			return fmt.Errorf("flag %q has no variants object", flagCurrentControllerCount)
		}
		if _, ok := variants[variant]; !ok {
			return fmt.Errorf("variant %q not present in flag %q", variant, flagCurrentControllerCount)
		}
		if err := f.set("defaultVariant", variant); err != nil {
			return err
		}
		return doc.putFlag(flagCurrentControllerCount, f)
	}); err != nil {
		return err
	}
	w.log.Debug("agent.rollout_written",
		"flag", flagCurrentControllerCount,
		"variant", variant,
		"path", w.path)
	return nil
}

// edit runs one read-modify-write cycle under the writer's mutex: read the file,
// apply fn to the order-preserving document, and write it back in place. Mirrors
// Writer.edit but over a separate file and lock.
func (w *RolloutWriter) edit(fn func(*orderedDoc) error) error {
	w.mu.Lock()
	defer w.mu.Unlock()

	raw, err := os.ReadFile(w.path)
	if err != nil {
		return fmt.Errorf("read rollout file %q: %w", w.path, err)
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

// writeInPlace overwrites the rollout file at the same path WITHOUT temp+rename
// (same EBUSY-avoidance reasoning as Writer.writeInPlace).
func (w *RolloutWriter) writeInPlace(data []byte) error {
	f, err := os.OpenFile(w.path, os.O_WRONLY|os.O_TRUNC|os.O_CREATE, 0o644)
	if err != nil {
		return fmt.Errorf("open rollout file for write: %w", err)
	}
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		return fmt.Errorf("write rollout file: %w", err)
	}
	if err := f.Close(); err != nil {
		return fmt.Errorf("close rollout file: %w", err)
	}
	return nil
}

// NextStage returns the next stage up the expansion ladder from the given
// current controller-count VALUE: the next variant name, its value, and ok=true.
// ok is false at or above the terminal stage (all/99), and also when current
// does not sit on a known ladder value (a malformed/unknown observed count never
// advances). It is the loop's "where do we expand to next?" helper, and pairs
// with StageVariantForValue so the loop always flips by the correct variant
// name.
func NextStage(current int) (variant string, value int, ok bool) {
	for i, stage := range rolloutLadder {
		if stage.value == current {
			if i+1 < len(rolloutLadder) {
				next := rolloutLadder[i+1]
				return next.variant, next.value, true
			}
			return "", 0, false // already at terminal stage (all)
		}
	}
	return "", 0, false // unknown current value: do not advance
}

// StageVariantForValue maps a controller-count VALUE to its ladder variant name
// (e.g. 3 → "three"). ok is false for any value not on the ladder. The loop uses
// it to translate the observed rollout count (a number) into the variant flagd
// flips by.
func StageVariantForValue(value int) (string, bool) {
	for _, stage := range rolloutLadder {
		if stage.value == value {
			return stage.variant, true
		}
	}
	return "", false
}

// NextStage / StageVariantForValue are also exposed as methods so a
// RolloutWriter can satisfy the decision package's actuator seam without that
// package depending on these free functions (actions imports decision, so the
// dependency cannot go the other way). They delegate to the package functions.
func (w *RolloutWriter) NextStage(current int) (variant string, value int, ok bool) {
	return NextStage(current)
}

func (w *RolloutWriter) StageVariantForValue(value int) (string, bool) {
	return StageVariantForValue(value)
}

// DryRun reports false: the real writer applies flips to rollout.json. The loop
// records this on the rollout.dry_run span attribute.
func (w *RolloutWriter) DryRun() bool { return false }

// DryRunRolloutWriter satisfies the same actuator seam as RolloutWriter but
// NEVER writes the rollout file: SetControllerCount logs the would-be flip and
// returns nil. It is the disabled-mode (AGENT_ROLLOUT_ENABLED=false) actuator —
// the loop still decides and spans the expansion (remediation.action="expand"),
// it just is not applied. The ladder helpers delegate to the real package
// functions, so the value↔variant mapping stays single-sourced.
type DryRunRolloutWriter struct {
	log *slog.Logger
}

// NewDryRunRolloutWriter builds the no-op actuator. log nil → slog.Default().
func NewDryRunRolloutWriter(log *slog.Logger) *DryRunRolloutWriter {
	if log == nil {
		log = slog.Default()
	}
	return &DryRunRolloutWriter{log: log}
}

// SetControllerCount logs the intended flip and returns nil without writing.
func (w *DryRunRolloutWriter) SetControllerCount(variant string) error {
	w.log.Info("agent.rollout_dry_run",
		"flag", flagCurrentControllerCount, "would_set_variant", variant)
	return nil
}

func (w *DryRunRolloutWriter) NextStage(current int) (variant string, value int, ok bool) {
	return NextStage(current)
}

func (w *DryRunRolloutWriter) StageVariantForValue(value int) (string, bool) {
	return StageVariantForValue(value)
}

// DryRun reports true: SetControllerCount never writes the rollout file. The
// loop records this on the rollout.dry_run span attribute so a rehearsed
// expand/rollback is distinguishable from a real one in Jaeger.
func (w *DryRunRolloutWriter) DryRun() bool { return true }
