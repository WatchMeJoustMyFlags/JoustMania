package llm

import (
	"encoding/json"
	"errors"
	"strings"
)

// decode.go holds the DEFENSIVE JSON extraction shared by the reply decoders
// (DecodeRetro in retro.go). A real model wraps its object in prose or ```json
// fences despite a "no prose" instruction, so the decoders locate the outermost
// JSON object and parse only that.

// ErrNotJSON: no single JSON object could be extracted/parsed from the reply.
var ErrNotJSON = errors.New("llm: response is not a single JSON object")

// extractJSONObject pulls the single JSON object out of a model reply. Models
// frequently wrap the object in prose or in a ```json fence despite the "no
// prose" instruction, so rather than demand a byte-perfect object we locate the
// first '{' and the matching last '}' and parse the span between them. This is
// deliberately forgiving about SURROUNDING text but strict about the object
// itself: the extracted span must parse as a JSON object (json.Valid + leading
// '{'), otherwise ErrNotJSON. It does not attempt to find a SECOND object — the
// contract is exactly one — so trailing junk after the object is ignored, which
// is the common, benign case (a fence close or a stray newline).
func extractJSONObject(s string) ([]byte, error) {
	start := strings.IndexByte(s, '{')
	end := strings.LastIndexByte(s, '}')
	if start < 0 || end < start {
		return nil, ErrNotJSON
	}
	candidate := s[start : end+1]
	if !json.Valid([]byte(candidate)) {
		return nil, ErrNotJSON
	}
	return []byte(candidate), nil
}
