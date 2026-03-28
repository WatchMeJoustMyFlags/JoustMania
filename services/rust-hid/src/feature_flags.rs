//! Feature flag integration via OpenFeature + flagd.
//!
//! Evaluates the `grpc_rpc_spans` boolean flag from the `performance` domain
//! to control whether gRPC SERVER spans are created. The flag is evaluated
//! on each RPC call to support hot-reload via flagd.

use open_feature::{EvaluationContext, OpenFeature};
use open_feature_flagd::{FlagdOptions, FlagdProvider, ResolverType};
use std::sync::OnceLock;
use tracing::{info, warn};

static INITIALIZED: OnceLock<bool> = OnceLock::new();

/// Initialize the OpenFeature flagd provider.
///
/// Connects to flagd via gRPC (port 8013) for flag evaluation.
/// Safe to call multiple times — only initializes once.
pub async fn init_flagd() {
    if INITIALIZED.get().is_some() {
        return;
    }

    let host = std::env::var("FLAGD_HOST").unwrap_or_else(|_| "flagd".into());
    let port: u16 = std::env::var("FLAGD_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8013);

    info!(host = %host, port = port, "Connecting to flagd");

    match FlagdProvider::new(FlagdOptions {
        host,
        port,
        resolver_type: ResolverType::Rpc,
        ..Default::default()
    })
    .await
    {
        Ok(provider) => {
            let mut of = OpenFeature::singleton_mut().await;
            of.set_provider(provider).await;
            drop(of);
            INITIALIZED.set(true).ok();
            info!("OpenFeature flagd provider initialized");
        }
        Err(e) => {
            warn!(error = %e, "Failed to initialize flagd provider — flags will use defaults");
            INITIALIZED.set(false).ok();
        }
    }
}

/// Build evaluation context with service identity for flag targeting.
fn build_eval_context() -> EvaluationContext {
    let mut ctx = EvaluationContext::default();
    ctx.add_custom_field(
        "service_name",
        std::env::var("OTEL_SERVICE_NAME")
            .unwrap_or_else(|_| "rust-hid".into())
            .into(),
    );
    ctx.add_custom_field("language", "rust".into());
    ctx.add_custom_field(
        "service_namespace",
        std::env::var("OTEL_SERVICE_NAMESPACE")
            .unwrap_or_else(|_| "infrastructure".into())
            .into(),
    );
    ctx.add_custom_field(
        "hostname",
        gethostname::gethostname()
            .into_string()
            .unwrap_or_else(|_| "unknown".into())
            .into(),
    );
    ctx
}

/// Check if the `grpc_rpc_spans` feature flag is enabled.
///
/// Returns false if flagd is unavailable or the flag is not defined.
/// Evaluated at runtime on each call to support hot-reload.
pub async fn grpc_rpc_spans_enabled() -> bool {
    let of = OpenFeature::singleton().await;
    let client = of.create_client();
    let ctx = build_eval_context();
    client
        .get_bool_value("grpc_rpc_spans", Some(&ctx), None)
        .await
        .unwrap_or(false)
}
