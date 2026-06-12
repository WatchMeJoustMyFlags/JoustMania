package experiment

import (
	"bytes"
	"encoding/json"
	"fmt"
)

// document / flagObj are an order-preserving model of the flagd game.json file.
// This MIRRORS services/agent/actions/document.go (the #730 interventions
// writer): encoding/json drops object key order, which would reshuffle the file
// on every write and defeat the "preserve unrelated flags byte-for-byte"
// contract the Gate's invariant relies on (an untouched real-resolving flag must
// round-trip identically). We keep the document root and each flag we mutate as
// ordered key lists, and store every value we do NOT touch as the exact
// json.RawMessage we read.
//
// It is a separate copy (not a shared package) because the experiment Writer and
// the actions Writer target different files with different lock scopes; the
// machinery is small and the duplication keeps each writer self-contained, as
// the actions/rollout_writer.go precedent does within its own package.
type document struct {
	keys   []string
	values map[string]json.RawMessage
}

// flagObj is an order-preserving view of a single flag object.
type flagObj struct {
	keys   []string
	values map[string]json.RawMessage
}

// parseDoc parses the game flag file into an order-preserving document.
func parseDoc(raw []byte) (*document, error) {
	keys, vals, err := decodeOrdered(raw)
	if err != nil {
		return nil, fmt.Errorf("parse game flag document: %w", err)
	}
	return &document{keys: keys, values: vals}, nil
}

// flag returns an order-preserving view of the named flag, or an error if it is
// absent. The Gate requires the flag to pre-exist, so absence is a hard error
// the experiment surface never invents flags.
func (d *document) flag(name string) (*flagObj, error) {
	_, vals, err := d.flagsObject()
	if err != nil {
		return nil, err
	}
	fr, ok := vals[name]
	if !ok {
		return nil, fmt.Errorf("flag %q not present in game flag file", name)
	}
	fkeys, fvals, err := decodeOrdered(fr)
	if err != nil {
		return nil, fmt.Errorf("decode flag %q: %w", name, err)
	}
	return &flagObj{keys: fkeys, values: fvals}, nil
}

// hasFlag reports whether the named flag exists (used by the Gate before any
// mutation).
func (d *document) hasFlag(name string) bool {
	_, vals, err := d.flagsObject()
	if err != nil {
		return false
	}
	_, ok := vals[name]
	return ok
}

// flagsObject decodes the top-level "flags" object in document order.
func (d *document) flagsObject() ([]string, map[string]json.RawMessage, error) {
	flagsRaw, ok := d.values["flags"]
	if !ok {
		return nil, nil, fmt.Errorf("game flag document has no \"flags\" object")
	}
	keys, vals, err := decodeOrdered(flagsRaw)
	if err != nil {
		return nil, nil, fmt.Errorf("decode flags object: %w", err)
	}
	return keys, vals, nil
}

// putFlag writes a mutated flag back into the document, preserving the order of
// both the flags object and every untouched sibling flag's raw bytes.
func (d *document) putFlag(name string, f *flagObj) error {
	keys, vals, err := d.flagsObject()
	if err != nil {
		return err
	}
	encoded, err := f.marshal()
	if err != nil {
		return err
	}
	vals[name] = encoded
	d.values["flags"] = encodeOrdered(keys, vals)
	return nil
}

// set replaces (or appends, preserving order) one key of the flag object.
func (f *flagObj) set(key string, value any) error {
	raw, err := json.Marshal(value)
	if err != nil {
		return fmt.Errorf("marshal flag field %q: %w", key, err)
	}
	if _, exists := f.values[key]; !exists {
		f.keys = append(f.keys, key)
	}
	f.values[key] = raw
	return nil
}

// setRaw replaces (or appends, preserving order) one key with already-encoded
// JSON. Used to write a targeting block whose raw bytes we have constructed.
func (f *flagObj) setRaw(key string, raw json.RawMessage) {
	if _, exists := f.values[key]; !exists {
		f.keys = append(f.keys, key)
	}
	f.values[key] = raw
}

// get decodes one key of the flag object into v. ok=false when the key is
// absent.
func (f *flagObj) get(key string, v any) (bool, error) {
	raw, ok := f.values[key]
	if !ok {
		return false, nil
	}
	if err := json.Unmarshal(raw, v); err != nil {
		return false, fmt.Errorf("decode flag field %q: %w", key, err)
	}
	return true, nil
}

// raw returns the exact bytes stored for a key (nil when absent). Used to read a
// pre-existing targeting block to compose under the shadow branch.
func (f *flagObj) raw(key string) (json.RawMessage, bool) {
	r, ok := f.values[key]
	return r, ok
}

func (f *flagObj) marshal() (json.RawMessage, error) {
	return encodeOrdered(f.keys, f.values), nil
}

// marshal renders the whole document with a 2-space indent and trailing newline,
// matching lib/flag_config_writer.py's json.dump(indent=2)+"\n" so admin-mode
// and agent writes produce byte-identical formatting.
func (d *document) marshal() ([]byte, error) {
	compact := encodeOrdered(d.keys, d.values)
	var buf bytes.Buffer
	if err := json.Indent(&buf, compact, "", "  "); err != nil {
		return nil, fmt.Errorf("indent game flag document: %w", err)
	}
	buf.WriteByte('\n')
	return buf.Bytes(), nil
}

// decodeOrdered decodes a JSON object into its key order plus a map of raw
// values, so callers can re-emit untouched entries byte-for-byte.
func decodeOrdered(raw []byte) ([]string, map[string]json.RawMessage, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	tok, err := dec.Token()
	if err != nil {
		return nil, nil, err
	}
	if d, ok := tok.(json.Delim); !ok || d != '{' {
		return nil, nil, fmt.Errorf("expected JSON object, got %v", tok)
	}
	var keys []string
	vals := map[string]json.RawMessage{}
	for dec.More() {
		keyTok, err := dec.Token()
		if err != nil {
			return nil, nil, err
		}
		key := keyTok.(string)
		var v json.RawMessage
		if err := dec.Decode(&v); err != nil {
			return nil, nil, err
		}
		if _, dup := vals[key]; !dup {
			keys = append(keys, key)
		}
		vals[key] = v
	}
	return keys, vals, nil
}

// encodeOrdered re-emits an object in the given key order. Values are already
// raw JSON, so untouched entries are byte-stable.
func encodeOrdered(keys []string, vals map[string]json.RawMessage) json.RawMessage {
	var buf bytes.Buffer
	buf.WriteByte('{')
	for i, k := range keys {
		if i > 0 {
			buf.WriteByte(',')
		}
		kb, _ := json.Marshal(k)
		buf.Write(kb)
		buf.WriteByte(':')
		buf.Write(vals[k])
	}
	buf.WriteByte('}')
	return buf.Bytes()
}
