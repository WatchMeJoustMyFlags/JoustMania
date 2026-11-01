package llmio

import (
	"encoding/json"
	"flag"
	"os"
	"path/filepath"
	"testing"
)

// UC: UC-040 A45 BR-034
// prediction_v1_golden_test.go pins the v1 in-game prompt (decide-contract.md
// §3): with Request.PredictionAsk nil (decision.predictions.ask_model off, the
// default and always under v1) the system prompt, the user message and the
// answer schema are byte-identical to the files below, which were written from
// main before #2479 PR D. Never regenerate them to make a change pass: a diff
// here is a change of the talk's prompt.
//
//	go test ./decide/llmio -run TestRenderV1Golden -update-llmio-golden

var updateLLMIOGolden = flag.Bool("update-llmio-golden", false, "rewrite decide/llmio/testdata/v1_*.golden")

func checkGolden(t *testing.T, name string, got []byte) {
	t.Helper()
	path := filepath.Join("testdata", name)
	if *updateLLMIOGolden {
		if err := os.WriteFile(path, got, 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read golden: %v", err)
	}
	if string(got) != string(want) {
		t.Fatalf("%s changed (v1 prompt must stay byte-identical while the ask is off)\n--- got ---\n%s\n--- want ---\n%s", name, got, want)
	}
}

func TestRenderV1Golden(t *testing.T) {
	t.Run("UC-040/A45 BR-034 ask off renders the v1 prompt byte for byte", func(t *testing.T) {
		req := testRequest()
		if req.PredictionAsk != nil {
			t.Fatal("testRequest must not carry an ask")
		}
		p := Render(req)
		checkGolden(t, "v1_system.golden", []byte(p.System))
		checkGolden(t, "v1_user.golden", []byte(p.User))
		schema, err := json.Marshal(AnswerSchema())
		if err != nil {
			t.Fatal(err)
		}
		checkGolden(t, "v1_schema.golden", schema)
	})
}
