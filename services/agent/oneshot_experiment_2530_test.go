package main

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"testing"

	"github.com/joustmania/agent/patterngame"
)

// UC: UC-040 BR-036
// oneshot_experiment_2530_test.go: a one-shot game bound to an experiment arm prints the
// experiment id and arm in its replay record, so scripts/agent-group-ab.sh can tell an agent
// that honoured SHADOW_GAME_EXPERIMENT_ID / SHADOW_GAME_ARM from one that ignored them (#2530).

func replayRecordOf(t *testing.T, rec patterngame.Record) map[string]any {
	t.Helper()
	var buf bytes.Buffer
	logReplayRecord(slog.New(slog.NewJSONHandler(&buf, nil)), rec)
	var line struct {
		Record string `json:"record"`
	}
	if err := json.Unmarshal(buf.Bytes(), &line); err != nil {
		t.Fatalf("log line %q: %v", buf.String(), err)
	}
	var out map[string]any
	if err := json.Unmarshal([]byte(line.Record), &out); err != nil {
		t.Fatalf("record %q: %v", line.Record, err)
	}
	return out
}

func TestReplayRecord_ExperimentBinding(t *testing.T) {
	t.Run("UC-040/BR-036 a bound one-shot game carries experiment_id and arm", func(t *testing.T) {
		rec := patterngame.Record{Info: patterngame.Info{GameID: "g-1", ExperimentID: "group-ab-1-2", Arm: "B", PairIndex: -1}}
		got := replayRecordOf(t, rec)
		if got["experiment_id"] != "group-ab-1-2" || got["arm"] != "B" {
			t.Fatalf("record = %v, want experiment_id group-ab-1-2 and arm B", got)
		}
	})
	t.Run("UC-040/BR-036 an unbound one-shot game has neither key (record unchanged)", func(t *testing.T) {
		got := replayRecordOf(t, patterngame.Record{Info: patterngame.Info{GameID: "g-2", PairIndex: -1}})
		if _, ok := got["experiment_id"]; ok {
			t.Errorf("experiment_id present: %v", got)
		}
		if _, ok := got["arm"]; ok {
			t.Errorf("arm present: %v", got)
		}
	})
}
