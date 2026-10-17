package promote

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// snapstore.go is the durable store of the post-revert cooldown stamps of the
// autonomous real-default reverter (#1065 item 3, #1076), so thrash protection
// survives an agent restart. The pre-promotion VALUE snapshots it used to hold
// are gone with the flag-file writer (#1415): the promotion is an overlay in the
// flag service and a revert removes it, so there is nothing to snapshot. Old
// files with a "snapshot" member still load (the member is ignored).
//
// It lives in the journal's persisted location (AGENT_EXPERIMENT_DIR) and uses
// the journal's atomic-write discipline (temp + fsync + rename). It is built only
// inside NewRealDefaultWriterFromEnv, behind the same two env gates.

// realDefaultSnapshotFile is the fixed file name under the persistence root that
// holds the durable snapshot + cooldown state. It is a SINGLE JSON document (not
// a per-flag dir) because the map is tiny — one entry per agent-touched flag.
const realDefaultSnapshotFile = "real_default_snapshots.json"

// snapStoreDirEnv overrides where the durable snapshot file lives. It mirrors the
// journal's AGENT_EXPERIMENT_DIR so, by default (env unset), the snapshot file
// lands in the SAME persisted volume as the journal. A test points it at a
// t.TempDir() to exercise the restart boundary without touching real state.
const snapStoreDirEnv = "AGENT_EXPERIMENT_DIR"

// defaultSnapStoreDir mirrors journal.DefaultDir. Duplicated as a string rather
// than imported so the promote package keeps no dependency on the journal package
// (which would couple two otherwise-independent durability mechanisms).
const defaultSnapStoreDir = "/var/lib/joustmania/agent/experiments"

// persistedRecord is the on-disk shape for one flag's cooldown state.
type persistedRecord struct {
	// LastRevert is the time of the most recent successful revert of this flag, used
	// to seed the Promoter's post-revert cooldown after a restart.
	LastRevert time.Time `json:"lastRevert,omitempty"`
}

// snapStore is the durable {flagKey -> persistedRecord} store. It owns the on-disk
// file and an in-memory copy guarded by its own mutex (guarded so the stamps can be read
// concurrently from the Promoter on boot).
type snapStore struct {
	path string

	mu      sync.Mutex
	records map[string]persistedRecord
}

// snapStoreDir resolves the persistence directory from snapStoreDirEnv, falling
// back to defaultSnapStoreDir — the single env-override seam, mirroring
// journal.DirFromEnv.
func snapStoreDir() string {
	if d := strings.TrimSpace(os.Getenv(snapStoreDirEnv)); d != "" {
		return d
	}
	return defaultSnapStoreDir
}

// newSnapStore builds a store over the snapshot file under dir and LOADS any
// existing records from disk (the restart-survival path). A missing file is not an
// error — a first-run agent starts with an empty store. A corrupt/unreadable file
// is logged-and-ignored by the caller (the store returns the error so the caller
// decides); the in-memory map then starts empty rather than aborting agent start.
func newSnapStore(dir string) (*snapStore, error) {
	s := &snapStore{
		path:    filepath.Join(dir, realDefaultSnapshotFile),
		records: map[string]persistedRecord{},
	}
	raw, err := os.ReadFile(s.path)
	if err != nil {
		if os.IsNotExist(err) {
			return s, nil
		}
		return s, fmt.Errorf("read real-default snapshot store %q: %w", s.path, err)
	}
	if len(raw) == 0 {
		return s, nil
	}
	var recs map[string]persistedRecord
	if err := json.Unmarshal(raw, &recs); err != nil {
		return s, fmt.Errorf("parse real-default snapshot store %q: %w", s.path, err)
	}
	if recs != nil {
		s.records = recs
	}
	return s, nil
}

// loadLastRevert returns the persisted last-revert timestamps so the Promoter can
// seed its post-revert cooldown after a restart. Records with a zero stamp are
// omitted.
func (s *snapStore) loadLastRevert() map[string]time.Time {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make(map[string]time.Time, len(s.records))
	for k, r := range s.records {
		if !r.LastRevert.IsZero() {
			out[k] = r.LastRevert
		}
	}
	return out
}

// markReverted records the revert time so the cooldown survives a restart.
func (s *snapStore) markReverted(flagKey string, at time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	rec := s.records[flagKey]
	rec.LastRevert = at
	s.records[flagKey] = rec
	return s.flushLocked()
}

// flushLocked atomically rewrites the snapshot file (temp + fsync + rename in the
// same directory), so a concurrent reader / a crash never observes a half-written
// store. Mirrors journal's writeJSONAtomic. Caller holds s.mu.
func (s *snapStore) flushLocked() error {
	dir := filepath.Dir(s.path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("create snapshot store dir %q: %w", dir, err)
	}
	data, err := json.MarshalIndent(s.records, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal snapshot store: %w", err)
	}
	data = append(data, '\n')

	tmp, err := os.CreateTemp(dir, "."+realDefaultSnapshotFile+"-*.tmp")
	if err != nil {
		return fmt.Errorf("create temp snapshot file: %w", err)
	}
	tmpName := tmp.Name()
	defer func() { _ = os.Remove(tmpName) }()

	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("write temp snapshot file: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("sync temp snapshot file: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temp snapshot file: %w", err)
	}
	if err := os.Rename(tmpName, s.path); err != nil {
		return fmt.Errorf("rename temp snapshot file to %q: %w", s.path, err)
	}
	return nil
}
