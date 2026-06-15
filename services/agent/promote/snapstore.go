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

// snapstore.go is the DURABLE backing for the autonomous real-default reverter
// (#1076). The pre-promotion snapshot the reverter rolls back through, and the
// post-revert cooldown timestamps, were previously in-memory ONLY: if the agent
// restarted between an autonomous apply and a later revert, RevertRealDefault
// found no snapshot and could not roll the regression back — the bad real default
// rode on live players indefinitely. The flag FILE still held the promoted value
// after restart, so a persisted snapshot is the only missing ingredient.
//
// This is a MINIMAL durable store, NOT a journal reuse: the #983 journal is
// modeled per-experiment ({intent, append-only events, rolling summary} keyed by
// experiment_id) and is the wrong shape for a flat {flagKey -> snapshot} map. It
// DOES live in the SAME persisted location as the journal (AGENT_EXPERIMENT_DIR /
// the #1025 named volume), reusing the journal's proven atomic-write discipline
// (temp + fsync + rename) so the file survives both a process restart and a
// container recreation.
//
// DEFAULT-SAFE: this store adds DURABILITY to the EXISTING reverter only. It is
// constructed solely inside NewRealDefaultWriterFromEnv, behind the same two env
// gates; it enables/weakens NO gate. A persistence error never aborts the apply
// or the revert — the in-memory map remains authoritative for the live process,
// and the durable copy is best-effort-plus-logged. The one safety-critical write
// (snapshot at apply time) IS surfaced as an error from SetRealDefault so a
// strand-on-restart cannot be introduced silently.

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

// persistedRecord is the on-disk shape for one flag's durable revert state. It
// carries BOTH the pre-promotion snapshot (so a post-restart revert restores the
// exact prior value, #1065) AND the last-revert timestamp (so post-revert thrash
// protection, #1065 item 3, survives restart). A record may hold only the
// cooldown stamp (snapshot already reverted) — Snapshot is then nil.
type persistedRecord struct {
	// Snapshot is the pre-promotion {hadDefault, variant, value} captured at
	// first-touch. nil once the flag has been reverted (the snapshot is dropped so
	// a fresh promotion re-snapshots) — but the record may persist for its
	// LastRevert stamp.
	Snapshot *persistedSnapshot `json:"snapshot,omitempty"`
	// LastRevert is the time of the most recent successful revert of this flag, used
	// to seed the Promoter's post-revert cooldown after a restart. Zero when never
	// reverted.
	LastRevert time.Time `json:"lastRevert,omitempty"`
}

// persistedSnapshot mirrors defaultSnapshot for on-disk storage. defaultSnapshot's
// fields are unexported (so they cannot be JSON-marshaled directly); this exported
// twin is the serialization boundary.
type persistedSnapshot struct {
	HadDefault bool            `json:"hadDefault"`
	Variant    string          `json:"variant"`
	Value      json.RawMessage `json:"value,omitempty"`
}

// snapStore is the durable {flagKey -> persistedRecord} store. It owns the on-disk
// file and an in-memory copy guarded by its own mutex (the fileRealDefaultWriter's
// mutex already serializes SetRealDefault/RevertRealDefault, but the store guards
// itself too so seedLastRevert can be read concurrently from the Promoter on boot).
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

// loadSnapshots returns the persisted snapshots as the in-memory defaultSnapshot
// map the writer uses. Only records that still HOLD a snapshot are returned (a
// reverted flag's record carries only a cooldown stamp). Copies the raw value
// bytes so a later in-place mutation cannot reach into the loaded map.
func (s *snapStore) loadSnapshots() map[string]defaultSnapshot {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make(map[string]defaultSnapshot, len(s.records))
	for k, r := range s.records {
		if r.Snapshot == nil {
			continue
		}
		snap := defaultSnapshot{
			hadDefault: r.Snapshot.HadDefault,
			variant:    r.Snapshot.Variant,
		}
		if r.Snapshot.Value != nil {
			snap.value = append(json.RawMessage(nil), r.Snapshot.Value...)
		}
		out[k] = snap
	}
	return out
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

// putSnapshot durably records the pre-promotion snapshot for flagKey, preserving
// any existing LastRevert stamp. Called at first-touch in SetRealDefault. Returns
// any persistence error so the caller can surface it (a snapshot that fails to
// persist is the strand-on-restart bug this issue closes).
func (s *snapStore) putSnapshot(flagKey string, snap defaultSnapshot) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	rec := s.records[flagKey]
	ps := &persistedSnapshot{HadDefault: snap.hadDefault, Variant: snap.variant}
	if snap.value != nil {
		ps.Value = append(json.RawMessage(nil), snap.value...)
	}
	rec.Snapshot = ps
	s.records[flagKey] = rec
	return s.flushLocked()
}

// markReverted drops flagKey's snapshot (so a fresh promotion re-snapshots) but
// records the revert time so the cooldown survives a restart. Called after a
// successful revert. The record is KEPT (not deleted) precisely so LastRevert
// persists; a future putSnapshot overwrites Snapshot in place.
func (s *snapStore) markReverted(flagKey string, at time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	rec := s.records[flagKey]
	rec.Snapshot = nil
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
