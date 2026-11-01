//! Output reports (LED + rumble): one writer thread and one coalescing slot per
//! controller (UC-002 BR-005, NFR-055, #2125).
//!
//! On stage with about 9 controllers the kernel logged "Output queue is full"
//! and rust-hid wedged. Writes used to run on the device-manager thread, fed by
//! an unbounded command queue with one entry per IOCommand. Now:
//! - a write never runs on the device-manager thread or a tokio worker: each
//!   open controller has its own writer thread, so a blocking or failing write
//!   stalls only that controller's output;
//! - the queue is one slot per controller (bounded by construction): the LED is
//!   last value wins, rumble keeps the highest value since the last write (a
//!   short pulse is not lost) and the latest value follows in the next write;
//! - the writer sends at most `rate_hz` reports per second per controller (the
//!   `rust_hid.output_rate_hz` flag); a failed write is retried at the next slot
//!   unless a newer value replaced it: a lit value at most `MAX_RETRIES` times,
//!   a quiet one (rumble 0, so also the off of a close) until it lands, so a
//!   controller never keeps rumbling because its "stop" was dropped.
//!
//! The writer shares the controller's input handle behind a mutex. A write
//! stuck in the kernel keeps that mutex, so the device loop's `try_lock` fails
//! and it skips that controller's reads (its input freezes) while the
//! heartbeat and the health check stay fine (#2142). Other controllers are
//! unaffected. A separate writer handle (#2335) was not adopted: the Pi showed
//! no write stall (#2313). Each slot records when its in-flight
//! write started; the gauge `rust_hid_output_write_in_flight_seconds` reads it,
//! and `StuckWriterMonitor` (driven by the watchdog tick, off the hot path)
//! logs a write in flight longer than `STUCK_WRITE_THRESHOLD` once per
//! episode. Report only: a stuck writer never trips the watchdog.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU32, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};
use tracing::{debug, info, warn};

use crate::hid::{self, HidDeviceIo};

/// Code default of `rust_hid.output_rate_hz`: used until flagd has synced.
pub const DEFAULT_RATE_HZ: u32 = 20;
/// A failed write of a value with rumble is retried at most this many times
/// (counted per value); the off of a close at most this many times too.
const MAX_RETRIES: u32 = 3;
/// A write in flight longer than this is reported as stuck (NFR-055, #2142).
/// A healthy hidraw write of one 49-byte report returns in milliseconds, so
/// 1 s (20 missed slots at the default rate cap) is far outside normal jitter.
/// A constant, not a flag (C-035): it only picks when a WARN line is logged,
/// changes no behaviour, and the gauge carries the raw age for any other cut.
pub const STUCK_WRITE_THRESHOLD: Duration = Duration::from_secs(1);

/// The per-controller output rate cap, shared by every writer and refreshed from the flag.
#[derive(Debug, Clone)]
pub struct OutputRate(Arc<AtomicU32>);

impl Default for OutputRate {
    fn default() -> Self {
        Self::new(DEFAULT_RATE_HZ)
    }
}

impl OutputRate {
    pub fn new(hz: u32) -> Self {
        Self(Arc::new(AtomicU32::new(hz)))
    }

    /// Set the cap; 0 or less is ignored (a cap of 0 would stop all output).
    pub fn set(&self, hz: i64) {
        if let Ok(hz) = u32::try_from(hz)
            && hz > 0
        {
            self.0.store(hz, Ordering::Relaxed);
        }
    }

    pub fn hz(&self) -> u32 {
        self.0.load(Ordering::Relaxed)
    }

