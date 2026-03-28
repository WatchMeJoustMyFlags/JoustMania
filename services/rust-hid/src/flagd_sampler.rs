//! FlagdSampler — Dynamic trace sampling via OpenFeature/flagd.
//!
//! Reads the `trace_sampling_rate` flag (float 0.0–1.0) from flagd
//! on every sampling decision. Falls back to 1.0 (sample all) if
//! flagd is unavailable.

use opentelemetry::trace::{Link, SamplingDecision, SamplingResult, SpanKind, TraceId, TraceState};
use opentelemetry_sdk::trace::ShouldSample;
use opentelemetry::KeyValue;
use std::sync::atomic::{AtomicU64, Ordering};

/// Shared cached sampling rate between the reader and background updater.
/// Initialized to f64::to_bits(1.0) so we sample all spans until the updater sets the real value.
static CACHED_RATE: AtomicU64 = AtomicU64::new(0x3FF0_0000_0000_0000);

/// Sampler that reads trace_sampling_rate from flagd.
#[derive(Debug, Clone, Default)]
pub struct FlagdSampler;

impl FlagdSampler {
    pub fn new() -> Self {
        Self
    }
}

impl ShouldSample for FlagdSampler {
    fn should_sample(
        &self,
        _parent_context: Option<&opentelemetry::Context>,
        trace_id: TraceId,
        _name: &str,
        _span_kind: &SpanKind,
        _attributes: &[KeyValue],
        _links: &[Link],
    ) -> SamplingResult {
        let rate = get_sampling_rate_sync();

        if rate >= 1.0 {
            return SamplingResult {
                decision: SamplingDecision::RecordAndSample,
                attributes: Vec::new(),
                trace_state: TraceState::default(),
            };
        }

        if rate <= 0.0 {
            return SamplingResult {
                decision: SamplingDecision::Drop,
                attributes: Vec::new(),
                trace_state: TraceState::default(),
            };
        }

        // Deterministic sampling based on trace ID for consistency
        let trace_id_bytes = trace_id.to_bytes();
        let hash = u64::from_be_bytes(trace_id_bytes[8..16].try_into().unwrap_or([0; 8]));
        let threshold = (rate * u64::MAX as f64) as u64;

        let decision = if hash < threshold {
            SamplingDecision::RecordAndSample
        } else {
            SamplingDecision::Drop
        };

        SamplingResult {
            decision,
            attributes: Vec::new(),
            trace_state: TraceState::default(),
        }
    }
}

/// Return the cached sampling rate.
/// Always valid — initialized to 1.0, updated by background task.
fn get_sampling_rate_sync() -> f64 {
    f64::from_bits(CACHED_RATE.load(Ordering::Relaxed))
}

/// Spawn a background task that periodically updates the cached sampling rate.
/// Call this once during initialization.
pub fn spawn_rate_updater() {
    tokio::spawn(async move {
        loop {
            let rate = get_sampling_rate_async().await;
            CACHED_RATE.store(rate.to_bits(), Ordering::Relaxed);
            tokio::time::sleep(std::time::Duration::from_secs(2)).await;
        }
    });
}

async fn get_sampling_rate_async() -> f64 {
    use open_feature::{EvaluationContext, OpenFeature};

    let of = OpenFeature::singleton().await;
    let client = of.create_client();
    let ctx = EvaluationContext::default();
    client
        .get_float_value("trace_sampling_rate", Some(&ctx), None)
        .await
        .unwrap_or(1.0)
}
