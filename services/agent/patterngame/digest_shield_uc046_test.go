package patterngame

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// UC: UC-046 BR-011 BR-018
func TestFlagDigestCoversShieldKeys(t *testing.T) {
	t.Run("UC-046/BR-011 game/shield.* keys are covered and change the digest on purpose", func(t *testing.T) {
		for _, key := range []string{"shield.activation", "shield.energy_max_seconds", "shield.recharge_per_second"} {
			if !DigestKeysCovered("game", key) {
				t.Errorf("game/%s not covered", key)
			}
		}
		if DigestKeysCovered("agent", "shield.activation") || DigestKeysCovered("game", "shieldx") {
			t.Error("the shield. prefix leaks into another set or key")
		}
		values := map[string]map[string]any{
			"game":          {"sensitivity": 2.0, "shield.activation": "trigger", "shield.energy_max_seconds": 5.0},
			"agent":         {"decision.backend": "rules"},
			"interventions": {"shield_seconds": 0.0},
		}
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
			set := strings.TrimPrefix(req.Header.Get("Flagd-Selector"), "flagSetId=")
			var flags []map[string]any
			for k, v := range values[set] {
				flags = append(flags, map[string]any{"key": k, "value": v})
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"flags": flags})
		}))
		defer srv.Close()
		evalCtx := map[string]any{"game_kind": "shadow"}
		got := ResolvedFlagDigest(context.Background(), srv.Client(), srv.URL, evalCtx)
		// Golden over {"agent/decision.backend","game/sensitivity","game/shield.activation",
		// "game/shield.energy_max_seconds","interventions/shield_seconds"}: the shield keys are part of
		// it since #2096, so digests recorded before that are not comparable (BR-011).
		const golden = "c36e30713738d381ed236ee889c8ff2c37f0b6fb786e7a5c9d77eb406af17042"
		if got != golden {
			t.Errorf("digest %s, want %s", got, golden)
		}
		values["game"]["shield.activation"] = "hold"
		if ResolvedFlagDigest(context.Background(), srv.Client(), srv.URL, evalCtx) == got {
			t.Error("a shield.activation change did not change the digest")
		}
	})
}
