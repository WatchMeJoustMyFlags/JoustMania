package claude

import (
	"context"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/semconv/v1.41.0/genaiconv"

	"github.com/joustmania/agent/decide/backends/internal/chathttp"
)

// GenAI client metrics (semconv gen_ai.client.operation.duration and
// gen_ai.client.token.usage). Instruments are created once from the global
// meter provider; the global delegates to the real provider once otel.go
// installs it, so creation order does not matter.
var (
	metricsOnce sync.Once
	opDuration  genaiconv.ClientOperationDuration
	tokenUsage  genaiconv.ClientTokenUsage
)

func instruments() (genaiconv.ClientOperationDuration, genaiconv.ClientTokenUsage) {
	metricsOnce.Do(func() {
		meter := otel.Meter(instrumentationName)
		// On error the constructors return a no-op instrument; metrics are
		// best effort and never fail a decision.
		opDuration, _ = genaiconv.NewClientOperationDuration(meter, chathttp.OperationDurationBucketsOption())
		tokenUsage, _ = genaiconv.NewClientTokenUsage(meter, chathttp.TokenUsageBucketsOption())
	})
	return opDuration, tokenUsage
}

func (b *Backend) metricAttrs(responseModel string) []attribute.KeyValue {
	attrs := []attribute.KeyValue{
		opDuration.AttrRequestModel(b.cfg.Model),
		opDuration.AttrServerAddress(b.host),
		opDuration.AttrServerPort(b.port),
	}
	if responseModel != "" {
		attrs = append(attrs, opDuration.AttrResponseModel(responseModel))
	}
	return attrs
}

func recordDuration(ctx context.Context, b *Backend, responseModel, errType string, d time.Duration) {
	dur, _ := instruments()
	attrs := b.metricAttrs(responseModel)
	if errType != "" {
		attrs = append(attrs, dur.AttrErrorType(genaiconv.ErrorTypeAttr(errType)))
	}
	dur.Record(context.WithoutCancel(ctx), d.Seconds(),
		genaiconv.OperationNameChat, genaiconv.ProviderNameAnthropic, attrs...)
}

func recordTokens(ctx context.Context, b *Backend, responseModel string, input, output int) {
	_, usage := instruments()
	attrs := b.metricAttrs(responseModel)
	ctx = context.WithoutCancel(ctx)
	usage.Record(ctx, int64(input), genaiconv.OperationNameChat, genaiconv.ProviderNameAnthropic,
		genaiconv.TokenTypeInput, attrs...)
	usage.Record(ctx, int64(output), genaiconv.OperationNameChat, genaiconv.ProviderNameAnthropic,
		genaiconv.TokenTypeOutput, attrs...)
}
