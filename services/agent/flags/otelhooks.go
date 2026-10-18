package flags

import (
	"context"
	"errors"
	"regexp"

	otelhooks "github.com/open-feature/go-sdk-contrib/hooks/open-telemetry/pkg"
	"github.com/open-feature/go-sdk/openfeature"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	semconv "go.opentelemetry.io/otel/semconv/v1.34.0"
)

// The standard OpenFeature OpenTelemetry hooks (go-sdk-contrib hooks/open-telemetry, #1427):
//
//   - the traces hook adds one `feature_flag.evaluation` span event per evaluation (OTel feature-flag
//     semantic conventions: feature_flag.key, .result.variant, .result.value, .result.reason,
//     .context.id, .provider.name) to the span in the evaluation's context;
//   - the metrics hook counts `feature_flag.evaluation_requests_total`, `_success_total`
//     (feature_flag.key, feature_flag.provider.name, feature_flag.result.variant, reason),
//     `_error_total` and the `feature_flag.evaluation_active_count` up-down counter.
//
// The Python services register the same metric names (lib/openfeature_otel_hooks.py), so one
// dashboard query covers both languages.

// ErrorMessageAttr is the attribute the standard metrics hook puts on
// feature_flag.evaluation_error_total. Error messages are free text (they can carry addresses
// and flag values), so the agent's MeterProvider drops it (see MetricViews).
const ErrorMessageAttr = "error.message"

// MetricErrorInstrument is the standard hook's error counter.
const MetricErrorInstrument = "feature_flag.evaluation_error_total"

// generatedVariant matches the opaque variant names the flag service generates for written
// values: "r<epoch>_<n>" for a game / experiment scoped value and "o<epoch>_<n>" for a stack
// overlay whose value is not a declared variant (services/flag-service/state/state.go
// nextVariant). A fresh one per write, so as a metric label it would grow without bound.
var generatedVariant = regexp.MustCompile(`^([ro])[0-9a-f]{1,16}_[0-9]+$`)

// MetricVariant is the value of the feature_flag.result.variant metric label for variant: the
// declared variant name, or "scoped_value" / "overlay_value" for a generated one. Spans keep
// the real name.
func MetricVariant(variant string) string {
	m := generatedVariant.FindStringSubmatch(variant)
	if m == nil {
		return variant
	}
	if m[1] == "o" {
		return "overlay_value"
	}
	return "scoped_value"
}

// MetricViews are the agent MeterProvider views that keep the standard hook's metric labels
// bounded: the free-text error.message is dropped from the error counter.
func MetricViews() []sdkmetric.View {
	return []sdkmetric.View{
		sdkmetric.NewView(
			sdkmetric.Instrument{Name: MetricErrorInstrument},
			sdkmetric.Stream{AttributeFilter: attribute.NewDenyKeysFilter(attribute.Key(ErrorMessageAttr))},
		),
	}
}

// MetricActiveInstrument is the standard hook's in-flight up-down counter.
const MetricActiveInstrument = "feature_flag.evaluation_active_count"

// boundedVariantHook passes every call to the standard metrics hook, with two corrections:
//
//   - a generated variant name is replaced by its class (MetricVariant); the evaluation result
//     itself is not changed, After receives a copy of the details;
//   - when the SDK short-circuits a NOT_READY or FATAL provider it runs Error and Finally but
//     not Before (go-sdk client.go evaluate), so the upstream hook's Finally -1 on
//     feature_flag.evaluation_active_count has no matching +1 and the gauge drifts negative.
//     Error adds the missing +1 for exactly those two sentinel errors. The go-sdk has no
//     per-evaluation hook data to carry a "Before ran" marker, so the compensation goes
//     through the same instrument (same meter scope and name, so the SDK returns the same
//     instrument).
type boundedVariantHook struct {
	openfeature.Hook
	active metric.Int64UpDownCounter
}

func (h boundedVariantHook) After(ctx context.Context, hCtx openfeature.HookContext,
	details openfeature.InterfaceEvaluationDetails, hints openfeature.HookHints,
) error {
	details.Variant = MetricVariant(details.Variant)
	return h.Hook.After(ctx, hCtx, details, hints)
}

func (h boundedVariantHook) Error(ctx context.Context, hCtx openfeature.HookContext, err error, hints openfeature.HookHints) {
	h.Hook.Error(ctx, hCtx, err, hints)
	if skippedBefore(err) {
		h.active.Add(ctx, 1, metric.WithAttributes(semconv.FeatureFlagKey(hCtx.FlagKey())))
	}
}

// skippedBefore reports whether err is one of the SDK's provider-state short circuits, which
// run the error and finally hooks without the before hooks.
func skippedBefore(err error) bool {
	var re openfeature.ResolutionError
	if !errors.As(err, &re) {
		return false
	}
	return re == openfeature.ProviderNotReadyError || re == openfeature.ProviderFatalError
}

// NewOTelHooks returns the standard OpenFeature metrics and traces hooks, backed by the global
// MeterProvider and TracerProvider (main sets both before SetupFlagd).
func NewOTelHooks() ([]openfeature.Hook, error) {
	return newOTelHooks(otel.GetMeterProvider())
}

func newOTelHooks(provider metric.MeterProvider) ([]openfeature.Hook, error) {
	metricsHook, err := otelhooks.NewMetricsHookForProvider(provider)
	if err != nil {
		return nil, err
	}
	active, err := provider.Meter(otelhooks.ScopeName).Int64UpDownCounter(MetricActiveInstrument,
		metric.WithDescription("active flag evaluations counter"))
	if err != nil {
		return nil, err
	}
	return []openfeature.Hook{boundedVariantHook{Hook: metricsHook, active: active}, otelhooks.NewTracesHook()}, nil
}
