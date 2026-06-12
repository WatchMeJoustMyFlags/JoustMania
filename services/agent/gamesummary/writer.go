package gamesummary

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// writer.go persists a Summary as one JSON file per game (#928). Unlike the
// interventions Writer (actions/writer.go, in-place to dodge inotify EBUSY on a
// flagd bind mount), NO process watches the summaries dir, so a real atomic
// temp+rename is both safe and required: a reader (the #929 window builder, the
// dashboard, an operator) must NEVER see a half-written file. rename(2) is atomic
// within a directory, so the destination either does not exist or is a complete
// summary — never a truncated one.

// DefaultDir is the in-container summaries directory. Override with
// AGENT_GAME_SUMMARY_DIR. It mirrors the house path-from-env pattern
// (actions.DefaultPath + INTERVENTIONS_FLAG_PATH): a sensible default plus a
// single env override, resolved by DirFromEnv.
const DefaultDir = "/var/lib/joustmania/agent/summaries"

// gameSummaryDirEnv is the env var that overrides DefaultDir.
const gameSummaryDirEnv = "AGENT_GAME_SUMMARY_DIR"

// safeFilenameChars matches any rune NOT allowed in a summary filename. game_ids
// are "game_<hex>" and synthetic ids are "session-<n>" (both already safe), but an
// adopted game_id is an external label, so we sanitize defensively: anything
// outside [A-Za-z0-9._-] becomes '_' so the id can never escape the summaries dir
// (path traversal) or produce an unwritable name.
var safeFilenameChars = regexp.MustCompile(`[^A-Za-z0-9._-]`)

// Writer persists summaries under a fixed directory. It holds no per-game state
// and is safe for concurrent use: each Write targets a distinct game_id file via a
// uniquely-named temp file, so two concurrent game-ends (#845) never collide.
type Writer struct {
	dir string
}

// NewWriter builds a Writer rooted at dir. An empty dir uses DefaultDir. The
// directory is created lazily on the first Write (not here) so construction never
// touches the filesystem and a test can point it at a t.TempDir().
func NewWriter(dir string) *Writer {
	if dir == "" {
		dir = DefaultDir
	}
	return &Writer{dir: dir}
}

// DirFromEnv resolves the summaries directory from AGENT_GAME_SUMMARY_DIR,
// falling back to DefaultDir — the single env-override seam, mirroring
// actions.NewWriterFromEnv.
func DirFromEnv() string {
	if d := strings.TrimSpace(os.Getenv(gameSummaryDirEnv)); d != "" {
		return d
	}
	return DefaultDir
}

// Path returns the on-disk path the given game_id's summary is written to, with
// the id sanitized to a safe filename. Exported so a caller/test can locate the
// file without re-deriving the naming rule.
func (w *Writer) Path(gameID string) string {
	return filepath.Join(w.dir, safeFilename(gameID)+".json")
}

// safeFilename sanitizes a game_id into a filesystem-safe base name. An empty or
// all-unsafe id collapses to "unknown" so a write never targets ".json" or
// escapes the dir.
func safeFilename(gameID string) string {
	name := safeFilenameChars.ReplaceAllString(gameID, "_")
	name = strings.Trim(name, ".")
	if name == "" {
		return "unknown"
	}
	return name
}

// Write atomically persists s to <dir>/<game_id>.json. It creates the directory
// if missing, marshals indented JSON (so the file is human-readable for an
// operator), writes it to a uniquely-named temp file in the SAME directory, fsyncs
// it, then renames it over the destination — so a concurrent reader observes only
// the complete file. The temp file shares the destination's directory so the
// rename stays within one filesystem (cross-device rename would fail).
func (w *Writer) Write(s Summary) error {
	if err := os.MkdirAll(w.dir, 0o755); err != nil {
		return fmt.Errorf("create summaries dir %q: %w", w.dir, err)
	}

	data, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal summary for %q: %w", s.GameID, err)
	}
	data = append(data, '\n')

	dst := w.Path(s.GameID)

	// Unique temp name (CreateTemp adds a random suffix) so two concurrent writes
	// for different games — or a retry for the same game — never share a temp file.
	tmp, err := os.CreateTemp(w.dir, "."+safeFilename(s.GameID)+"-*.tmp")
	if err != nil {
		return fmt.Errorf("create temp summary file: %w", err)
	}
	tmpName := tmp.Name()
	// Best-effort cleanup of the temp file on any failure before the rename; a
	// successful rename consumes it, so the deferred Remove then no-ops.
	defer func() { _ = os.Remove(tmpName) }()

	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("write temp summary: %w", err)
	}
	// fsync before rename so the bytes are durable on disk before the directory
	// entry flips — a crash can never leave the destination pointing at empty data.
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("sync temp summary: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temp summary: %w", err)
	}
	if err := os.Rename(tmpName, dst); err != nil {
		return fmt.Errorf("rename temp summary to %q: %w", dst, err)
	}
	return nil
}
