//! Feature flag integration via OpenFeature + flagd.
//!
//! Evaluates the `grpc_rpc_spans` boolean flag (observability flag set) to control
//! whether gRPC SERVER spans are created. The flag is evaluated on each RPC call to
//! support hot-reload via flagd.
//!
//! In-process resolver (#1713, docs/specs/flags.md §1.1): the flag configuration comes
//! over flagd's sync port (8015, selector `flagSetId=observability`) and every read is
//! a local evaluation. The provider is built in the background once flagd's sync port
//! accepts a connection, and built again if that fails: the crate gives up on its first
//! sync after 5 s, and a provider that never got one would serve code defaults for good.
//!
//! A second provider, the named domain `controller` (selector `flagSetId=controller`),
//! serves `rust_hid.output_rate_hz`, the per-controller output rate cap (UC-002 BR-005,
//! #2125); a 1 s follower copies it into the writers' shared `OutputRate`.

use open_feature::{EvaluationContext, OpenFeature};
use open_feature_flagd::{CacheSettings, CacheType, FlagdOptions, FlagdProvider, ResolverType};
use std::sync::OnceLock;
use std::time::Duration;
use tokio::net::TcpStream;
use tracing::{info, warn};

use crate::output::{DEFAULT_RATE_HZ, OutputRate};

/// The flag set rust-hid reads (`metadata.flagSetId` of services/flagd/observability.json).
const FLAG_SET: &str = "observability";
/// The flag set (and OpenFeature domain) of the output rate cap.
const CONTROLLER_SET: &str = "controller";
/// Per-controller output reports per second (services/flagd/controller.json).
const OUTPUT_RATE_FLAG: &str = "rust_hid.output_rate_hz";
/// flagd's sync port (in-process resolver).
const DEFAULT_SYNC_PORT: u16 = 8015;
/// Pause between two attempts to reach flagd: doubles from the first to the max.
const FIRST_PAUSE: Duration = Duration::from_millis(500);
const MAX_PAUSE: Duration = Duration::from_secs(5);
/// Sync-stream reconnect backoff once built (the crate's default caps it at 120 s).
const STREAM_RETRY_BACKOFF_MS: u32 = 500;
const STREAM_RETRY_BACKOFF_MAX_MS: u32 = 2000;
/// Background read interval: every read drains the crate's store-change channel (see
/// `draining_cache`), so this keeps it drained while no RPC arrives (its stream listener
/// blocks, holding the flag write lock, once 1000 changes are unread).
const DRAIN_INTERVAL: Duration = Duration::from_secs(1);

static STARTED: OnceLock<()> = OnceLock::new();

