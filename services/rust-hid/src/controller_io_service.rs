//! ControllerIOService gRPC implementation.
//!
//! Translates gRPC calls into DeviceCmd messages sent to the device manager
//! thread. The StreamIO bidir stream registers latest-sample slots for
//! sensor data (`input_slot`, #2372) and hands IOCommands to the per-controller output slots
//! (`output::OutputRouter`, coalesced and rate-capped, UC-002 BR-005).

use std::pin::Pin;
use std::sync::mpsc as std_mpsc;
use std::time::Duration;
use tokio::sync::oneshot;
use tokio_stream::StreamExt;
use tonic::{Request, Response, Status, Streaming};
use tracing::{info, trace, warn};

use crate::device_manager::{DeviceCmd, next_stream_id};
use crate::hid::normalize_serial;
use crate::input_slot;
use crate::output::OutputRouter;
use crate::service::proto;

use proto::controller_io_service_server::ControllerIoService;
use proto::{
    CloseAllRequest, CloseAllResponse, CloseRequest, CloseResponse, DiscoverRequest,
    DiscoverResponse, DiscoveredController, IoCommand, OpenRequest, OpenResponse, SensorData,
};

/// How long Discover / Open wait for the device-manager thread (#2125): a
/// stalled loop answers UNAVAILABLE instead of holding the RPC for good.
const DEVICE_REPLY_TIMEOUT: Duration = Duration::from_secs(10);

/// Wait for the device-manager thread's reply, within `DEVICE_REPLY_TIMEOUT`.
#[allow(clippy::result_large_err)]
async fn device_reply<T>(reply: oneshot::Receiver<T>) -> Result<T, Status> {
    match tokio::time::timeout(DEVICE_REPLY_TIMEOUT, reply).await {
        Ok(Ok(value)) => Ok(value),
        Ok(Err(_)) => Err(Status::internal("Device manager did not respond")),
        Err(_) => Err(Status::unavailable(
            "Device manager busy: no reply within 10 s",
        )),
    }
}

/// gRPC service implementation for PS Move controller I/O.
pub struct ControllerIoServiceImpl {
    cmd_tx: std_mpsc::Sender<DeviceCmd>,
    outputs: OutputRouter,
}

impl ControllerIoServiceImpl {
    pub fn new(cmd_tx: std_mpsc::Sender<DeviceCmd>, outputs: OutputRouter) -> Self {
        Self { cmd_tx, outputs }
    }

    #[allow(clippy::result_large_err)]
    fn send_cmd(&self, cmd: DeviceCmd) -> Result<(), Status> {
        self.cmd_tx
            .send(cmd)
            .map_err(|_| Status::internal("Device manager thread not running"))
    }
}

#[tonic::async_trait]
impl ControllerIoService for ControllerIoServiceImpl {
    async fn discover(
        &self,
        request: Request<DiscoverRequest>,
    ) -> Result<Response<DiscoverResponse>, Status> {
        let req = request.into_inner();
        let (reply_tx, reply_rx) = oneshot::channel();

        self.send_cmd(DeviceCmd::Discover {
            force: req.force,
            verify_only: req.verify_only,
            exclude_serials: req
                .exclude_serials
                .iter()
                .map(|s| normalize_serial(s))
                .collect(),
            reply: reply_tx,
        })?;

        let discovered = device_reply(reply_rx).await?;

        if req.verify_only {
            trace!(count = discovered.len(), "Discover verify_only");
        } else {
            info!(count = discovered.len(), force = req.force, "Discover");
        }
        let controllers = discovered
            .into_iter()
            .map(|c| DiscoveredController {
                serial: c.serial,
                adapter_name: c.adapter_name,
                product_id: c.product_id,
            })
            .collect();
        Ok(Response::new(DiscoverResponse { controllers }))
    }

    async fn open(&self, request: Request<OpenRequest>) -> Result<Response<OpenResponse>, Status> {
        let serial = request.into_inner().serial;
        if serial.is_empty() {
            return Err(Status::invalid_argument("serial is required"));
        }
        // One key per controller: the same form discovery uses (B6, #2373).
        let serial = normalize_serial(&serial);

        let (reply_tx, reply_rx) = oneshot::channel();
        self.send_cmd(DeviceCmd::Open {
            serial: serial.clone(),
            reply: reply_tx,
        })?;

        let success = device_reply(reply_rx).await?;

        info!(serial = %serial, success, "Open");
        Ok(Response::new(OpenResponse { success }))
    }

