//! PairingService gRPC implementation.

use hidapi::HidApi;
use std::sync::Mutex;
use tonic::{Request, Response, Status};
use tracing::{info, instrument, warn};

pub mod proto {
    tonic::include_proto!("joustmania.psmove_hid");
}

use proto::pairing_service_server::PairingService;
use proto::{
    GetUsbControllersRequest, GetUsbControllersResponse, PairControllerRequest,
    PairControllerResponse, UsbController,
};

use crate::hid;

/// gRPC service implementation for PS Move pairing operations.
pub struct PairingServiceImpl {
    hid_api: Mutex<HidApi>,
}

impl PairingServiceImpl {
    pub fn new() -> Result<Self, String> {
        let api = HidApi::new().map_err(|e| format!("Failed to initialize HID API: {e}"))?;
        Ok(Self {
            hid_api: Mutex::new(api),
        })
    }
}

#[tonic::async_trait]
impl PairingService for PairingServiceImpl {
    #[instrument(
        skip(self, _request),
        fields(rpc.system = "grpc", rpc.service = "PairingService", rpc.method = "GetUsbControllers")
    )]
    async fn get_usb_controllers(
        &self,
        _request: Request<GetUsbControllersRequest>,
    ) -> Result<Response<GetUsbControllersResponse>, Status> {
        let controllers = {
            let mut api = self
                .hid_api
                .lock()
                .map_err(|e| Status::internal(format!("HID API lock poisoned: {e}")))?;

            // Refresh device list to pick up hot-plugged controllers
            if let Err(e) = api.refresh_devices() {
                warn!(error = %e, "Failed to refresh HID device list");
            }

            hid::enumerate_usb_controllers(&api)
        };

        info!(count = controllers.len(), "Enumerated USB controllers");

        let proto_controllers: Vec<UsbController> = controllers
            .into_iter()
            .map(|c| UsbController {
                device_path: c.device_path,
                serial: c.serial,
            })
            .collect();

        Ok(Response::new(GetUsbControllersResponse {
            controllers: proto_controllers,
        }))
    }

    #[instrument(
        skip(self, request),
        fields(rpc.system = "grpc", rpc.service = "PairingService", rpc.method = "PairController", serial, adapter_address)
    )]
    async fn pair_controller(
        &self,
        request: Request<PairControllerRequest>,
    ) -> Result<Response<PairControllerResponse>, Status> {
        let req = request.into_inner();
        tracing::Span::current().record("serial", req.serial.as_str());
        tracing::Span::current().record("adapter_address", req.adapter_address.as_str());

        if req.device_path.is_empty() {
            return Err(Status::invalid_argument("device_path is required"));
        }
        if req.adapter_address.is_empty() {
            return Err(Status::invalid_argument("adapter_address is required"));
        }

        info!(
            device_path = %req.device_path,
            serial = %req.serial,
            adapter_address = %req.adapter_address,
            "Pairing controller"
        );

        let api = self
            .hid_api
            .lock()
            .map_err(|e| Status::internal(format!("HID API lock poisoned: {e}")))?;

        match hid::pair_controller(&api, &req.device_path, &req.adapter_address) {
            Ok((already_paired, previous_host)) => {
                if already_paired {
                    info!(serial = %req.serial, "Controller already paired");
                } else {
                    info!(
                        serial = %req.serial,
                        previous_host = %previous_host,
                        new_host = %req.adapter_address,
                        "Controller paired successfully"
                    );
                }

                Ok(Response::new(PairControllerResponse {
                    success: true,
                    already_paired,
                    previous_host,
                }))
            }
            Err(e) => {
                warn!(
                    serial = %req.serial,
                    error = %e,
                    "Pairing failed"
                );
                Ok(Response::new(PairControllerResponse {
                    success: false,
                    already_paired: false,
                    previous_host: String::new(),
                }))
            }
        }
    }
}
