package decision

import (
	"context"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/decide/llmio"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/telemetry/keys"
)

// prediction_ask_uc040_test.go: the decision side of the model-stated prediction
// (#2479, UC-040 A45, BR-034): the ask is set only with
// decision.predictions.ask_model on and a claude or openai backend, a valid
// model prediction is registered with Source model instead of the lever default,
// and a dropped one registers nothing, is reported on agent.decide and counted.

// parsedAnswer answers grant_shield on A through llmio.Parse, plus prediction
// (a raw answers entry, "" for none).
func parsedAnswer(backend, prediction string) func(decide.Request) (decide.Response, error) {
	return func(req decide.Request) (decide.Response, error) {
		raw := `{"answers": [
  {"id": "ffa.intervention", "choice": "grant_shield", "score": 0, "probabilities": [{"option": "grant_shield", "p": 1}], "confidence": 0.9},
  {"id": "ffa.target", "choice": "A", "score": 0, "probabilities": [{"option": "A", "p": 1}], "confidence": 0.9},
  {"id": "ffa.magnitude", "choice": "", "score": 0.5, "probabilities": [], "confidence": 0.9}`
		if prediction != "" {
			raw += ",\n  " + prediction
		}
		return llmio.Parse(req, []byte(raw+`], "rationale": "shield A"}`), backend)
	}
}

func askSnapshot(backend string, ask bool) flags.Snapshot {
	s := remoteSnapshot(time.Hour)
	s.Decision.Backend = backend
	s.Decision.PredictionsAskModel = ask
	cfg := flags.RemoteBackendConfig{Model: "m", Timeout: time.Hour}
	s.Decision.OpenAI, s.Decision.Jev = cfg, cfg
	return s
}

func droppedCounts(t *testing.T, reader *metric.ManualReader) map[[2]string]int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatalf("collect: %v", err)
	}
	out := map[[2]string]int64{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != MetricPredictionDropped {
				continue
			}
			for _, dp := range m.Data.(metricdata.Sum[int64]).DataPoints {
				b, _ := dp.Attributes.Value("backend")
				r, _ := dp.Attributes.Value("reason")
				out[[2]string{b.AsString(), r.AsString()}] = dp.Value
			}
		}
	}
	return out
}

// UC: UC-040 A45 BR-034
// TestPredictionAsk_FlagAndBackend: the ask is sent only when the flag is on and
// the backend is claude or openai; jev never gets it.
func TestPredictionAsk_FlagAndBackend(t *testing.T) {
	for _, tc := range []struct {
		name    string
		backend string
		ask     bool
		want    bool
	}{
		{"UC-040/A45 BR-034 claude flag on asks", flags.DecisionBackendClaude, true, true},
		{"UC-040/A45 BR-034 openai flag on asks", flags.DecisionBackendOpenAI, true, true},
		{"UC-040/A45 BR-034 claude flag off does not ask", flags.DecisionBackendClaude, false, false},
		{"UC-040/A45 BR-034 openai flag off does not ask", flags.DecisionBackendOpenAI, false, false},
		{"UC-040/A45 BR-034 jev never asks", flags.DecisionBackendJev, true, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			be := newFakeDecideBackend(ffaAnswer("noop", 0.9, "none", 0, "calm"))
			be.name = tc.backend
			h := newRemoteHarness(t, askSnapshot(tc.backend, tc.ask), be)
			h.run(t)
			if len(be.reqs) != 1 {
				t.Fatalf("calls = %d", len(be.reqs))
			}
			req := be.reqs[0]
			if got := req.PredictionAsk != nil; got != tc.want {
				t.Fatalf("ask sent = %v, want %v", got, tc.want)
			}
			if !tc.want {
				v1 := req
				v1.PredictionAsk = nil
				if llmio.Render(req) != llmio.Render(v1) {
					t.Fatal("without the ask the prompt must be the v1 prompt")
				}
				return
			}
			if req.PredictionAsk.ID != decide.QuestionFFAPrediction {
				t.Fatalf("ask = %+v", req.PredictionAsk)
			}
			for _, q := range req.Questions {
				if q.ID == decide.QuestionFFAPrediction {
					t.Fatal("the ask must stay outside Questions")
				}
			}
		})
	}
}

