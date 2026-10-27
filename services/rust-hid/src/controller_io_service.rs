//! ControllerIOService gRPC implementation.
//!
//! Translates gRPC calls into DeviceCmd messages sent to the device manager
//! thread. The StreamIO bidir stream registers a sender for continuous
//! sensor data and hands IOCommands to the per-controller output slots
//! (`output::OutputRouter`, coalesced and rate-capped, UC-002 BR-005).

use std::pin::Pin;
use std::sync::mpsc as std_mpsc;
use std::time::Duration;
use tokio::sync::{mpsc, oneshot};
use tokio_stream::StreamExt;
use tokio_stream::wrappers::ReceiverStream;
use tonic::{Request, Response, Status, Streaming};
use tracing::{info, trace, warn};

use crate::device_manager::{DeviceCmd, StreamSensorData};
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
            exclude_serials: req.exclude_serials,
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
        let serial = request.into_inner().serial;
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

        // Create channel for sensor data (device manager -> gRPC stream)
        let (sensor_tx, sensor_rx) = mpsc::channel::<StreamSensorData>(1024);

        // Register the sender with the device manager
        self.send_cmd(DeviceCmd::RegisterStream { sender: sensor_tx })?;

        // Spawn task to read IOCommands from the client into the output slots
        // (never blocks: coalesced per controller, written by its writer thread)
        let cmd_tx_clone = cmd_tx.clone();
        let outputs = self.outputs.clone();
        tokio::spawn(async move {
            while let Some(result) = inbound.next().await {
                match result {
                    Ok(io_cmd) => {
                        outputs.submit(
                            &io_cmd.serial,
                            io_cmd.r.clamp(0, 255) as u8,
                            io_cmd.g.clamp(0, 255) as u8,
                            io_cmd.b.clamp(0, 255) as u8,
                            io_cmd.rumble.clamp(0, 255) as u8,
                        );
                    }
                    Err(e) => {
                        warn!(error = %e, "Error reading IOCommand from stream");
                        break;
                    }
                }
            }
            // Client disconnected — unregister stream
            let _ = cmd_tx_clone.send(DeviceCmd::UnregisterStream);
            info!("StreamIO client disconnected");
        });

        // Convert sensor data channel to a gRPC response stream
        #[allow(clippy::result_large_err)] // tonic::Status is inherently large
        let output_stream = ReceiverStream::new(sensor_rx).map(|data| {
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

        info!("StreamIO started");
        Ok(Response::new(Box::pin(output_stream)))
    }
}
