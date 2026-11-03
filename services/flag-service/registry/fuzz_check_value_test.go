package registry

import (
	"bytes"
	"encoding/json"
	"net/http"
	"path/filepath"
	"testing"

	"github.com/joustmania/flag-service/baseline"
	"github.com/joustmania/flag-service/fserr"
)

// FuzzCheckValue checks CheckValue against every non-event policy of the real
// registry (#2370). A plain `go test` runs the seed corpus only, so it is
// deterministic; `go test -fuzz=FuzzCheckValue ./registry/` explores further.
// The caller (the HTTP API) hands it a valid JSON value or a variant name,
// exactly one of the two. Properties:
//
//   - it never panics, and answers exactly one of a result or an error;
//   - a refusal is 422 out_of_bounds;
//   - an accepted declared variant is a variant of the flag, inside the
//     bounds' variant subset, and its value is that variant's value;
//   - an accepted free value is valid JSON within the policy's bounds (a range
//     bound gets a number within the range), and checking it again gives the
//     same answer (the check is idempotent: a stored value revalidates, which
//     the state file restore relies on).
//
// UC: UC-053 A5
func FuzzCheckValue(f *testing.F) {
	b, err := baseline.Load(filepath.Join(root(f), "..", "flagd", "ci"), baseline.DefaultFiles)
	if err != nil {
		f.Fatal(err)
	}
	reg, err := LoadFile(filepath.Join(root(f), "registry.yaml"), b)
	if err != nil {
		f.Fatal(err)
	}
	var policies []*Policy
	for _, p := range reg.Policies() {
		if p.Kind != KindEvent {
			policies = append(policies, p)
		}
	}
	if len(policies) == 0 {
		f.Fatal("no non-event policies")
	}
	seeds := []struct {
		variant string
		value   string
	}{
		{"medium", ""}, {"nope", ""}, {"on", ""}, {"default", ""},
		{"", "2"}, {"", "0"}, {"", "-1"}, {"", "2.5"}, {"", "1e400"}, {"", "1e-400"}, {"", "-0"},
		{"", `"x"`}, {"", `""`}, {"", `"1.2:8:ease"`}, {"", `"warn"`}, {"", `"tighten:5:0.5"`},
		{"", "true"}, {"", "null"}, {"", "[]"}, {"", "{}"}, {"", `{"a":1}`}, {"", ` 3 `},
		{"", `{"min_music_fast_time":4,"max_music_fast_time":8,"min_music_slow_time":10,"max_music_slow_time":23,"end_min_music_fast_time":6,"end_max_music_fast_time":10,"end_min_music_slow_time":10,"end_max_music_slow_time":20}`},
	}
	for i := range policies {
		for _, s := range seeds {
			f.Add(uint16(i), s.variant, []byte(s.value))
		}
	}
	f.Fuzz(func(t *testing.T, idx uint16, variant string, value []byte) {
		p := policies[int(idx)%len(policies)]
		flag := b.Flag(p.Set, p.Key)
		var raw json.RawMessage
		if variant == "" {
			if !json.Valid(value) {
				t.Skip("the API only passes valid JSON")
			}
			raw = json.RawMessage(value)
		}
		got, ferr := CheckValue(p, flag, variant, raw)
		if (got == nil) == (ferr == nil) {
			t.Fatalf("%s: want exactly one of result and error, got %+v and %v", p.Key, got, ferr)
		}
		if ferr != nil {
			if ferr.Status != http.StatusUnprocessableEntity || ferr.Reason != fserr.OutOfBounds {
				t.Fatalf("%s: refusal %d %s, want 422 %s", p.Key, ferr.Status, ferr.Reason, fserr.OutOfBounds)
			}
			return
		}
		if !json.Valid(got.Value) {
			t.Fatalf("%s: accepted value %q is not valid JSON", p.Key, got.Value)
		}
		if got.Variant != "" {
			declared, ok := flag.Variants[got.Variant]
			if !ok {
				t.Fatalf("%s: accepted variant %q is not declared", p.Key, got.Variant)
			}
			if len(p.Bounds.Variants) > 0 && !contains(p.Bounds.Variants, got.Variant) {
				t.Fatalf("%s: accepted variant %q outside %v", p.Key, got.Variant, p.Bounds.Variants)
			}
			if !bytes.Equal(declared, got.Value) {
				t.Fatalf("%s: variant %q carries %s, declared %s", p.Key, got.Variant, got.Value, declared)
			}
		} else if p.Bounds.Range != nil {
			var n float64
			if err := json.Unmarshal(got.Value, &n); err != nil {
				t.Fatalf("%s: range bound accepted a non-number %s", p.Key, got.Value)
			}
			if n < p.Bounds.Range[0] || n > p.Bounds.Range[1] {
				t.Fatalf("%s: accepted %v outside %v", p.Key, n, p.Bounds.Range)
			}
		}
		againVariant, againValue := "", got.Value
		if variant != "" {
			againVariant, againValue = got.Variant, nil
		}
		again, ferr := CheckValue(p, flag, againVariant, againValue)
		if ferr != nil || again.Variant != got.Variant || !bytes.Equal(again.Value, got.Value) {
			t.Fatalf("%s: re-checking the accepted %s gave %+v %v, want variant %q", p.Key, got.Value, again, ferr, got.Variant)
		}
	})
}
