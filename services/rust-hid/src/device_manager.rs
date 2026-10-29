//! Device Manager — manages HID device handles on a dedicated blocking thread.
//!
//! Discovery, open, close and input polling run on this one thread via a
//! command channel. The polling loop continuously reads input reports from all
//! open devices and pushes SensorData through a registered stream sender.
//! Output reports (LED, rumble) never run here: each open device has a writer
//! thread in `output` (UC-002 BR-005, #2125). The device sits behind a mutex
//! the writer holds only during one write; the poll uses `try_lock`, so a
//! blocked write skips that device's reads instead of stalling the loop.

use hidapi::HidApi;
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, TryLockError, mpsc};
use std::time::Duration;
use tracing::{debug, info, warn};

use crate::hid::{self, HidDeviceIo};
use crate::input_slot::InputSink;
use crate::output::{OutputRate, OutputRouter};
use crate::watchdog::Heartbeat;

/// Interval between two polls of the open devices. PS Move hardware reports
/// at ~1 kHz, so 1 ms keeps up with it without busy-waiting (UC-030 step 2).
const POLL_INTERVAL: Duration = Duration::from_millis(1);

/// Commands handled per loop pass before the devices are polled again (#2125).
const MAX_CMDS_PER_PASS: usize = 64;

/// Discovered controller info returned from discovery.
#[derive(Debug, Clone)]
pub struct DiscoveredControllerInfo {
    pub serial: String,
    pub adapter_name: String, // empty if unknown
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
    /// Register the latest-sample sink for streaming sensor data (#2372).
    /// `id` (from `next_stream_id`) names this registration; the newest one
    /// wins. Every open device's input is flushed first (not forwarded).
    RegisterStream {
        id: u64,
        sender: InputSink,
    },
    /// Unregister the stream registered with `id`. A late unregister from an
    /// older stream (its id no longer registered) is ignored (#2371).
    UnregisterStream {
        id: u64,
    },
    /// Shutdown the device manager thread: every open controller is closed,
    /// so each writer sends LED off and rumble 0 (B5, #2373).
    Shutdown,
}

/// Source of `RegisterStream` ids: unique for the life of the process.
static NEXT_STREAM_ID: AtomicU64 = AtomicU64::new(1);

/// A new, unique stream registration id.
pub fn next_stream_id() -> u64 {
    NEXT_STREAM_ID.fetch_add(1, Ordering::Relaxed)
}

/// The registered input stream: its id and the sink to its gRPC stream.
struct ActiveStream {
    id: u64,
    sender: InputSink,
}

/// Device enumeration and opening, the seam between the device loop and
/// HidApi. Production uses `HidApi` (static dispatch); tests use a fake.
pub trait HidBackend {
    type Device: HidDeviceIo + Send + 'static;

    /// Re-enumerate the attached HID devices.
    fn refresh_devices(&mut self) -> hidapi::HidResult<()>;
    /// Open every attached PS Move whose serial `skip` rejects:
    /// (serial, device, product_id, adapter_name). A skipped serial is not opened.
    fn discover_controllers(
        &self,
        skip: &dyn Fn(&str) -> bool,
    ) -> Vec<(String, Self::Device, u16, Option<String>)>;
    /// Open the attached PS Move with this serial: (device, product_id, adapter_name).
    /// The adapter is resolved once here, the same way discovery does (#2200).
    fn open_controller_by_serial(
        &self,
        serial: &str,
    ) -> Option<(Self::Device, u16, Option<String>)>;
}

impl HidBackend for HidApi {
    type Device = hidapi::HidDevice;

    fn refresh_devices(&mut self) -> hidapi::HidResult<()> {
        HidApi::refresh_devices(self)
    }

    fn discover_controllers(
        &self,
        skip: &dyn Fn(&str) -> bool,
    ) -> Vec<(String, Self::Device, u16, Option<String>)> {
        hid::discover_controllers(self, skip)
    }

    fn open_controller_by_serial(
        &self,
        serial: &str,
    ) -> Option<(Self::Device, u16, Option<String>)> {
        hid::open_controller_by_serial(self, serial)
    }
}

/// An open HID device with its product metadata.
struct DeviceEntry<D> {
    device: Arc<Mutex<D>>,
    product_id: u16,
    adapter_name: Option<String>,
    /// A poll of the input stream hit a read error: the handle is gone and the
    /// next discovery pass drops it (UC-002 BR-006, #2314).
    read_failed: bool,
    /// A stream was registered since the last poll: the next poll drains the
    /// hidraw ring and forwards nothing, so the stream starts with fresh input
    /// instead of the reports queued while nobody read (#2372).
    flush_input: bool,
}

/// Run the device manager loop on the current thread (blocking).
///
/// This function does not return until a `Shutdown` command is received.
/// It owns all HidDevice handles and processes commands + polls devices.
/// Every loop pass beats `heartbeat`, which the watchdog reads (UC-002 A7).
pub fn run_device_manager(
    cmd_rx: mpsc::Receiver<DeviceCmd>,
    heartbeat: Heartbeat,
    outputs: DeviceOutputs,
) {
    let api = match HidApi::new() {
        Ok(api) => api,
        Err(e) => {
            warn!(error = %e, "Failed to initialize HID API for device manager");
            return;
        }
    };
    run_device_loop(api, cmd_rx, &heartbeat, &outputs);
}

/// Stop the device loop and wait, at most `timeout`, until every writer has sent
/// its LED-off / rumble-off (process stop, #2373). True when all writers ended in time.
pub fn graceful_stop(
    cmd_tx: &mpsc::Sender<DeviceCmd>,
    router: &OutputRouter,
    timeout: Duration,
) -> bool {
    if cmd_tx.send(DeviceCmd::Shutdown).is_err() {
        warn!("Device manager already stopped; no off command sent");
    }
    router.wait_writers_stopped(timeout)
}

/// The output side the device loop hands every opened device to.
#[derive(Debug, Clone, Default)]
pub struct DeviceOutputs {
    pub router: OutputRouter,
    pub rate: OutputRate,
}

impl DeviceOutputs {
    fn open<D: HidDeviceIo + Send + 'static>(&self, serial: &str, device: &Arc<Mutex<D>>) {
        self.router.open(serial, device.clone(), self.rate.clone());
    }
}