    fn min_interval(&self) -> Duration {
        Duration::from_secs_f64(1.0 / f64::from(self.hz().max(1)))
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Pending {
    rgb: (u8, u8, u8),
    /// Highest rumble since the last write: what the next write sends.
    rumble_peak: u8,
    /// The rumble last asked for: sent after the peak when it differs.
    rumble_latest: u8,
    /// Failed writes of this value so far (0 for a new value).
    attempts: u32,
}

#[derive(Debug, Default)]
struct SlotState {
    pending: Option<Pending>,
    closing: bool,
}

/// One controller's output slot and the condition its writer waits on.
#[derive(Debug, Default)]
pub struct OutputSlot {
    state: Mutex<SlotState>,
    wake: Condvar,
    /// Start of the write in flight, as milliseconds since the router's
    /// origin plus 1; 0 while no write is in flight.
    write_started: AtomicU64,
}

impl OutputSlot {
    /// Coalesce a new output into the slot; never blocks on the device.
    pub fn submit(&self, r: u8, g: u8, b: u8, rumble: u8) {
        let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if st.closing {
            return;
        }
        let peak = st.pending.map_or(rumble, |p| p.rumble_peak.max(rumble));
        st.pending = Some(Pending {
            rgb: (r, g, b),
            rumble_peak: peak,
            rumble_latest: rumble,
            attempts: 0,
        });
        drop(st);
        self.wake.notify_one();
    }

    /// Ask the writer to send LED off / rumble off once, then stop.
    fn close(&self) {
        let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
        st.pending = Some(Pending {
            rgb: (0, 0, 0),
            rumble_peak: 0,
            rumble_latest: 0,
            attempts: 0,
        });
        st.closing = true;
        drop(st);
        self.wake.notify_one();
    }
}

/// One open controller's writer, as seen by the stuck-writer check and the gauge.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WriteInFlight {
    pub serial: String,
    /// Identity of the write in flight (its start stamp); `None` when idle.
    pub started: Option<u64>,
    /// How long the write has been in flight; zero when idle.
    pub age: Duration,
}

/// Serial -> slot of every open controller, shared by the gRPC stream and the device loop.
#[derive(Debug, Clone)]
pub struct OutputRouter {
    slots: Arc<Mutex<HashMap<String, Arc<OutputSlot>>>>,
    /// Time base of every slot's `write_started` stamp.
    origin: Instant,
    /// Writer threads that have not ended yet (a closed one counts until its off is sent).
    live_writers: Arc<AtomicUsize>,
}

impl Default for OutputRouter {
    fn default() -> Self {
        Self {
            slots: Arc::default(),
            origin: Instant::now(),
            live_writers: Arc::default(),
        }
    }
}

/// Counts a writer thread as live until it is dropped (also on a panic).
struct LiveWriter(Arc<AtomicUsize>);

impl LiveWriter {
    fn new(count: &Arc<AtomicUsize>) -> Self {
        count.fetch_add(1, Ordering::SeqCst);
        Self(count.clone())
    }
}

impl Drop for LiveWriter {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::SeqCst);
    }
}