/// Start the OpenFeature flagd provider in the background (non-blocking).
///
/// Until flagd has sent the first flag sync every read returns its code default.
/// Safe to call multiple times: only the first call starts it.
pub async fn init_flagd(output_rate: OutputRate) {
    if STARTED.set(()).is_err() {
        return;
    }
    let host = std::env::var("FLAGD_HOST").unwrap_or_else(|_| "flagd".into());
    let port: u16 = std::env::var("FLAGD_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(DEFAULT_SYNC_PORT);
    info!(host = %host, port = port, flag_set = FLAG_SET, "Connecting to flagd (in-process resolver)");
    tokio::spawn(connect_when_reachable(host.clone(), port));
    tokio::spawn(connect_controller_set(host, port, output_rate));
}

fn provider_options(host: String, port: u16) -> FlagdOptions {
    set_provider_options(host, port, FLAG_SET)
}

fn set_provider_options(host: String, port: u16, flag_set: &str) -> FlagdOptions {
    FlagdOptions {
        host,
        port,
        resolver_type: ResolverType::InProcess,
        selector: Some(format!("flagSetId={flag_set}")),
        retry_backoff_ms: STREAM_RETRY_BACKOFF_MS,
        retry_backoff_max_ms: STREAM_RETRY_BACKOFF_MAX_MS,
        cache_settings: Some(draining_cache()),
        ..Default::default()
    }
}

/// A cache of type `Disabled`: it stores and serves nothing, but it is `Some` (#2280).
///
/// open-feature-flagd 0.3 sets `cache_settings` to `None` for the in-process resolver
/// (`FlagdOptions::default`). The resolver drains its flag store's bounded (1000)
/// state-change channel only on a read that has a cache (`check_for_updates`); with
/// `None` nothing ever reads it, and the store's sync task, which holds the flag write
/// lock while it waits for room on that channel, blocks for good after about 1000
/// syncs: every later read hangs. A disabled cache keeps the drain on every read and
/// serves each read from the flag store, so a flip is seen on the next read.
fn draining_cache() -> CacheSettings {
    CacheSettings {
        cache_type: CacheType::Disabled,
        max_size: 0,
        ttl: None,
    }
}

async fn reachable(host: &str, port: u16) -> bool {
    matches!(
        tokio::time::timeout(Duration::from_secs(1), TcpStream::connect((host, port))).await,
        Ok(Ok(_))
    )
}

/// Build and register the provider once flagd answers, retrying with backoff for as
/// long as it takes; then keep reading the flag every second (see DRAIN_INTERVAL).
pub(crate) async fn connect_when_reachable(host: String, port: u16) {
    let mut pause = FIRST_PAUSE;
    let mut attempt: u32 = 0;
    loop {
        attempt += 1;
        if reachable(&host, port).await {
            match FlagdProvider::new(provider_options(host.clone(), port)).await {
                Ok(provider) => {
                    let mut of = OpenFeature::singleton_mut().await;
                    of.set_provider(provider).await;
                    drop(of);
                    info!(attempt, "OpenFeature flagd provider synced (in-process)");
                    break;
                }
                Err(e) => {
                    warn!(attempt, error = %e, "flagd provider got no first sync; flags use defaults, retrying")
                }
            }
        } else if attempt == 1 || attempt.is_multiple_of(10) {
            warn!(attempt, host = %host, port, "flagd sync port not reachable yet; flags use defaults");
        }
        tokio::time::sleep(pause).await;
        pause = (pause * 2).min(MAX_PAUSE);
    }
    let mut tick = tokio::time::interval(DRAIN_INTERVAL);
    loop {
        tick.tick().await;
        grpc_rpc_spans_enabled().await;
    }
}

/// The `controller` domain: build its provider like the default one (same backoff),
/// and copy `rust_hid.output_rate_hz` into `rate` every second, before and after
/// the first sync (until then the read is the code default).
async fn connect_controller_set(host: String, port: u16, rate: OutputRate) {
    tokio::spawn(follow_output_rate(rate));
    let mut pause = FIRST_PAUSE;
    let mut attempt: u32 = 0;
    loop {
        attempt += 1;
        if reachable(&host, port).await {
            match FlagdProvider::new(set_provider_options(host.clone(), port, CONTROLLER_SET)).await
            {
                Ok(provider) => {
                    let mut of = OpenFeature::singleton_mut().await;
                    of.set_named_provider(CONTROLLER_SET, provider).await;
                    drop(of);
                    info!(
                        attempt,
                        flag_set = CONTROLLER_SET,
                        "OpenFeature flagd provider synced (in-process)"
                    );
                    return;
                }
                Err(e) => {
                    warn!(attempt, flag_set = CONTROLLER_SET, error = %e, "flagd provider got no first sync; flags use defaults, retrying")
                }
            }
        }
        tokio::time::sleep(pause).await;
        pause = (pause * 2).min(MAX_PAUSE);
    }
}

async fn follow_output_rate(rate: OutputRate) {
    let mut tick = tokio::time::interval(DRAIN_INTERVAL);
    loop {
        tick.tick().await;
        refresh_output_rate(&rate).await;
    }
}

/// One follower step: the flag's current value into the writers' shared rate.
async fn refresh_output_rate(rate: &OutputRate) {
    rate.set(output_rate_hz().await);
}

/// `rust_hid.output_rate_hz` from the `controller` set; the code default until it resolves.
pub async fn output_rate_hz() -> i64 {
    let of = OpenFeature::singleton().await;
    of.create_named_client(CONTROLLER_SET)
        .get_int_value(OUTPUT_RATE_FLAG, None, None)
        .await
        .unwrap_or(i64::from(DEFAULT_RATE_HZ))
}

/// Check if the `grpc_rpc_spans` feature flag is enabled.
///
/// Returns false if flagd is unavailable or the flag is not defined.
/// Evaluated at runtime on each call to support hot-reload.
pub async fn grpc_rpc_spans_enabled() -> bool {
    let of = OpenFeature::singleton().await;
    let client = of.create_client();
    let ctx = EvaluationContext::default();
    client
        .get_bool_value("grpc_rpc_spans", Some(&ctx), None)
        .await
        .unwrap_or(false)
}

/// Live flag plane test (#1713): runs only when tests/service/test_rust_hid_flag_live.py
/// passes a real flagd and flag service; otherwise it returns at once.
#[cfg(test)]
mod live_tests {
    use super::*;
    use std::time::Instant;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    const PROBE_FLAG: &str = "flagservice.propagation_probe";
    /// Longer than the crate's 5 s first-sync wait: a provider built at once would give up.
    const LATE_FLAGD: Duration = Duration::from_secs(7);
    const FLIP_BOUND: Duration = Duration::from_millis(1500);

    async fn probe() -> String {
        let of = OpenFeature::singleton().await;
        of.create_client()
            .get_string_value(PROBE_FLAG, None, None)
            .await
            .unwrap_or_default()
    }

    /// PUT a probe value through the flag service (writer integration-test).
    async fn put_probe(service: &str, value: &str) {
        let addr = service.trim_start_matches("http://");
        let body = format!(r#"{{"writer":"integration-test","value":"{value}"}}"#);
        let req = format!(
            "PUT /{FLAG_SET}/{PROBE_FLAG} HTTP/1.1\r\nHost: {addr}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let mut conn = TcpStream::connect(addr).await.expect("flag service");
        conn.write_all(req.as_bytes()).await.unwrap();
        let mut resp = String::new();
        conn.read_to_string(&mut resp).await.unwrap();
        assert!(resp.starts_with("HTTP/1.1 2"), "PUT {PROBE_FLAG}: {resp}");
    }

    async fn wait_probe(want: &str, timeout: Duration) -> Duration {
        let start = Instant::now();
        loop {
            if probe().await == want {
                return start.elapsed();
            }
            assert!(
                start.elapsed() < timeout,
                "probe still not {want:?} after {timeout:?}"
            );
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }

    /// A relay to flagd that listens only after `delay` (flagd still starting).
    async fn late_relay(target: String, delay: Duration) -> u16 {
        let port = std::net::TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port();
        tokio::spawn(async move {
            tokio::time::sleep(delay).await;
            let ln = TcpListener::bind(("127.0.0.1", port))
                .await
                .expect("relay port");
            while let Ok((mut client, _)) = ln.accept().await {
                let target = target.clone();
                tokio::spawn(async move {
                    if let Ok(mut upstream) = TcpStream::connect(target).await {
                        let _ = tokio::io::copy_bidirectional(&mut client, &mut upstream).await;
                    }
                });
            }
        });
        port
    }

    // UC: UC-035 (FR-080..082, grpc_rpc_spans' flag set), #1713
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn live_flips_reach_the_provider_after_a_late_flagd() {
        let (Ok(flagd_port), Ok(service)) = (
            std::env::var("RUST_HID_LIVE_FLAGD_PORT"),
            std::env::var("RUST_HID_LIVE_FLAG_SERVICE_URL"),
        ) else {
            eprintln!(
                "live flag plane not configured: run via tests/service/test_rust_hid_flag_live.py"
            );
            return;
        };
        put_probe(&service, "before").await;
        let relay = late_relay(format!("127.0.0.1:{flagd_port}"), LATE_FLAGD).await;
        tokio::spawn(connect_when_reachable("127.0.0.1".into(), relay));

        let took = wait_probe("before", Duration::from_secs(40)).await;
        println!("first sync after a late flagd: {took:?}");
        assert!(
            grpc_rpc_spans_enabled().await,
            "grpc_rpc_spans: the CI baseline serves enabled"
        );
        for i in 0..5 {
            let value = format!("flip-{i}");
            put_probe(&service, &value).await;
            let took = wait_probe(&value, FLIP_BOUND).await;
            println!("flip {i} seen after {took:?}");
        }
        println!("RUST_HID_LIVE_FLIP_RAN");
    }
}

/// The output rate cap's key, domain and follower (#2138 review): a wrong key or
/// domain would silently serve the code default for good.
#[cfg(test)]
mod output_rate_tests {
    use super::*;
    use open_feature::provider::{FeatureProvider, ProviderMetadata, ResolutionDetails};
    use open_feature::{EvaluationError, EvaluationErrorCode, EvaluationResult, StructValue};

    /// Serves one int flag; everything else is FLAG_NOT_FOUND.
    struct OneIntFlag {
        metadata: ProviderMetadata,
        key: &'static str,
        value: i64,
    }

    fn not_found<T>() -> EvaluationResult<T> {
        Err(EvaluationError::builder()
            .code(EvaluationErrorCode::FlagNotFound)
            .build())
    }

    #[tonic::async_trait]
    impl FeatureProvider for OneIntFlag {
        fn metadata(&self) -> &ProviderMetadata {
            &self.metadata
        }
        async fn resolve_bool_value(
            &self,
            _: &str,
            _: &EvaluationContext,
        ) -> EvaluationResult<ResolutionDetails<bool>> {
            not_found()
        }
        async fn resolve_int_value(
            &self,
            key: &str,
            _: &EvaluationContext,
        ) -> EvaluationResult<ResolutionDetails<i64>> {
            if key == self.key {
                Ok(ResolutionDetails::new(self.value))
            } else {
                not_found()
            }
        }
        async fn resolve_float_value(
            &self,
            _: &str,
            _: &EvaluationContext,
        ) -> EvaluationResult<ResolutionDetails<f64>> {
            not_found()
        }
        async fn resolve_string_value(
            &self,
            _: &str,
            _: &EvaluationContext,
        ) -> EvaluationResult<ResolutionDetails<String>> {
            not_found()
        }
        async fn resolve_struct_value(
            &self,
            _: &str,
            _: &EvaluationContext,
        ) -> EvaluationResult<ResolutionDetails<StructValue>> {
            not_found()
        }
    }

    // UC: UC-002 BR-005
    // NFR: NFR-055
    #[tokio::test]
    async fn test_follower_reads_the_rate_flag_from_the_controller_domain() {
        let rate = OutputRate::default();
        assert_eq!(rate.hz(), DEFAULT_RATE_HZ);
        {
            let mut of = OpenFeature::singleton_mut().await;
            of.set_named_provider(
                "controller",
                OneIntFlag {
                    metadata: ProviderMetadata::new("test"),
                    key: "rust_hid.output_rate_hz",
                    value: 30,
                },
            )
            .await;
        }
        refresh_output_rate(&rate).await;
        assert_eq!(
            rate.hz(),
            30,
            "the follower did not read rust_hid.output_rate_hz from domain controller"
        );
    }

    // UC: UC-002 BR-005
    #[test]
    fn test_rate_flag_is_declared_in_the_controller_set_with_the_code_default() {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../flagd/controller.json");
        let text = std::fs::read_to_string(path).expect("services/flagd/controller.json");
        assert!(text.contains(r#""flagSetId": "controller""#));
        let start = text
            .find(&format!("\"{OUTPUT_RATE_FLAG}\""))
            .expect("rust_hid.output_rate_hz missing from controller.json");
        let block = &text[start..text[start..].find('}').map_or(text.len(), |e| start + e)];
        assert!(
            block.contains(&format!(r#""normal": {DEFAULT_RATE_HZ}"#)),
            "{block}"
        );
        let rest = &text[start..];
        assert!(
            rest.contains(r#""defaultVariant": "normal""#),
            "default variant is not normal"
        );
        assert_eq!(CONTROLLER_SET, "controller");
    }
}

/// #2280: the crate's flag store reports every sync on a bounded (1000) state-change
/// channel and holds its flag write lock while it waits for room there. Nothing reads
/// that channel unless the resolver has a cache, so once it is full every read hangs.
/// A fake flagd sync server sends more syncs than the channel holds; the provider must
/// still serve the last one.
#[cfg(test)]
mod sync_queue_tests {
    use super::*;
    use open_feature::provider::FeatureProvider;
    use open_feature_flagd::flagd::sync::v1::flag_sync_service_server::{
        FlagSyncService, FlagSyncServiceServer,
    };
    #[allow(deprecated)]
    use open_feature_flagd::flagd::sync::v1::{
        FetchAllFlagsRequest, FetchAllFlagsResponse, GetMetadataRequest, GetMetadataResponse,
        SyncFlagsRequest, SyncFlagsResponse,
    };
    use std::time::Instant;
    use tokio::net::TcpListener;
    use tokio::sync::{Mutex, mpsc};
    use tokio_stream::wrappers::UnboundedReceiverStream;
    use tonic::{Request, Response, Status};

    /// More syncs than the crate's state-change channel holds (1000).
    const SYNCS: usize = 1500;
    const READ_BOUND: Duration = Duration::from_secs(2);

    type Sync = Result<SyncFlagsResponse, Status>;

    /// Serves one sync stream fed by the test.
    struct FakeFlagd {
        syncs: Mutex<Option<mpsc::UnboundedReceiver<Sync>>>,
    }

    #[tonic::async_trait]
    impl FlagSyncService for FakeFlagd {
        type SyncFlagsStream = UnboundedReceiverStream<Sync>;

        async fn sync_flags(
            &self,
            _: Request<SyncFlagsRequest>,
        ) -> Result<Response<Self::SyncFlagsStream>, Status> {
            let syncs = self.syncs.lock().await.take();
            let syncs = syncs.ok_or_else(|| Status::unavailable("one stream only"))?;
            Ok(Response::new(UnboundedReceiverStream::new(syncs)))
        }

        async fn fetch_all_flags(
            &self,
            _: Request<FetchAllFlagsRequest>,
        ) -> Result<Response<FetchAllFlagsResponse>, Status> {
            Err(Status::unimplemented("fetch_all_flags"))
        }

        #[allow(deprecated)]
        async fn get_metadata(
            &self,
            _: Request<GetMetadataRequest>,
        ) -> Result<Response<GetMetadataResponse>, Status> {
            Err(Status::unimplemented("get_metadata"))
        }
    }

    fn sync(value: &str) -> Sync {
        Ok(SyncFlagsResponse {
            flag_configuration: format!(
                r#"{{"flags":{{"probe":{{"state":"ENABLED","variants":{{"v":"{value}"}},"defaultVariant":"v"}}}}}}"#
            ),
            sync_context: None,
        })
    }

    // UC: UC-035 (FR-080..082, grpc_rpc_spans), UC-002 BR-005 (rust_hid.output_rate_hz), #2280
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn test_provider_follows_flag_changes_past_the_crate_queue_size() {
        let (feed, syncs) = mpsc::unbounded_channel();
        feed.send(sync("v0")).unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let flagd = FakeFlagd {
            syncs: Mutex::new(Some(syncs)),
        };
        tokio::spawn(
            tonic::transport::Server::builder()
                .add_service(FlagSyncServiceServer::new(flagd))
                .serve_with_incoming(tonic::transport::server::TcpIncoming::from(listener)),
        );
        // The options rust-hid builds both of its providers with.
        let provider = FlagdProvider::new(set_provider_options("127.0.0.1".into(), port, FLAG_SET))
            .await
            .expect("first sync from the fake flagd");
        let ctx = EvaluationContext::default();

        for i in 1..=SYNCS {
            feed.send(sync(&format!("v{i}"))).unwrap();
        }
        let want = format!("v{SYNCS}");
        let start = Instant::now();
        loop {
            let read = tokio::time::timeout(
                READ_BOUND,
                provider.resolve_string_value("probe", &ctx),
            )
            .await
            .unwrap_or_else(|_| {
                panic!(
                    "a read hung for {READ_BOUND:?} after {SYNCS} syncs: the crate's store is \
                         blocked on its full state-change channel (#2280)"
                )
            })
            .expect("probe resolves");
            if read.value == want {
                break;
            }
            assert!(
                start.elapsed() < Duration::from_secs(20),
                "still {} after 20 s, want {want}",
                read.value
            );
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }
}
