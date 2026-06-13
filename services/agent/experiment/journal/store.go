package journal

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// store.go is the on-disk layout + persistence primitives (design §9.3). It
// reuses the proven gamesummary.Writer pattern (writer.go:24, :82-129): one
// directory per experiment under a fixed root, atomic temp+fsync+rename for the
// rewritten files so a reader NEVER observes a half-written intent.json or
// summary.json. The append-only events.jsonl is opened O_APPEND and fsynced per
// append — it is never rewritten, so it needs no rename dance.

// DefaultDir is the in-container experiments root. Override with
// AGENT_EXPERIMENT_DIR. Mirrors gamesummary.DefaultDir / AGENT_GAME_SUMMARY_DIR.
const DefaultDir = "/var/lib/joustmania/agent/experiments"

// experimentDirEnv is the env var that overrides DefaultDir.
const experimentDirEnv = "AGENT_EXPERIMENT_DIR"

const (
	intentFile  = "intent.json"
	eventsFile  = "events.jsonl"
	summaryFile = "summary.json"
)

// safeDirChars matches any rune NOT allowed in an experiment directory name.
// experiment_ids are "exp_<hex>" (already safe), but the id is treated defensively
// as an external label: anything outside [A-Za-z0-9._-] becomes '_' so the id can
// never escape the experiments root (path traversal) or produce an unwritable name.
// Mirrors gamesummary.safeFilenameChars.
var safeDirChars = regexp.MustCompile(`[^A-Za-z0-9._-]`)

// safeDirName sanitizes an experiment_id into a filesystem-safe directory name. An
// empty or all-unsafe id collapses to "unknown" so a write never targets the root
// itself or escapes it.
func safeDirName(experimentID string) string {
	name := safeDirChars.ReplaceAllString(experimentID, "_")
	name = strings.Trim(name, ".")
	if name == "" {
		return "unknown"
	}
	return name
}

// DirFromEnv resolves the experiments root from AGENT_EXPERIMENT_DIR, falling back
// to DefaultDir — the single env-override seam, mirroring gamesummary.DirFromEnv.
func DirFromEnv() string {
	if d := strings.TrimSpace(os.Getenv(experimentDirEnv)); d != "" {
		return d
	}
	return DefaultDir
}

// store binds a root experiments directory. It holds no per-experiment state and
// resolves each experiment's sub-directory from its (sanitized) id.
type store struct {
	root string
}

// newStore builds a store rooted at root. An empty root uses DefaultDir. The
// directory is created lazily on first write (not here) so construction never
// touches the filesystem and a test can point it at a t.TempDir().
func newStore(root string) *store {
	if root == "" {
		root = DefaultDir
	}
	return &store{root: root}
}

// dir returns the per-experiment directory path.
func (s *store) dir(experimentID string) string {
	return filepath.Join(s.root, safeDirName(experimentID))
}

// writeJSONAtomic marshals v as indented JSON and persists it to path via a
// temp+fsync+rename in the SAME directory — so a concurrent reader observes only
// the complete file (gamesummary.Writer.Write, writer.go:82-129). Used for the two
// rewritten files (intent.json, summary.json).
func writeJSONAtomic(path string, v any) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("create experiment dir %q: %w", dir, err)
	}

	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal %q: %w", path, err)
	}
	data = append(data, '\n')

	// Unique temp name in the destination dir so concurrent writes / retries never
	// share a temp file, and the rename stays within one filesystem.
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(path)+"-*.tmp")
	if err != nil {
		return fmt.Errorf("create temp file for %q: %w", path, err)
	}
	tmpName := tmp.Name()
	defer func() { _ = os.Remove(tmpName) }()

	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("write temp file for %q: %w", path, err)
	}
	// fsync before rename so the bytes are durable before the directory entry
	// flips — a crash can never leave the destination pointing at empty data.
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("sync temp file for %q: %w", path, err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temp file for %q: %w", path, err)
	}
	if err := os.Rename(tmpName, path); err != nil {
		return fmt.Errorf("rename temp file to %q: %w", path, err)
	}
	return nil
}

// appendEventLine appends one JSON-encoded event as a single line to events.jsonl.
// The file is opened O_APPEND|O_CREATE and fsynced after the write, so the line is
// durable and ordered; the file is NEVER rewritten or truncated (the append-only
// audit-trail guarantee, design §9.1b). The event is marshaled WITHOUT indentation
// so it occupies exactly one line — the JSONL contract.
func appendEventLine(path string, e Event) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("create experiment dir for events: %w", err)
	}
	line, err := json.Marshal(e)
	if err != nil {
		return fmt.Errorf("marshal event seq %d: %w", e.Seq, err)
	}
	line = append(line, '\n')

	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return fmt.Errorf("open events log %q: %w", path, err)
	}
	if _, err := f.Write(line); err != nil {
		_ = f.Close()
		return fmt.Errorf("append event to %q: %w", path, err)
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		return fmt.Errorf("sync events log %q: %w", path, err)
	}
	if err := f.Close(); err != nil {
		return fmt.Errorf("close events log %q: %w", path, err)
	}
	return nil
}

// readIntent loads and decodes intent.json for an experiment.
func (s *store) readIntent(experimentID string) (Intent, error) {
	var in Intent
	data, err := os.ReadFile(filepath.Join(s.dir(experimentID), intentFile))
	if err != nil {
		return in, fmt.Errorf("read intent for %q: %w", experimentID, err)
	}
	if err := json.Unmarshal(data, &in); err != nil {
		return in, fmt.Errorf("decode intent for %q: %w", experimentID, err)
	}
	return in, nil
}

// readEvents loads events.jsonl in order. A missing file yields an empty slice
// (an experiment created but with no events yet is valid). A malformed line is a
// hard error — the log is the source of truth and silently skipping a line would
// corrupt the rehydrated statistics.
func (s *store) readEvents(experimentID string) ([]Event, error) {
	path := filepath.Join(s.dir(experimentID), eventsFile)
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("open events log for %q: %w", experimentID, err)
	}
	defer func() { _ = f.Close() }()

	var events []Event
	scanner := bufio.NewScanner(f)
	// Allow long lines (a large experimental_value is not on events, but a Note
	// could be sizable); 1 MiB ceiling is generous for a single event line.
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	lineNo := 0
	for scanner.Scan() {
		lineNo++
		raw := scanner.Bytes()
		if len(strings.TrimSpace(string(raw))) == 0 {
			continue
		}
		var e Event
		if err := json.Unmarshal(raw, &e); err != nil {
			return nil, fmt.Errorf("decode event line %d for %q: %w", lineNo, experimentID, err)
		}
		events = append(events, e)
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("scan events log for %q: %w", experimentID, err)
	}
	return events, nil
}
