package journal

import (
	"bufio"
	"encoding/json"
	"fmt"
	"log"
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

// List enumerates the experiment_ids with a durable journal under root — every
// sub-directory that holds an intent.json. It is the rehydration scan (#831 / the
// #978 registry's Rehydrate input): the registry Loads each id to rebuild its
// live set on startup. A missing root yields an empty slice (no experiments yet),
// never an error, so a first-run agent rehydrates trivially. The returned ids are
// the DIRECTORY names — for a sanitized id (exp_<hex> is already safe) this is the
// experiment_id verbatim; Load reads intent.json's experiment_id regardless.
func List(root string) ([]string, error) {
	if root == "" {
		root = DefaultDir
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("list experiments root %q: %w", root, err)
	}
	var ids []string
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		// Only count a dir that actually holds an intent.json — skip stray dirs.
		if _, err := os.Stat(filepath.Join(root, e.Name(), intentFile)); err != nil {
			continue
		}
		ids = append(ids, e.Name())
	}
	return ids, nil
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
// (an experiment created but with no events yet is valid).
//
// Torn-tail recovery (design §9.3 durability): appendEventLine commits each event
// with O_APPEND → single write → fsync, and POSIX gives an append NO cross-crash
// atomicity. A crash mid-write of event N+1 — after event N was durably fsynced
// AND acknowledged to the caller — can leave a partial final line. That last event
// was never acknowledged, so dropping it is correct: the summary (rewritten only
// AFTER the append returns) never counted it, and Load must still reconverge.
// So a malformed LAST non-empty line is DROPPED with a warning, and the torn bytes
// are TRUNCATED off the file so the next AppendEvent does not turn the partial line
// into an interior line (which would then hard-error on the NEXT Load).
//
// A malformed INTERIOR (non-last) line stays a HARD error: an event followed by a
// later durably-committed event cannot itself be a torn tail — it signals a genuine
// lost/corrupted write, and silently skipping it would corrupt the rehydrated
// statistics (consistent with the Seq-gap reasoning).
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

	// We must distinguish "malformed interior line" (hard error) from "malformed
	// final line" (torn tail → drop). bufio.Scanner cannot look ahead, so when a
	// line fails to unmarshal we DEFER the decision: stash it and keep scanning. If
	// nothing more follows, it was the tail; if another non-empty line follows, the
	// stashed one was interior and we hard-error.
	lineNo := 0
	// offset tracks the byte position of the END of the last successfully parsed
	// line, so a torn tail can be truncated back to that point.
	var validBytes int64
	var pendingBad bool      // a malformed line is awaiting the tail/interior verdict
	var pendingBadLineNo int // its 1-based line number, for the error/log message
	for scanner.Scan() {
		lineNo++
		raw := scanner.Bytes()
		// +1 for the newline bufio strips. The final line may have no trailing
		// newline (exactly the torn-tail case), but that line is either dropped
		// (no truncation past validBytes) or — if valid — the file already ends
		// there, so validBytes is never used to truncate beyond EOF.
		lineLen := int64(len(raw)) + 1

		if len(strings.TrimSpace(string(raw))) == 0 {
			validBytes += lineLen
			continue
		}
		// A non-empty line follows a previously stashed bad line → that bad line was
		// interior, not a torn tail. Hard error.
		if pendingBad {
			return nil, fmt.Errorf("decode event line %d for %q: malformed interior line (a later event follows it)", pendingBadLineNo, experimentID)
		}
		var e Event
		if err := json.Unmarshal(raw, &e); err != nil {
			// Could be a torn final line — defer the verdict until we know whether
			// another line follows.
			pendingBad = true
			pendingBadLineNo = lineNo
			continue
		}
		events = append(events, e)
		validBytes += lineLen
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("scan events log for %q: %w", experimentID, err)
	}

	// A still-pending bad line at EOF is the torn tail: drop it and truncate the
	// partial bytes so the recovered journal stays loadable and the next append
	// continues cleanly after the last VALID event.
	if pendingBad {
		log.Printf("journal: dropping torn final line %d of events log for %q (partial write from a crash); reconverging from %d prior event(s)", pendingBadLineNo, experimentID, len(events))
		_ = f.Close() // release the read handle before truncating
		if err := os.Truncate(path, validBytes); err != nil {
			return nil, fmt.Errorf("truncate torn tail of events log for %q: %w", experimentID, err)
		}
	}
	return events, nil
}
