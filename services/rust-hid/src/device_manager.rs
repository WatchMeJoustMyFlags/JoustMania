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

use crate::hid::{self, HidDeviceIo};
use crate::watchdog::Heartbeat;

/// Interval between two polls of the open devices. PS Move hardware reports
/// at ~1 kHz, so 1 ms keeps up with it without busy-waiting (UC-030 step 2).
const POLL_INTERVAL: Duration = Duration::from_millis(1);

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
        exclude_serials: Vec<String>,
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

/// Device enumeration and opening, the seam between the device loop and
/// HidApi. Production uses `HidApi` (static dispatch); tests use a fake.
pub trait HidBackend {
    type Device: HidDeviceIo;

    /// Re-enumerate the attached HID devices.
    fn refresh_devices(&mut self) -> hidapi::HidResult<()>;
    /// Open every attached PS Move: (serial, device, product_id, adapter_name).
    fn discover_controllers(&self) -> Vec<(String, Self::Device, u16, Option<String>)>;
    /// Open the attached PS Move with this serial: (device, product_id).
    fn open_controller_by_serial(&self, serial: &str) -> Option<(Self::Device, u16)>;
}

impl HidBackend for HidApi {
    type Device = hidapi::HidDevice;

    fn refresh_devices(&mut self) -> hidapi::HidResult<()> {
        HidApi::refresh_devices(self)
    }

    fn discover_controllers(&self) -> Vec<(String, Self::Device, u16, Option<String>)> {
        hid::discover_controllers(self)
    }

    fn open_controller_by_serial(&self, serial: &str) -> Option<(Self::Device, u16)> {
        hid::open_controller_by_serial(self, serial)
    }
}

/// An open HID device with its product metadata.
struct DeviceEntry<D> {
    device: D,
    product_id: u16,
    adapter_name: Option<String>,
}

/// Run the device manager loop on the current thread (blocking).
///
/// This function does not return until a `Shutdown` command is received.
/// It owns all HidDevice handles and processes commands + polls devices.
/// Every loop pass beats `heartbeat`, which the watchdog reads (UC-002 A7).
pub fn run_device_manager(cmd_rx: mpsc::Receiver<DeviceCmd>, heartbeat: Heartbeat) {
    let api = match HidApi::new() {
        Ok(api) => api,
        Err(e) => {
            warn!(error = %e, "Failed to initialize HID API for device manager");
            return;
        }
    };
    run_device_loop(api, cmd_rx, &heartbeat);
}

/// The device manager loop over any `HidBackend` (see `run_device_manager`).
fn run_device_loop<B: HidBackend>(
    mut api: B,
    cmd_rx: mpsc::Receiver<DeviceCmd>,
    heartbeat: &Heartbeat,
) {
    let mut devices: HashMap<String, DeviceEntry<B::Device>> = HashMap::new();
    let mut stream_sender: Option<tokio::sync::mpsc::Sender<StreamSensorData>> = None;

    info!("Device manager thread started");

    loop {
        heartbeat.beat();

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

        // Brief sleep to avoid busy-waiting (~1 kHz poll rate)
        std::thread::sleep(POLL_INTERVAL);
    }
}

