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
    // Initialize OTLP log export (must live until end of main to flush)
    let log_provider = init_logs();

    // Initialize tracing with optional OpenTelemetry bridge
    let _otel_guard = init_tracing(&log_provider);

    // Initialize OTEL metrics (process metrics pushed to collector)
    let _meter_provider = init_metrics();

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

/// Initialize OTLP log export.
///
/// When OTEL_EXPORTER_OTLP_ENDPOINT is set, creates a LoggerProvider that
/// exports logs via OTLP. The provider is wired into tracing via the
/// OpenTelemetryTracingBridge layer in `init_tracing()`.
/// The provider must outlive the tracing subscriber to flush on shutdown.
fn init_logs() -> Option<opentelemetry_sdk::logs::LoggerProvider> {
    use opentelemetry::KeyValue;
    use opentelemetry_otlp::WithExportConfig;

    let endpoint = std::env::var("OTEL_EXPORTER_OTLP_ENDPOINT").ok()?;
    let service_name =
        std::env::var("OTEL_SERVICE_NAME").unwrap_or_else(|_| "rust-hid".into());
    let namespace =
        std::env::var("OTEL_SERVICE_NAMESPACE").unwrap_or_else(|_| "infrastructure".into());

    let exporter = opentelemetry_otlp::LogExporter::builder()
        .with_tonic()
        .with_endpoint(&endpoint)
        .build()
        .ok()?;

    let resource = opentelemetry_sdk::Resource::new(vec![
        KeyValue::new("service.name", service_name),
        KeyValue::new("service.namespace", namespace),
    ]);

    let provider = opentelemetry_sdk::logs::LoggerProvider::builder()
        .with_batch_exporter(exporter, opentelemetry_sdk::runtime::Tokio)
        .with_resource(resource)
        .build();

    Some(provider)
}

/// Initialize tracing subscriber with optional OpenTelemetry bridge.
///
/// When OTEL_EXPORTER_OTLP_ENDPOINT is set, traces are exported via OTLP
/// with the tracing-opentelemetry bridge so `tracing` spans become OTEL spans.
/// If a LoggerProvider is given, logs are also exported via OTLP using
/// the OpenTelemetryTracingBridge layer.
/// Returns a guard that shuts down the tracer provider on drop.
fn init_tracing(
    log_provider: &Option<opentelemetry_sdk::logs::LoggerProvider>,
) -> Option<opentelemetry_sdk::trace::TracerProvider> {
    use opentelemetry::trace::TracerProvider as _;
    use opentelemetry::KeyValue;
    use opentelemetry_appender_tracing::layer::OpenTelemetryTracingBridge;
    use opentelemetry_otlp::WithExportConfig;
    use tracing_opentelemetry::OpenTelemetryLayer;
    use tracing_subscriber::layer::SubscriberExt;
    use tracing_subscriber::util::SubscriberInitExt;

    let env_filter =
        EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    let fmt_layer = tracing_subscriber::fmt::layer();
    let logs_layer = log_provider.as_ref().map(OpenTelemetryTracingBridge::new);

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
            .with(logs_layer)
            .init();

        Some(provider)
    } else {
        tracing_subscriber::registry()
            .with(env_filter)
            .with(fmt_layer)
            .with(logs_layer)
            .init();

        None
    }
}

/// Initialize OTEL metrics with process metrics (CPU, memory, threads).
///
/// Reads from /proc/self/stat and /proc/self/status to report:
/// - process_cpu_seconds_total (observable counter)
/// - process_resident_memory_bytes (gauge)
/// - process_threads (gauge)
fn init_metrics() -> Option<opentelemetry_sdk::metrics::SdkMeterProvider> {
    use opentelemetry::metrics::MeterProvider;
    use opentelemetry::KeyValue;
    use opentelemetry_otlp::WithExportConfig;

    let endpoint = std::env::var("OTEL_EXPORTER_OTLP_ENDPOINT").ok()?;
    let service_name =
        std::env::var("OTEL_SERVICE_NAME").unwrap_or_else(|_| "rust-hid".into());
    let namespace =
        std::env::var("OTEL_SERVICE_NAMESPACE").unwrap_or_else(|_| "infrastructure".into());

    let exporter = opentelemetry_otlp::MetricExporter::builder()
        .with_tonic()
        .with_endpoint(&endpoint)
        .build()
        .ok()?;

    let resource = opentelemetry_sdk::Resource::new(vec![
        KeyValue::new("service.name", service_name),
        KeyValue::new("service.namespace", namespace),
    ]);

    let reader = opentelemetry_sdk::metrics::PeriodicReader::builder(
        exporter,
        opentelemetry_sdk::runtime::Tokio,
    )
    .with_interval(std::time::Duration::from_secs(1))
    .build();

    let provider = opentelemetry_sdk::metrics::SdkMeterProvider::builder()
        .with_reader(reader)
        .with_resource(resource)
        .build();

    let meter = provider.meter("process");

    // process_cpu_seconds_total — cumulative CPU time from /proc/self/stat
    // Must be counter (not gauge) to match Python services and Prometheus conventions.
    let _cpu = meter
        .f64_observable_counter("process_cpu_seconds_total")
        .with_description("Total user and system CPU time spent in seconds")
        .with_callback(|observer| {
            if let Ok(me) = procfs::process::Process::myself() {
                if let Ok(stat) = me.stat() {
                    let ticks_per_sec = procfs::ticks_per_second() as f64;
                    let total_secs =
                        (stat.utime as f64 + stat.stime as f64) / ticks_per_sec;
                    observer.observe(total_secs, &[]);
                }
            }
        })
        .build();

    // process_resident_memory_bytes — RSS from /proc/self/stat
    let _mem = meter
        .i64_observable_gauge("process_resident_memory_bytes")
        .with_description("Resident memory size in bytes")
        .with_callback(|observer| {
            if let Ok(me) = procfs::process::Process::myself() {
                if let Ok(stat) = me.stat() {
                    let page_size = procfs::page_size() as i64;
                    let rss_bytes = stat.rss as i64 * page_size;
                    observer.observe(rss_bytes, &[]);
                }
            }
        })
        .build();

    // process_threads — thread count from /proc/self/stat
    let _threads = meter
        .i64_observable_gauge("process_threads")
        .with_description("Number of active threads")
        .with_callback(|observer| {
            if let Ok(me) = procfs::process::Process::myself() {
                if let Ok(stat) = me.stat() {
                    observer.observe(stat.num_threads, &[]);
                }
            }
        })
        .build();

    info!("OTEL metrics initialized (1s export interval)");
    Some(provider)
}
