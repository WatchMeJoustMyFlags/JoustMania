package state

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/joustmania/flag-service/registry"
)

// Admin persistence (architecture note §10.1, C-030, FR-018): only admin
// values written with "persist": true live in the state file. The file is
// rewritten atomically (temp file + rename in the same directory, on the
// stack's own named volume); a corrupt file is moved aside and never stops
// the start; stale entries are dropped with a warning.

const stateFileVersion = 1

type persistedValue struct {
	Set       string          `json:"flag_set"`
	Flag      string          `json:"flag"`
	Variant   string          `json:"variant,omitempty"`
	Value     json.RawMessage `json:"value,omitempty"`
	Writer    string          `json:"writer,omitempty"`
	WrittenAt time.Time       `json:"written_at"`
}

type persistedDoc struct {
	Version int              `json:"version"`
	Values  []persistedValue `json:"values"`
}

// loadAdmin restores the admin values from the state file. Called once from
// New, before any stream exists.
func (s *State) loadAdmin() LoadReport {
	var report LoadReport
	path := s.opts.StateFile
	if path == "" {
		return report
	}
	raw, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		s.lastPersistOK = true
		return report
	}
	if err != nil {
		// A read error (permissions, I/O) is not corruption: keep the file and
		// do not write over it; admin values apply in memory only.
		s.persistBlocked = true
		s.opts.Logger.Error("flag service: state file unreadable; admin settings will not be persisted until the next start",
			"path", path, "error", err)
		return report
	}
	var doc persistedDoc
	if err == nil {
		err = json.Unmarshal(raw, &doc)
		if err == nil && doc.Version != stateFileVersion {
			err = fmt.Errorf("unsupported state file version %d", doc.Version)
		}
	}
	if err != nil {
		report.Corrupt = true
		pruneCorrupt(path, maxCorruptFiles-1)
		report.CorruptPath = fmt.Sprintf("%s.corrupt-%d", path, s.opts.Now().Unix())
		if mvErr := moveAside(path, report.CorruptPath); mvErr != nil {
			s.opts.Logger.Error("flag service: state file unreadable and could not be moved aside; starting from the baseline",
				"path", path, "error", err, "move_error", mvErr)
			report.CorruptPath = ""
		} else {
			s.opts.Logger.Error("flag service: corrupt state file moved aside; starting from the baseline",
				"path", path, "moved_to", report.CorruptPath, "error", err)
		}
		return report
	}
	for _, pv := range doc.Values {
		if reason := s.restoreLocked(pv); reason != "" {
			label := pv.Set + "/" + pv.Flag
			report.Dropped = append(report.Dropped, label)
			s.opts.Logger.Warn("flag service: dropped a stale admin value from the state file", "flag", label, "reason", reason)
			continue
		}
		report.AdminValues++
	}
	s.lastPersistOK = true
	return report
}

// restoreLocked applies one persisted value, or returns why it is stale.
func (s *State) restoreLocked(pv persistedValue) string {
	f := s.flags[pv.Flag]
	switch {
	case f == nil || f.set != pv.Set:
		return "unknown flag"
	case f.policy == nil || !f.policy.Persist:
		return "flag no longer persistable"
	case (pv.Variant == "") == (pv.Value == nil):
		return "needs exactly one of variant and value"
	}
	checked, err := registry.CheckValue(f.policy, f.base, pv.Variant, pv.Value)
	if err != nil {
		return err.Detail
	}
	v := &value{Writer: pv.Writer, WrittenAt: pv.WrittenAt, Persisted: true}
	if checked.Variant != "" {
		v.Variant = checked.Variant
	} else {
		v.Variant = s.nextVariant("o")
		v.Value = checked.Value
	}
	f.admin = v
	return ""
}

// saveAdminLocked rewrites the state file from the current admin values. A
// failure is logged, counted and returned: the caller rolls its in-memory
// change back, so memory and disk never disagree (#2368). With persistence
// disabled (no state file) there is nothing to fail: it returns nil and
// lastPersistOK stays false, so the write reports persisted:false.
func (s *State) saveAdminLocked() error {
	path := s.opts.StateFile
	if path == "" {
		s.lastPersistOK = false
		return nil
	}
	if s.persistBlocked {
		s.persistErrors++
		s.lastPersistOK = false
		s.opts.Logger.Error("flag service: the state file was unreadable at start; the admin write is refused", "path", path)
		return errors.New("the state file was unreadable at start; admin settings cannot be persisted until the next start")
	}
	doc := persistedDoc{Version: stateFileVersion, Values: []persistedValue{}}
	for key, f := range s.flags {
		if f.admin == nil {
			continue
		}
		pv := persistedValue{Set: f.set, Flag: key, Writer: f.admin.Writer, WrittenAt: f.admin.WrittenAt}
		if f.admin.generated() {
			pv.Value = f.admin.Value
		} else {
			pv.Variant = f.admin.Variant
		}
		doc.Values = append(doc.Values, pv)
	}
	sort.Slice(doc.Values, func(i, j int) bool {
		return doc.Values[i].Set+"/"+doc.Values[i].Flag < doc.Values[j].Set+"/"+doc.Values[j].Flag
	})
	if err := writeAtomic(path, doc); err != nil {
		s.persistErrors++
		s.lastPersistOK = false
		s.opts.Logger.Error("flag service: could not write the state file; the admin write is refused and rolled back", "path", path, "error", err)
		return err
	}
	s.lastPersistOK = true
	return nil
}

func writeAtomic(path string, doc persistedDoc) error {
	raw, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		return err
	}
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(path)+".*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer func() { _ = os.Remove(tmpName) }() // no-op after a successful rename
	if _, err := tmp.Write(append(raw, '\n')); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpName, path); err != nil {
		return err
	}
	// fsync the directory so the rename itself survives a power cut.
	if d, err := os.Open(dir); err == nil {
		_ = d.Sync()
		_ = d.Close()
	}
	return nil
}

// moveAside renames a corrupt state file; a variable so tests can make the
// rename fail (UC-053 A12).
var moveAside = os.Rename

// maxCorruptFiles caps the .corrupt-<ts> copies kept next to the state file.
const maxCorruptFiles = 3

// pruneCorrupt deletes the oldest .corrupt-<ts> copies so at most keep remain.
func pruneCorrupt(path string, keep int) {
	matches, err := filepath.Glob(path + ".corrupt-*")
	if err != nil || len(matches) <= keep {
		return
	}
	sort.Strings(matches) // the unix-seconds suffix has a fixed width for centuries
	for _, m := range matches[:len(matches)-keep] {
		_ = os.Remove(m)
	}
}

// PersistErrors returns the number of failed state-file writes.
func (s *State) PersistErrors() int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.persistErrors
}