impl OutputRouter {
    /// Wait until every writer thread has ended (after a close each sends its
    /// off first), at most `timeout`. True when none is left.
    pub fn wait_writers_stopped(&self, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        loop {
            if self.live_writers.load(Ordering::SeqCst) == 0 {
                return true;
            }
            if Instant::now() >= deadline {
                return false;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
    }

    /// Every open controller's write in flight, if any (never touches a device).
    pub fn writes_in_flight(&self) -> Vec<WriteInFlight> {
        let now_ms = self.origin.elapsed().as_millis() as u64;
        self.slots
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .map(|(serial, slot)| {
                let stamp = slot.write_started.load(Ordering::Relaxed);
                let started = (stamp != 0).then_some(stamp);
                let age = started.map_or(Duration::ZERO, |s| {
                    Duration::from_millis(now_ms.saturating_sub(s - 1))
                });
                WriteInFlight {
                    serial: serial.clone(),
                    started,
                    age,
                }
            })
            .collect()
    }

    /// Route one IOCommand; an unknown serial is dropped (as before: no open device).
    pub fn submit(&self, serial: &str, r: u8, g: u8, b: u8, rumble: u8) -> bool {
        let slot = self
            .slots
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(serial)
            .cloned();
        match slot {
            Some(slot) => {
                slot.submit(r, g, b, rumble);
                true
            }
            None => false,
        }
    }

    /// Start the writer thread of a newly opened controller.
    pub fn open<D: HidDeviceIo + Send + 'static>(
        &self,
        serial: &str,
        device: Arc<Mutex<D>>,
        rate: OutputRate,
    ) {
        let slot = Arc::new(OutputSlot::default());
        let writer = Writer {
            serial: serial.to_string(),
            labels: crate::metrics::ControllerLabels::new(serial),
            slot: slot.clone(),
            slots: self.slots.clone(),
            origin: self.origin,
        };
        let live = LiveWriter::new(&self.live_writers);
        let spawned = std::thread::Builder::new()
            .name(format!("hid-out-{serial}"))
            .spawn(move || {
                let _live = live;
                writer.run(&device, &rate)
            });
        if let Err(e) = spawned {
            warn!(serial, error = %e, "Failed to start the output writer; no LED / rumble for this controller");
            return;
        }
        if let Some(old) = self
            .slots
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(serial.to_string(), slot)
        {
            old.close();
        }
    }

    /// Send off once and stop the controller's writer. Never waits for it: a
    /// writer stuck in a write ends when the write returns.
    pub fn close(&self, serial: &str) {
        if let Some(slot) = self
            .slots
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(serial)
        {
            slot.close();
        }
    }
}

/// One controller's writer: its slot, and the router's map to tell whether a
/// newer slot has replaced it.
struct Writer {
    serial: String,
    labels: crate::metrics::ControllerLabels,
    slot: Arc<OutputSlot>,
    slots: Arc<Mutex<HashMap<String, Arc<OutputSlot>>>>,
    origin: Instant,
}

impl Writer {
    /// True when the serial was opened again after this writer was closed: its
    /// "off" would land on top of the new writer's colour (#2138 review), so it is skipped.
    fn superseded(&self) -> bool {
        self.slots
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(&self.serial)
            .is_some_and(|current| !Arc::ptr_eq(current, &self.slot))
    }

    /// Wait for a value, write it, keep the rate cap; return after a close.
    fn run<D: HidDeviceIo>(&self, device: &Mutex<D>, rate: &OutputRate) {
        let serial = self.serial.as_str();
        let slot = &*self.slot;
        let mut last_write: Option<Instant> = None;
        let mut streak: u32 = 0; // failed writes in a row, for the log only
        loop {
            // Wait for a value, then for the rate-cap slot (a close skips the wait).
            let (out, closing) = {
                let mut st = slot.state.lock().unwrap_or_else(|e| e.into_inner());
                while st.pending.is_none() {
                    st = slot.wake.wait(st).unwrap_or_else(|e| e.into_inner());
                }
                if let Some(last) = last_write {
                    loop {
                        let due = last + rate.min_interval();
                        let now = Instant::now();
                        if st.closing || now >= due {
                            break;
                        }
                        st = slot
                            .wake
                            .wait_timeout(st, due - now)
                            .unwrap_or_else(|e| e.into_inner())
                            .0;
                    }
                }
                let Some(p) = st.pending.take() else { continue };
                if p.rumble_latest != p.rumble_peak {
                    // The peak goes out now; the latest rumble follows in the next write.
                    st.pending = Some(Pending {
                        rumble_peak: p.rumble_latest,
                        attempts: 0,
                        ..p
                    });
                }
                (p, st.closing)
            };
            if closing && self.superseded() {
                return;
            }

            let (r, g, b) = out.rgb;
            let report = hid::build_output_report(r, g, b, out.rumble_peak);
            // Stamped before the device lock: waiting on it is part of the stall.
            let stamp = self.origin.elapsed().as_millis() as u64 + 1;
            slot.write_started.store(stamp, Ordering::Relaxed);
            let write_started = Instant::now();
            let result = device
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .write(&report);
            slot.write_started.store(0, Ordering::Relaxed);
            crate::metrics::record_write(&self.labels, write_started.elapsed(), result.is_ok());
            last_write = Some(Instant::now());
            let mut gave_up = false;
            match result {
                Ok(_) => streak = 0,
                Err(e) => {
                    streak += 1;
                    if streak == 1 {
                        warn!(serial, error = %e, "Failed to write output report");
                    } else {
                        debug!(serial, error = %e, streak, "Failed to write output report");
                    }
                    let attempts = out.attempts + 1;
                    // A quiet value (rumble off) is retried until it lands, except the
                    // off of a close: that one is bounded so the writer can end.
                    let retry = attempts <= MAX_RETRIES || (out.rumble_peak == 0 && !closing);
                    let mut st = slot.state.lock().unwrap_or_else(|e| e.into_inner());
                    if retry && st.pending.is_none() {
                        st.pending = Some(Pending { attempts, ..out }); // unless a newer value replaced it
                    }
                    gave_up = !retry;
                }
            }
            if closing {
                let st = slot.state.lock().unwrap_or_else(|e| e.into_inner());
                if st.pending.is_none() || gave_up {
                    return;
                }
            }
        }
    }
}

/// A change in the stuck-writer report.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StuckWriterEvent {
    /// A write has been in flight longer than the threshold.
    Stuck { serial: String, age: Duration },
    /// No longer stuck: the write returned or its controller was closed.
    Recovered { serial: String },
}

