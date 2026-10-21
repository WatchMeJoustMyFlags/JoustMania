package keysafe

import "testing"

// TestKeySafeBaseURL: a base URL an API key is sent to must be https, or plain
// http only to a loopback host, with a host and without credentials, query or
// fragment (#1774, #1814).
// UC: UC-044 BR-003
// UC: C-016
func TestKeySafeBaseURL(t *testing.T) {
	accepted := []string{
		"https://api.openai.com/v1",
		"https://192.0.2.1/v1", // the talk's blackhole variant
		"http://127.0.0.1:1/v1",
		"http://127.8.9.10/v1",
		"http://localhost:8080/v1",
		"http://LOCALHOST/v1",
		"http://[::1]:9/v1",
		" https://api.typesafe.ai/v1 ",
	}
	for _, raw := range accepted {
		t.Run("UC-044/BR-003 accepts "+raw, func(t *testing.T) {
			if reason, ok := BaseURL(raw); !ok {
				t.Errorf("BaseURL(%q) = (%q, false), want ok", raw, reason)
			}
		})
	}
	rejected := map[string]string{
		"http://fake-openai:8080/v1":        ReasonCleartextRemote,
		"http://192.168.1.5/v1":             ReasonCleartextRemote,
		"http://10.0.0.1/v1":                ReasonCleartextRemote,
		"http://127.0.0.1.nip.io/v1":        ReasonCleartextRemote,
		"http://localhost.evil.test/v1":     ReasonCleartextRemote,
		"ftp://x/v1":                        ReasonMalformed,
		"https://user:pw@api.openai.com/v1": ReasonMalformed,
		"https://h/v1?k=1":                  ReasonMalformed,
		"https://h/v1#f":                    ReasonMalformed,
		"::nope":                            ReasonMalformed,
		"https:///v1":                       ReasonMalformed,
		"":                                  ReasonMalformed,
	}
	for raw, wantReason := range rejected {
		t.Run("UC-044/BR-003 rejects "+raw, func(t *testing.T) {
			reason, ok := BaseURL(raw)
			if ok || reason != wantReason {
				t.Errorf("BaseURL(%q) = (%q, %v), want (%q, false)", raw, reason, ok, wantReason)
			}
		})
	}
}