    async fn close(
        &self,
        request: Request<CloseRequest>,
    ) -> Result<Response<CloseResponse>, Status> {
        let serial = normalize_serial(&request.into_inner().serial);
        self.send_cmd(DeviceCmd::Close { serial })?;
        Ok(Response::new(CloseResponse {}))
    }

    async fn close_all(
        &self,
        _request: Request<CloseAllRequest>,
    ) -> Result<Response<CloseAllResponse>, Status> {
        self.send_cmd(DeviceCmd::CloseAll)?;
        Ok(Response::new(CloseAllResponse {}))
    }

    type StreamIOStream =
        Pin<Box<dyn tokio_stream::Stream<Item = Result<SensorData, Status>> + Send>>;

    async fn stream_io(
        &self,
        request: Request<Streaming<IoCommand>>,
    ) -> Result<Response<Self::StreamIOStream>, Status> {
        let mut inbound = request.into_inner();
        let cmd_tx = self.cmd_tx.clone();

        // Latest-sample slots for sensor data (device manager -> gRPC stream):
        // at most one sample per controller waits, so a lagging reader gets
        // the newest input, not a queue of old samples (#2372)
        let (sensor_tx, sensor_rx) = input_slot::channel();

        // Register the sender with the device manager. The id lets this call's
        // unregister leave a newer stream alone (#2371).
        let stream_id = next_stream_id();
        self.send_cmd(DeviceCmd::RegisterStream {
            id: stream_id,
            sender: sensor_tx,
        })?;

        // Spawn task to read IOCommands from the client into the output slots
        // (never blocks: coalesced per controller, written by its writer thread)
        let cmd_tx_clone = cmd_tx.clone();
        let outputs = self.outputs.clone();
        tokio::spawn(async move {
            while let Some(result) = inbound.next().await {
                match result {
                    Ok(io_cmd) => {
                        route_command(&outputs, &io_cmd);
                    }
                    Err(e) => {
                        warn!(error = %e, "Error reading IOCommand from stream");
                        break;
                    }
                }
            }
            // Client disconnected — unregister stream
            let _ = cmd_tx_clone.send(DeviceCmd::UnregisterStream { id: stream_id });
            info!(stream_id, "StreamIO client disconnected");
        });

        // Convert the sensor slots to a gRPC response stream
        #[allow(clippy::result_large_err)] // tonic::Status is inherently large
        let output_stream = sensor_rx.map(|data| {
            let report = &data.report;
            Ok(SensorData {
                serial: data.serial,
                battery: report.battery,
                trigger: report.trigger as i32,
                temperature: report.temperature,
                button_move: report.button_move(),
                button_trigger: report.button_trigger(),
                button_ps: report.button_ps(),
                button_cross: report.button_cross(),
                button_circle: report.button_circle(),
                button_square: report.button_square(),
                button_triangle: report.button_triangle(),
                button_select: report.button_select(),
                button_start: report.button_start(),
                accel_x: report.accel_x,
                accel_y: report.accel_y,
                accel_z: report.accel_z,
                gyro_x: report.gyro_x,
                gyro_y: report.gyro_y,
                gyro_z: report.gyro_z,
            })
        });

        info!(stream_id, "StreamIO started");
        Ok(Response::new(Box::pin(output_stream)))
    }
}