/// Reports a writer stuck in one write once per episode (#2142). Report only:
/// it never closes a controller or trips the watchdog.
#[derive(Debug)]
pub struct StuckWriterMonitor {
    threshold: Duration,
    /// Serial -> start stamp of the write reported as stuck.
    reported: HashMap<String, u64>,
}

impl StuckWriterMonitor {
    pub fn new(threshold: Duration) -> Self {
        Self {
            threshold,
            reported: HashMap::new(),
        }
    }

    /// Compare the router's writes in flight with the last report; log and
    /// return what changed. A new write that sticks again is a new episode.
    pub fn check(&mut self, router: &OutputRouter) -> Vec<StuckWriterEvent> {
        self.observe(router.writes_in_flight())
    }

    /// `check` on a given snapshot of the writes in flight.
    fn observe(&mut self, writes: Vec<WriteInFlight>) -> Vec<StuckWriterEvent> {
        let mut stuck: HashMap<String, (u64, Duration)> = HashMap::new();
        for w in writes {
            if let Some(started) = w.started
                && w.age > self.threshold
            {
                stuck.insert(w.serial, (started, w.age));
            }
        }
        let mut events = Vec::new();
        let mut gone: Vec<&String> = self
            .reported
            .iter()
            .filter(|(serial, started)| stuck.get(*serial).map(|s| s.0) != Some(**started))
            .map(|(serial, _)| serial)
            .collect();
        gone.sort();
        for serial in gone {
            info!(
                serial = %serial,
                "Output write no longer stuck (the write returned or the controller was closed)"
            );
            events.push(StuckWriterEvent::Recovered {
                serial: serial.clone(),
            });
        }
        let mut new: Vec<(&String, &(u64, Duration))> = stuck
            .iter()
            .filter(|(serial, (started, _))| self.reported.get(*serial) != Some(started))
            .collect();
        new.sort();
        for (serial, (_, age)) in new {
            warn!(
                serial = %serial,
                in_flight_ms = age.as_millis() as u64,
                "Output write stuck: this controller's input is frozen until it returns"
            );
            events.push(StuckWriterEvent::Stuck {
                serial: serial.clone(),
                age: *age,
            });
        }
        self.reported = stuck.into_iter().map(|(k, (s, _))| (k, s)).collect();
        events
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::sync::atomic::AtomicBool;

    /// What a fake device's writes do.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub(crate) enum WriteMode {
        Ok,
        /// Every write fails as with a full kernel queue (EAGAIN).
        WouldBlock,
        /// Every write blocks until `release` is set.
        Block,
    }

    /// Every write a fake device accepted: (time, report).
    type WriteLog = Arc<Mutex<Vec<(Instant, Vec<u8>)>>>;

    /// A device that records its writes.
    #[derive(Clone)]
    pub(crate) struct RecordingDevice {
        pub writes: WriteLog,
        pub mode: Arc<Mutex<WriteMode>>,
        pub release: Arc<AtomicBool>,
    }

