//! FlagdSpanEnrichmentLayer — Configurable span attribute enrichment.
//!
//! Reads the `span_enrichment_config` structured flag from flagd and adds
//! diagnostic attributes to spans. Uses the same attribute sets as the
//! Python and Go implementations for cross-language consistency.

use open_feature::{EvaluationContext, OpenFeature};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::OnceLock;
use tracing_subscriber::layer::Context;
use tracing_subscriber::Layer;

/// Cached enrichment state (updated by background task).
static ENRICHMENT_ENABLED: AtomicBool = AtomicBool::new(false);

// Which attribute sets are active (stored as bitflags for simplicity)
static ATTR_SETS: std::sync::atomic::AtomicU8 = std::sync::atomic::AtomicU8::new(0);

const SET_SERVICE_IDENTITY: u8 = 1;
const SET_FLAG_VALUES: u8 = 2;
const SET_RUNTIME: u8 = 4;
const SET_SYSTEM: u8 = 8;

// Cached values for hot-path span enrichment (immutable after first read)
static HOSTNAME: OnceLock<String> = OnceLock::new();
static SERVICE_NAME: OnceLock<String> = OnceLock::new();
static SERVICE_NS: OnceLock<String> = OnceLock::new();

/// A tracing Layer that adds enrichment attributes to spans.
pub struct FlagdSpanEnrichmentLayer;

impl<S> Layer<S> for FlagdSpanEnrichmentLayer
where
    S: tracing::Subscriber + for<'span> tracing_subscriber::registry::LookupSpan<'span>,
{
    fn on_new_span(
        &self,
        _attrs: &tracing::span::Attributes<'_>,
        id: &tracing::span::Id,
        ctx: Context<'_, S>,
    ) {
        if !ENRICHMENT_ENABLED.load(Ordering::Relaxed) {
            return;
        }

        let sets = ATTR_SETS.load(Ordering::Relaxed);
        if sets == 0 {
            return;
        }

        let span = ctx.span(id);
        if let Some(span) = span {
            let mut extensions = span.extensions_mut();

            // We use OtelData if available, otherwise skip
            // The attributes will be added when the span is exported
            if let Some(otel_data) = extensions
                .get_mut::<tracing_opentelemetry::OtelData>()
            {
                let builder = &mut otel_data.builder;

                let mut attrs = Vec::new();

                if sets & SET_SERVICE_IDENTITY != 0 {
                    let svc = SERVICE_NAME.get_or_init(|| {
                        std::env::var("OTEL_SERVICE_NAME").unwrap_or_else(|_| "unknown".into())
                    });
                    let ns = SERVICE_NS.get_or_init(|| {
                        std::env::var("OTEL_SERVICE_NAMESPACE").unwrap_or_else(|_| "unknown".into())
                    });
                    attrs.push(opentelemetry::KeyValue::new("demo.enriched", true));
                    attrs.push(opentelemetry::KeyValue::new("demo.language", "rust"));
                    attrs.push(opentelemetry::KeyValue::new(
                        "demo.service",
                        svc.clone(),
                    ));
                    attrs.push(opentelemetry::KeyValue::new(
                        "demo.namespace",
                        ns.clone(),
                    ));
                }

                if sets & SET_FLAG_VALUES != 0 {
                    // TODO: read actual flag values from the OpenFeature client
                    // (requires async access or a second cache layer). For now,
                    // emit a marker so dashboards can detect the set is active.
                    attrs.push(opentelemetry::KeyValue::new(
                        "demo.flag.enrichment_active",
                        true,
                    ));
                }

                if sets & SET_RUNTIME != 0 {
                    attrs.push(opentelemetry::KeyValue::new(
                        "demo.runtime.version",
                        env!("CARGO_PKG_VERSION"),
                    ));
                    attrs.push(opentelemetry::KeyValue::new(
                        "demo.runtime.pid",
                        std::process::id() as i64,
                    ));
                    // Thread count: use available parallelism as a proxy since
                    // tokio runtime metrics require the tokio_unstable cfg flag.
                    attrs.push(opentelemetry::KeyValue::new(
                        "demo.runtime.threads",
                        num_cpus() as i64,
                    ));
                }

                if sets & SET_SYSTEM != 0 {
                    let hostname = HOSTNAME.get_or_init(resolve_host_name);
                    attrs.push(opentelemetry::KeyValue::new(
                        "demo.system.hostname",
                        hostname.clone(),
                    ));
                    attrs.push(opentelemetry::KeyValue::new(
                        "demo.system.cpu_count",
                        num_cpus() as i64,
                    ));
                    attrs.push(opentelemetry::KeyValue::new(
                        "demo.system.os_type",
                        std::env::consts::OS,
                    ));
                }

                if !attrs.is_empty() {
                    let existing = builder.attributes.get_or_insert_with(Vec::new);
                    existing.extend(attrs);
                }
            }
        }
    }
}

/// Resolve host.name from OTEL_RESOURCE_ATTRIBUTES if set, falling back to
/// gethostname(). Inside Docker, gethostname() returns the container ID;
/// OTEL_RESOURCE_ATTRIBUTES provides the real host name.
fn resolve_host_name() -> String {
    if let Ok(attrs) = std::env::var("OTEL_RESOURCE_ATTRIBUTES") {
        for pair in attrs.split(',') {
            if let Some((key, value)) = pair.split_once('=')
                && key.trim() == "host.name"
            {
                return value.trim().to_string();
            }
        }
    }
    gethostname::gethostname()
        .into_string()
        .unwrap_or_else(|_| "unknown".into())
}

fn num_cpus() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1)
}

/// Spawn a background task that periodically updates enrichment config from flagd.
pub fn spawn_enrichment_updater() {
    tokio::spawn(async move {
        loop {
            update_enrichment_config().await;
            tokio::time::sleep(std::time::Duration::from_secs(2)).await;
        }
    });
}

/// Map a flagd variant name to (enabled, attribute_sets) config.
/// These mirror the variants defined in `services/flagd/performance.json`.
fn variant_to_config(variant: &str) -> (bool, u8) {
    match variant {
        "basic" => (true, SET_SERVICE_IDENTITY),
        "flags" => (true, SET_SERVICE_IDENTITY | SET_FLAG_VALUES),
        "full" => (
            true,
            SET_SERVICE_IDENTITY | SET_FLAG_VALUES | SET_RUNTIME | SET_SYSTEM,
        ),
        // "off" and any unknown variant
        _ => (false, 0),
    }
}

async fn update_enrichment_config() {
    let of = OpenFeature::singleton().await;
    let client = of.create_client();
    let ctx = EvaluationContext::default();

    // span_enrichment_config is an object-typed flag in flagd. The Rust
    // OpenFeature SDK's get_struct_value requires TryFrom<StructValue>,
    // which serde_json::Value doesn't implement. Instead we fetch the
    // resolved variant name via get_string_value and map it locally to
    // the known (enabled, attribute_sets) configuration.
    let variant = client
        .get_string_value("span_enrichment_config", Some(&ctx), None)
        .await
        .unwrap_or_else(|_| "off".into());

    let (enabled, sets) = variant_to_config(&variant);
    ENRICHMENT_ENABLED.store(enabled, Ordering::Relaxed);
    ATTR_SETS.store(sets, Ordering::Relaxed);
}