/// Hand one IOCommand to its controller's output slot (serial normalized, values clamped).
fn route_command(outputs: &OutputRouter, cmd: &IoCommand) -> bool {
    outputs.submit(
        &normalize_serial(&cmd.serial),
        cmd.r.clamp(0, 255) as u8,
        cmd.g.clamp(0, 255) as u8,
        cmd.b.clamp(0, 255) as u8,
        cmd.rumble.clamp(0, 255) as u8,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    const RAW: &str = "00:06:f7:aa:bb:cc";
    const NORMALIZED: &str = "0006F7AABBCC";

    // UC: UC-002 main step 2
    #[tokio::test]
    async fn test_open_close_and_discover_use_the_normalized_serial() {
        let (cmd_tx, cmd_rx) = std_mpsc::channel();
        let service = ControllerIoServiceImpl::new(cmd_tx, OutputRouter::default());

        let responder = std::thread::spawn(move || {
            let mut seen = Vec::new();
            for _ in 0..3 {
                match cmd_rx.recv().unwrap() {
                    DeviceCmd::Open { serial, reply } => {
                        seen.push(serial);
                        let _ = reply.send(true);
                    }
                    DeviceCmd::Close { serial } => seen.push(serial),
                    DeviceCmd::Discover {
                        exclude_serials,
                        reply,
                        ..
                    } => {
                        seen.extend(exclude_serials);
                        let _ = reply.send(vec![]);
                    }
                    _ => panic!("unexpected command"),
                }
            }
            seen
        });

        service
            .open(Request::new(OpenRequest { serial: RAW.into() }))
            .await
            .unwrap();
        service
            .close(Request::new(CloseRequest { serial: RAW.into() }))
            .await
            .unwrap();
        service
            .discover(Request::new(DiscoverRequest {
                force: false,
                verify_only: false,
                exclude_serials: vec![RAW.into()],
            }))
            .await
            .unwrap();

        assert_eq!(responder.join().unwrap(), vec![NORMALIZED; 3]);
    }

    // UC: UC-002 BR-005
    #[test]
    fn test_a_stream_command_reaches_the_slot_opened_under_the_normalized_serial() {
        struct Sink(std::sync::Arc<std::sync::Mutex<Vec<Vec<u8>>>>);
        impl crate::hid::HidDeviceIo for Sink {
            fn read(&self, _buf: &mut [u8]) -> hidapi::HidResult<usize> {
                Ok(0)
            }
            fn write(&self, data: &[u8]) -> hidapi::HidResult<usize> {
                self.0.lock().unwrap().push(data.to_vec());
                Ok(data.len())
            }
        }
        let writes = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let router = OutputRouter::default();
        router.open(
            NORMALIZED,
            std::sync::Arc::new(std::sync::Mutex::new(Sink(writes.clone()))),
            crate::output::OutputRate::new(100),
        );

        let cmd = IoCommand {
            serial: RAW.into(),
            r: 9,
            g: 8,
            b: 7,
            rumble: 0,
        };
        assert!(route_command(&router, &cmd), "colon-form serial not routed");

        let deadline = std::time::Instant::now() + Duration::from_secs(1);
        while writes.lock().unwrap().is_empty() && std::time::Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(5));
        }
        let first = writes.lock().unwrap().first().cloned().expect("a write");
        assert_eq!((first[2], first[3], first[4]), (9, 8, 7));
    }

    // --- StreamIO through a real tonic server (#2374) ---

    use crate::device_manager::DeviceOutputs;
    use crate::device_manager::tests::{
        FakeDevice, SERIAL, loop_polls, sample_report, shutdown, spawn_loop_with,
    };
    use crate::service::proto::controller_io_service_client::ControllerIoServiceClient;
    use crate::service::proto::controller_io_service_server::ControllerIoServiceServer;
    use crate::watchdog::Heartbeat;
    use tokio::sync::mpsc as tokio_mpsc;
    use tokio_stream::wrappers::ReceiverStream;

    /// The service on an ephemeral port in front of a device loop that owns one fake controller.
    struct Harness {
        client: ControllerIoServiceClient<tonic::transport::Channel>,
        device: FakeDevice,
        cmd_tx: std_mpsc::Sender<DeviceCmd>,
        device_loop: std::thread::JoinHandle<()>,
    }

    impl Harness {
        async fn start() -> Self {
            let device = FakeDevice::default();
            let outputs = DeviceOutputs::default();
            let (cmd_tx, device_loop) = spawn_loop_with(&device, Heartbeat::new(), outputs.clone());
            let service = ControllerIoServiceImpl::new(cmd_tx.clone(), outputs.router.clone());

            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let addr = listener.local_addr().unwrap();
            tokio::spawn(async move {
                tonic::transport::Server::builder()
                    .add_service(ControllerIoServiceServer::new(service))
                    .serve_with_incoming(tonic::transport::server::TcpIncoming::from(listener))
                    .await
                    .unwrap();
            });
            let client = ControllerIoServiceClient::connect(format!("http://{addr}"))
                .await
                .unwrap();
            Self {
                client,
                device,
                cmd_tx,
                device_loop,
            }
        }

        async fn discover(&mut self, force: bool, verify_only: bool) -> Vec<DiscoveredController> {
            self.client
                .discover(DiscoverRequest {
                    force,
                    verify_only,
                    exclude_serials: vec![],
                })
                .await
                .unwrap()
                .into_inner()
                .controllers
        }

        /// Open a StreamIO call; the sender feeds its commands. Returns once the
        /// device loop polls, so the stream is registered and its input flushed.
        async fn open_stream(&mut self) -> (tokio_mpsc::Sender<IoCommand>, Streaming<SensorData>) {
            let (tx, rx) = tokio_mpsc::channel(8);
            let responses = self
                .client
                .stream_io(ReceiverStream::new(rx))
                .await
                .unwrap()
                .into_inner();
            let device = self.device.clone();
            assert!(
                tokio::task::spawn_blocking(move || loop_polls(&device))
                    .await
                    .unwrap(),
                "the device loop did not poll after the stream opened"
            );
            (tx, responses)
        }

        fn stop(self) {
            shutdown(&self.cmd_tx, self.device_loop);
        }
    }

    async fn next_sensor(responses: &mut Streaming<SensorData>) -> SensorData {
        tokio::time::timeout(Duration::from_secs(3), responses.message())
            .await
            .expect("no sensor data within 3 s")
            .unwrap()
            .expect("the stream ended")
    }

    async fn wait_for_write(device: &FakeDevice) -> Vec<u8> {
        let deadline = std::time::Instant::now() + Duration::from_secs(3);
        loop {
            if let Some(first) = device.0.lock().unwrap().writes.first().cloned() {
                return first;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "no output report written"
            );
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    }

    // UC: UC-002 main step 2
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn test_stream_io_sends_sensor_data_and_takes_led_commands_over_grpc() {
        let mut h = Harness::start().await;
        let found = h.discover(false, false).await;
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].serial, SERIAL);
        assert_eq!(found[0].adapter_name, "hci0");

        let (commands, mut responses) = h.open_stream().await;

        // Device -> client: a report arrives as SensorData
        h.device
            .0
            .lock()
            .unwrap()
            .pending
            .push_back(sample_report());
        let data = next_sensor(&mut responses).await;
        assert_eq!(data.serial, SERIAL);
        assert_eq!(data.trigger, 200);
        assert!(data.button_cross);
        assert!(!data.button_circle);
        assert_eq!(data.gyro_x, -1234.0);

        // Client -> device: an IoCommand (colon-form serial, out-of-range values) is written
        commands
            .send(IoCommand {
                serial: "00:06:f7:aa:bb:cc".into(),
                r: 300,
                g: 8,
                b: -5,
                rumble: 0,
            })
            .await
            .unwrap();
        let report = wait_for_write(&h.device).await;
        assert_eq!((report[2], report[3], report[4]), (255, 8, 0));
        h.stop();
    }

    // UC: UC-002 A7 step 2
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn test_a_client_that_hangs_up_leaves_room_for_a_new_stream() {
        let mut h = Harness::start().await;
        h.discover(false, false).await;

        let (commands, responses) = h.open_stream().await;
        drop(commands);
        drop(responses);
        // The server notices the hang-up and unregisters the first stream
        tokio::time::sleep(Duration::from_millis(200)).await;

        let (_commands, mut responses) = h.open_stream().await;
        h.device
            .0
            .lock()
            .unwrap()
            .pending
            .push_back(sample_report());
        let data = next_sensor(&mut responses).await;
        assert_eq!(data.trigger, 200);
        h.stop();
    }

    // UC: UC-002 main step 2
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn test_a_command_for_an_unopened_controller_is_ignored_and_the_stream_stays_up() {
        let mut h = Harness::start().await;
        h.discover(false, false).await; // the loop polls an open controller
        let (commands, mut responses) = h.open_stream().await;
        commands
            .send(IoCommand {
                serial: "0006F7000000".into(),
                r: 1,
                g: 2,
                b: 3,
                rumble: 0,
            })
            .await
            .unwrap();
        // The call stays up, and nothing was written
        assert!(
            tokio::time::timeout(Duration::from_millis(300), responses.message())
                .await
                .is_err(),
            "the stream ended or produced data"
        );
        assert!(h.device.0.lock().unwrap().writes.is_empty());
        h.stop();
    }

    // UC: UC-002 BR-006
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn test_discover_options_over_grpc_while_a_stream_is_open() {
        let mut h = Harness::start().await;

        // verify_only skips the enumeration: nothing is open yet, so nothing is found
        assert!(h.discover(false, true).await.is_empty());
        // A plain pass enumerates and opens the controller
        assert_eq!(h.discover(false, false).await.len(), 1);

        let (_commands, _responses) = h.open_stream().await;
        // With a stream open the device loop's poll finds dead controllers, so
        // `force` changes nothing: same answer, and the controller is still open
        let plain = h.discover(false, false).await;
        let forced = h.discover(true, false).await;
        assert_eq!(plain, forced);
        assert_eq!(plain.len(), 1);
        // verify_only keeps working: it reports the open controller without enumerating
        assert_eq!(h.discover(false, true).await, plain);
        h.stop();
    }
}
