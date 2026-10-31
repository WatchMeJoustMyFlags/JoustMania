package patterngame

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
)

// DigestUnavailable is the flag digest when flagd cannot be read (UC-046 BR-011).
const DigestUnavailable = "unavailable"

// digestKeys are the flags that shape a pattern game (UC-046 BR-011), per flag set.
// An entry ending in "." is a key prefix; "*" means every key of the set.
var digestKeys = map[string][]string{
	"game": {
		"sensitivity", "thresholds", "windows", "death_grace_period_seconds",
		"invincibility_seconds", "countdown_phase_duration_ms", "winner_rainbow_duration_ms", "shadow_policy",
		// Shield activation and hold values shape a game whose archetype presses
		// the trigger (UC-046 BR-018, #2096). Adding them changed every digest.
		"shield.",
	},
	"agent": {
		"enabled", "interventions_enabled", "interventions_allowed", "objectives", "perception.ema_weight",
		"decision.backend", CapFlagKey, "policy.", "fitness.",
	},
	"interventions": {"*"},
}

// DigestKeysCovered reports whether set/key is part of the flag digest.
func DigestKeysCovered(set, key string) bool {
	for _, want := range digestKeys[set] {
		switch {
		case want == "*", want == key:
			return true
		case strings.HasSuffix(want, ".") && strings.HasPrefix(key, want):
			return true
		}
	}
	return false
}

type ofrepBulkResponse struct {
	Flags []struct {
		Key       string `json:"key"`
		Value     any    `json:"value"`
		ErrorCode string `json:"errorCode"`
	} `json:"flags"`
}

// ResolvedFlagDigest is UC-046 BR-011: it evaluates every flag set of digestKeys
// through flagd's OFREP bulk endpoint (baseURL, e.g. http://flagd:8016) with the
// evaluation context, keeps the covered keys, and returns the SHA-256 (lowercase
// hex) of their canonical JSON ({"set/key": value}, keys sorted, no whitespace).
// Any transport or decode error yields DigestUnavailable.
func ResolvedFlagDigest(ctx context.Context, client *http.Client, baseURL string, evalCtx map[string]any) string {
	values := map[string]any{}
	sets := make([]string, 0, len(digestKeys))
	for set := range digestKeys {
		sets = append(sets, set)
	}
	sort.Strings(sets)
	body, err := json.Marshal(map[string]any{"context": evalCtx})
	if err != nil {
		return DigestUnavailable
	}
	for _, set := range sets {
		req, err := http.NewRequestWithContext(ctx, http.MethodPost,
			strings.TrimRight(baseURL, "/")+"/ofrep/v1/evaluate/flags", bytes.NewReader(body))
		if err != nil {
			return DigestUnavailable
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Flagd-Selector", "flagSetId="+set)
		resp, err := client.Do(req)
		if err != nil {
			return DigestUnavailable
		}
		data, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
		_ = resp.Body.Close()
		if err != nil || resp.StatusCode != http.StatusOK {
			return DigestUnavailable
		}
		var bulk ofrepBulkResponse
		if err := json.Unmarshal(data, &bulk); err != nil {
			return DigestUnavailable
		}
		for _, f := range bulk.Flags {
			if DigestKeysCovered(set, f.Key) {
				if f.ErrorCode != "" {
					values[set+"/"+f.Key] = fmt.Sprintf("<error:%s>", f.ErrorCode)
					continue
				}
				values[set+"/"+f.Key] = f.Value
			}
		}
	}
	canonical, err := json.Marshal(values) // map keys (also nested) are emitted sorted
	if err != nil {
		return DigestUnavailable
	}
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:])
}
