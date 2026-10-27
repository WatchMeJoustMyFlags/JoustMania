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

use std::collections::HashMap;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};
use tracing::{debug, warn};

use crate::hid::{self, HidDeviceIo};

/// Code default of `rust_hid.output_rate_hz`: used until flagd has synced.
pub const DEFAULT_RATE_HZ: u32 = 20;
/// A failed write of a value with rumble is retried at most this many times
/// (counted per value); the off of a close at most this many times too.
const MAX_RETRIES: u32 = 3;

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
}

impl OutputSlot {
    /// Coalesce a new output into the slot; never blocks on the device.
    pub fn submit(&self, r: u8, g: u8, b: u8, rumble: u8) {
        let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if st.closing {
            return;
        }
        let peak = st.pending.map_or(rumble, |p| p.rumble_peak.max(rumble));
        st.pending =
            Some(Pending { rgb: (r, g, b), rumble_peak: peak, rumble_latest: rumble, attempts: 0 });
        drop(st);
        self.wake.notify_one();
    }

    /// Ask the writer to send LED off / rumble off once, then stop.
    fn close(&self) {
        let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
        st.pending = Some(Pending { rgb: (0, 0, 0), rumble_peak: 0, rumble_latest: 0, attempts: 0 });
        st.closing = true;
        drop(st);
        self.wake.notify_one();
    }
}

/// Serial -> slot of every open controller, shared by the gRPC stream and the device loop.
#[derive(Debug, Clone, Default)]
pub struct OutputRouter {
    slots: Arc<Mutex<HashMap<String, Arc<OutputSlot>>>>,
}

impl OutputRouter {
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
            slot: slot.clone(),
            slots: self.slots.clone(),
        };
        let spawned = std::thread::Builder::new()
            .name(format!("hid-out-{serial}"))
            .spawn(move || writer.run(&device, &rate));
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
        if let Some(slot) = self.slots.lock().unwrap_or_else(|e| e.into_inner()).remove(serial) {
            slot.close();
        }
    }
}

/// One controller's writer: its slot, and the router's map to tell whether a
/// newer slot has replaced it.
struct Writer {
    serial: String,
    slot: Arc<OutputSlot>,
    slots: Arc<Mutex<HashMap<String, Arc<OutputSlot>>>>,
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
                        st = slot.wake.wait_timeout(st, due - now).unwrap_or_else(|e| e.into_inner()).0;
                    }
                }
                let Some(p) = st.pending.take() else { continue };
                if p.rumble_latest != p.rumble_peak {
                    // The peak goes out now; the latest rumble follows in the next write.
                    st.pending = Some(Pending { rumble_peak: p.rumble_latest, attempts: 0, ..p });
                }
                (p, st.closing)
            };
            if closing && self.superseded() {
                return;
            }

            let (r, g, b) = out.rgb;
            let report = hid::build_output_report(r, g, b, out.rumble_peak);
            let result = device.lock().unwrap_or_else(|e| e.into_inner()).write(&report);
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
            self.writes.lock().unwrap().iter().map(|(_, r)| r.clone()).collect()
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
            self.writes.lock().unwrap().push((Instant::now(), data.to_vec()));
            Ok(data.len())
        }
    }

    fn rgb_rumble(report: &[u8]) -> (u8, u8, u8, u8) {
        (report[2], report[3], report[4], report[6])
    }

    fn open(router: &OutputRouter, device: &RecordingDevice, hz: u32) {
        router.open("s1", Arc::new(Mutex::new(device.clone())), OutputRate::new(hz));
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
        assert!(wait_for(|| device.reports().last().map(|r| r[2]) == Some(49), Duration::from_secs(1)));
        let writes = device.writes.lock().unwrap().clone();

        // 20 Hz over the window, plus the first write and the final catch-up.
        let max = (elapsed.as_secs_f64() * 20.0).ceil() as usize + 2;
        assert!(writes.len() <= max, "{} writes in {elapsed:?} at a 20 Hz cap", writes.len());
        assert!(writes.len() >= 5, "only {} writes: output starved", writes.len());
        for pair in writes.windows(2) {
            let gap = pair[1].0 - pair[0].0;
            assert!(gap >= Duration::from_millis(45), "two writes {gap:?} apart at a 20 Hz cap");
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
        assert!(wait_for(|| device.reports().len() == 1, Duration::from_secs(1)));

        // Pulse on and off inside one rate-cap interval.
        router.submit("s1", 1, 1, 1, 200);
        router.submit("s1", 9, 9, 9, 0);
        assert!(wait_for(|| device.reports().len() >= 3, Duration::from_secs(1)));
        let got: Vec<_> = device.reports().iter().map(|r| rgb_rumble(r)).collect();
        assert_eq!(got[1], (9, 9, 9, 200), "the pulse was coalesced away");
        assert_eq!(got[2], (9, 9, 9, 0), "rumble did not return to the latest value");
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
        assert_eq!(attempts.load(Ordering::Relaxed), 1 + MAX_RETRIES, "retries not bounded");

        // A new value after a failure streak gets its own retries (counted per value).
        router.submit("s1", 6, 6, 6, 100);
        std::thread::sleep(Duration::from_millis(300));
        assert_eq!(attempts.load(Ordering::Relaxed), 2 * (1 + MAX_RETRIES));

        // The queue recovers: the next value is written once the device accepts it.
        *device.mode.lock().unwrap() = WriteMode::Ok;
        router.submit("s1", 7, 7, 7, 0);
        assert!(wait_for(|| device.reports().len() == 1, Duration::from_secs(1)));
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
        router.open("s1", Arc::new(Mutex::new(old.clone())), OutputRate::new(100));
        router.submit("s1", 1, 1, 1, 0); // in flight on the old handle
        std::thread::sleep(Duration::from_millis(20));

        // Close, then open the same serial again before the old write returns.
        router.close("s1");
        let new = RecordingDevice::new(WriteMode::Ok);
        router.open("s1", Arc::new(Mutex::new(new.clone())), OutputRate::new(100));
        router.submit("s1", 9, 9, 9, 0);
        assert!(wait_for(|| new.reports().len() == 1, Duration::from_secs(1)));

        old.release.store(true, Ordering::Relaxed);
        std::thread::sleep(Duration::from_millis(100));
        let old_writes: Vec<_> = old.reports().iter().map(|r| rgb_rumble(r)).collect();
        assert_eq!(old_writes, vec![(1, 1, 1, 0)], "the superseded writer still sent off");
    }

    // UC: UC-002 BR-005
    // NFR: NFR-055
    #[test]
    fn test_a_blocked_write_never_blocks_submit_or_other_controllers() {
        let router = OutputRouter::default();
        let stuck = RecordingDevice::new(WriteMode::Block);
        let healthy = RecordingDevice::new(WriteMode::Ok);
        router.open("stuck", Arc::new(Mutex::new(stuck.clone())), OutputRate::new(100));
        router.open("ok", Arc::new(Mutex::new(healthy.clone())), OutputRate::new(100));

        let start = Instant::now();
        for i in 0..1000u32 {
            router.submit("stuck", (i % 256) as u8, 0, 0, 0);
            router.submit("ok", 1, 2, 3, 0);
        }
        assert!(start.elapsed() < Duration::from_millis(200), "submit waited on a blocked write");
        assert!(wait_for(|| !healthy.reports().is_empty(), Duration::from_secs(1)));

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
        assert!(stuck.reports().len() <= 2, "the backlog was written: {}", stuck.reports().len());
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