/// The device manager loop over any `HidBackend` (see `run_device_manager`).
fn run_device_loop<B: HidBackend>(
    mut api: B,
    cmd_rx: mpsc::Receiver<DeviceCmd>,
    heartbeat: &Heartbeat,
    outputs: &DeviceOutputs,
) {
    let mut devices: HashMap<String, DeviceEntry<B::Device>> = HashMap::new();
    let mut stream: Option<ActiveStream> = None;

    info!("Device manager thread started");

    loop {
        heartbeat.beat();

        // Process pending commands (non-blocking), at most MAX_CMDS_PER_PASS per
        // pass, so a flood of commands cannot starve the poll or the heartbeat
        for _ in 0..MAX_CMDS_PER_PASS {
            match cmd_rx.try_recv() {
                Ok(cmd) => {
                    if !handle_command(cmd, &mut api, &mut devices, &mut stream, outputs) {
                        info!("Device manager shutting down");
                        // Close all devices: each writer sends off and stops
                        handle_close_all(&mut devices, outputs);
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

        // Poll all open devices for input reports. A stream whose receiver is
        // gone (its gRPC call ended) is unregistered instead (#2371).
        if let Some(active) = &stream {
            let closed =
                active.sender.is_closed() || poll_all_devices(&mut devices, &active.sender);
            if closed {
                info!(
                    stream_id = active.id,
                    "Stream closed by its receiver, unregistered"
                );
                stream = None;
            }
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
    stream: &mut Option<ActiveStream>,
    outputs: &DeviceOutputs,
) -> bool {
    match cmd {
        // A closed reply channel means the caller already gave up (client
        // deadline or DEVICE_REPLY_TIMEOUT): skip the work nobody waits for (#2140).
        DeviceCmd::Discover { reply, .. } if reply.is_closed() => {
            debug!("Skipped Discover: caller gone");
        }
        DeviceCmd::Discover {
            force,
            verify_only,
            exclude_serials,
            reply,
        } => {
            let polling = stream.is_some();
            let serials = handle_discover(
                api,
                devices,
                DiscoverPass {
                    force,
                    verify_only,
                    polling,
                },
                &exclude_serials,
                outputs,
            );
            let _ = reply.send(serials);
        }
        DeviceCmd::Open { serial, reply } if reply.is_closed() => {
            debug!(serial = %serial, "Skipped Open: caller gone");
        }
        DeviceCmd::Open { serial, reply } => {
            let success = handle_open(api, devices, &serial, outputs);
            let _ = reply.send(success);
        }
        DeviceCmd::Close { serial } => {
            handle_close(devices, &serial, outputs);
        }
        DeviceCmd::CloseAll => {
            handle_close_all(devices, outputs);
        }
        DeviceCmd::RegisterStream { id, sender } => {
            let replaced = stream.as_ref().map(|old| old.id);
            info!(stream_id = id, replaced = ?replaced, "Stream registered");
            for entry in devices.values_mut() {
                entry.flush_input = true;
            }
            *stream = Some(ActiveStream { id, sender });
        }
        DeviceCmd::UnregisterStream { id } => match stream {
            Some(active) if active.id == id => {
                info!(stream_id = id, "Stream unregistered");
                *stream = None;
            }
            // A late unregister from a replaced stream must not drop the new one.
            _ => {
                debug!(
                    stream_id = id,
                    "Ignored unregister of a stream no longer registered"
                );
            }
        },
        DeviceCmd::Shutdown => {
            return false;
        }
    }
    true
}

/// One Discover request as the device loop sees it.
#[derive(Debug, Clone, Copy)]
struct DiscoverPass {
    /// Skip the read check of open devices (a periodic forced rescan).
    force: bool,
    /// Skip the enumeration: only drop the devices that are gone.
    verify_only: bool,
    /// The input stream is registered, so the device loop reads every open
    /// device each pass and records its read errors.
    polling: bool,
}

/// Discover controllers: drop the ones that are gone, enumerate, open new ones.
///
/// A pass never reopens an open controller and never reads from one the input
/// stream polls (UC-002 BR-006, #2314): an open device whose poll hit a read
/// error is dropped; without a stream (nothing polls, nothing consumes the
/// reports) a non-forced pass reads each device once to find the dead ones.
/// The enumeration then opens only serials that are not open yet.
///
/// `exclude_serials` lists controllers claimed by another adapter (e.g. hidapi).
/// Excluded serials are skipped during enumeration — the device handle is not
/// opened, avoiding the open→close churn that causes LED flashing.
fn handle_discover<B: HidBackend>(
    api: &mut B,
    devices: &mut HashMap<String, DeviceEntry<B::Device>>,
    pass: DiscoverPass,
    exclude_serials: &[String],
    outputs: &DeviceOutputs,
) -> Vec<DiscoveredControllerInfo> {
    let read_check = !pass.force && !pass.polling;
    let stale: Vec<String> = devices
        .iter()
        .filter_map(|(serial, entry)| {
            if entry.read_failed {
                return Some(serial.clone());
            }
            if !read_check {
                return None;
            }
            let mut buf = [0u8; 64];
            // Busy = its writer is in a write: the device is there.
            let Ok(device) = entry.device.try_lock() else {
                return None;
            };
            match device.read(&mut buf) {
                Ok(_) => None,                  // Data or no data, device is alive
                Err(_) => Some(serial.clone()), // Device gone
            }
        })
        .collect();
    for serial in stale {
        info!(serial = %serial, "Removing stale device");
        devices.remove(&serial);
        outputs.router.close(&serial);
    }

    if !pass.verify_only {
        if let Err(e) = api.refresh_devices() {
            warn!(error = %e, "Failed to refresh HID device list");
        }

        // Open only what is not open yet and not claimed by another adapter.
        let open_or_excluded = |serial: &str| {
            devices.contains_key(serial) || exclude_serials.iter().any(|s| s == serial)
        };
        let discovered = api.discover_controllers(&open_or_excluded);
        for (serial, device, product_id, adapter_name) in discovered {
            devices.entry(serial.clone()).or_insert_with(|| {
                info!(serial = %serial, "New controller discovered");
                let device = Arc::new(Mutex::new(device));
                outputs.open(&serial, &device);
                DeviceEntry {
                    device,
                    product_id,
                    adapter_name,
                    read_failed: false,
                    flush_input: false,
                }
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
    outputs: &DeviceOutputs,
) -> bool {
    if devices.contains_key(serial) {
        return true; // Already open
    }

    if let Err(e) = api.refresh_devices() {
        warn!(error = %e, "Failed to refresh HID device list");
    }

    match api.open_controller_by_serial(serial) {
        Some((device, product_id, adapter_name)) => {
            info!(serial = %serial, adapter = ?adapter_name, "Opened controller");
            let device = Arc::new(Mutex::new(device));
            outputs.open(serial, &device);
            devices.insert(
                serial.to_string(),
                DeviceEntry {
                    device,
                    product_id,
                    adapter_name,
                    read_failed: false,
                    flush_input: false,
                },
            );
            true
        }
        None => {
            warn!(serial = %serial, "Controller not found");
            false
        }
    }
}

/// Close a specific controller: its writer sends LED / rumble off, then stops.
fn handle_close<D: HidDeviceIo>(
    devices: &mut HashMap<String, DeviceEntry<D>>,
    serial: &str,
    outputs: &DeviceOutputs,
) {
    if devices.remove(serial).is_some() {
        outputs.router.close(serial);
        info!(serial = %serial, "Closed controller");
    }
}

/// Close all controllers.
fn handle_close_all<D: HidDeviceIo>(
    devices: &mut HashMap<String, DeviceEntry<D>>,
    outputs: &DeviceOutputs,
) {
    for (serial, _) in devices.drain() {
        outputs.router.close(&serial);
        debug!(serial = %serial, "Closed controller (close_all)");
    }
    info!("All controllers closed");
}

/// Poll all open devices and offer their newest report to the stream's
/// latest-sample slots (#2372). Returns true when the stream's receiver is
/// gone (the pass stops there, #2371).
fn poll_all_devices<D: HidDeviceIo>(
    devices: &mut HashMap<String, DeviceEntry<D>>,
    sender: &InputSink,
) -> bool {
    for (serial, entry) in devices.iter_mut() {
        let zcm2 = hid::is_zcm2(entry.product_id);
        let device = match entry.device.try_lock() {
            Ok(device) => device,
            // Its writer is in a write (maybe blocked): read it on the next pass.
            Err(TryLockError::WouldBlock) => continue,
            Err(TryLockError::Poisoned(e)) => e.into_inner(),
        };
        let drain = hid::read_latest_input(&*device, zcm2);
        drop(device);
        if drain.read_failed && !entry.read_failed {
            info!(serial = %serial, "Read failed, dropping the controller on the next discovery");
            entry.read_failed = true;
        }
        if entry.flush_input {
            // Reports queued before the stream registered: stale, not forwarded.
            entry.flush_input = false;
            continue;
        }
        if let Some(report) = drain.report {
            // Never blocks: a sample the stream has not taken yet is replaced,
            // keeping its button bits (UC-010 BR-012).
            if sender.offer(serial, report).is_err() {
                return true;
            }
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;
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
        /// Every read fails: the controller is gone (#2314).
        read_fails: bool,
    }

    /// In-memory HID device shared between the test and the device loop thread.
    #[derive(Clone, Default)]
    struct FakeDevice(Arc<Mutex<FakeDeviceState>>);

    impl HidDeviceIo for FakeDevice {
        fn read(&self, buf: &mut [u8]) -> hidapi::HidResult<usize> {
            let mut state = self.0.lock().unwrap();
            if state.read_fails {
                return Err(hidapi::HidError::HidApiError {
                    message: "No such device".to_string(),
                });
            }
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

        fn discover_controllers(
            &self,
            skip: &dyn Fn(&str) -> bool,
        ) -> Vec<(String, FakeDevice, u16, Option<String>)> {
            if skip(SERIAL) {
                return vec![];
            }
            vec![(
                SERIAL.to_string(),
                self.device.clone(),
                PRODUCT_ID_ZCM2,
                Some("hci0".to_string()),
            )]
        }

        fn open_controller_by_serial(
            &self,
            serial: &str,
        ) -> Option<(FakeDevice, u16, Option<String>)> {
            (serial == SERIAL).then(|| {
                (
                    self.device.clone(),
                    PRODUCT_ID_ZCM2,
                    Some("hci0".to_string()),
                )
            })
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
        let backend = FakeBackend {
            device: device.clone(),
        };
        let handle = std::thread::spawn(move || {
            run_device_loop(backend, cmd_rx, &heartbeat, &DeviceOutputs::default())
        });
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
        rx: &mut crate::input_slot::InputReceiver,
        timeout: Duration,
    ) -> Option<crate::input_slot::StreamSensorData> {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if let Some(data) = rx.try_recv() {
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

        let (sensor_tx, mut sensor_rx) = crate::input_slot::channel();
        cmd_tx
            .send(DeviceCmd::RegisterStream {
                id: next_stream_id(),
                sender: sensor_tx,
            })
            .unwrap();
        // The register flushes the ring first (#2372): feed after it.
        assert!(loop_polls(&device), "the registered stream was not polled");
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

    fn open_device(cmd_tx: &mpsc::Sender<DeviceCmd>) {
        let (reply_tx, reply_rx) = tokio::sync::oneshot::channel();
        cmd_tx
            .send(DeviceCmd::Open {
                serial: SERIAL.to_string(),
                reply: reply_tx,
            })
            .unwrap();
        assert!(reply_rx.blocking_recv().unwrap());
    }

    fn register(cmd_tx: &mpsc::Sender<DeviceCmd>) -> (u64, crate::input_slot::InputReceiver) {
        let (sender, rx) = crate::input_slot::channel();
        let id = next_stream_id();
        cmd_tx
            .send(DeviceCmd::RegisterStream { id, sender })
            .unwrap();
        (id, rx)
    }

    /// True when the loop polls the device: its empty-read count grows.
    fn loop_polls(device: &FakeDevice) -> bool {
        let before = device.0.lock().unwrap().drained_at.len();
        std::thread::sleep(Duration::from_millis(100));
        device.0.lock().unwrap().drained_at.len() > before
    }

    // UC: UC-002 A7 step 2
    #[test]
    fn test_a_late_unregister_of_an_old_stream_keeps_the_new_one() {
        let device = FakeDevice::default();
        let (cmd_tx, handle) = spawn_loop(&device);
        open_device(&cmd_tx);

        // The client reopened its stream; the old call's end lands last (#2371).
        let (old_id, _old_rx) = register(&cmd_tx);
        let (new_id, mut new_rx) = register(&cmd_tx);
        cmd_tx
            .send(DeviceCmd::UnregisterStream { id: old_id })
            .unwrap();
        assert!(loop_polls(&device), "the new stream was not polled");
        device.0.lock().unwrap().pending.push_back(sample_report());

        let data = recv_within(&mut new_rx, Duration::from_secs(2));
        assert!(
            data.is_some(),
            "a late unregister of the old stream dropped the new one"
        );

        // The new stream's own unregister still removes it: the poll stops.
        cmd_tx
            .send(DeviceCmd::UnregisterStream { id: new_id })
            .unwrap();
        std::thread::sleep(Duration::from_millis(50));
        let polling = loop_polls(&device);
        shutdown(&cmd_tx, handle);
        assert!(
            !polling,
            "the stream stayed registered after its unregister"
        );
    }

    // UC: UC-002 A7 step 2
    #[test]
    fn test_a_closed_stream_is_unregistered_and_a_new_one_streams() {
        let device = FakeDevice::default();
        let (cmd_tx, handle) = spawn_loop(&device);
        open_device(&cmd_tx);
        let (_id, old_rx) = register(&cmd_tx);
        assert!(loop_polls(&device), "the registered stream was not polled");

        // The gRPC call ended without an unregister: its receiver is gone.
        drop(old_rx);
        std::thread::sleep(Duration::from_millis(50));
        assert!(
            !loop_polls(&device),
            "the loop kept polling for a closed stream"
        );

        let (_id, mut new_rx) = register(&cmd_tx);
        assert!(loop_polls(&device), "the new stream was not polled");
        device.0.lock().unwrap().pending.push_back(sample_report());
        let data = recv_within(&mut new_rx, Duration::from_secs(2));
        shutdown(&cmd_tx, handle);
        assert!(
            data.is_some(),
            "a new stream after a closed one got no data"
        );
    }

    /// A ZCM2 input report with these buttons and this raw accel z.
    fn report_with(buttons: u8, accel_z: i16) -> [u8; hid::INPUT_REPORT_SIZE] {
        let mut data = [0u8; hid::INPUT_REPORT_SIZE];
        data[0] = 0x01;
        data[2] = buttons;
        data[23..25].copy_from_slice(&accel_z.to_le_bytes());
        data
    }

    fn accel_z(raw: i16) -> f32 {
        hid::parse_input_report(&report_with(0, raw), true)
            .unwrap()
            .accel_z
    }

    /// Queue `report` and wait until the loop has read it and found the
    /// device empty again (one full drain).
    fn feed_and_wait_drained(device: &FakeDevice, report: [u8; hid::INPUT_REPORT_SIZE]) {
        let before = {
            let mut state = device.0.lock().unwrap();
            state.pending.push_back(report);
            state.drained_at.len()
        };
        let deadline = Instant::now() + Duration::from_secs(2);
        while device.0.lock().unwrap().drained_at.len() <= before {
            assert!(
                Instant::now() < deadline,
                "the loop did not drain the device"
            );
            std::thread::sleep(Duration::from_millis(1));
        }
    }

    // UC: UC-002 main step 2
    #[test]
    fn test_a_lagging_reader_gets_the_newest_sample() {
        let device = FakeDevice::default();
        let (cmd_tx, handle) = spawn_loop(&device);
        open_device(&cmd_tx);
        let (_id, mut rx) = register(&cmd_tx);
        assert!(loop_polls(&device), "the registered stream was not polled");

        // The reader takes nothing while two polls deliver a sample each (#2372).
        feed_and_wait_drained(&device, report_with(0, 100));
        feed_and_wait_drained(&device, report_with(0, 200));

        let first = recv_within(&mut rx, Duration::from_secs(2));
        let second = rx.try_recv();
        shutdown(&cmd_tx, handle);
        let first = first.expect("no sample streamed");
        assert_eq!(
            first.report.accel_z,
            accel_z(200),
            "a lagging reader got an old sample, not the newest"
        );
        assert!(second.is_none(), "an older sample was still queued");
    }

    // UC: UC-010 BR-012
    #[test]
    fn test_a_press_in_a_replaced_sample_is_still_delivered() {
        let device = FakeDevice::default();
        let (cmd_tx, handle) = spawn_loop(&device);
        open_device(&cmd_tx);
        let (_id, mut rx) = register(&cmd_tx);
        assert!(loop_polls(&device), "the registered stream was not polled");

        // A tap whose press and release both land before the reader reads.
        feed_and_wait_drained(&device, report_with(hid::Button::CROSS as u8, 100));
        feed_and_wait_drained(&device, report_with(0, 200));

        let data = recv_within(&mut rx, Duration::from_secs(2));
        shutdown(&cmd_tx, handle);
        let data = data.expect("no sample streamed");
        assert_eq!(data.report.accel_z, accel_z(200), "not the newest sample");
        assert!(
            data.report.button_cross(),
            "the press in the replaced sample was lost"
        );
    }

    // UC: UC-002 A7 step 2
    #[test]
    fn test_the_first_sample_after_register_is_fresh() {
        let device = FakeDevice::default();
        let (cmd_tx, handle) = spawn_loop(&device);
        open_device(&cmd_tx);
        // Reports the hidraw ring kept while no stream was registered,
        // including an old press.
        {
            let mut state = device.0.lock().unwrap();
            state
                .pending
                .push_back(report_with(hid::Button::CROSS as u8, 100));
            state.pending.push_back(report_with(0, 150));
        }

        let (_id, mut rx) = register(&cmd_tx);
        let deadline = Instant::now() + Duration::from_secs(2);
        while !device.0.lock().unwrap().pending.is_empty() {
            assert!(Instant::now() < deadline, "the ring was never read");
            std::thread::sleep(Duration::from_millis(1));
        }
        // The ring is empty after the last pop, but the flush poll still has
        // its final empty read to do; a report fed now would be discarded.
        let polls_before = device.0.lock().unwrap().drained_at.len();
        let deadline = Instant::now() + Duration::from_secs(2);
        while device.0.lock().unwrap().drained_at.len() <= polls_before {
            assert!(Instant::now() < deadline, "the flush never finished");
            std::thread::sleep(Duration::from_millis(1));
        }
        feed_and_wait_drained(&device, report_with(0, 200));

        let first = recv_within(&mut rx, Duration::from_secs(2));
        shutdown(&cmd_tx, handle);
        let first = first.expect("no sample streamed after register");
        assert_eq!(
            first.report.accel_z,
            accel_z(200),
            "the first sample after register was a stale ring report"
        );
        assert!(
            !first.report.button_cross(),
            "a stale press from before the register was delivered"
        );
    }

    // UC: UC-002 main step 3
    #[test]
    fn test_on_demand_open_keeps_the_adapter_name() {
        let device = FakeDevice::default();
        let (cmd_tx, handle) = spawn_loop(&device);

        let (open_tx, open_rx) = tokio::sync::oneshot::channel();
        cmd_tx
            .send(DeviceCmd::Open {
                serial: SERIAL.to_string(),
                reply: open_tx,
            })
            .unwrap();
        assert!(open_rx.blocking_recv().unwrap());

        // A verify-only pass lists the open devices without enumerating again.
        let (reply_tx, reply_rx) = tokio::sync::oneshot::channel();
        cmd_tx
            .send(DeviceCmd::Discover {
                force: false,
                verify_only: true,
                exclude_serials: vec![],
                reply: reply_tx,
            })
            .unwrap();
        let listed = reply_rx.blocking_recv().unwrap();
        shutdown(&cmd_tx, handle);

        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].serial, SERIAL);
        assert_eq!(
            listed[0].adapter_name, "hci0",
            "a controller opened on demand lost its adapter name (#2200)"
        );
    }

    // UC: UC-030 main step 2
    // NFR: NFR-001
    #[test]
    fn test_poll_loop_polls_open_device_about_every_millisecond() {
        let device = FakeDevice::default();
        let (cmd_tx, handle) = spawn_loop(&device);

        let (reply_tx, reply_rx) = tokio::sync::oneshot::channel();
        cmd_tx
            .send(DeviceCmd::Open {
                serial: SERIAL.to_string(),
                reply: reply_tx,
            })
            .unwrap();
        assert!(reply_rx.blocking_recv().unwrap());
        let (sensor_tx, _sensor_rx) = crate::input_slot::channel();
        cmd_tx
            .send(DeviceCmd::RegisterStream {
                id: next_stream_id(),
                sender: sensor_tx,
            })
            .unwrap();

        std::thread::sleep(Duration::from_millis(300));
        shutdown(&cmd_tx, handle);

        let polls = std::mem::take(&mut device.0.lock().unwrap().drained_at);
        let mut intervals: Vec<Duration> = polls.windows(2).map(|w| w[1] - w[0]).collect();
        assert!(
            intervals.len() >= 50,
            "only {} poll intervals in 300 ms",
            intervals.len()
        );
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
        assert!(
            min >= POLL_INTERVAL,
            "min poll interval {min:?} < 1 ms: busy-waiting"
        );
        assert!(
            median <= Duration::from_millis(2),
            "median poll interval {median:?} > 2 ms"
        );
        assert!(
            p90 <= Duration::from_millis(5),
            "p90 poll interval {p90:?} > 5 ms"
        );
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

    /// Backend whose opens of an unknown serial are recorded with the time and
    /// the heartbeat age the device loop left at that moment.
    struct FloodBackend {
        inner: FakeBackend,
        heartbeat: Heartbeat,
        opens: Arc<Mutex<Vec<(Instant, Duration)>>>,
    }

    impl HidBackend for FloodBackend {
        type Device = FakeDevice;

        fn refresh_devices(&mut self) -> hidapi::HidResult<()> {
            Ok(())
        }

        fn discover_controllers(
            &self,
            skip: &dyn Fn(&str) -> bool,
        ) -> Vec<(String, FakeDevice, u16, Option<String>)> {
            self.inner.discover_controllers(skip)
        }

        fn open_controller_by_serial(
            &self,
            serial: &str,
        ) -> Option<(FakeDevice, u16, Option<String>)> {
            if serial != SERIAL {
                self.opens
                    .lock()
                    .unwrap()
                    .push((Instant::now(), self.heartbeat.age()));
                std::thread::sleep(Duration::from_micros(200));
            }
            self.inner.open_controller_by_serial(serial)
        }
    }

    // UC: UC-002 BR-005
    // NFR: NFR-055
    #[test]
    fn test_a_command_flood_still_lets_the_loop_poll_and_beat() {
        const FLOOD: usize = 4 * MAX_CMDS_PER_PASS;
        let device = FakeDevice::default();
        let heartbeat = Heartbeat::new();
        let opens = Arc::new(Mutex::new(Vec::new()));
        let backend = FloodBackend {
            inner: FakeBackend {
                device: device.clone(),
            },
            heartbeat: heartbeat.clone(),
            opens: opens.clone(),
        };

        // Everything is queued before the loop starts, so its first pass sees the whole flood.
        let (cmd_tx, cmd_rx) = mpsc::channel();
        let (open_tx, open_rx) = tokio::sync::oneshot::channel();
        cmd_tx
            .send(DeviceCmd::Open {
                serial: SERIAL.to_string(),
                reply: open_tx,
            })
            .unwrap();
        let (sensor_tx, _sensor_rx) = crate::input_slot::channel();
        cmd_tx
            .send(DeviceCmd::RegisterStream {
                id: next_stream_id(),
                sender: sensor_tx,
            })
            .unwrap();
        let mut replies = Vec::new();
        for i in 0..FLOOD {
            let (tx, rx) = tokio::sync::oneshot::channel();
            cmd_tx
                .send(DeviceCmd::Open {
                    serial: format!("unknown-{i}"),
                    reply: tx,
                })
                .unwrap();
            replies.push(rx);
        }
        let handle = std::thread::spawn(move || {
            run_device_loop(backend, cmd_rx, &heartbeat, &DeviceOutputs::default())
        });
        assert!(open_rx.blocking_recv().unwrap());
        for rx in replies {
            assert!(!rx.blocking_recv().unwrap());
        }
        shutdown(&cmd_tx, handle);

        let opens = opens.lock().unwrap().clone();
        assert_eq!(opens.len(), FLOOD);
        let (first, last) = (opens[0].0, opens[FLOOD - 1].0);
        let polls_inside = device
            .0
            .lock()
            .unwrap()
            .drained_at
            .iter()
            .filter(|t| **t > first && **t < last)
            .count();
        assert!(
            polls_inside >= 3,
            "{polls_inside} device polls during a {FLOOD}-command flood: input starved"
        );
        // The heartbeat age only drops when a new loop pass beats it.
        let beats_inside = opens.windows(2).filter(|w| w[1].1 < w[0].1).count();
        assert!(
            beats_inside >= 3,
            "{beats_inside} heartbeats during a {FLOOD}-command flood: the watchdog would trip"
        );
    }

    // --- load: 9 controllers, one with a blocking write (#2125) -------------

    /// A controller that reports at 100 Hz and counts its writes; `block` makes
    /// every write hang until `release`.
    #[derive(Default)]
    struct LoadState {
        last_report: Mutex<Option<Instant>>,
        writes: std::sync::atomic::AtomicUsize,
        block: std::sync::atomic::AtomicBool,
        release: std::sync::atomic::AtomicBool,
    }

    #[derive(Clone, Default)]
    struct LoadDevice(Arc<LoadState>);

    impl HidDeviceIo for LoadDevice {
        fn read(&self, buf: &mut [u8]) -> hidapi::HidResult<usize> {
            let mut last = self.0.last_report.lock().unwrap();
            if last.is_some_and(|t| t.elapsed() < Duration::from_millis(10)) {
                return Ok(0);
            }
            *last = Some(Instant::now());
            let report = sample_report();
            buf[..report.len()].copy_from_slice(&report);
            Ok(report.len())
        }

        fn write(&self, data: &[u8]) -> hidapi::HidResult<usize> {
            use std::sync::atomic::Ordering::Relaxed;
            if self.0.block.load(Relaxed) {
                while !self.0.release.load(Relaxed) {
                    std::thread::sleep(Duration::from_millis(1));
                }
            }
            self.0.writes.fetch_add(1, Relaxed);
            Ok(data.len())
        }
    }

    struct LoadBackend {
        devices: Vec<(String, LoadDevice)>,
    }

    impl HidBackend for LoadBackend {
        type Device = LoadDevice;

        fn refresh_devices(&mut self) -> hidapi::HidResult<()> {
            Ok(())
        }

        fn discover_controllers(
            &self,
            skip: &dyn Fn(&str) -> bool,
        ) -> Vec<(String, LoadDevice, u16, Option<String>)> {
            self.devices
                .iter()
                .filter(|(s, _)| !skip(s))
                .map(|(s, d)| {
                    (
                        s.clone(),
                        d.clone(),
                        PRODUCT_ID_ZCM2,
                        Some("hci0".to_string()),
                    )
                })
                .collect()
        }

        fn open_controller_by_serial(
            &self,
            _serial: &str,
        ) -> Option<(LoadDevice, u16, Option<String>)> {
            None
        }
    }

    // UC: UC-002 BR-005
    // NFR: NFR-055
    #[test]
    fn test_nine_controllers_with_a_blocked_write_keep_grpc_and_input_alive() {
        use crate::controller_io_service::ControllerIoServiceImpl;
        use crate::service::proto::controller_io_service_server::ControllerIoServiceServer;
        use std::sync::atomic::Ordering::Relaxed;

        const RATE_HZ: u32 = 20;
        const RUN: Duration = Duration::from_secs(2);
        let devices: Vec<(String, LoadDevice)> = (0..9)
            .map(|i| (format!("00:06:f7:00:00:{i:02x}"), LoadDevice::default()))
            .collect();
        let stuck = devices[0].1.clone();
        stuck.0.block.store(true, Relaxed);

        let outputs = DeviceOutputs {
            router: OutputRouter::default(),
            rate: OutputRate::new(RATE_HZ),
        };
        let heartbeat = Heartbeat::new();
        let (cmd_tx, cmd_rx) = mpsc::channel();
        let handle = {
            let backend = LoadBackend {
                devices: devices.clone(),
            };
            let (outputs, heartbeat) = (outputs.clone(), heartbeat.clone());
            std::thread::spawn(move || run_device_loop(backend, cmd_rx, &heartbeat, &outputs))
        };
        let (reply_tx, reply_rx) = tokio::sync::oneshot::channel();
        cmd_tx
            .send(DeviceCmd::Discover {
                force: false,
                verify_only: false,
                exclude_serials: vec![],
                reply: reply_tx,
            })
            .unwrap();
        assert_eq!(reply_rx.blocking_recv().unwrap().len(), 9);

        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();
        let (health_latencies, samples) = rt.block_on(async {
            // The production server shape: health + ControllerIOService.
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let addr = listener.local_addr().unwrap().to_string();
            let (_reporter, health) = tonic_health::server::health_reporter();
            let io = ControllerIoServiceImpl::new(cmd_tx.clone(), outputs.router.clone());
            tokio::spawn(
                tonic::transport::Server::builder()
                    .add_service(health)
                    .add_service(ControllerIoServiceServer::new(io))
                    .serve_with_incoming(tonic::transport::server::TcpIncoming::from(listener)),
            );

            // Sensor stream consumer: samples per serial.
            let (sensor_tx, mut sensor_rx) = crate::input_slot::channel();
            cmd_tx
                .send(DeviceCmd::RegisterStream {
                    id: next_stream_id(),
                    sender: sensor_tx,
                })
                .unwrap();
            let counter = tokio::spawn(async move {
                let mut per_serial: HashMap<String, usize> = HashMap::new();
                let deadline = tokio::time::Instant::now() + RUN;
                while let Ok(Some(d)) =
                    tokio::time::timeout_at(deadline, tokio_stream::StreamExt::next(&mut sensor_rx))
                        .await
                {
                    *per_serial.entry(d.serial).or_default() += 1;
                }
                per_serial
            });

            // 100 Hz of LED changes to every controller (lobby repaint + pulses).
            let router = outputs.router.clone();
            let serials: Vec<String> = devices.iter().map(|(s, _)| s.clone()).collect();
            let feeder = std::thread::spawn(move || {
                let start = Instant::now();
                let mut i = 0u32;
                while start.elapsed() < RUN {
                    for s in &serials {
                        router.submit(
                            s,
                            (i % 256) as u8,
                            0,
                            0,
                            if i.is_multiple_of(20) { 255 } else { 0 },
                        );
                    }
                    i += 1;
                    std::thread::sleep(Duration::from_millis(10));
                }
            });

            let mut latencies = Vec::new();
            let start = Instant::now();
            while start.elapsed() < RUN {
                let t = Instant::now();
                assert!(
                    crate::watchdog::probe_health(&addr, Duration::from_secs(1)).await,
                    "health probe failed under load"
                );
                latencies.push(t.elapsed());
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
            feeder.join().unwrap();
            (latencies, counter.await.unwrap())
        });

        let heartbeat_age = heartbeat.age();
        stuck.0.release.store(true, Relaxed);
        cmd_tx.send(DeviceCmd::Shutdown).unwrap();
        handle.join().unwrap();

        let max_health = health_latencies.iter().max().copied().unwrap();
        println!(
            "load: health probes n={} max={max_health:?}; samples per serial {:?}",
            health_latencies.len(),
            samples.values().collect::<Vec<_>>()
        );
        assert!(
            max_health < Duration::from_millis(500),
            "health answered in {max_health:?}"
        );
        assert!(
            heartbeat_age < Duration::from_millis(100),
            "device loop stalled: {heartbeat_age:?}"
        );
        let max_writes = (RUN.as_secs_f64() * f64::from(RATE_HZ)).ceil() as usize + 3;
        for (serial, device) in &devices[1..] {
            let writes = device.0.writes.load(Relaxed);
            assert!(
                (10..=max_writes).contains(&writes),
                "{serial}: {writes} writes in {RUN:?} at a {RATE_HZ} Hz cap"
            );
            let n = samples.get(serial).copied().unwrap_or(0);
            assert!(n >= 50, "{serial}: only {n} sensor samples in {RUN:?}");
        }
    }

    /// Backend that counts every enumeration and open (#2140), and every
    /// device a discovery pass opened (#2314).
    #[derive(Default)]
    struct CountingBackend {
        device: FakeDevice,
        refreshes: usize,
        opens: std::cell::Cell<usize>,
        discover_opens: std::cell::Cell<usize>,
    }

    impl HidBackend for CountingBackend {
        type Device = FakeDevice;

        fn refresh_devices(&mut self) -> hidapi::HidResult<()> {
            self.refreshes += 1;
            Ok(())
        }

        fn discover_controllers(
            &self,
            skip: &dyn Fn(&str) -> bool,
        ) -> Vec<(String, FakeDevice, u16, Option<String>)> {
            if skip(SERIAL) {
                return vec![];
            }
            self.discover_opens.set(self.discover_opens.get() + 1);
            vec![(
                SERIAL.to_string(),
                self.device.clone(),
                PRODUCT_ID_ZCM2,
                None,
            )]
        }

        fn open_controller_by_serial(
            &self,
            serial: &str,
        ) -> Option<(FakeDevice, u16, Option<String>)> {
            self.opens.set(self.opens.get() + 1);
            (serial == SERIAL).then(|| (self.device.clone(), PRODUCT_ID_ZCM2, None))
        }
    }

    fn handle(
        cmd: DeviceCmd,
        api: &mut CountingBackend,
    ) -> HashMap<String, DeviceEntry<FakeDevice>> {
        let mut devices = HashMap::new();
        let mut stream = None;
        assert!(handle_command(
            cmd,
            api,
            &mut devices,
            &mut stream,
            &DeviceOutputs::default()
        ));
        devices
    }

    fn discover_cmd(
        reply: tokio::sync::oneshot::Sender<Vec<DiscoveredControllerInfo>>,
    ) -> DeviceCmd {
        DeviceCmd::Discover {
            force: true,
            verify_only: false,
            exclude_serials: vec![],
            reply,
        }
    }

    // UC: UC-002 BR-005
    // NFR: NFR-055
    #[test]
    fn test_abandoned_discover_is_skipped() {
        let mut api = CountingBackend::default();
        let (reply_tx, reply_rx) = tokio::sync::oneshot::channel();
        drop(reply_rx); // the caller gave up (deadline passed) before the loop got to it

        let devices = handle(discover_cmd(reply_tx), &mut api);

        assert_eq!(api.refreshes, 0, "an abandoned Discover still enumerated");
        assert!(
            devices.is_empty(),
            "an abandoned Discover still opened a device"
        );
    }

    // UC: UC-002 BR-005
    // NFR: NFR-055
    #[test]
    fn test_abandoned_open_is_skipped() {
        let mut api = CountingBackend::default();
        let (reply_tx, reply_rx) = tokio::sync::oneshot::channel();
        drop(reply_rx);

        let devices = handle(
            DeviceCmd::Open {
                serial: SERIAL.to_string(),
                reply: reply_tx,
            },
            &mut api,
        );

        assert_eq!(
            api.opens.get(),
            0,
            "an abandoned Open still opened the device"
        );
        assert!(devices.is_empty());
    }

    // UC: UC-002 BR-005
    // NFR: NFR-055
    #[test]
    fn test_abandoned_check_keeps_live_discover_and_open() {
        let mut api = CountingBackend::default();
        let (reply_tx, mut reply_rx) = tokio::sync::oneshot::channel();
        let devices = handle(discover_cmd(reply_tx), &mut api);
        assert_eq!(api.refreshes, 1);
        assert_eq!(reply_rx.try_recv().unwrap().len(), 1);
        assert!(devices.contains_key(SERIAL));

        let (reply_tx, mut reply_rx) = tokio::sync::oneshot::channel();
        let devices = handle(
            DeviceCmd::Open {
                serial: SERIAL.to_string(),
                reply: reply_tx,
            },
            &mut api,
        );
        assert_eq!(api.opens.get(), 1);
        assert!(reply_rx.try_recv().unwrap());
        assert!(devices.contains_key(SERIAL));
    }

    /// One Discover pass of the 1 Hz discovery check (controller manager:
    /// full enumeration, not forced) on `devices`.
    fn discover_pass(
        api: &mut CountingBackend,
        devices: &mut HashMap<String, DeviceEntry<FakeDevice>>,
        verify_only: bool,
        polling: bool,
    ) -> Vec<DiscoveredControllerInfo> {
        handle_discover(
            api,
            devices,
            DiscoverPass {
                force: false,
                verify_only,
                polling,
            },
            &[],
            &DeviceOutputs::default(),
        )
    }

    // UC: UC-002 BR-006
    #[test]
    fn test_full_discovery_does_not_reopen_an_open_controller() {
        let mut api = CountingBackend::default();
        let mut devices = HashMap::new();

        for _ in 0..3 {
            let found = discover_pass(&mut api, &mut devices, false, true);
            assert_eq!(found.len(), 1);
        }

        assert_eq!(
            api.refreshes, 3,
            "each full pass still enumerates (hot-plug)"
        );
        assert_eq!(
            api.discover_opens.get(),
            1,
            "a full discovery pass reopened an open controller"
        );
    }

    // UC: UC-002 BR-006
    #[test]
    fn test_discovery_does_not_open_an_excluded_controller() {
        let mut api = CountingBackend::default();
        let mut devices = HashMap::new();

        let found = handle_discover(
            &mut api,
            &mut devices,
            DiscoverPass {
                force: false,
                verify_only: false,
                polling: true,
            },
            &[SERIAL.to_string()],
            &DeviceOutputs::default(),
        );

        assert!(found.is_empty());
        assert_eq!(
            api.discover_opens.get(),
            0,
            "an excluded controller was opened and dropped"
        );
    }

    // UC: UC-002 BR-006
    #[test]
    fn test_discovery_does_not_read_a_polled_controller() {
        let mut api = CountingBackend::default();
        let mut devices = HashMap::new();
        discover_pass(&mut api, &mut devices, false, true);
        api.device
            .0
            .lock()
            .unwrap()
            .pending
            .push_back(sample_report());

        // Verify-only and full passes, the input stream registered.
        discover_pass(&mut api, &mut devices, true, true);
        discover_pass(&mut api, &mut devices, false, true);

        assert_eq!(
            api.device.0.lock().unwrap().pending.len(),
            1,
            "a discovery pass consumed an input report the stream needs"
        );
        assert!(devices.contains_key(SERIAL));
    }

    // UC: UC-002 BR-006
    #[test]
    fn test_a_read_error_drops_the_controller_on_the_next_pass() {
        let mut api = CountingBackend::default();
        let mut devices = HashMap::new();
        discover_pass(&mut api, &mut devices, false, true);

        // The poll hit a read error: the handle is gone.
        devices.get_mut(SERIAL).unwrap().read_failed = true;
        let found = discover_pass(&mut api, &mut devices, true, true);
        assert!(found.is_empty(), "a controller with a failed read stayed");
        assert!(!devices.contains_key(SERIAL));

        // Still attached (it reconnected): the next full pass opens it again.
        let found = discover_pass(&mut api, &mut devices, false, true);
        assert_eq!(found.len(), 1);
        assert_eq!(api.discover_opens.get(), 2);
        assert!(!devices[SERIAL].read_failed);
    }

    // UC: UC-002 BR-006
    #[test]
    fn test_a_read_error_drop_and_reopen_happen_in_one_pass() {
        let mut api = CountingBackend::default();
        let mut devices = HashMap::new();
        discover_pass(&mut api, &mut devices, false, true);
        devices.get_mut(SERIAL).unwrap().read_failed = true;

        // The drop runs before the enumeration, so a reconnected controller
        // is reopened in the same pass instead of vanishing for one pass.
        let found = discover_pass(&mut api, &mut devices, false, true);

        assert_eq!(
            found.len(),
            1,
            "the reconnected controller was not reopened"
        );
        assert_eq!(api.discover_opens.get(), 2);
        assert!(!devices[SERIAL].read_failed);
    }

    /// A non-forced Discover through `handle_command`, which derives the
    /// pass's `polling` from the registered stream.
    fn discover_via_command(
        api: &mut CountingBackend,
        devices: &mut HashMap<String, DeviceEntry<FakeDevice>>,
        stream: &mut Option<ActiveStream>,
        verify_only: bool,
    ) -> Vec<DiscoveredControllerInfo> {
        let (reply, mut reply_rx) = tokio::sync::oneshot::channel();
        assert!(handle_command(
            DeviceCmd::Discover {
                force: false,
                verify_only,
                exclude_serials: vec![],
                reply,
            },
            api,
            devices,
            stream,
            &DeviceOutputs::default(),
        ));
        reply_rx.try_recv().unwrap()
    }

    // UC: UC-002 BR-006
    #[test]
    fn test_discover_command_does_not_read_while_the_stream_is_registered() {
        let mut api = CountingBackend::default();
        let mut devices = HashMap::new();
        let mut stream = None;
        let outputs = DeviceOutputs::default();
        let (sender, _stream_rx) = crate::input_slot::channel();
        assert!(handle_command(
            DeviceCmd::RegisterStream {
                id: next_stream_id(),
                sender,
            },
            &mut api,
            &mut devices,
            &mut stream,
            &outputs,
        ));
        discover_via_command(&mut api, &mut devices, &mut stream, false);
        api.device
            .0
            .lock()
            .unwrap()
            .pending
            .push_back(sample_report());

        discover_via_command(&mut api, &mut devices, &mut stream, true);
        discover_via_command(&mut api, &mut devices, &mut stream, false);

        assert_eq!(
            api.device.0.lock().unwrap().pending.len(),
            1,
            "a Discover consumed an input report the stream needs"
        );
        assert!(devices.contains_key(SERIAL));
    }

    // UC: UC-002 BR-006
    #[test]
    fn test_discover_command_without_a_stream_drops_a_dead_controller() {
        let mut api = CountingBackend::default();
        let mut devices = HashMap::new();
        let mut stream = None;
        discover_via_command(&mut api, &mut devices, &mut stream, false);
        assert!(devices.contains_key(SERIAL));
        api.device.0.lock().unwrap().read_fails = true;

        // Nothing polls without a stream: the Discover's read check finds it.
        let found = discover_via_command(&mut api, &mut devices, &mut stream, true);

        assert!(
            found.is_empty(),
            "a dead controller stayed without a stream"
        );
        assert!(!devices.contains_key(SERIAL));
    }

    // UC: UC-002 BR-006
    #[test]
    fn test_the_poll_marks_a_failed_read() {
        struct GoneDevice;
        impl HidDeviceIo for GoneDevice {
            fn read(&self, _buf: &mut [u8]) -> hidapi::HidResult<usize> {
                Err(hidapi::HidError::HidApiError {
                    message: "No such device".to_string(),
                })
            }
            fn write(&self, _data: &[u8]) -> hidapi::HidResult<usize> {
                Ok(0)
            }
        }
        let mut devices = HashMap::from([(
            SERIAL.to_string(),
            DeviceEntry {
                device: Arc::new(Mutex::new(GoneDevice)),
                product_id: PRODUCT_ID_ZCM2,
                adapter_name: None,
                read_failed: false,
                flush_input: false,
            },
        )]);
        let (sender, _rx) = crate::input_slot::channel();

        poll_all_devices(&mut devices, &sender);

        assert!(devices[SERIAL].read_failed);
    }

    /// Backend whose one controller records its writes.
    struct RecordingBackend(crate::output::tests::RecordingDevice);

    impl HidBackend for RecordingBackend {
        type Device = crate::output::tests::RecordingDevice;

        fn refresh_devices(&mut self) -> hidapi::HidResult<()> {
            Ok(())
        }

        fn discover_controllers(
            &self,
            skip: &dyn Fn(&str) -> bool,
        ) -> Vec<(String, Self::Device, u16, Option<String>)> {
            if skip(SERIAL) {
                return vec![];
            }
            vec![(SERIAL.to_string(), self.0.clone(), PRODUCT_ID_ZCM2, None)]
        }

        fn open_controller_by_serial(
            &self,
            _serial: &str,
        ) -> Option<(Self::Device, u16, Option<String>)> {
            None
        }
    }

    // UC: UC-002 A7
    #[test]
    fn test_graceful_stop_sends_led_and_rumble_off_to_every_controller() {
        use crate::output::tests::{RecordingDevice, WriteMode};
        let device = RecordingDevice::new(WriteMode::Ok);
        let outputs = DeviceOutputs::default();
        let (cmd_tx, cmd_rx) = mpsc::channel();
        let backend = RecordingBackend(device.clone());
        let loop_outputs = outputs.clone();
        let handle = std::thread::spawn(move || {
            run_device_loop(backend, cmd_rx, &Heartbeat::new(), &loop_outputs)
        });

        let (reply_tx, reply_rx) = tokio::sync::oneshot::channel();
        cmd_tx
            .send(DeviceCmd::Discover {
                force: false,
                verify_only: false,
                exclude_serials: vec![],
                reply: reply_tx,
            })
            .unwrap();
        assert_eq!(reply_rx.blocking_recv().unwrap().len(), 1);
        assert!(outputs.router.submit(SERIAL, 255, 0, 0, 200));

        assert!(graceful_stop(
            &cmd_tx,
            &outputs.router,
            Duration::from_secs(2)
        ));
        handle.join().unwrap();

        let last = device.reports().pop().expect("a write");
        assert_eq!(
            (last[2], last[3], last[4], last[6]),
            (0, 0, 0, 0),
            "the last write must be LED off, rumble 0"
        );
    }
}
