//! Device Manager — manages HID device handles on a dedicated blocking thread.
//!
//! All HID I/O is funnelled through a single thread via command channels,
//! avoiding Send/Sync concerns with HidDevice. The polling loop continuously
//! reads input reports from all open devices and pushes SensorData through
//! a registered stream sender.

use hidapi::HidApi;
use std::collections::HashMap;
use std::sync::mpsc;
use std::time::Duration;
use tracing::{debug, info, warn};

use crate::hid;

/// Discovered controller info returned from discovery.
#[derive(Debug, Clone)]
pub struct DiscoveredControllerInfo {
    pub serial: String,
    pub adapter_name: String,  // empty if unknown
    pub product_id: u32,
}

/// Commands sent from gRPC handlers to the device manager thread.
pub enum DeviceCmd {
    Discover {
        force: bool,
        verify_only: bool,
        reply: tokio::sync::oneshot::Sender<Vec<DiscoveredControllerInfo>>,
    },
    Open {
        serial: String,
        reply: tokio::sync::oneshot::Sender<bool>,
    },
    Close {
        serial: String,
    },
    CloseAll,
    SetOutput {
        serial: String,
        r: u8,
        g: u8,
        b: u8,
        rumble: u8,
    },
    /// Register the tokio mpsc sender for streaming sensor data.
    RegisterStream {
        sender: tokio::sync::mpsc::Sender<StreamSensorData>,
    },
    /// Unregister the stream sender.
    UnregisterStream,
    /// Shutdown the device manager thread.
    #[allow(dead_code)]
    Shutdown,
}

/// Sensor data sent from the device manager to the gRPC stream.
#[derive(Debug, Clone)]
pub struct StreamSensorData {
    pub serial: String,
    pub report: hid::ParsedInputReport,
}

/// An open HID device with its product metadata.
struct DeviceEntry {
    device: hidapi::HidDevice,
    product_id: u16,
    adapter_name: Option<String>,
}

/// Run the device manager loop on the current thread (blocking).
///
/// This function does not return until a `Shutdown` command is received.
/// It owns all HidDevice handles and processes commands + polls devices.
pub fn run_device_manager(cmd_rx: mpsc::Receiver<DeviceCmd>) {
    let mut api = match HidApi::new() {
        Ok(api) => api,
        Err(e) => {
            warn!(error = %e, "Failed to initialize HID API for device manager");
            return;
        }
    };

    let mut devices: HashMap<String, DeviceEntry> = HashMap::new();
    let mut stream_sender: Option<tokio::sync::mpsc::Sender<StreamSensorData>> = None;

    info!("Device manager thread started");

    loop {
        // Process all pending commands (non-blocking)
        loop {
            match cmd_rx.try_recv() {
                Ok(cmd) => {
                    if !handle_command(cmd, &mut api, &mut devices, &mut stream_sender) {
                        info!("Device manager shutting down");
                        // Close all devices
                        for (serial, entry) in devices.drain() {
                            let off = hid::build_output_report(0, 0, 0, 0);
                            let _ = entry.device.write(&off);
                            debug!(serial = %serial, "Closed device on shutdown");
                        }
                        return;
                    }
                }
                Err(mpsc::TryRecvError::Empty) => break,
                Err(mpsc::TryRecvError::Disconnected) => {
                    info!("Device manager command channel closed");
                    return;
                }
            }
        }

        // Poll all open devices for input reports
        if let Some(ref sender) = stream_sender {
            poll_all_devices(&devices, sender);
        }

        // Brief sleep to avoid busy-waiting
        // 1ms gives ~1kHz poll rate (hardware is 1kHz for PS Move)
        std::thread::sleep(Duration::from_millis(1));
    }
}

