//! rust-hid: PS Move HID gRPC service for JoustMania.
//!
//! Serves PairingService and ControllerIOService RPCs on port 50058
//! with gRPC health checking.

mod controller_io_service;
mod device_manager;
mod hid;
mod service;

use controller_io_service::ControllerIoServiceImpl;
use service::proto::controller_io_service_server::ControllerIoServiceServer;
use service::proto::pairing_service_server::PairingServiceServer;
use service::PairingServiceImpl;
use tonic::transport::Server;
use tonic_health::server::health_reporter;
use tracing::info;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Initialize tracing with optional OpenTelemetry bridge
    let _otel_guard = init_tracing();

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

    // ControllerIOService — start device manager on a dedicated thread
    let (cmd_tx, cmd_rx) = std::sync::mpsc::channel();
    std::thread::Builder::new()
        .name("device-manager".into())
        .spawn(move || device_manager::run_device_manager(cmd_rx))
        .expect("Failed to spawn device manager thread");

    let controller_io_service = ControllerIoServiceImpl::new(cmd_tx);

    health_reporter
        .set_serving::<ControllerIoServiceServer<ControllerIoServiceImpl>>()
        .await;

    Server::builder()
        .add_service(health_service)
        .add_service(PairingServiceServer::new(pairing_service))
        .add_service(ControllerIoServiceServer::new(controller_io_service))
        .serve(addr)
        .await?;

    Ok(())
}

/// Initialize tracing subscriber with optional OpenTelemetry bridge.
///
/// When OTEL_EXPORTER_OTLP_ENDPOINT is set, traces are exported via OTLP
/// with the tracing-opentelemetry bridge so `tracing` spans become OTEL spans.
/// Returns a guard that shuts down the tracer provider on drop.
fn init_tracing() -> Option<opentelemetry_sdk::trace::TracerProvider> {
    use opentelemetry::trace::TracerProvider as _;
    use opentelemetry::KeyValue;
    use opentelemetry_otlp::WithExportConfig;
    use tracing_opentelemetry::OpenTelemetryLayer;
    use tracing_subscriber::layer::SubscriberExt;
    use tracing_subscriber::util::SubscriberInitExt;

    let env_filter =
        EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    let fmt_layer = tracing_subscriber::fmt::layer();

    let endpoint = std::env::var("OTEL_EXPORTER_OTLP_ENDPOINT").ok();

    if let Some(endpoint) = endpoint {
        let service_name =
            std::env::var("OTEL_SERVICE_NAME").unwrap_or_else(|_| "rust-hid".into());
        let namespace =
            std::env::var("OTEL_SERVICE_NAMESPACE").unwrap_or_else(|_| "infrastructure".into());

        let exporter = opentelemetry_otlp::SpanExporter::builder()
            .with_tonic()
            .with_endpoint(&endpoint)
            .build()
            .ok()?;

        let resource = opentelemetry_sdk::Resource::new(vec![
            KeyValue::new("service.name", service_name.clone()),
            KeyValue::new("service.namespace", namespace),
        ]);

        let provider = opentelemetry_sdk::trace::TracerProvider::builder()
            .with_batch_exporter(exporter, opentelemetry_sdk::runtime::Tokio)
            .with_resource(resource)
            .build();

        opentelemetry::global::set_tracer_provider(provider.clone());

        let tracer = provider.tracer(service_name);
        let otel_layer = OpenTelemetryLayer::new(tracer);

        tracing_subscriber::registry()
            .with(env_filter)
            .with(fmt_layer)
            .with(otel_layer)
            .init();

        Some(provider)
    } else {
        tracing_subscriber::registry()
            .with(env_filter)
            .with(fmt_layer)
            .init();

        None
    }
}
