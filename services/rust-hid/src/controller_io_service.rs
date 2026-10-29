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
                        outputs.submit(
                            &normalize_serial(&io_cmd.serial),
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
}
