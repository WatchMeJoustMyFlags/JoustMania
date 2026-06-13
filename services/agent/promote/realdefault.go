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
type fileRealDefaultWriter struct {
	path string
	log  *slog.Logger
	mu   sync.Mutex
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
	log.Warn("code_improvement AUTONOMOUS real-default writer ENABLED (#936): autonomous promotions WILL change the REAL defaultVariant for live players",
		"path", path)
	return &fileRealDefaultWriter{path: path, log: log}, nil
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