/// Handle a single command. Returns false if shutdown requested.
fn handle_command(
    cmd: DeviceCmd,
    api: &mut HidApi,
    devices: &mut HashMap<String, DeviceEntry>,
    stream_sender: &mut Option<tokio::sync::mpsc::Sender<StreamSensorData>>,
) -> bool {
    match cmd {
        DeviceCmd::Discover { force, verify_only, reply } => {
            let serials = handle_discover(api, devices, force, verify_only);
            let _ = reply.send(serials);
        }
        DeviceCmd::Open { serial, reply } => {
            let success = handle_open(api, devices, &serial);
            let _ = reply.send(success);
        }
        DeviceCmd::Close { serial } => {
            handle_close(devices, &serial);
        }
        DeviceCmd::CloseAll => {
            handle_close_all(devices);
        }
        DeviceCmd::SetOutput { serial, r, g, b, rumble } => {
            if let Some(entry) = devices.get(&serial) {
                let report = hid::build_output_report(r, g, b, rumble);
                if let Err(e) = entry.device.write(&report) {
                    warn!(serial = %serial, error = %e, "Failed to write output report");
                }
            }
        }
        DeviceCmd::RegisterStream { sender } => {
            info!("Stream registered");
            *stream_sender = Some(sender);
        }
        DeviceCmd::UnregisterStream => {
            info!("Stream unregistered");
            *stream_sender = None;
        }
        DeviceCmd::Shutdown => {
            return false;
        }
    }
    true
}

/// Discover controllers: enumerate HID devices, open new ones.
fn handle_discover(
    api: &mut HidApi,
    devices: &mut HashMap<String, DeviceEntry>,
    force: bool,
    verify_only: bool,
) -> Vec<DiscoveredControllerInfo> {
    // Verify existing devices if not forcing
    if !force && !devices.is_empty() {
        let stale: Vec<String> = devices
            .iter()
            .filter_map(|(serial, entry)| {
                let mut buf = [0u8; 64];
                match entry.device.read(&mut buf) {
                    Ok(_) => None,             // Data or no data, device is alive
                    Err(_) => Some(serial.clone()), // Device gone
                }
            })
            .collect();
        for serial in stale {
            info!(serial = %serial, "Removing stale device");
            devices.remove(&serial);
        }
    }

    if !verify_only {
        if let Err(e) = api.refresh_devices() {
            warn!(error = %e, "Failed to refresh HID device list");
        }

        let discovered = hid::discover_controllers(api);
        for (serial, device, product_id, adapter_name) in discovered {
            devices.entry(serial.clone()).or_insert_with(|| {
                info!(serial = %serial, "New controller discovered");
                DeviceEntry { device, product_id, adapter_name }
            });
        }
    }

    devices
        .iter()
        .map(|(serial, entry)| DiscoveredControllerInfo {
            serial: serial.clone(),
            adapter_name: entry.adapter_name.clone().unwrap_or_default(),
            product_id: entry.product_id as u32,
        })
        .collect()
}

/// Open a specific controller by serial.
fn handle_open(
    api: &mut HidApi,
    devices: &mut HashMap<String, DeviceEntry>,
    serial: &str,
) -> bool {
    if devices.contains_key(serial) {
        return true; // Already open
    }

    if let Err(e) = api.refresh_devices() {
        warn!(error = %e, "Failed to refresh HID device list");
    }

    match hid::open_controller_by_serial(api, serial) {
        Some((device, product_id)) => {
            info!(serial = %serial, "Opened controller");
            devices.insert(serial.to_string(), DeviceEntry { device, product_id, adapter_name: None });
            true
        }
        None => {
            warn!(serial = %serial, "Controller not found");
            false
        }
    }
}

/// Close a specific controller.
fn handle_close(devices: &mut HashMap<String, DeviceEntry>, serial: &str) {
    if let Some(entry) = devices.remove(serial) {
        let off = hid::build_output_report(0, 0, 0, 0);
        let _ = entry.device.write(&off);
        info!(serial = %serial, "Closed controller");
    }
}

/// Close all controllers.
fn handle_close_all(devices: &mut HashMap<String, DeviceEntry>) {
    for (serial, entry) in devices.drain() {
        let off = hid::build_output_report(0, 0, 0, 0);
        let _ = entry.device.write(&off);
        debug!(serial = %serial, "Closed controller (close_all)");
    }
    info!("All controllers closed");
}

/// Poll all open devices and send sensor data through the stream.
fn poll_all_devices(
    devices: &HashMap<String, DeviceEntry>,
    sender: &tokio::sync::mpsc::Sender<StreamSensorData>,
) {
    for (serial, entry) in devices.iter() {
        let zcm2 = hid::is_zcm2(entry.product_id);
        if let Some(report) = hid::read_latest_input(&entry.device, zcm2) {
            let data = StreamSensorData {
                serial: serial.clone(),
                report,
            };
            // Non-blocking send: if the channel is full, drop the data
            // (the stream consumer will get the next one)
            if sender.try_send(data).is_err() {
                debug!(serial = %serial, "Stream channel full, dropping sensor data");
            }
        }
    }
}
