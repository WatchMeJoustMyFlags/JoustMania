//! rust-hid: PS Move HID gRPC service for JoustMania.
//!
//! Serves PairingService RPCs on port 50058 with gRPC health checking.
//! Future: will also serve ControllerIO RPCs for motion/LED streaming.

mod hid;
mod service;

use service::proto::pairing_service_server::PairingServiceServer;
use service::PairingServiceImpl;
use tonic::transport::Server;
use tonic_health::server::health_reporter;
use tracing::info;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Initialize tracing
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();

    // Initialize OpenTelemetry if OTLP endpoint is configured
    let _otel_guard = init_otel();

    let addr = "[::]:50058".parse()?;
    info!(%addr, "Starting rust-hid gRPC server");

    // Health checking
    let (mut health_reporter, health_service) = health_reporter();
    health_reporter
        .set_serving::<PairingServiceServer<PairingServiceImpl>>()
        .await;

    // PairingService
    let pairing_service =
        PairingServiceImpl::new().map_err(|e| format!("Failed to init PairingService: {e}"))?;

    Server::builder()
        .add_service(health_service)
        .add_service(PairingServiceServer::new(pairing_service))
        .serve(addr)
        .await?;

    Ok(())
}

/// Initialize OpenTelemetry tracing with OTLP exporter.
///
/// Returns a guard that shuts down the tracer provider on drop.
/// Returns None if OTLP endpoint is not configured.
fn init_otel() -> Option<opentelemetry_sdk::trace::TracerProvider> {
    use opentelemetry::KeyValue;
    use opentelemetry_otlp::WithExportConfig;

    let endpoint = std::env::var("OTEL_EXPORTER_OTLP_ENDPOINT").ok()?;
    let service_name = std::env::var("OTEL_SERVICE_NAME").unwrap_or_else(|_| "rust-hid".into());

    info!(%endpoint, %service_name, "Initializing OpenTelemetry");

    let exporter = opentelemetry_otlp::SpanExporter::builder()
        .with_tonic()
        .with_endpoint(&endpoint)
        .build()
        .ok()?;

    let resource = opentelemetry_sdk::Resource::new(vec![
        KeyValue::new("service.name", service_name),
    ]);

    let provider = opentelemetry_sdk::trace::TracerProvider::builder()
        .with_batch_exporter(exporter, opentelemetry_sdk::runtime::Tokio)
        .with_resource(resource)
        .build();

    opentelemetry::global::set_tracer_provider(provider.clone());

    Some(provider)
}
