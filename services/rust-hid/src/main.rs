//! rust-hid: PS Move HID gRPC service for JoustMania.
//!
//! Serves PairingService and ControllerIOService RPCs on port 50058
//! with gRPC health checking.

mod controller_io_service;
mod device_manager;
mod feature_flags;
mod grpc_tracing;
mod hid;
mod input_slot;
mod metrics;
mod output;
mod service;
mod watchdog;

use controller_io_service::ControllerIoServiceImpl;
use service::PairingServiceImpl;
use service::proto::controller_io_service_server::ControllerIoServiceServer;
use service::proto::pairing_service_server::PairingServiceServer;
use std::sync::{Arc, Mutex};
use tonic::transport::Server;
use tonic_health::server::health_reporter;
use tracing::{info, warn};
use tracing_subscriber::EnvFilter;

/// The gRPC port (health, pairing, controller I/O); the watchdog probes it on loopback.
const GRPC_PORT: u16 = 50058;

// Four runtime workers, pinned (#2125): tokio sizes the pool from
// available_parallelism, which follows the container's cgroup CPU limit, so a
// limit at or below 1 CPU gave ONE worker and any blocking call stalled every RPC.
#[tokio::main(flavor = "multi_thread", worker_threads = 4)]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Initialize OTLP log export (must live until end of main to flush)
    let log_provider = init_logs();

    // Initialize tracing with optional OpenTelemetry bridge
    let otel_guard = init_tracing(&log_provider);
    let flusher = TelemetryFlusher {
        logs: log_provider.clone(),
        traces: otel_guard.clone(),
        metrics: Arc::new(Mutex::new(None)),
    };

    // Register W3C TraceContext propagator for incoming context extraction
    opentelemetry::global::set_text_map_propagator(
        opentelemetry_sdk::propagation::TraceContextPropagator::new(),
    );

    // Output side of the device loop: per-controller writers, rate cap from a flag
    let outputs = device_manager::DeviceOutputs::default();

    // Initialize OTEL metrics (process metrics and the output writers' in-flight age)
    let meter_provider = init_metrics(outputs.router.clone());
    *flusher.metrics.lock().unwrap_or_else(|e| e.into_inner()) = meter_provider;

    // Initialize OpenFeature flagd provider for feature flags
    feature_flags::init_flagd(outputs.rate.clone()).await;

    let addr = format!("[::]:{GRPC_PORT}").parse()?;
    info!(%addr, "Starting rust-hid gRPC server");

    // Health checking
    let (health_reporter, health_service) = health_reporter();
    health_reporter
        .set_serving::<PairingServiceServer<PairingServiceImpl>>()
        .await;

    // PairingService
    let pairing_service =
        PairingServiceImpl::new().map_err(|e| format!("Failed to init PairingService: {e}"))?;

    // ControllerIOService — start device manager on a dedicated thread
    let (cmd_tx, cmd_rx) = std::sync::mpsc::channel();
    let device_heartbeat = watchdog::Heartbeat::new();
    let loop_heartbeat = device_heartbeat.clone();
    let loop_outputs = outputs.clone();
    std::thread::Builder::new()
        .name("device-manager".into())
        .spawn(move || device_manager::run_device_manager(cmd_rx, loop_heartbeat, loop_outputs))
        .expect("Failed to spawn device manager thread");

    // Self-healing (UC-002 A7, NFR-054, #2125): exits when this server or the
    // device loop hangs, so the compose restart policy brings rust-hid back.
    // Its tick also reports a stuck output writer (#2142); that never trips it.
    let mut stuck_writers = output::StuckWriterMonitor::new(output::STUCK_WRITE_THRESHOLD);
    let watched_router = outputs.router.clone();
    watchdog::spawn(
        format!("127.0.0.1:{GRPC_PORT}"),
        device_heartbeat,
        move || {
            stuck_writers.check(&watched_router);
        },
        {
            // Traces and metrics must reach the collector before the exit; the trip reason itself is read in `docker logs rust-hid` (B5, #2373).
            let flusher = flusher.clone();
            move || flusher.flush(FLUSH_BUDGET)
        },
    );

    let controller_io_service =
        ControllerIoServiceImpl::new(cmd_tx.clone(), outputs.router.clone());

    health_reporter
        .set_serving::<ControllerIoServiceServer<ControllerIoServiceImpl>>()
        .await;

    let server = Server::builder()
        .layer(grpc_tracing::GrpcTracingLayer)
        .add_service(health_service)
        .add_service(PairingServiceServer::new(pairing_service))
        .add_service(ControllerIoServiceServer::new(controller_io_service))
        .serve(addr);

    // Not serve_with_shutdown: that waits for the open StreamIO calls, which only
    // end when the client does. On a signal the controllers are switched off
    // first, then the process ends (B5, #2373).
    tokio::select! {
        result = server => result?,
        () = shutdown_signal() => {
            info!("Stop signal received: switching controllers off");
            // A second signal no longer kills (tokio keeps the handlers), so a
            // stuck stop is cut short here.
            force_exit_after(FORCE_EXIT_AFTER, || std::process::exit(0));
            let off_sent = tokio::task::spawn_blocking({
                let router = outputs.router.clone();
                move || device_manager::graceful_stop(&cmd_tx, &router, SHUTDOWN_OFF_BUDGET)
            })
            .await
            .unwrap_or(false);
            if off_sent {
                info!("All controllers switched off");
            } else {
                warn!("Not every controller was switched off within {SHUTDOWN_OFF_BUDGET:?}");
            }
            // Off the runtime workers: the OTLP export needs them while we wait.
            let _ = tokio::task::spawn_blocking(move || flusher.flush(FLUSH_BUDGET)).await;
            std::process::exit(0);
        }
    }

    Ok(())
}

