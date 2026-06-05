package actions

import (
	"bytes"
	"encoding/json"
	"fmt"
)

// orderedDoc is a minimal order-preserving model of the flagd interventions
// file. encoding/json drops object key order, which would reshuffle the file on
// every write and defeat the "preserve unrelated flags byte-for-byte" contract.
// We keep the top-level objects we mutate (the document root and each flag) as
// ordered key lists, and store every value we do NOT touch as the exact
// json.RawMessage we read — so untouched flags round-trip byte-for-byte.
type orderedDoc struct {
	keys   []string
	values map[string]json.RawMessage
}

// flagObj is an order-preserving view of a single flag object.
type flagObj struct {
	keys   []string
	values map[string]json.RawMessage
}

// parseDoc parses the interventions file into an order-preserving document.
func parseDoc(raw []byte) (*orderedDoc, error) {
	keys, vals, err := decodeOrdered(raw)
	if err != nil {
		return nil, fmt.Errorf("parse interventions document: %w", err)
	}
	return &orderedDoc{keys: keys, values: vals}, nil
}

// flag returns an order-preserving view of the named flag, or an error if it is
// absent (the agent never invents flags — the schema file ships them all).
func (d *orderedDoc) flag(name string) (*flagObj, error) {
	flagsRaw, ok := d.values["flags"]
	if !ok {
		return nil, fmt.Errorf("interventions document has no \"flags\" object")
	}
	_, vals, err := decodeOrdered(flagsRaw)
	if err != nil {
		return nil, fmt.Errorf("decode flags object: %w", err)
	}
	fr, ok := vals[name]
	if !ok {
		return nil, fmt.Errorf("flag %q not present in interventions file", name)
	}
	fkeys, fvals, err := decodeOrdered(fr)
	if err != nil {
		return nil, fmt.Errorf("decode flag %q: %w", name, err)
	}
	return &flagObj{keys: fkeys, values: fvals}, nil
}

// putFlag writes a mutated flag back into the document, preserving the order of
// both the flags object and every untouched sibling flag's raw bytes.
func (d *orderedDoc) putFlag(name string, f *flagObj) error {
	flagsRaw, ok := d.values["flags"]
	if !ok {
		return fmt.Errorf("interventions document has no \"flags\" object")
	}
	keys, vals, err := decodeOrdered(flagsRaw)
	if err != nil {
		return fmt.Errorf("decode flags object: %w", err)
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

// get decodes one key of the flag object into v.
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

// delete removes a key from the flag object (and its order slot) if present.
func (f *flagObj) delete(key string) {
	if _, ok := f.values[key]; !ok {
		return
	}
	delete(f.values, key)
	for i, k := range f.keys {
		if k == key {
			f.keys = append(f.keys[:i], f.keys[i+1:]...)
			break
		}
	}
}

func (f *flagObj) marshal() (json.RawMessage, error) {
	return encodeOrdered(f.keys, f.values), nil
}

// marshal renders the whole document with a 2-space indent and trailing
// newline, matching lib/flag_config_writer.py's json.dump(indent=2)+"\n" so
// admin-mode and agent writes produce byte-identical formatting.
func (d *orderedDoc) marshal() ([]byte, error) {
	compact := encodeOrdered(d.keys, d.values)
	var buf bytes.Buffer
	if err := json.Indent(&buf, compact, "", "  "); err != nil {
		return nil, fmt.Errorf("indent interventions document: %w", err)
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
