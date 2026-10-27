//! Self-healing watchdog (UC-002 A7, NFR-054, #2125).
//!
//! rust-hid wedged on stage with about 9 controllers: its gRPC server stopped
//! answering, the container went `unhealthy` and stayed so, because compose
//! restart policies do not act on health. The watchdog turns such a hang into
//! a process exit, so `restart: unless-stopped` brings the service back.
//!
//! It runs on its own OS thread with its own current-thread runtime, so a
//! stalled main runtime cannot stall it. Two checks:
//! - the server's own `grpc.health.v1.Health/Check` (the overall "" service),
//!   over a fresh connection like the container healthcheck; the watchdog
//!   trips after `max_failed_probes` failures in a row, counted only once a
//!   probe has succeeded or the start grace has passed;
//! - the device-manager loop's heartbeat (bumped on every loop pass); the
//!   watchdog trips when it is older than `device_stall_limit`.
//!
//! Each pass also runs a report-only tick (the stuck output-writer check of
//! #2142); it never trips the watchdog.

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use tonic_health::pb::HealthCheckRequest;
use tonic_health::pb::health_check_response::ServingStatus;
use tonic_health::pb::health_client::HealthClient;
use tracing::{error, warn};

/// Exit code of a tripped watchdog (EX_SOFTWARE): non-zero, so any restart policy restarts it.
pub const EXIT_CODE: i32 = 70;

/// Thresholds of the watchdog (NFR-054).
#[derive(Debug, Clone, Copy)]
pub struct Config {
    /// Pause between two health probes.
    pub probe_interval: Duration,
    /// Budget of one probe: connect plus the Check call.
    pub probe_timeout: Duration,
    /// Failed probes in a row that trip the watchdog.
    pub max_failed_probes: u32,
    /// Failed probes are not counted before the first success or before this has passed.
    pub start_grace: Duration,
    /// A device-loop heartbeat older than this trips the watchdog.
    pub device_stall_limit: Duration,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            probe_interval: Duration::from_secs(3),
            probe_timeout: Duration::from_secs(3),
            max_failed_probes: 5,
            start_grace: Duration::from_secs(30),
            device_stall_limit: Duration::from_secs(15),
        }
    }
}

/// Why the watchdog tripped.
#[derive(Debug, Clone, PartialEq)]
pub enum Trip {
    GrpcUnresponsive { failed_probes: u32 },
    DeviceLoopStalled { age: Duration },
}

/// Liveness stamp of a loop, shared between the loop and the watchdog.
#[derive(Debug, Clone)]
pub struct Heartbeat {
    origin: Instant,
    last_ms: Arc<AtomicU64>,
}

impl Default for Heartbeat {
    fn default() -> Self {
        Self::new()
    }
}

impl Heartbeat {
    /// A heartbeat that counts as beaten now.
    pub fn new() -> Self {
        Self {
            origin: Instant::now(),
            last_ms: Arc::new(AtomicU64::new(0)),
        }
    }

    pub fn beat(&self) {
        let now_ms = self.origin.elapsed().as_millis() as u64;
        self.last_ms.store(now_ms, Ordering::Relaxed);
    }

    /// Time since the last beat.
    pub fn age(&self) -> Duration {
        let last = Duration::from_millis(self.last_ms.load(Ordering::Relaxed));
        self.origin.elapsed().saturating_sub(last)
    }
}

/// The trip decision, kept free of I/O so it can be tested on its own.
#[derive(Debug)]
pub struct WatchdogState {
    cfg: Config,
    started: Instant,
    armed: bool,
    failed: u32,
}

impl WatchdogState {
    pub fn new(cfg: Config, started: Instant) -> Self {
        Self {
            cfg,
            started,
            armed: false,
            failed: 0,
        }
    }

    /// Feed one probe result and the device-loop heartbeat age.
    pub fn observe(&mut self, probe_ok: bool, device_age: Duration, now: Instant) -> Option<Trip> {
        if device_age > self.cfg.device_stall_limit {
            return Some(Trip::DeviceLoopStalled { age: device_age });
        }
        if probe_ok {
            self.armed = true;
            self.failed = 0;
            return None;
        }
        if !self.armed && now.duration_since(self.started) < self.cfg.start_grace {
            return None;
        }
        self.armed = true;
        self.failed += 1;
        (self.failed >= self.cfg.max_failed_probes).then_some(Trip::GrpcUnresponsive {
            failed_probes: self.failed,
        })
    }
}

/// One health probe over a fresh connection: true only for SERVING within the budget.
pub(crate) async fn probe_health(addr: &str, budget: Duration) -> bool {
    let call = async {
        let endpoint = tonic::transport::Endpoint::from_shared(format!("http://{addr}"))
            .ok()?
            .connect_timeout(budget)
            .timeout(budget);
        let channel = endpoint.connect().await.ok()?;
        let response = HealthClient::new(channel)
            .check(HealthCheckRequest {
                service: String::new(),
            })
            .await
            .ok()?;
        Some(response.into_inner().status == ServingStatus::Serving as i32)
    };
    matches!(tokio::time::timeout(budget, call).await, Ok(Some(true)))
}