// UC: UC-040 A45 BR-034
// TestPredictionAsk_ModelPredictionRegisters: with the ask, a valid model
// prediction is registered with Source model (it overrides the lever default),
// and no dropped reason is reported.
func TestPredictionAsk_ModelPredictionRegisters(t *testing.T) {
	for _, backend := range []string{flags.DecisionBackendClaude, flags.DecisionBackendOpenAI} {
		t.Run("UC-040/A45 BR-034 "+backend+" model prediction registers", func(t *testing.T) {
			be := newFakeDecideBackend(parsedAnswer(backend,
				`{"id": "ffa.prediction", "choice": "survives_30s", "score": 0, "probabilities": [], "confidence": 0.65}`))
			be.name = backend
			h := newRemoteHarness(t, askSnapshot(backend, true), be)
			b := testBook(t, h.clock)
			h.loop.SetPredictionBook(b)
			span := h.run(t)
			if strAttr(t, span, keys.AttrDecidePredictionSource) != decide.SourceModel {
				t.Fatal("agent.decide must report the model's prediction")
			}
			if _, ok := attrValue(span, keys.AttrDecidePredictionDroppedReason); ok {
				t.Fatal("nothing was dropped")
			}
			rs := actedResults(t, b, remoteContext(), decide.SourceModel)
			if len(rs) != 1 || rs[0].Prediction.Template != decide.TemplateSurvives || rs[0].Prediction.HorizonS != 30 ||
				rs[0].Prediction.P != 0.65 || rs[0].Prediction.Target != "A" || rs[0].Backend != backend {
				t.Fatalf("model results = %+v", rs)
			}
			if lever := actedResults(t, b, remoteContext(), decide.SourceLeverDefault); len(lever) != 0 {
				t.Fatalf("the lever default must not be registered too: %+v", lever)
			}
		})
	}
}

// UC: UC-040 A45 BR-034
// TestPredictionAsk_DroppedNeverBlocks: a dropped model prediction leaves the
// decision acting exactly as without it, registers nothing (no lever default),
// and is reported on agent.decide and in agent_prediction_dropped_total.
func TestPredictionAsk_DroppedNeverBlocks(t *testing.T) {
	for _, tc := range []struct {
		name, backend, entry, reason string
	}{
		{"UC-040/A45 BR-034 claude missing prediction", flags.DecisionBackendClaude, "", decide.PredictionDropMissing},
		{"UC-040/A45 BR-034 openai bad option", flags.DecisionBackendOpenAI,
			`{"id": "ffa.prediction", "choice": "survives_99s", "score": 0, "probabilities": [], "confidence": 0.7}`, decide.PredictionDropBadOption},
		{"UC-040/A45 BR-034 claude bad confidence", flags.DecisionBackendClaude,
			`{"id": "ffa.prediction", "choice": "survives_15s", "score": 0, "probabilities": [], "confidence": 7}`, decide.PredictionDropBadConfidence},
	} {
		t.Run(tc.name, func(t *testing.T) {
			be := newFakeDecideBackend(parsedAnswer(tc.backend, tc.entry))
			be.name = tc.backend
			h := newRemoteHarness(t, askSnapshot(tc.backend, true), be)
			b := testBook(t, h.clock)
			h.loop.SetPredictionBook(b)
			reader := metric.NewManualReader()
			h.loop.predictionDropped = newPredictionDroppedCounter(metric.NewMeterProvider(metric.WithReader(reader)))
			span := h.run(t)
			if got := strAttr(t, span, AttrDecideOutcome); got != DecideOutcomeAct {
				t.Fatalf("outcome = %q, want act: a dropped prediction never blocks", got)
			}
			if got := h.sink.decisions(); len(got) != 1 || got[0].Intervention != "grant_shield" || got[0].TargetSerial != "A" {
				t.Fatalf("applied = %+v", got)
			}
			if got := strAttr(t, span, keys.AttrDecidePredictionDroppedReason); got != tc.reason {
				t.Fatalf("dropped_reason = %q, want %q", got, tc.reason)
			}
			if got := droppedCounts(t, reader); got[[2]string{tc.backend, tc.reason}] != 1 || len(got) != 1 {
				t.Fatalf("agent_prediction_dropped_total = %v", got)
			}
			// Only the base-rate control may be pending: no model prediction and no
			// lever default stands in for the dropped one.
			for _, r := range b.Results(remoteGameID) {
				t.Fatalf("unexpected result before game end: %+v", r)
			}
			end := remoteContext()
			b.OnGameEnd(end)
			for _, r := range b.Results(end.SessionID) {
				if r.Prediction.Source != decide.SourceControl {
					t.Fatalf("a dropped prediction must register nothing, got %+v", r)
				}
			}
		})
	}
}
