package main

import (
	"reflect"
	"testing"
)

// TestParseSeedValue covers the seed-value parser, including the #1090 addition:
// a JSON object/array seed (so an OBJECT-valued game flag such as game.windows or
// game.thresholds — the real FFA difficulty/pacing levers — can be seeded). The
// scalar cases pin the pre-existing number/bool/string behavior so the object
// extension does not silently reinterpret a plain string seed.
func TestParseSeedValue(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		want any
	}{
		{"empty", "", ""},
		{"number_float", "0.75", 0.75},
		{"number_int", "6", 6.0}, // JSON has one number kind; ints decode to float64.
		{"bool_true", "true", true},
		{"bool_false", "false", false},
		{"plain_string", "windows", "windows"},
		// A bare string that merely contains braces mid-text is NOT JSON-composite
		// (does not start with '{'/'[') so it stays a string.
		{"string_with_brace", "a{b}", "a{b}"},
		{
			name: "json_object",
			raw:  `{"min_music_fast_time":6,"max_music_fast_time":11}`,
			want: map[string]any{"min_music_fast_time": 6.0, "max_music_fast_time": 11.0},
		},
		{
			name: "json_array",
			raw:  `[1.6,1.8,2.8,3.2,3.5]`,
			want: []any{1.6, 1.8, 2.8, 3.2, 3.5},
		},
		// Malformed JSON object falls back to the raw string (the Gate then
		// fail-closed rejects the type mismatch at Start).
		{"malformed_object", `{not json`, `{not json`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := parseSeedValue(tc.raw)
			if !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("parseSeedValue(%q) = %#v, want %#v", tc.raw, got, tc.want)
			}
		})
	}
}