/// Probe `addr` and watch `device` until the watchdog trips, then call `on_trip`.
/// `on_tick` runs once per pass, before the probe; it only reports.
pub fn run(
    cfg: Config,
    addr: &str,
    device: &Heartbeat,
    mut on_tick: impl FnMut(),
    on_trip: impl FnOnce(Trip),
) {
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("watchdog runtime");
    let mut state = WatchdogState::new(cfg, Instant::now());
    loop {
        std::thread::sleep(cfg.probe_interval);
        on_tick();
        let ok = rt.block_on(probe_health(addr, cfg.probe_timeout));
        if !ok {
            warn!(addr, "watchdog: health probe failed");
        }
        if let Some(trip) = state.observe(ok, device.age(), Instant::now()) {
            on_trip(trip);
            return;
        }
    }
}

/// Start the watchdog thread; a trip exits the process with `EXIT_CODE`.
/// `on_tick` runs every probe interval on the watchdog thread (report only).
pub fn spawn(addr: String, device: Heartbeat, on_tick: impl FnMut() + Send + 'static) {
    std::thread::Builder::new()
        .name("watchdog".into())
        .spawn(move || {
            run(Config::default(), &addr, &device, on_tick, |trip| {
                error!(
                    ?trip,
                    exit_code = EXIT_CODE,
                    "watchdog: rust-hid is wedged, exiting so the restart policy restarts it"
                );
                std::process::exit(EXIT_CODE);
            })
        })
        .expect("Failed to spawn watchdog thread");
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;

    fn cfg() -> Config {
        Config {
            probe_interval: Duration::from_millis(50),
            probe_timeout: Duration::from_millis(200),
            max_failed_probes: 3,
            start_grace: Duration::from_secs(30),
            device_stall_limit: Duration::from_millis(400),
        }
    }

    const FRESH: Duration = Duration::from_millis(1);

    // UC: UC-002 A7
    // NFR: NFR-054
    #[test]
    fn test_default_config_and_exit_code_match_nfr_054() {
        let d = Config::default();
        assert_eq!(d.max_failed_probes, 5, "NFR-054: 5 failed probes in a row");
        assert_eq!(
            d.probe_interval,
            Duration::from_secs(3),
            "NFR-054: one probe every 3 s"
        );
        assert_eq!(
            d.probe_timeout,
            Duration::from_secs(3),
            "NFR-054: 3 s budget per probe"
        );
        assert_eq!(
            d.device_stall_limit,
            Duration::from_secs(15),
            "NFR-054: 15 s device stall limit"
        );
        assert_eq!(
            d.start_grace,
            Duration::from_secs(30),
            "NFR-054: 30 s start grace"
        );
        // Non-zero (EX_SOFTWARE), so `restart: unless-stopped` restarts the container.
        assert_eq!(EXIT_CODE, 70);
    }

    // UC: UC-002 A7
    // NFR: NFR-054
    #[test]
    fn test_state_trips_only_after_consecutive_failures_once_armed() {
        let t0 = Instant::now();
        let mut s = WatchdogState::new(cfg(), t0);
        // Before the first success and inside the grace: not counted.
        for _ in 0..10 {
            assert_eq!(s.observe(false, FRESH, t0), None);
        }
        assert_eq!(s.observe(true, FRESH, t0), None);
        assert_eq!(s.observe(false, FRESH, t0), None);
        assert_eq!(s.observe(false, FRESH, t0), None);
        // A success resets the count.
        assert_eq!(s.observe(true, FRESH, t0), None);
        assert_eq!(s.observe(false, FRESH, t0), None);
        assert_eq!(s.observe(false, FRESH, t0), None);
        assert_eq!(
            s.observe(false, FRESH, t0),
            Some(Trip::GrpcUnresponsive { failed_probes: 3 })
        );
    }

    // UC: UC-002 A7
    // NFR: NFR-054
    #[test]
    fn test_state_counts_failures_after_the_start_grace_without_a_success() {
        let t0 = Instant::now();
        let mut s = WatchdogState::new(cfg(), t0);
        let late = t0 + Duration::from_secs(31);
        assert_eq!(s.observe(false, FRESH, late), None);
        assert_eq!(s.observe(false, FRESH, late), None);
        assert_eq!(
            s.observe(false, FRESH, late),
            Some(Trip::GrpcUnresponsive { failed_probes: 3 })
        );
    }

    // UC: UC-002 A7
    // NFR: NFR-054
    #[test]
    fn test_state_trips_on_a_stalled_device_loop() {
        let t0 = Instant::now();
        let mut s = WatchdogState::new(cfg(), t0);
        assert_eq!(s.observe(true, Duration::from_millis(400), t0), None);
        assert_eq!(
            s.observe(true, Duration::from_millis(401), t0),
            Some(Trip::DeviceLoopStalled {
                age: Duration::from_millis(401)
            })
        );
    }

    #[test]
    fn test_heartbeat_age_resets_on_beat() {
        let hb = Heartbeat::new();
        std::thread::sleep(Duration::from_millis(30));
        assert!(hb.age() >= Duration::from_millis(30));
        hb.beat();
        assert!(hb.age() < Duration::from_millis(20));
    }

    /// A real tonic health server on its own single-threaded runtime. Returns its
    /// address and a sender: a message makes a task on that runtime block the only
    /// worker thread for good (the stalled-runtime case of #2125).
    fn spawn_stallable_health_server() -> (String, mpsc::Sender<()>) {
        let (addr_tx, addr_rx) = mpsc::channel();
        let (stall_tx, stall_rx) = mpsc::channel::<()>();
        std::thread::spawn(move || {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap();
            rt.block_on(async move {
                let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
                addr_tx
                    .send(listener.local_addr().unwrap().to_string())
                    .unwrap();
                let (_reporter, health) = tonic_health::server::health_reporter();
                tokio::spawn(async move {
                    // Poll the stall order without blocking the runtime, then block it.
                    loop {
                        if stall_rx.try_recv().is_ok() {
                            std::thread::sleep(Duration::from_secs(3600));
                        }
                        tokio::time::sleep(Duration::from_millis(5)).await;
                    }
                });
                tonic::transport::Server::builder()
                    .add_service(health)
                    .serve_with_incoming(tonic::transport::server::TcpIncoming::from(listener))
                    .await
                    .unwrap();
            });
        });
        (addr_rx.recv().unwrap(), stall_tx)
    }

    fn spawn_watchdog(addr: String, device: Heartbeat) -> mpsc::Receiver<Trip> {
        let (trip_tx, trip_rx) = mpsc::channel();
        std::thread::spawn(move || {
            run(
                cfg(),
                &addr,
                &device,
                || {},
                |trip| trip_tx.send(trip).unwrap(),
            )
        });
        trip_rx
    }

    /// Keeps `hb` beating every 10 ms until `stop` is dropped.
    fn keep_beating(hb: &Heartbeat) -> mpsc::Sender<()> {
        let (stop_tx, stop_rx) = mpsc::channel::<()>();
        let hb = hb.clone();
        std::thread::spawn(move || {
            while let Err(mpsc::RecvTimeoutError::Timeout) =
                stop_rx.recv_timeout(Duration::from_millis(10))
            {
                hb.beat();
            }
        });
        stop_tx
    }

    // UC: UC-002 A7
    // NFR: NFR-054
    #[test]
    fn test_watchdog_trips_when_the_grpc_runtime_is_blocked() {
        let (addr, stall) = spawn_stallable_health_server();
        let device = Heartbeat::new();
        let _beating = keep_beating(&device);
        let trips = spawn_watchdog(addr, device);

        // Healthy server: no trip over many probe intervals.
        assert_eq!(
            trips.recv_timeout(Duration::from_millis(800)),
            Err(mpsc::RecvTimeoutError::Timeout),
            "the watchdog tripped on a healthy server"
        );

        stall.send(()).unwrap();
        let trip = trips
            .recv_timeout(Duration::from_secs(5))
            .expect("the watchdog did not trip on a blocked runtime");
        assert_eq!(trip, Trip::GrpcUnresponsive { failed_probes: 3 });
    }

    // UC: UC-002 BR-005
    // NFR: NFR-055
    #[test]
    fn test_watchdog_runs_the_report_tick_every_pass_without_tripping() {
        let (addr, _stall) = spawn_stallable_health_server();
        let device = Heartbeat::new();
        let _beating = keep_beating(&device);
        let ticks = Arc::new(AtomicU64::new(0));
        let (trip_tx, trip_rx) = mpsc::channel();
        let counter = ticks.clone();
        std::thread::spawn(move || {
            run(
                cfg(),
                &addr,
                &device,
                move || {
                    counter.fetch_add(1, Ordering::Relaxed);
                },
                |trip| trip_tx.send(trip).unwrap(),
            )
        });
        assert_eq!(
            trip_rx.recv_timeout(Duration::from_millis(500)),
            Err(mpsc::RecvTimeoutError::Timeout),
            "the watchdog tripped on a healthy server"
        );
        let n = ticks.load(Ordering::Relaxed);
        assert!(
            n >= 3,
            "report tick ran {n} times in 500 ms at 50 ms passes"
        );
    }

    // UC: UC-002 A7
    // NFR: NFR-054
    #[test]
    fn test_watchdog_trips_when_the_device_loop_stops_beating() {
        let (addr, _stall) = spawn_stallable_health_server();
        let device = Heartbeat::new();
        let beating = keep_beating(&device);
        let trips = spawn_watchdog(addr, device);

        assert_eq!(
            trips.recv_timeout(Duration::from_millis(800)),
            Err(mpsc::RecvTimeoutError::Timeout),
            "the watchdog tripped while the device loop was beating"
        );

        drop(beating);
        match trips.recv_timeout(Duration::from_secs(5)) {
            Ok(Trip::DeviceLoopStalled { age }) => assert!(age > cfg().device_stall_limit),
            other => panic!("expected a device-loop stall trip, got {other:?}"),
        }
    }
}
