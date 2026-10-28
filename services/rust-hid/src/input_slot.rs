//! Latest-sample slots between the device loop and the StreamIO response
//! (#2372).
//!
//! The device loop offers each controller's newest input report; a report
//! not yet taken by the stream is replaced in place, with the button bits of
//! the replaced reports OR'ed in, so a press is never lost (UC-010 BR-012,
//! #2317). At most one sample per controller waits, so a reader that lags
//! gets the newest input instead of a queue of old samples.

use std::pin::Pin;
use std::sync::{Arc, Mutex, MutexGuard};
use std::task::{Context, Poll, Waker};

use crate::hid::ParsedInputReport;

/// Sensor data sent from the device manager to the gRPC stream.
#[derive(Debug, Clone)]
pub struct StreamSensorData {
    pub serial: String,
    pub report: ParsedInputReport,
}

/// The receiver is gone: its gRPC call ended.
#[derive(Debug, PartialEq, Eq)]
pub struct Closed;

#[derive(Default)]
struct State {
    /// At most one sample per serial, in the order the serials first waited.
    pending: Vec<StreamSensorData>,
    /// The stream task waiting for a sample.
    waker: Option<Waker>,
    sink_gone: bool,
    receiver_gone: bool,
}

#[derive(Default)]
struct Shared(Mutex<State>);

impl Shared {
    fn lock(&self) -> MutexGuard<'_, State> {
        self.0.lock().unwrap_or_else(|e| e.into_inner())
    }
}

/// A new pair of slots: the device loop's sink and the stream's receiver.
pub fn channel() -> (InputSink, InputReceiver) {
    let shared = Arc::new(Shared::default());
    (InputSink(shared.clone()), InputReceiver(shared))
}

/// Device-loop side. Dropping it ends the receiver's stream once it is empty.
pub struct InputSink(Arc<Shared>);

impl InputSink {
    /// Offer a controller's newest report. A sample of this serial still
    /// waiting is replaced, keeping its button bits and its queue position.
    pub fn offer(&self, serial: &str, mut report: ParsedInputReport) -> Result<(), Closed> {
        let waker = {
            let mut state = self.0.lock();
            if state.receiver_gone {
                return Err(Closed);
            }
            match state.pending.iter_mut().find(|s| s.serial == serial) {
                Some(waiting) => {
                    report.buttons |= waiting.report.buttons;
                    waiting.report = report;
                }
                None => state.pending.push(StreamSensorData {
                    serial: serial.to_string(),
                    report,
                }),
            }
            state.waker.take()
        };
        if let Some(waker) = waker {
            waker.wake();
        }
        Ok(())
    }

    /// The receiver is gone.
    pub fn is_closed(&self) -> bool {
        self.0.lock().receiver_gone
    }
}

impl Drop for InputSink {
    fn drop(&mut self) {
        let waker = {
            let mut state = self.0.lock();
            state.sink_gone = true;
            state.waker.take()
        };
        if let Some(waker) = waker {
            waker.wake();
        }
    }
}

/// Stream side: yields the waiting samples, oldest-waiting serial first.
pub struct InputReceiver(Arc<Shared>);

impl InputReceiver {
    /// The next waiting sample, without waiting.
    #[cfg(test)]
    pub fn try_recv(&mut self) -> Option<StreamSensorData> {
        let mut state = self.0.lock();
        (!state.pending.is_empty()).then(|| state.pending.remove(0))
    }
}

impl tokio_stream::Stream for InputReceiver {
    type Item = StreamSensorData;

    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let mut state = self.0.lock();
        if !state.pending.is_empty() {
            return Poll::Ready(Some(state.pending.remove(0)));
        }
        if state.sink_gone {
            return Poll::Ready(None);
        }
        // Stored under the lock the sink offers under: no wake-up is lost.
        state.waker = Some(cx.waker().clone());
        Poll::Pending
    }
}

impl Drop for InputReceiver {
    fn drop(&mut self) {
        let mut state = self.0.lock();
        state.receiver_gone = true;
        state.pending.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hid::{self, Button};
    use std::time::Duration;
    use tokio_stream::StreamExt;

    fn report(buttons: u32, accel_z: f32) -> ParsedInputReport {
        let mut data = [0u8; hid::INPUT_REPORT_SIZE];
        data[0] = 0x01;
        let mut report = hid::parse_input_report(&data, true).unwrap();
        report.buttons = buttons;
        report.accel_z = accel_z;
        report
    }

    // UC: UC-010 BR-012
    #[test]
    fn test_a_replaced_sample_keeps_its_buttons_and_only_the_newest_waits() {
        let (sink, mut rx) = channel();
        sink.offer("A", report(Button::CROSS, 100.0)).unwrap();
        sink.offer("A", report(0, 200.0)).unwrap();

        let data = rx.try_recv().expect("no sample waiting");
        assert_eq!(data.report.accel_z, 200.0, "not the newest");
        assert!(data.report.button_cross(), "the replaced press was lost");
        assert!(rx.try_recv().is_none(), "an older sample still waited");
    }

    #[test]
    fn test_serials_are_taken_in_the_order_they_first_waited() {
        let (sink, mut rx) = channel();
        sink.offer("A", report(0, 1.0)).unwrap();
        sink.offer("B", report(0, 2.0)).unwrap();
        sink.offer("A", report(0, 3.0)).unwrap();

        let order: Vec<String> = std::iter::from_fn(|| rx.try_recv())
            .map(|d| d.serial)
            .collect();
        assert_eq!(order, ["A", "B"]);
    }

    #[test]
    fn test_a_dropped_receiver_closes_the_sink() {
        let (sink, rx) = channel();
        assert!(!sink.is_closed());
        drop(rx);
        assert!(sink.is_closed());
        assert_eq!(sink.offer("A", report(0, 1.0)), Err(Closed));
    }

    #[tokio::test]
    async fn test_the_stream_wakes_on_an_offer_and_ends_after_the_sink() {
        let (sink, mut rx) = channel();
        let reader = tokio::spawn(async move {
            let mut serials = Vec::new();
            while let Some(data) = rx.next().await {
                serials.push(data.serial);
            }
            serials
        });
        tokio::time::sleep(Duration::from_millis(20)).await;
        sink.offer("A", report(0, 1.0)).unwrap();
        tokio::time::sleep(Duration::from_millis(20)).await;
        sink.offer("B", report(0, 2.0)).unwrap();
        drop(sink);

        let serials = tokio::time::timeout(Duration::from_secs(2), reader)
            .await
            .expect("the stream did not end after its sink was dropped")
            .unwrap();
        assert_eq!(serials, ["A", "B"]);
    }
}