    impl RecordingDevice {
        pub(crate) fn new(mode: WriteMode) -> Self {
            Self {
                writes: Arc::default(),
                mode: Arc::new(Mutex::new(mode)),
                release: Arc::default(),
            }
        }

        pub(crate) fn reports(&self) -> Vec<Vec<u8>> {
            self.writes
                .lock()
                .unwrap()
                .iter()
                .map(|(_, r)| r.clone())
                .collect()
        }
    }

    impl HidDeviceIo for RecordingDevice {
        fn read(&self, _buf: &mut [u8]) -> hidapi::HidResult<usize> {
            Ok(0)
        }

        fn write(&self, data: &[u8]) -> hidapi::HidResult<usize> {
            let mode = *self.mode.lock().unwrap();
            match mode {
                WriteMode::Ok => {}
                WriteMode::WouldBlock => {
                    return Err(hidapi::HidError::HidApiError {
                        message: "Resource temporarily unavailable".into(),
                    });
                }
                WriteMode::Block => {
                    while !self.release.load(Ordering::Relaxed) {
                        std::thread::sleep(Duration::from_millis(1));
                    }
                }
            }
            self.writes
                .lock()
                .unwrap()
                .push((Instant::now(), data.to_vec()));
            Ok(data.len())
        }
    }

    fn rgb_rumble(report: &[u8]) -> (u8, u8, u8, u8) {
        (report[2], report[3], report[4], report[6])
    }

    fn open(router: &OutputRouter, device: &RecordingDevice, hz: u32) {
        router.open(
            "s1",
            Arc::new(Mutex::new(device.clone())),
            OutputRate::new(hz),
        );
    }

