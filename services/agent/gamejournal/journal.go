package gamejournal

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

// DefaultMaxGames bounds the journal: one more record evicts the oldest
// (UC-047 BR-009). A constant, not a flag (C-035): 500 records are about five
// calibration windows at the default N = 100 and a few hundred KB on disk.
const DefaultMaxGames = 500

// Dir and File place the journal under the experiments root
// (AGENT_EXPERIMENT_DIR, the agent-experiments volume). The journal.List scan
// counts only directories that hold an intent.json, so this directory is never
// read as an experiment.
const (
	Dir  = "calibration"
	File = "real_games.jsonl"
)

// Journal is the bounded, durable list of journaled games. It is rewritten in
// full on every append with a durable atomic write (temporary file, fsync,
// rename, fsync of the directory; UC-047 BR-008 b), so a reader or a restart
// sees the previous file or the new one, never a torn one. Safe for concurrent use.
type Journal struct {
	mu      sync.Mutex
	path    string
	max     int
	records []Record
	ids     map[string]bool
	// broken is set when an existing file could not be read: the journal then
	// refuses to write rather than replace a file it could not load.
	broken error

	// rename is os.Rename; a test seam for the atomic-write checks.
	rename func(oldpath, newpath string) error
}

// LoadReport says what Open found in an existing file.
type LoadReport struct {
	Records int
	// Skipped counts lines that were not a valid record of a known schema version;
	// they are dropped on the next write.
	Skipped int
}

// Open loads the journal at <root>/calibration/real_games.jsonl. A missing file
// is an empty journal. An unreadable file returns an error and a journal that
// refuses every write (so the file is never replaced by an empty one); invalid
// lines are skipped and reported. max <= 0 uses DefaultMaxGames.
func Open(root string, max int) (*Journal, LoadReport, error) {
	if max <= 0 {
		max = DefaultMaxGames
	}
	j := &Journal{
		path:   filepath.Join(root, Dir, File),
		max:    max,
		ids:    map[string]bool{},
		rename: os.Rename,
	}
	var rep LoadReport
	data, err := os.ReadFile(j.path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return j, rep, nil
		}
		j.broken = fmt.Errorf("read calibration journal %q: %w", j.path, err)
		return j, rep, j.broken
	}
	sc := bufio.NewScanner(bytes.NewReader(data))
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for sc.Scan() {
		line := bytes.TrimSpace(sc.Bytes())
		if len(line) == 0 {
			continue
		}
		var r Record
		if err := json.Unmarshal(line, &r); err != nil || r.SchemaVersion != SchemaVersion || r.GameID == "" || j.ids[r.GameID] {
			rep.Skipped++
			continue
		}
		j.records = append(j.records, r)
		j.ids[r.GameID] = true
	}
	if err := sc.Err(); err != nil {
		j.broken = fmt.Errorf("scan calibration journal %q: %w", j.path, err)
		return j, rep, j.broken
	}
	j.trimLocked()
	rep.Records = len(j.records)
	return j, rep, nil
}

// Path is the journal file.
func (j *Journal) Path() string { return j.path }

// Append adds r (a game id already journaled is ignored), evicts the oldest
// records beyond the bound and persists the journal. On a write error the record
// stays in memory, so the next successful write carries it.
func (j *Journal) Append(r Record) error {
	j.mu.Lock()
	defer j.mu.Unlock()
	if j.broken != nil {
		return j.broken
	}
	if r.GameID == "" || j.ids[r.GameID] {
		return nil
	}
	j.records = append(j.records, r)
	j.ids[r.GameID] = true
	j.trimLocked()
	return j.writeLocked()
}

// Records returns a copy of the journaled records, oldest first.
func (j *Journal) Records() []Record {
	j.mu.Lock()
	defer j.mu.Unlock()
	out := make([]Record, len(j.records))
	copy(out, j.records)
	return out
}

func (j *Journal) trimLocked() {
	if over := len(j.records) - j.max; over > 0 {
		for _, r := range j.records[:over] {
			delete(j.ids, r.GameID)
		}
		j.records = append([]Record(nil), j.records[over:]...)
	}
}

func (j *Journal) writeLocked() error {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf) // one compact record per line
	for _, r := range j.records {
		if err := enc.Encode(r); err != nil {
			return fmt.Errorf("marshal calibration record %q: %w", r.GameID, err)
		}
	}
	return writeFileAtomic(j.path, buf.Bytes(), j.rename)
}

// writeFileAtomic writes data to path durably: a temporary file in the same
// directory, fsync, rename over path, then fsync of the directory so the rename
// itself survives a power loss (UC-047 BR-008 b). On any error the temporary file
// is removed and path keeps its previous content.
func writeFileAtomic(path string, data []byte, rename func(string, string) error) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("create calibration dir %q: %w", dir, err)
	}
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(path)+"-*.tmp")
	if err != nil {
		return fmt.Errorf("create temp file for %q: %w", path, err)
	}
	tmpName := tmp.Name()
	renamed := false
	defer func() {
		if !renamed {
			_ = os.Remove(tmpName)
		}
	}()
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("write temp file for %q: %w", path, err)
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("sync temp file for %q: %w", path, err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temp file for %q: %w", path, err)
	}
	if err := rename(tmpName, path); err != nil {
		return fmt.Errorf("rename temp file to %q: %w", path, err)
	}
	renamed = true
	d, err := os.Open(dir)
	if err != nil {
		return fmt.Errorf("open calibration dir %q for sync: %w", dir, err)
	}
	defer func() { _ = d.Close() }()
	if err := d.Sync(); err != nil {
		return fmt.Errorf("sync calibration dir %q: %w", dir, err)
	}
	return nil
}