/// Handle a single command. Returns false if shutdown requested.
fn handle_command<B: HidBackend>(
    cmd: DeviceCmd,
    api: &mut B,
    devices: &mut HashMap<String, DeviceEntry<B::Device>>,
    stream_sender: &mut Option<tokio::sync::mpsc::Sender<StreamSensorData>>,
) -> bool {
    match cmd {
        DeviceCmd::Discover { force, verify_only, exclude_serials, reply } => {
            let serials = handle_discover(api, devices, force, verify_only, &exclude_serials);
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
///
/// `exclude_serials` lists controllers claimed by another adapter (e.g. hidapi).
/// Excluded serials are skipped during enumeration — the device handle is not
/// opened, avoiding the open→close churn that causes LED flashing.
fn handle_discover<B: HidBackend>(
    api: &mut B,
    devices: &mut HashMap<String, DeviceEntry<B::Device>>,
    force: bool,
    verify_only: bool,
    exclude_serials: &[String],
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

        let discovered = api.discover_controllers();
        for (serial, device, product_id, adapter_name) in discovered {
            // Skip controllers claimed by another adapter
            if exclude_serials.iter().any(|s| s == &serial) {
                debug!(serial = %serial, "Skipping excluded controller (claimed by another adapter)");
                continue;
            }
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
fn handle_open<B: HidBackend>(
    api: &mut B,
    devices: &mut HashMap<String, DeviceEntry<B::Device>>,
    serial: &str,
) -> bool {
    if devices.contains_key(serial) {
        return true; // Already open
    }

    if let Err(e) = api.refresh_devices() {
        warn!(error = %e, "Failed to refresh HID device list");
    }

    match api.open_controller_by_serial(serial) {
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
fn handle_close<D: HidDeviceIo>(devices: &mut HashMap<String, DeviceEntry<D>>, serial: &str) {
    if let Some(entry) = devices.remove(serial) {
        let off = hid::build_output_report(0, 0, 0, 0);
        let _ = entry.device.write(&off);
        info!(serial = %serial, "Closed controller");
    }
}

/// Close all controllers.
fn handle_close_all<D: HidDeviceIo>(devices: &mut HashMap<String, DeviceEntry<D>>) {
    for (serial, entry) in devices.drain() {
        let off = hid::build_output_report(0, 0, 0, 0);
        let _ = entry.device.write(&off);
        debug!(serial = %serial, "Closed controller (close_all)");
    }
    info!("All controllers closed");
}

/// Poll all open devices and send sensor data through the stream.
fn poll_all_devices<D: HidDeviceIo>(
    devices: &HashMap<String, DeviceEntry<D>>,
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;
    use std::sync::{Arc, Mutex};
    use std::thread::JoinHandle;
    use std::time::Instant;

    const SERIAL: &str = "00:06:f7:aa:bb:cc";
    const PRODUCT_ID_ZCM2: u16 = 0x0C5E;

    #[derive(Default)]
    struct FakeDeviceState {
        /// Input reports the device will return, oldest first.
        pending: VecDeque<[u8; hid::INPUT_REPORT_SIZE]>,
        /// When a read found no pending report: once per poll of this device.
        drained_at: Vec<Instant>,
    }

    /// In-memory HID device shared between the test and the device loop thread.
    #[derive(Clone, Default)]
    struct FakeDevice(Arc<Mutex<FakeDeviceState>>);

    impl HidDeviceIo for FakeDevice {
        fn read(&self, buf: &mut [u8]) -> hidapi::HidResult<usize> {
            let mut state = self.0.lock().unwrap();
            match state.pending.pop_front() {
                Some(report) => {
                    buf[..report.len()].copy_from_slice(&report);
                    Ok(report.len())
                }
                None => {
                    state.drained_at.push(Instant::now());
                    Ok(0)
                }
            }
        }

        fn write(&self, data: &[u8]) -> hidapi::HidResult<usize> {
            Ok(data.len())
        }
    }

    /// Backend with one attached ZCM2 controller.
    struct FakeBackend {
        device: FakeDevice,
    }

    impl HidBackend for FakeBackend {
        type Device = FakeDevice;

        fn refresh_devices(&mut self) -> hidapi::HidResult<()> {
            Ok(())
        }

        fn discover_controllers(&self) -> Vec<(String, FakeDevice, u16, Option<String>)> {
            vec![(SERIAL.to_string(), self.device.clone(), PRODUCT_ID_ZCM2, Some("hci0".to_string()))]
        }

        fn open_controller_by_serial(&self, serial: &str) -> Option<(FakeDevice, u16)> {
            (serial == SERIAL).then(|| (self.device.clone(), PRODUCT_ID_ZCM2))
        }
    }

    fn spawn_loop(device: &FakeDevice) -> (mpsc::Sender<DeviceCmd>, JoinHandle<()>) {
        spawn_loop_with_heartbeat(device, Heartbeat::new())
    }

    fn spawn_loop_with_heartbeat(
        device: &FakeDevice,
        heartbeat: Heartbeat,
    ) -> (mpsc::Sender<DeviceCmd>, JoinHandle<()>) {
        let (cmd_tx, cmd_rx) = mpsc::channel();
        let backend = FakeBackend { device: device.clone() };
        let handle = std::thread::spawn(move || run_device_loop(backend, cmd_rx, &heartbeat));
        (cmd_tx, handle)
    }

    fn shutdown(cmd_tx: &mpsc::Sender<DeviceCmd>, handle: JoinHandle<()>) {
        cmd_tx.send(DeviceCmd::Shutdown).unwrap();
        handle.join().unwrap();
    }

    /// A ZCM2 input report: trigger 200, Cross pressed, gyro x = -1234.
    fn sample_report() -> [u8; hid::INPUT_REPORT_SIZE] {
        let mut data = [0u8; hid::INPUT_REPORT_SIZE];
        data[0] = 0x01;
        data[2] = hid::Button::CROSS as u8;
        data[6] = 200;
        data[31..33].copy_from_slice(&(-1234i16).to_le_bytes());
        data
    }

    fn recv_within(
        rx: &mut tokio::sync::mpsc::Receiver<StreamSensorData>,
        timeout: Duration,
    ) -> Option<StreamSensorData> {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if let Ok(data) = rx.try_recv() {
                return Some(data);
            }
            std::thread::sleep(Duration::from_millis(1));
        }
        None
    }

    // UC: UC-002 main step 2
    #[test]
    fn test_discovered_device_is_opened_and_its_reports_streamed() {
        let device = FakeDevice::default();
        let (cmd_tx, handle) = spawn_loop(&device);

        let (reply_tx, reply_rx) = tokio::sync::oneshot::channel();
        cmd_tx
            .send(DeviceCmd::Discover {
                force: false,
                verify_only: false,
                exclude_serials: vec![],
                reply: reply_tx,
            })
            .unwrap();
        let discovered = reply_rx.blocking_recv().unwrap();
        assert_eq!(discovered.len(), 1);
        assert_eq!(discovered[0].serial, SERIAL);
        assert_eq!(discovered[0].adapter_name, "hci0");
        assert_eq!(discovered[0].product_id, PRODUCT_ID_ZCM2 as u32);

        let (sensor_tx, mut sensor_rx) = tokio::sync::mpsc::channel(16);
        cmd_tx.send(DeviceCmd::RegisterStream { sender: sensor_tx }).unwrap();
        device.0.lock().unwrap().pending.push_back(sample_report());

        let data = recv_within(&mut sensor_rx, Duration::from_secs(2))
            .expect("the device loop streamed no sensor data from the opened device");
        shutdown(&cmd_tx, handle);

        assert_eq!(data.serial, SERIAL);
        assert!(data.report.button_cross());
        assert_eq!(data.report.trigger, 200);
        // ZCM2 product id: gyro decoded as signed 16-bit
        assert_eq!(data.report.gyro_x, -1234.0);
    }

    // UC: UC-030 main step 2
    // NFR: NFR-001
    #[test]
    fn test_poll_loop_polls_open_device_about_every_millisecond() {
        let device = FakeDevice::default();
        let (cmd_tx, handle) = spawn_loop(&device);

        let (reply_tx, reply_rx) = tokio::sync::oneshot::channel();
        cmd_tx
            .send(DeviceCmd::Open { serial: SERIAL.to_string(), reply: reply_tx })
            .unwrap();
        assert!(reply_rx.blocking_recv().unwrap());
        let (sensor_tx, _sensor_rx) = tokio::sync::mpsc::channel(16);
        cmd_tx.send(DeviceCmd::RegisterStream { sender: sensor_tx }).unwrap();

        std::thread::sleep(Duration::from_millis(300));
        shutdown(&cmd_tx, handle);

        let polls = std::mem::take(&mut device.0.lock().unwrap().drained_at);
        let mut intervals: Vec<Duration> = polls.windows(2).map(|w| w[1] - w[0]).collect();
        assert!(intervals.len() >= 50, "only {} poll intervals in 300 ms", intervals.len());
        intervals.sort();
        let min = intervals[0];
        let median = intervals[intervals.len() / 2];
        let p90 = intervals[intervals.len() * 9 / 10];
        println!(
            "rust-hid poll interval: n={} min={:?} median={:?} p90={:?} max={:?}",
            intervals.len(),
            min,
            median,
            p90,
            intervals[intervals.len() - 1]
        );
        // Every pass sleeps POLL_INTERVAL (1 ms), so no interval is shorter (no busy-wait).
        // A median within 2 ms keeps up with the ~1 kHz controller, and p90 within 5 ms
        // catches periodic stalls. The max is not bounded: one scheduler hiccup under
        // CI load is not a defect.
        assert!(min >= POLL_INTERVAL, "min poll interval {min:?} < 1 ms: busy-waiting");
        assert!(median <= Duration::from_millis(2), "median poll interval {median:?} > 2 ms");
        assert!(p90 <= Duration::from_millis(5), "p90 poll interval {p90:?} > 5 ms");
    }

    // UC: UC-002 A7
    // NFR: NFR-054
    #[test]
    fn test_device_loop_beats_the_watchdog_heartbeat() {
        let device = FakeDevice::default();
        let heartbeat = Heartbeat::new();
        let (cmd_tx, handle) = spawn_loop_with_heartbeat(&device, heartbeat.clone());

        std::thread::sleep(Duration::from_millis(200));
        let age_running = heartbeat.age();
        shutdown(&cmd_tx, handle);

        assert!(
            age_running < Duration::from_millis(100),
            "heartbeat {age_running:?} old while the device loop runs"
        );
    }
}
