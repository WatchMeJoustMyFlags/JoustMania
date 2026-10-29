//! Push-side metrics: histograms and counters recorded where the work happens
//! (#2374, docs/specs/telemetry.md §6). The observable process and in-flight
//! gauges stay in `main.rs`. Until `install` runs (no OTLP endpoint, most
//! tests) every `record_*` call does nothing.

use std::sync::OnceLock;
use std::time::Duration;

use opentelemetry::KeyValue;
use opentelemetry::metrics::{Counter, Histogram, Meter};

/// Output write wall time: fine around 1-100 ms, up to the 1 s stuck threshold and past it.
const WRITE_BUCKETS: [f64; 12] = [
    0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5,
];
/// Device-loop pass work time: the loop aims at 1 kHz, a starved pass shows past 10 ms.
const PASS_BUCKETS: [f64; 10] = [
    0.0001, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.1, 0.5, 1.0,
];

pub struct Instruments {
    write_duration: Histogram<f64>,
    pass_duration: Histogram<f64>,
    reports_read: Counter<u64>,
    reports_drained: Counter<u64>,
    slot_replacements: Counter<u64>,
}

impl Instruments {
    pub fn new(meter: &Meter) -> Self {
        Self {
            write_duration: meter
                .f64_histogram("rust_hid_output_write_duration_seconds")
                .with_description("Wall time of one output HID write")
                .with_unit("s")
                .with_boundaries(WRITE_BUCKETS.to_vec())
                .build(),
            pass_duration: meter
                .f64_histogram("rust_hid_device_loop_pass_seconds")
                .with_description("Work time of one device-loop pass, without the sleep")
                .with_unit("s")
                .with_boundaries(PASS_BUCKETS.to_vec())
                .build(),
            reports_read: meter
                .u64_counter("rust_hid_input_reports_read_total")
                .with_description("Input reports read from a controller")
                .build(),
            reports_drained: meter
                .u64_counter("rust_hid_input_reports_drained_total")
                .with_description("Input reports dropped in a drain for a newer one")
                .build(),
            slot_replacements: meter
                .u64_counter("rust_hid_input_slot_replacements_total")
                .with_description("Samples replaced in the input slot before the stream took them")
                .build(),
        }
    }

    pub fn record_write(&self, serial: &str, took: Duration, ok: bool) {
        self.write_duration.record(
            took.as_secs_f64(),
            &[
                KeyValue::new("serial", serial.to_string()),
                KeyValue::new("result", if ok { "ok" } else { "error" }),
            ],
        );
    }

    pub fn record_pass(&self, took: Duration) {
        self.pass_duration.record(took.as_secs_f64(), &[]);
    }

    /// One drain of one controller: `read` reports came off the device.
    pub fn record_drain(&self, serial: &str, read: u64) {
        if read == 0 {
            return;
        }
        self.reports_read
            .add(read, &[KeyValue::new("serial", serial.to_string())]);
        if read > 1 {
            self.reports_drained.add(read - 1, &[]);
        }
    }

    pub fn record_slot_replacement(&self) {
        self.slot_replacements.add(1, &[]);
    }
}

static INSTALLED: OnceLock<Instruments> = OnceLock::new();

/// Create the instruments on `meter`; the first call wins.
pub fn install(meter: &Meter) {
    let _ = INSTALLED.set(Instruments::new(meter));
}

pub fn record_write(serial: &str, took: Duration, ok: bool) {
    if let Some(i) = INSTALLED.get() {
        i.record_write(serial, took, ok);
    }
}

pub fn record_pass(took: Duration) {
    if let Some(i) = INSTALLED.get() {
        i.record_pass(took);
    }
}

pub fn record_drain(serial: &str, read: u64) {
    if let Some(i) = INSTALLED.get() {
        i.record_drain(serial, read);
    }
}

pub fn record_slot_replacement() {
    if let Some(i) = INSTALLED.get() {
        i.record_slot_replacement();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hid::{HidDeviceIo, INPUT_REPORT_SIZE};
    use opentelemetry::metrics::MeterProvider;
    use opentelemetry_sdk::metrics::{InMemoryMetricExporter, SdkMeterProvider};
    use std::collections::BTreeSet;

    fn names(exporter: &InMemoryMetricExporter) -> BTreeSet<String> {
        exporter
            .get_finished_metrics()
            .unwrap()
            .iter()
            .flat_map(|rm| rm.scope_metrics())
            .flat_map(|sm| sm.metrics())
            .map(|m| m.name().to_string())
            .collect()
    }

    struct OneReportDevice(std::cell::Cell<u32>);
    impl HidDeviceIo for OneReportDevice {
        fn read(&self, buf: &mut [u8]) -> hidapi::HidResult<usize> {
            if self.0.get() == 0 {
                return Ok(0);
            }
            self.0.set(self.0.get() - 1);
            buf[..INPUT_REPORT_SIZE].fill(0);
            Ok(INPUT_REPORT_SIZE)
        }
        fn write(&self, data: &[u8]) -> hidapi::HidResult<usize> {
            Ok(data.len())
        }
    }

    #[test]
    fn test_every_new_metric_is_exported_once_recorded() {
        let exporter = InMemoryMetricExporter::default();
        let provider = SdkMeterProvider::builder()
            .with_periodic_exporter(exporter.clone())
            .build();
        let i = Instruments::new(&provider.meter("test"));

        i.record_write("aa", Duration::from_millis(3), true);
        i.record_write("aa", Duration::from_millis(700), false);
        i.record_pass(Duration::from_micros(200));
        i.record_drain("aa", 3);
        i.record_slot_replacement();
        provider.force_flush().unwrap();

        let got = names(&exporter);
        for want in [
            "rust_hid_output_write_duration_seconds",
            "rust_hid_device_loop_pass_seconds",
            "rust_hid_input_reports_read_total",
            "rust_hid_input_reports_drained_total",
            "rust_hid_input_slot_replacements_total",
        ] {
            assert!(got.contains(want), "{want} missing from {got:?}");
        }
    }

    #[test]
    fn test_a_drain_counts_the_superseded_reports() {
        let device = OneReportDevice(std::cell::Cell::new(4));
        let drain = crate::hid::read_latest_input(&device, false);
        assert_eq!(drain.reports_read, 4);
        assert!(drain.report.is_some());
    }
}
