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
//!   unless a newer value replaced it, at most `MAX_RETRIES` times in a row.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};
use tracing::{debug, warn};

use crate::hid::{self, HidDeviceIo};

/// Code default of `rust_hid.output_rate_hz`: used until flagd has synced.
pub const DEFAULT_RATE_HZ: u32 = 20;
/// A failed write is retried at most this many times in a row.
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
        st.pending = Some(Pending { rgb: (r, g, b), rumble_peak: peak, rumble_latest: rumble });
        drop(st);
        self.wake.notify_one();
    }

    /// Ask the writer to send LED off / rumble off once, then stop.
    fn close(&self) {
        let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
        st.pending = Some(Pending { rgb: (0, 0, 0), rumble_peak: 0, rumble_latest: 0 });
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
        let writer_slot = slot.clone();
        let name = format!("hid-out-{serial}");
        let thread_serial = serial.to_string();
        let spawned = std::thread::Builder::new()
            .name(name)
            .spawn(move || run_writer(&thread_serial, &writer_slot, &device, &rate));
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

/// Writer loop of one controller: wait for a value, write it, keep the rate cap.
fn run_writer<D: HidDeviceIo>(
    serial: &str,
    slot: &OutputSlot,
    device: &Mutex<D>,
    rate: &OutputRate,
) {
    let mut last_write: Option<Instant> = None;
    let mut failures: u32 = 0;
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
                st.pending = Some(Pending { rumble_peak: p.rumble_latest, ..p });
            }
            (p, st.closing)
        };

        let (r, g, b) = out.rgb;
        let report = hid::build_output_report(r, g, b, out.rumble_peak);
        let result = device.lock().unwrap_or_else(|e| e.into_inner()).write(&report);
        last_write = Some(Instant::now());
        match result {
            Ok(_) => failures = 0,
            Err(e) => {
                failures += 1;
                if failures == 1 {
                    warn!(serial, error = %e, "Failed to write output report");
                } else {
                    debug!(serial, error = %e, failures, "Failed to write output report");
                }
                if failures <= MAX_RETRIES {
                    let mut st = slot.state.lock().unwrap_or_else(|e| e.into_inner());
                    if st.pending.is_none() {
                        st.pending = Some(out); // retry unless a newer value replaced it
                    }
                }
            }
        }
        if closing {
            let st = slot.state.lock().unwrap_or_else(|e| e.into_inner());
            if st.pending.is_none() || failures > MAX_RETRIES {
                return;
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

    /// A device that records its writes: (time, report).
    #[derive(Clone)]
    pub(crate) struct RecordingDevice {
        pub writes: Arc<Mutex<Vec<(Instant, Vec<u8>)>>>,
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
    // NFR: NFR-055
    #[test]
    fn test_a_failing_write_is_retried_a_bounded_number_of_times() {
        let router = OutputRouter::default();
        let device = RecordingDevice::new(WriteMode::WouldBlock);
        let attempts = Arc::new(AtomicU32::new(0));
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
        router.open(
            "s1",
            Arc::new(Mutex::new(Counting(device.clone(), attempts.clone()))),
            OutputRate::new(100),
        );
        router.submit("s1", 5, 5, 5, 0);
        std::thread::sleep(Duration::from_millis(300));
        assert_eq!(attempts.load(Ordering::Relaxed), 1 + MAX_RETRIES, "retries not bounded");

        // The queue recovers: the next value is written once the device accepts it.
        *device.mode.lock().unwrap() = WriteMode::Ok;
        router.submit("s1", 7, 7, 7, 0);
        assert!(wait_for(|| device.reports().len() == 1, Duration::from_secs(1)));
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