    fn wait_for(cond: impl Fn() -> bool, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if cond() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(2));
        }
        cond()
    }

    // UC: UC-002 BR-005
    // NFR: NFR-055
    #[test]
    fn test_output_rate_is_capped_and_the_last_led_value_wins() {
        let router = OutputRouter::default();
        let device = RecordingDevice::new(WriteMode::Ok);
        open(&router, &device, 20);

        // 100 Hz of colour changes for 500 ms, as an LED animation sends them.
        let start = Instant::now();
        for i in 0..50u8 {
            assert!(router.submit("s1", i, 0, 0, 0));
            std::thread::sleep(Duration::from_millis(10));
        }
        let elapsed = start.elapsed();
        assert!(wait_for(
            || device.reports().last().map(|r| r[2]) == Some(49),
            Duration::from_secs(1)
        ));
        let writes = device.writes.lock().unwrap().clone();

        // 20 Hz over the window, plus the first write and the final catch-up.
        let max = (elapsed.as_secs_f64() * 20.0).ceil() as usize + 2;
        assert!(
            writes.len() <= max,
            "{} writes in {elapsed:?} at a 20 Hz cap",
            writes.len()
        );
        assert!(
            writes.len() >= 5,
            "only {} writes: output starved",
            writes.len()
        );
        for pair in writes.windows(2) {
            let gap = pair[1].0 - pair[0].0;
            assert!(
                gap >= Duration::from_millis(45),
                "two writes {gap:?} apart at a 20 Hz cap"
            );
        }
    }

    // UC: UC-002 BR-005
    // NFR: NFR-055
    #[test]
    fn test_a_short_rumble_pulse_survives_coalescing() {
        let router = OutputRouter::default();
        let device = RecordingDevice::new(WriteMode::Ok);
        open(&router, &device, 20);
        router.submit("s1", 1, 1, 1, 0);
        assert!(wait_for(
            || device.reports().len() == 1,
            Duration::from_secs(1)
        ));

        // Pulse on and off inside one rate-cap interval.
        router.submit("s1", 1, 1, 1, 200);
        router.submit("s1", 9, 9, 9, 0);
        assert!(wait_for(
            || device.reports().len() >= 3,
            Duration::from_secs(1)
        ));
        let got: Vec<_> = device.reports().iter().map(|r| rgb_rumble(r)).collect();
        assert_eq!(got[1], (9, 9, 9, 200), "the pulse was coalesced away");
        assert_eq!(
            got[2],
            (9, 9, 9, 0),
            "rumble did not return to the latest value"
        );
    }

    // UC: UC-002 BR-005
    #[test]
    fn test_an_identical_value_in_a_later_interval_is_still_written() {
        // The controller manager re-sends the unchanged LED colour every 2 s as a
        // keep-alive (the Move turns its LED off without one): no dedupe here.
        let router = OutputRouter::default();
        let device = RecordingDevice::new(WriteMode::Ok);
        open(&router, &device, 20);
        for n in 1..=3usize {
            assert!(router.submit("s1", 7, 7, 7, 0));
            assert!(
                wait_for(|| device.reports().len() == n, Duration::from_secs(1)),
                "submit {n} of an identical value was not written ({} writes)",
                device.reports().len()
            );
            // Past the 50 ms rate-cap interval before the next identical submit.
            std::thread::sleep(Duration::from_millis(120));
        }
        let got: Vec<_> = device.reports().iter().map(|r| rgb_rumble(r)).collect();
        assert_eq!(got, vec![(7, 7, 7, 0); 3]);
    }

    /// Counts every write attempt, then delegates to the recording device.
    struct Counting(RecordingDevice, Arc<AtomicU32>);

    impl HidDeviceIo for Counting {
        fn read(&self, buf: &mut [u8]) -> hidapi::HidResult<usize> {
            self.0.read(buf)
        }
        fn write(&self, data: &[u8]) -> hidapi::HidResult<usize> {
            self.1.fetch_add(1, Ordering::Relaxed);
            self.0.write(data)
        }
    }

    fn open_counting(router: &OutputRouter, device: &RecordingDevice) -> Arc<AtomicU32> {
        let attempts = Arc::new(AtomicU32::new(0));
        router.open(
            "s1",
            Arc::new(Mutex::new(Counting(device.clone(), attempts.clone()))),
            OutputRate::new(100),
        );
        attempts
    }

    // UC: UC-002 BR-005
    // NFR: NFR-055
    #[test]
    fn test_a_failing_write_is_retried_a_bounded_number_of_times_per_value() {
        let router = OutputRouter::default();
        let device = RecordingDevice::new(WriteMode::WouldBlock);
        let attempts = open_counting(&router, &device);

        router.submit("s1", 5, 5, 5, 100);
        std::thread::sleep(Duration::from_millis(300));
        assert_eq!(
            attempts.load(Ordering::Relaxed),
            1 + MAX_RETRIES,
            "retries not bounded"
        );

        // A new value after a failure streak gets its own retries (counted per value).
        router.submit("s1", 6, 6, 6, 100);
        std::thread::sleep(Duration::from_millis(300));
        assert_eq!(attempts.load(Ordering::Relaxed), 2 * (1 + MAX_RETRIES));

        // The queue recovers: the next value is written once the device accepts it.
        *device.mode.lock().unwrap() = WriteMode::Ok;
        router.submit("s1", 7, 7, 7, 0);
        assert!(wait_for(
            || device.reports().len() == 1,
            Duration::from_secs(1)
        ));
    }

    // UC: UC-002 BR-005
    // NFR: NFR-055
    #[test]
    fn test_a_rumble_stop_is_retried_until_it_lands() {
        let router = OutputRouter::default();
        let device = RecordingDevice::new(WriteMode::WouldBlock);
        let attempts = open_counting(&router, &device);

        router.submit("s1", 5, 5, 5, 0);
        std::thread::sleep(Duration::from_millis(200));
        assert!(
            attempts.load(Ordering::Relaxed) > 2 * (1 + MAX_RETRIES),
            "a rumble-off write was given up after {} attempts",
            attempts.load(Ordering::Relaxed)
        );
        *device.mode.lock().unwrap() = WriteMode::Ok;
        assert!(wait_for(
            || device.reports().first().map(|r| rgb_rumble(r)) == Some((5, 5, 5, 0)),
            Duration::from_secs(1)
        ));
    }

    // UC: UC-002 BR-005
    // NFR: NFR-055
    #[test]
    fn test_a_reopened_controller_does_not_get_the_old_writers_off() {
        let router = OutputRouter::default();
        let old = RecordingDevice::new(WriteMode::Block);
        router.open(
            "s1",
            Arc::new(Mutex::new(old.clone())),
            OutputRate::new(100),
        );
        router.submit("s1", 1, 1, 1, 0); // in flight on the old handle
        std::thread::sleep(Duration::from_millis(20));

        // Close, then open the same serial again before the old write returns.
        router.close("s1");
        let new = RecordingDevice::new(WriteMode::Ok);
        router.open(
            "s1",
            Arc::new(Mutex::new(new.clone())),
            OutputRate::new(100),
        );
        router.submit("s1", 9, 9, 9, 0);
        assert!(wait_for(
            || new.reports().len() == 1,
            Duration::from_secs(1)
        ));

        old.release.store(true, Ordering::Relaxed);
        std::thread::sleep(Duration::from_millis(100));
        let old_writes: Vec<_> = old.reports().iter().map(|r| rgb_rumble(r)).collect();
        assert_eq!(
            old_writes,
            vec![(1, 1, 1, 0)],
            "the superseded writer still sent off"
        );
    }

    // UC: UC-002 BR-005
    // NFR: NFR-055
    #[test]
    fn test_a_blocked_write_never_blocks_submit_or_other_controllers() {
        let router = OutputRouter::default();
        let stuck = RecordingDevice::new(WriteMode::Block);
        let healthy = RecordingDevice::new(WriteMode::Ok);
        router.open(
            "stuck",
            Arc::new(Mutex::new(stuck.clone())),
            OutputRate::new(100),
        );
        router.open(
            "ok",
            Arc::new(Mutex::new(healthy.clone())),
            OutputRate::new(100),
        );

        let start = Instant::now();
        for i in 0..1000u32 {
            router.submit("stuck", (i % 256) as u8, 0, 0, 0);
            router.submit("ok", 1, 2, 3, 0);
        }
        assert!(
            start.elapsed() < Duration::from_millis(200),
            "submit waited on a blocked write"
        );
        assert!(wait_for(
            || !healthy.reports().is_empty(),
            Duration::from_secs(1)
        ));

        // Closing the stuck controller does not wait for its writer either.
        let t = Instant::now();
        router.close("stuck");
        assert!(t.elapsed() < Duration::from_millis(50));
        stuck.release.store(true, Ordering::Relaxed);
        // Once released: the in-flight write, then off.
        assert!(wait_for(
            || stuck.reports().last().map(|r| rgb_rumble(r)) == Some((0, 0, 0, 0)),
            Duration::from_secs(1)
        ));
        assert!(
            stuck.reports().len() <= 2,
            "the backlog was written: {}",
            stuck.reports().len()
        );
    }

    // UC: UC-002 BR-005
    // NFR: NFR-055
    #[test]
    fn test_a_stuck_write_is_reported_once_and_cleared_when_it_returns() {
        let router = OutputRouter::default();
        let stuck = RecordingDevice::new(WriteMode::Block);
        let healthy = RecordingDevice::new(WriteMode::Ok);
        for (serial, dev) in [("stuck", &stuck), ("ok", &healthy)] {
            router.open(
                serial,
                Arc::new(Mutex::new(dev.clone())),
                OutputRate::new(100),
            );
        }
        let threshold = Duration::from_millis(60);
        let mut monitor = StuckWriterMonitor::new(threshold);
        router.submit("stuck", 1, 1, 1, 0);
        router.submit("ok", 2, 2, 2, 0);
        assert!(wait_for(
            || healthy.reports().len() == 1,
            Duration::from_secs(1)
        ));
        std::thread::sleep(threshold + Duration::from_millis(40));
        let events = monitor.check(&router);
        assert!(
            matches!(&events[..], [StuckWriterEvent::Stuck { serial, age }]
                if serial == "stuck" && *age > threshold),
            "expected one stuck report for the blocked writer only, got {events:?}"
        );
        // Still stuck on the same write: logged once per episode.
        std::thread::sleep(Duration::from_millis(20));
        assert_eq!(monitor.check(&router), vec![]);

        stuck.release.store(true, Ordering::Relaxed);
        assert!(wait_for(
            || stuck.reports().len() == 1,
            Duration::from_secs(1)
        ));
        assert!(wait_for(
            || router
                .writes_in_flight()
                .iter()
                .all(|w| w.started.is_none()),
            Duration::from_secs(1)
        ));
        assert_eq!(
            monitor.check(&router),
            vec![StuckWriterEvent::Recovered {
                serial: "stuck".into()
            }]
        );
        assert_eq!(monitor.check(&router), vec![]);
    }

    // UC: UC-002 BR-005
    // NFR: NFR-055
    #[test]
    fn test_writes_in_flight_reports_the_age_of_a_blocked_write_and_zero_when_idle() {
        let router = OutputRouter::default();
        let stuck = RecordingDevice::new(WriteMode::Block);
        let healthy = RecordingDevice::new(WriteMode::Ok);
        router.open(
            "stuck",
            Arc::new(Mutex::new(stuck.clone())),
            OutputRate::new(100),
        );
        router.open(
            "ok",
            Arc::new(Mutex::new(healthy.clone())),
            OutputRate::new(100),
        );
        router.submit("stuck", 1, 1, 1, 0);
        router.submit("ok", 1, 1, 1, 0);
        assert!(wait_for(
            || healthy.reports().len() == 1,
            Duration::from_secs(1)
        ));
        std::thread::sleep(Duration::from_millis(50));
        let mut got = router.writes_in_flight();
        got.sort_by(|a, b| a.serial.cmp(&b.serial));
        assert_eq!(got[0].serial, "ok");
        assert_eq!((got[0].started, got[0].age), (None, Duration::ZERO));
        assert_eq!(got[1].serial, "stuck");
        assert!(got[1].started.is_some());
        assert!(
            got[1].age >= Duration::from_millis(40),
            "age {:?}",
            got[1].age
        );

        // A closed controller has no entry, even while its last write is still stuck.
        router.close("stuck");
        assert!(router.writes_in_flight().iter().all(|w| w.serial == "ok"));
        stuck.release.store(true, Ordering::Relaxed);
    }

    // UC: UC-002 BR-005
    // NFR: NFR-055
    #[test]
    fn test_a_new_write_that_sticks_again_is_a_new_episode() {
        let threshold = Duration::from_secs(1);
        let mut monitor = StuckWriterMonitor::new(threshold);
        let write = |started: Option<u64>, age_ms: u64| WriteInFlight {
            serial: "s1".into(),
            started,
            age: Duration::from_millis(age_ms),
        };
        assert_eq!(
            monitor.observe(vec![write(Some(10), 1500)]),
            vec![StuckWriterEvent::Stuck {
                serial: "s1".into(),
                age: Duration::from_millis(1500)
            }]
        );
        // The same write, still stuck: no new report.
        assert_eq!(monitor.observe(vec![write(Some(10), 4500)]), vec![]);
        // Between two ticks the write returned and the next one stuck too.
        assert_eq!(
            monitor.observe(vec![write(Some(9000), 1200)]),
            vec![
                StuckWriterEvent::Recovered {
                    serial: "s1".into()
                },
                StuckWriterEvent::Stuck {
                    serial: "s1".into(),
                    age: Duration::from_millis(1200)
                },
            ]
        );
        // Under the threshold or idle: not stuck.
        assert_eq!(
            monitor.observe(vec![write(Some(20000), 900)]),
            vec![StuckWriterEvent::Recovered {
                serial: "s1".into()
            }]
        );
        assert_eq!(monitor.observe(vec![write(None, 0)]), vec![]);
    }

    // NFR: NFR-055
    #[test]
    fn test_stuck_write_threshold_matches_nfr_055() {
        assert_eq!(STUCK_WRITE_THRESHOLD, Duration::from_secs(1));
    }

    #[test]
    fn test_rate_ignores_non_positive_values() {
        let rate = OutputRate::new(20);
        rate.set(0);
        rate.set(-5);
        assert_eq!(rate.hz(), 20);
        rate.set(30);
        assert_eq!(rate.hz(), 30);
    }
}