/// How long a stop waits for the writers to send LED off / rumble 0. Docker
/// gives 10 s before SIGKILL.
const SHUTDOWN_OFF_BUDGET: std::time::Duration = std::time::Duration::from_secs(2);

/// How long a flush of logs, traces and metrics may take before the exit.
const FLUSH_BUDGET: std::time::Duration = std::time::Duration::from_secs(1);

/// A stop that has not ended the process after this long is cut short.
const FORCE_EXIT_AFTER: std::time::Duration = std::time::Duration::from_secs(5);

/// Run `exit` on a plain thread after `after`, whatever the runtime is doing.
fn force_exit_after(after: std::time::Duration, exit: impl FnOnce() + Send + 'static) {
    let _ = std::thread::Builder::new()
        .name("force-exit".into())
        .spawn(move || {
            std::thread::sleep(after);
            warn!("Stop did not finish in {after:?}: exiting now");
            exit();
        });
}

/// Resolves on SIGTERM (docker stop) or SIGINT.
async fn shutdown_signal() {
    use tokio::signal::unix::{SignalKind, signal};
    let (Ok(mut term), Ok(mut int)) = (
        signal(SignalKind::terminate()),
        signal(SignalKind::interrupt()),
    ) else {
        warn!("Could not install the stop signal handlers; a stop will not switch controllers off");
        std::future::pending::<()>().await;
        return;
    };
    tokio::select! {
        _ = term.recv() => {}
        _ = int.recv() => {}
    }
}

/// The OTLP providers, flushed before the process exits (stop and watchdog trip).
#[derive(Clone)]
struct TelemetryFlusher {
    logs: Option<opentelemetry_sdk::logs::SdkLoggerProvider>,
    traces: Option<opentelemetry_sdk::trace::SdkTracerProvider>,
    metrics: Arc<Mutex<Option<opentelemetry_sdk::metrics::SdkMeterProvider>>>,
}

