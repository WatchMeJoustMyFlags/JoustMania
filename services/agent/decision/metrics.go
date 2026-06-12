package decision

import (
	"go.opentelemetry.io/otel"
	otelmetric "go.opentelemetry.io/otel/metric"
	metricnoop "go.opentelemetry.io/otel/metric/noop"
)

// metrics.go holds the decision loop's OTel instruments. Today that is the LLM
// call-gate counter (#847); the audit trace (#724) carries the rest of the
// loop's observability as spans.

// decisionMeterName scopes the decision loop's OTel instruments, mirroring
// actions/writer.go's meterName convention (module path + package).
const decisionMeterName = "github.com/joustmania/agent/decision"

// newLLMGatedCounter builds the agent_llm_gated_total counter from the given meter
// provider. It is incremented once per gated llm cycle, labeled reason=<fallback
// reason> (llm_not_eligible / llm_interval / llm_budget_exhausted), so the gate's
// effect is observable per layer (#847 acceptance #6). An instrument-creation
// error yields a no-op counter so the Loop always has a usable instrument and
// tests that never wire a real meter provider still work — mirrors
// actions.newWritesCounter.
func newLLMGatedCounter(mp otelmetric.MeterProvider) otelmetric.Int64Counter {
	c, err := mp.Meter(decisionMeterName).Int64Counter(
		"agent_llm_gated_total",
		otelmetric.WithDescription("Total LLM decision cycles denied by the call gate, labeled by fallback reason (#847)"),
	)
	if err != nil {
		c, _ = metricnoop.NewMeterProvider().Meter(decisionMeterName).Int64Counter("agent_llm_gated_total")
	}
	return c
}

// defaultLLMGatedCounter is the no-op fallback counter a Loop uses until one is
// injected (NewLoop wires the global meter provider; tests may leave it). It keeps
// the gate-increment path nil-safe without every test having to set a counter.
func defaultLLMGatedCounter() otelmetric.Int64Counter {
	return newLLMGatedCounter(otel.GetMeterProvider())
}