impl TelemetryFlusher {
    /// Flush everything, giving up after `budget` (an unreachable collector must
    /// not hold the exit).
    fn flush(&self, budget: std::time::Duration) {
        let this = self.clone();
        let (done_tx, done_rx) = std::sync::mpsc::channel();
        let spawned = std::thread::Builder::new()
            .name("otel-flush".into())
            .spawn(move || {
                if let Some(p) = &this.logs {
                    let _ = p.force_flush();
                }
                if let Some(p) = &this.traces {
                    let _ = p.force_flush();
                }
                if let Some(p) = this
                    .metrics
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .as_ref()
                {
                    let _ = p.force_flush();
                }
                let _ = done_tx.send(());
            });
        if spawned.is_ok() {
            let _ = done_rx.recv_timeout(budget);
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

/// Build a shared OTEL resource with standard OTEL semantic-convention attributes.
fn build_otel_resource() -> opentelemetry_sdk::Resource {
    use opentelemetry::KeyValue;

    let service_name = std::env::var("OTEL_SERVICE_NAME").unwrap_or_else(|_| "rust-hid".into());
    let namespace =
        std::env::var("OTEL_SERVICE_NAMESPACE").unwrap_or_else(|_| "infrastructure".into());
    let version = std::env::var("SERVICE_VERSION").unwrap_or_else(|_| "0.0.0-dev".into());
    let hostname = resolve_host_name();
    let instance_id = std::env::var("HOSTNAME").unwrap_or_else(|_| hostname.clone());
    let environment =
        std::env::var("DEPLOYMENT_ENVIRONMENT").unwrap_or_else(|_| "development".into());

    opentelemetry_sdk::Resource::builder()
        .with_attributes(vec![
            KeyValue::new("container.name", service_name.clone()),
            KeyValue::new("service.name", service_name),
            KeyValue::new("service.namespace", namespace),
            KeyValue::new("service.version", version),
            KeyValue::new("service.instance.id", instance_id),
            KeyValue::new("deployment.environment", environment),
            KeyValue::new("host.name", hostname),
            KeyValue::new("process.pid", std::process::id() as i64),
            KeyValue::new("process.executable.name", "rust-hid"),
            KeyValue::new("process.runtime.name", "rustc"),
            KeyValue::new("telemetry.sdk.name", "opentelemetry"),
            KeyValue::new("telemetry.sdk.language", "rust"),
        ])
        .build()
}

/// Initialize OTLP log export.
///
/// When OTEL_EXPORTER_OTLP_ENDPOINT is set, creates a LoggerProvider that
/// exports logs via OTLP. The provider is wired into tracing via the
/// OpenTelemetryTracingBridge layer in `init_tracing()`.
/// The provider must outlive the tracing subscriber to flush on shutdown.
fn init_logs() -> Option<opentelemetry_sdk::logs::SdkLoggerProvider> {
    use opentelemetry_otlp::WithExportConfig;

    let endpoint = std::env::var("OTEL_EXPORTER_OTLP_ENDPOINT").ok()?;

    let exporter = opentelemetry_otlp::LogExporter::builder()
        .with_tonic()
        .with_endpoint(&endpoint)
        .build()
        .ok()?;

    let resource = build_otel_resource();

    let provider = opentelemetry_sdk::logs::SdkLoggerProvider::builder()
        .with_batch_exporter(exporter)
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
    log_provider: &Option<opentelemetry_sdk::logs::SdkLoggerProvider>,
) -> Option<opentelemetry_sdk::trace::SdkTracerProvider> {
    use opentelemetry::trace::TracerProvider as _;
    use opentelemetry_appender_tracing::layer::OpenTelemetryTracingBridge;
    use opentelemetry_otlp::WithExportConfig;
    use tracing_opentelemetry::OpenTelemetryLayer;
    use tracing_subscriber::layer::SubscriberExt;
    use tracing_subscriber::util::SubscriberInitExt;

    let env_filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    let fmt_layer = tracing_subscriber::fmt::layer();
    let logs_layer = log_provider.as_ref().map(OpenTelemetryTracingBridge::new);

    let endpoint = std::env::var("OTEL_EXPORTER_OTLP_ENDPOINT").ok();

    if let Some(endpoint) = endpoint {
        let service_name = std::env::var("OTEL_SERVICE_NAME").unwrap_or_else(|_| "rust-hid".into());

        let exporter = match opentelemetry_otlp::SpanExporter::builder()
            .with_tonic()
            .with_endpoint(&endpoint)
            .build()
        {
            Ok(exporter) => exporter,
            Err(e) => {
                // No trace export, but never no logs (B4, #2373).
                fallback_subscriber(
                    env_filter,
                    log_provider,
                    tracing_subscriber::fmt::writer::BoxMakeWriter::new(std::io::stdout),
                )
                .init();
                warn!(error = %e, "Trace exporter failed to build; logging without trace export");
                return None;
            }
        };

        let resource = build_otel_resource();

        let provider = opentelemetry_sdk::trace::SdkTracerProvider::builder()
            .with_batch_exporter(exporter)
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

/// Log output without span export: stdout, plus OTLP logs when a provider exists.
fn fallback_subscriber(
    env_filter: EnvFilter,
    log_provider: &Option<opentelemetry_sdk::logs::SdkLoggerProvider>,
    writer: tracing_subscriber::fmt::writer::BoxMakeWriter,
) -> impl tracing::Subscriber + Send + Sync {
    use opentelemetry_appender_tracing::layer::OpenTelemetryTracingBridge;
    use tracing_subscriber::layer::SubscriberExt;

    tracing_subscriber::registry()
        .with(env_filter)
        .with(tracing_subscriber::fmt::layer().with_writer(writer))
        .with(log_provider.as_ref().map(OpenTelemetryTracingBridge::new))
}

/// Initialize OTEL metrics with process metrics (CPU, memory, threads).
///
/// Reads from /proc/self/stat and /proc/self/status to report:
/// - process_cpu_seconds_total (observable counter)
/// - process_resident_memory_bytes (gauge)
/// - process_threads (gauge)
///
/// and, per open controller, rust_hid_output_write_in_flight_seconds (gauge, #2142).
fn init_metrics(
    router: output::OutputRouter,
) -> Option<opentelemetry_sdk::metrics::SdkMeterProvider> {
    use opentelemetry::metrics::MeterProvider;
    use opentelemetry_otlp::WithExportConfig;

    let endpoint = std::env::var("OTEL_EXPORTER_OTLP_ENDPOINT").ok()?;

    let exporter = opentelemetry_otlp::MetricExporter::builder()
        .with_tonic()
        .with_endpoint(&endpoint)
        .build()
        .ok()?;

    let resource = build_otel_resource();

    let reader = opentelemetry_sdk::metrics::PeriodicReader::builder(exporter)
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
            if let Ok(me) = procfs::process::Process::myself()
                && let Ok(stat) = me.stat()
            {
                let ticks_per_sec = procfs::ticks_per_second() as f64;
                let total_secs = (stat.utime as f64 + stat.stime as f64) / ticks_per_sec;
                observer.observe(total_secs, &[]);
            }
        })
        .build();

    // process_resident_memory_bytes — RSS from /proc/self/stat
    let _mem = meter
        .i64_observable_gauge("process_resident_memory_bytes")
        .with_description("Resident memory size in bytes")
        .with_callback(|observer| {
            if let Ok(me) = procfs::process::Process::myself()
                && let Ok(stat) = me.stat()
            {
                let page_size = procfs::page_size() as i64;
                let rss_bytes = stat.rss as i64 * page_size;
                observer.observe(rss_bytes, &[]);
            }
        })
        .build();

    // process_threads — thread count from /proc/self/stat
    let _threads = meter
        .i64_observable_gauge("process_threads")
        .with_description("Number of active threads")
        .with_callback(|observer| {
            if let Ok(me) = procfs::process::Process::myself()
                && let Ok(stat) = me.stat()
            {
                observer.observe(stat.num_threads, &[]);
            }
        })
        .build();

    // rust_hid_output_write_in_flight_seconds{serial}: how long the controller's
    // current output write has been in flight, 0 when idle (#2142, NFR-055).
    let _in_flight = meter
        .f64_observable_gauge("rust_hid_output_write_in_flight_seconds")
        .with_description(
            "Seconds the controller's current output write has been in flight; 0 when idle",
        )
        .with_callback(move |observer| {
            for w in router.writes_in_flight() {
                observer.observe(
                    w.age.as_secs_f64(),
                    &[opentelemetry::KeyValue::new("serial", w.serial)],
                );
            }
        })
        .build();

    // Histograms and counters recorded where the work happens (#2374).
    metrics::install(&provider.meter("rust_hid"));

    info!("OTEL metrics initialized (1s export interval)");
    Some(provider)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tracing_subscriber::fmt::writer::BoxMakeWriter;

    /// Collects what the fmt layer writes.
    #[derive(Clone, Default)]
    struct Buffer(Arc<Mutex<Vec<u8>>>);

    impl std::io::Write for Buffer {
        fn write(&mut self, data: &[u8]) -> std::io::Result<usize> {
            self.0.lock().unwrap().extend_from_slice(data);
            Ok(data.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    // UC: UC-002 A7
    #[test]
    fn test_fallback_subscriber_logs_to_the_writer_without_otlp() {
        let buffer = Buffer::default();
        let sink = buffer.clone();
        let subscriber = fallback_subscriber(
            EnvFilter::new("info"),
            &None,
            BoxMakeWriter::new(move || sink.clone()),
        );
        tracing::subscriber::with_default(subscriber, || {
            warn!("trace exporter failed to build");
        });
        let out = String::from_utf8(buffer.0.lock().unwrap().clone()).unwrap();
        assert!(out.contains("trace exporter failed to build"), "got: {out}");
    }

    /// A log exporter whose export hangs for a long time (an unreachable collector).
    #[derive(Debug)]
    struct HangingExporter;

    impl opentelemetry_sdk::logs::LogExporter for HangingExporter {
        async fn export(
            &self,
            _batch: opentelemetry_sdk::logs::LogBatch<'_>,
        ) -> opentelemetry_sdk::error::OTelSdkResult {
            std::thread::sleep(std::time::Duration::from_secs(4));
            Ok(())
        }
    }

    // UC: UC-002 A7
    #[test]
    fn test_flush_gives_up_after_its_budget_when_the_exporter_hangs() {
        use opentelemetry::logs::{LogRecord as _, Logger as _, LoggerProvider as _};
        let provider = opentelemetry_sdk::logs::SdkLoggerProvider::builder()
            .with_batch_exporter(HangingExporter)
            .build();
        let logger = provider.logger("test");
        let mut record = logger.create_log_record();
        record.set_body("x".into());
        logger.emit(record);
        let flusher = TelemetryFlusher {
            logs: Some(provider),
            traces: None,
            metrics: Arc::new(Mutex::new(None)),
        };

        let start = std::time::Instant::now();
        flusher.flush(std::time::Duration::from_secs(1));
        let elapsed = start.elapsed();
        assert!(
            elapsed >= std::time::Duration::from_millis(900)
                && elapsed < std::time::Duration::from_millis(1500),
            "flush took {elapsed:?}, the budget is 1 s"
        );
    }

    // UC: UC-002 A7
    #[test]
    fn test_force_exit_runs_after_the_delay_on_its_own_thread() {
        let (tx, rx) = std::sync::mpsc::channel();
        force_exit_after(std::time::Duration::from_millis(100), move || {
            let _ = tx.send(());
        });
        assert!(rx.try_recv().is_err(), "exited before the delay");
        assert!(rx.recv_timeout(std::time::Duration::from_secs(2)).is_ok());
    }

    // UC: UC-002 A7
    #[test]
    fn test_flush_is_bounded_and_ok_without_providers() {
        let flusher = TelemetryFlusher {
            logs: None,
            traces: None,
            metrics: Arc::new(Mutex::new(None)),
        };
        let start = std::time::Instant::now();
        flusher.flush(std::time::Duration::from_secs(1));
        assert!(start.elapsed() < std::time::Duration::from_secs(1));
    }
}
