//! HID protocol for PS Move controllers.
//!
//! Ports `lib/psmove_hid.py`:
//! - Enumerate USB-connected PS Move controllers
//! - Read/write BT addresses via feature reports 0x04/0x05
//! - MAC address conversion (LSB-first bytes <-> colon string)
//! - Input report parsing (49 bytes: buttons, trigger, accel, gyro, battery)
//! - Output report building (9 bytes, report 0x06: LED RGB, rumble)
//! - Battery level conversion

use hidapi::{HidApi, HidDevice};
use std::path::Path;
use tracing::{debug, info, warn};

// PS Move USB vendor/product IDs
const VENDOR_ID: u16 = 0x054C; // Sony
const PRODUCT_ID_ZCM1: u16 = 0x03D5; // PS Move (PS3 era)
const PRODUCT_ID_ZCM2: u16 = 0x042F; // PS Move (PS4 era)
const PRODUCT_ID_ZCM2E: u16 = 0x0C5E; // PS Move (PS4 era variant)
const ALL_PRODUCT_IDS: [u16; 3] = [PRODUCT_ID_ZCM1, PRODUCT_ID_ZCM2, PRODUCT_ID_ZCM2E];

// Feature report IDs and sizes (USB pairing)
const FEATURE_REPORT_GET_BTADDR: u8 = 0x04;
const FEATURE_REPORT_SET_BTADDR: u8 = 0x05;
const FEATURE_REPORT_GET_SIZE: usize = 16;
const FEATURE_REPORT_SET_SIZE: usize = 23;

// Controller I/O report sizes
pub const INPUT_REPORT_SIZE: usize = 49;
const OUTPUT_REPORT_SIZE: usize = 9;
const OUTPUT_REPORT_ID: u8 = 0x06; // SetLEDs, works for all PS Move models

// Accelerometer scale factor: raw ~4096 per 1g
const ACCEL_SCALE: f32 = 4096.0;

/// Button bitmask values (matches Python Button IntFlag).
pub struct Button;
impl Button {
    pub const TRIANGLE: u32 = 0x0010;
    pub const CIRCLE: u32 = 0x0020;
    pub const CROSS: u32 = 0x0040;
    pub const SQUARE: u32 = 0x0080;
    pub const SELECT: u32 = 0x0100;
    pub const START: u32 = 0x0800;
    pub const PS: u32 = 0x010000;
    pub const MOVE: u32 = 0x080000;
    pub const T: u32 = 0x100000; // Trigger (digital)
}

/// A USB-connected PS Move controller.
#[derive(Debug, Clone)]
pub struct UsbController {
    pub device_path: String,
    pub serial: String,
}

/// Result of reading the current BT address configuration from a controller.
#[derive(Debug)]
pub struct BtAddrInfo {
    pub controller_mac: String,
    pub host_mac: String,
}

/// Convert 6 LSB-first bytes to "AA:BB:CC:DD:EE:FF".
///
/// The PS Move stores MAC addresses in reverse byte order.
pub fn mac_bytes_to_string(data: &[u8]) -> Result<String, String> {
    if data.len() < 6 {
        return Err(format!(
            "MAC address needs at least 6 bytes, got {}",
            data.len()
        ));
    }
    let reversed: Vec<u8> = data[..6].iter().rev().copied().collect();
    Ok(reversed
        .iter()
        .map(|b| format!("{:02X}", b))
        .collect::<Vec<_>>()
        .join(":"))
}

/// Convert "AA:BB:CC:DD:EE:FF" to 6 LSB-first bytes.
pub fn mac_string_to_bytes(mac: &str) -> Result<[u8; 6], String> {
    let octets: Vec<u8> = mac
        .split(':')
        .map(|s| u8::from_str_radix(s, 16).map_err(|e| format!("Invalid MAC octet: {e}")))
        .collect::<Result<Vec<_>, _>>()?;

    if octets.len() != 6 {
        return Err(format!("Invalid MAC address: {mac}"));
    }

    let mut bytes = [0u8; 6];
    for (i, &b) in octets.iter().rev().enumerate() {
        bytes[i] = b;
    }
    Ok(bytes)
}

/// Parse a GET_FEATURE 0x04 report into controller and host MAC addresses.
///
/// Report layout (16 bytes):
///   [0x04, ctrl[0..5], pad(3B), host[0..5]]
pub fn parse_btaddr_report(data: &[u8]) -> Result<BtAddrInfo, String> {
    if data.len() < FEATURE_REPORT_GET_SIZE {
        return Err(format!(
            "Feature report too short: {} bytes (need {FEATURE_REPORT_GET_SIZE})",
            data.len()
        ));
    }
    let controller_mac = mac_bytes_to_string(&data[1..7])?;
    let host_mac = mac_bytes_to_string(&data[10..16])?;
    Ok(BtAddrInfo {
        controller_mac,
        host_mac,
    })
}

/// Build a SET_FEATURE 0x05 report to write a new host BT address.
///
/// Report layout (23 bytes):
///   [0x05, addr[0..5], 0x00 * 16]
pub fn build_set_btaddr_report(host_mac: &str) -> Result<[u8; FEATURE_REPORT_SET_SIZE], String> {
    let mac_bytes = mac_string_to_bytes(host_mac)?;
    let mut report = [0u8; FEATURE_REPORT_SET_SIZE];
    report[0] = FEATURE_REPORT_SET_BTADDR;
    report[1..7].copy_from_slice(&mac_bytes);
    Ok(report)
}

/// Enumerate USB-connected PS Move controllers.
///
/// Filters to USB-only devices (interface_number >= 0) and reads
/// each controller's BT MAC from feature report 0x04.
pub fn enumerate_usb_controllers(api: &HidApi) -> Vec<UsbController> {
    let mut controllers = Vec::new();

    for &pid in &ALL_PRODUCT_IDS {
        for dev_info in api.device_list() {
            if dev_info.vendor_id() != VENDOR_ID || dev_info.product_id() != pid {
                continue;
            }

            // Filter to USB only (Bluetooth devices have interface_number == -1)
            if dev_info.interface_number() < 0 {
                continue;
            }

            let path = dev_info.path().to_string_lossy().to_string();

            match api.open_path(dev_info.path()) {
                Ok(device) => {
                    let mut buf = [0u8; FEATURE_REPORT_GET_SIZE];
                    buf[0] = FEATURE_REPORT_GET_BTADDR;

                    match device.get_feature_report(&mut buf) {
                        Ok(len) if len >= FEATURE_REPORT_GET_SIZE => {
                            match parse_btaddr_report(&buf) {
                                Ok(info) => {
                                    let serial = info.controller_mac.to_uppercase();
                                    debug!(path = %path, serial = %serial, "USB controller found");
                                    controllers.push(UsbController {
                                        device_path: path,
                                        serial,
                                    });
                                }
                                Err(e) => {
                                    warn!(path = %path, error = %e, "Failed to parse feature report");
                                }
                            }
                        }
                        Ok(len) => {
                            warn!(path = %path, len, "Feature report too short");
                        }
                        Err(e) => {
                            warn!(path = %path, error = %e, "Failed to read feature report");
                        }
                    }
                }
                Err(e) => {
                    warn!(path = %path, error = %e, "Failed to open HID device");
                }
            }
        }
    }

    controllers
}

/// Pair a controller by writing the host BT address.
///
/// Opens the device by path, reads the current host, and writes the new
/// host address if different. Returns (success, already_paired, previous_host).
pub fn pair_controller(
    api: &HidApi,
    device_path: &str,
    adapter_address: &str,
) -> Result<(bool, String), String> {
    let c_path = std::ffi::CString::new(device_path)
        .map_err(|e| format!("Invalid device path: {e}"))?;

    let device = api
        .open_path(&c_path)
        .map_err(|e| format!("Failed to open {device_path}: {e}"))?;

    // Read current host address
    let mut buf = [0u8; FEATURE_REPORT_GET_SIZE];
    buf[0] = FEATURE_REPORT_GET_BTADDR;
    let len = device
        .get_feature_report(&mut buf)
        .map_err(|e| format!("Failed to read feature report: {e}"))?;

    if len < FEATURE_REPORT_GET_SIZE {
        return Err(format!("Feature report too short: {len} bytes"));
    }

    let info = parse_btaddr_report(&buf)?;
    let current_host = info.host_mac;

    let already_paired = current_host.eq_ignore_ascii_case(adapter_address);

    // Write new host address
    let set_report = build_set_btaddr_report(adapter_address)?;
    device
        .send_feature_report(&set_report)
        .map_err(|e| format!("Failed to write feature report: {e}"))?;

    Ok((already_paired, current_host))
}

// ---------------------------------------------------------------------------
// Controller I/O: input report parsing, output report building, battery
// ---------------------------------------------------------------------------

/// Parsed controller sensor data from a 49-byte input report.
#[derive(Debug, Clone)]
pub struct ParsedInputReport {
    pub buttons: u32,
    pub trigger: u8,         // Second frame analog trigger (0-255)
    pub battery: i32,        // Percentage (0-100)
    pub temperature: i32,
    pub accel_x: f32,        // Scaled, units of g
    pub accel_y: f32,
    pub accel_z: f32,
    pub gyro_x: f32,         // Raw units
    pub gyro_y: f32,
    pub gyro_z: f32,
}

impl ParsedInputReport {
    pub fn button_move(&self) -> bool { self.buttons & Button::MOVE != 0 }
    pub fn button_trigger(&self) -> bool { self.buttons & Button::T != 0 }
    pub fn button_ps(&self) -> bool { self.buttons & Button::PS != 0 }
    pub fn button_cross(&self) -> bool { self.buttons & Button::CROSS != 0 }
    pub fn button_circle(&self) -> bool { self.buttons & Button::CIRCLE != 0 }
    pub fn button_square(&self) -> bool { self.buttons & Button::SQUARE != 0 }
    pub fn button_triangle(&self) -> bool { self.buttons & Button::TRIANGLE != 0 }
    pub fn button_select(&self) -> bool { self.buttons & Button::SELECT != 0 }
    pub fn button_start(&self) -> bool { self.buttons & Button::START != 0 }
}

/// Convert battery raw value to percentage (0-100).
///
/// Matches Python `battery_to_percent()`.
pub fn battery_to_percent(raw: u8) -> i32 {
    match raw {
        0x00 => 0,
        0x01 => 20,
        0x02 => 40,
        0x03 => 60,
        0x04 => 80,
        0x05 => 100,
        0xEE => 100, // CHARGING
        0xEF => 100, // CHARGING_DONE
        _ => 50,     // Unknown
    }
}

/// Parse a 49-byte PS Move HID input report.
///
/// Matches Python `parse_input_report()` and psmoveapi's PSMove_Data_Input_Common.
/// Uses second frame of sensor data (bytes 19-24 for accel, 31-36 for gyro)
/// and second trigger value (byte 6), matching the HidapiAdapter convention.
///
/// The 49-byte report includes the report ID (0x01) at byte 0. Button bytes
/// are at offsets 1-4 and are combined using psmoveapi's formula:
///   buttons2 | (buttons1 << 8) | ((buttons3 & 0x01) << 16) | ((buttons4 & 0xF0) << 13)
///
/// `zcm2`: If true, decode accel/gyro as signed 16-bit two's complement
/// (ZCM2/PS4-era). If false, decode as unsigned 16-bit offset by 0x8000.
pub fn parse_input_report(data: &[u8], zcm2: bool) -> Result<ParsedInputReport, String> {
    // Strip redundant leading report ID byte if the HID library prepended one
    let data = if data.len() > INPUT_REPORT_SIZE && data[0] == 0x01 {
        &data[1..]
    } else {
        data
    };

    if data.len() < INPUT_REPORT_SIZE {
        return Err(format!(
            "Input report too short: {} bytes (need {INPUT_REPORT_SIZE})",
            data.len()
        ));
    }

    // Buttons: psmoveapi formula (PSMove_Data_Input_Common bytes 1-4)
    //   buttons2 | (buttons1 << 8) | ((buttons3 & 0x01) << 16) | ((buttons4 & 0xF0) << 13)
    let buttons = (data[2] as u32)                          // buttons2: face buttons
        | ((data[1] as u32) << 8)                           // buttons1: Select, Start
        | (((data[3] & 0x01) as u32) << 16)                 // buttons3 bit 0: PS
        | (((data[4] & 0xF0) as u32) << 13);                // buttons4 bits 4-7: Move, T

    // Trigger analog: byte 6 (frame 2)
    let trigger = data[6];

    // Battery: byte 12
    let battery = battery_to_percent(data[12]);

    // Second frame of sensor data (matching Python HidapiAdapter: parsed["accel"][1])
    let (ax, ay, az, gx, gy, gz) = if zcm2 {
        // ZCM2: signed 16-bit two's complement
        let ax = i16::from_le_bytes([data[19], data[20]]);
        let ay = i16::from_le_bytes([data[21], data[22]]);
        let az = i16::from_le_bytes([data[23], data[24]]);
        let gx = i16::from_le_bytes([data[31], data[32]]);
        let gy = i16::from_le_bytes([data[33], data[34]]);
        let gz = i16::from_le_bytes([data[35], data[36]]);
        (ax as i32, ay as i32, az as i32, gx as i32, gy as i32, gz as i32)
    } else {
        // ZCM1: unsigned 16-bit offset by 0x8000
        let ax = u16::from_le_bytes([data[19], data[20]]) as i32 - 0x8000;
        let ay = u16::from_le_bytes([data[21], data[22]]) as i32 - 0x8000;
        let az = u16::from_le_bytes([data[23], data[24]]) as i32 - 0x8000;
        let gx = u16::from_le_bytes([data[31], data[32]]) as i32 - 0x8000;
        let gy = u16::from_le_bytes([data[33], data[34]]) as i32 - 0x8000;
        let gz = u16::from_le_bytes([data[35], data[36]]) as i32 - 0x8000;
        (ax, ay, az, gx, gy, gz)
    };

    // Temperature: bytes 37-38 (12-bit value)
    let temp_raw = u16::from_le_bytes([data[37], data[38]]);
    let temperature = (temp_raw & 0x0FFF) as i32;

    Ok(ParsedInputReport {
        buttons,
        trigger,
        battery,
        temperature,
        accel_x: ax as f32 / ACCEL_SCALE,
        accel_y: ay as f32 / ACCEL_SCALE,
        accel_z: az as f32 / ACCEL_SCALE,
        gyro_x: gx as f32,
        gyro_y: gy as f32,
        gyro_z: gz as f32,
    })
}

/// Build a 9-byte PS Move output report (report 0x06) for LED and rumble.
///
/// Layout: [0x06, 0x00, R, G, B, 0x00, rumble, 0x00, 0x00]
pub fn build_output_report(r: u8, g: u8, b: u8, rumble: u8) -> [u8; OUTPUT_REPORT_SIZE] {
    let mut report = [0u8; OUTPUT_REPORT_SIZE];
    report[0] = OUTPUT_REPORT_ID;
    report[2] = r;
    report[3] = g;
    report[4] = b;
    report[6] = rumble;
    report
}

/// Normalize a serial to canonical format (uppercase, no colons).
///
/// MAC-format serials (12 hex chars) are uppercased and stripped of colons.
/// Non-MAC serials (e.g. mock controller IDs) are returned unchanged.
pub fn normalize_serial(serial: &str) -> String {
    let cleaned: String = serial.to_uppercase().replace(':', "");
    if cleaned.len() == 12 && cleaned.chars().all(|c| c.is_ascii_hexdigit()) {
        cleaned
    } else {
        serial.to_string()
    }
}

/// Check if a product ID is a ZCM2 (PS4-era) controller.
pub fn is_zcm2(product_id: u16) -> bool {
    product_id == PRODUCT_ID_ZCM2 || product_id == PRODUCT_ID_ZCM2E
}

/// Resolve the Bluetooth adapter name from a hidraw device path via sysfs.
///
/// For Bluetooth HID devices, the resolved sysfs path includes the
/// adapter name: `.../bluetooth/hci0/hci0:XX/.../hidraw/hidrawN`
///
/// Port of `bt_discovery.py:_resolve_adapter_from_sysfs()`.
pub fn resolve_adapter_from_sysfs(hidraw_path: &str) -> Option<String> {
    // Extract hidraw name from path (e.g. "/dev/hidraw3" -> "hidraw3")
    let hidraw_name = Path::new(hidraw_path).file_name()?.to_str()?;
    let sysfs = Path::new("/sys/class/hidraw").join(hidraw_name);
    if !sysfs.exists() {
        return None;
    }
    let resolved = sysfs.canonicalize().ok()?;
    let resolved_str = resolved.to_str()?;
    for part in resolved_str.split('/') {
        if part.starts_with("hci") && !part.contains(':') {
            return Some(part.to_string());
        }
    }
    None
}

/// Discover PS Move controllers via HID enumeration.
///
/// Opens each discovered device, sets it to non-blocking mode, and
/// returns a list of (serial, HidDevice, product_id, adapter_name) tuples.
/// `adapter_name` is resolved from sysfs (e.g. "hci0") or None for USB.
pub fn discover_controllers(api: &HidApi) -> Vec<(String, HidDevice, u16, Option<String>)> {
    let mut results = Vec::new();

    for &pid in &ALL_PRODUCT_IDS {
        for dev_info in api.device_list() {
            if dev_info.vendor_id() != VENDOR_ID || dev_info.product_id() != pid {
                continue;
            }

            let serial_raw = dev_info.serial_number().unwrap_or_default();
            if serial_raw.is_empty() {
                continue;
            }
            let serial = normalize_serial(serial_raw);

            match api.open_path(dev_info.path()) {
                Ok(device) => {
                    if let Err(e) = device.set_blocking_mode(false) {
                        warn!(serial = %serial, error = %e, "Failed to set non-blocking");
                        continue;
                    }
                    let path = dev_info.path().to_string_lossy();
                    let adapter_name = resolve_adapter_from_sysfs(&path);
                    info!(serial = %serial, path = %path, product_id = pid, adapter = ?adapter_name, "Discovered controller");
                    results.push((serial, device, pid, adapter_name));
                }
                Err(e) => {
                    warn!(serial = %serial, error = %e, "Failed to open controller");
                }
            }
        }
    }

    results
}

/// Open a specific controller by serial via HID enumeration.
///
/// Enumerates devices, finds one matching the serial, opens it in
/// non-blocking mode, and returns (HidDevice, product_id).
pub fn open_controller_by_serial(api: &HidApi, target_serial: &str) -> Option<(HidDevice, u16)> {
    let target = normalize_serial(target_serial);

    for &pid in &ALL_PRODUCT_IDS {
        for dev_info in api.device_list() {
            if dev_info.vendor_id() != VENDOR_ID || dev_info.product_id() != pid {
                continue;
            }

            let serial_raw = dev_info.serial_number().unwrap_or_default();
            if serial_raw.is_empty() {
                continue;
            }
            let serial = normalize_serial(serial_raw);

            if serial != target {
                continue;
            }

            match api.open_path(dev_info.path()) {
                Ok(device) => {
                    if let Err(e) = device.set_blocking_mode(false) {
                        warn!(serial = %serial, error = %e, "Failed to set non-blocking");
                        return None;
                    }
                    return Some((device, pid));
                }
                Err(e) => {
                    warn!(serial = %serial, error = %e, "Failed to open controller");
                    return None;
                }
            }
        }
    }

    None
}

/// Read the latest input report from a device (non-blocking).
///
/// Drains all available reports and returns the most recent one,
/// matching the Python HidapiAdapter.poll() pattern.
pub fn read_latest_input(device: &HidDevice, zcm2: bool) -> Option<ParsedInputReport> {
    let mut latest: Option<[u8; INPUT_REPORT_SIZE]> = None;
    let mut buf = [0u8; INPUT_REPORT_SIZE];

    loop {
        match device.read(&mut buf) {
            Ok(len) if len >= INPUT_REPORT_SIZE => {
                latest = Some(buf);
            }
            Ok(_) => break, // Short read or no data
            Err(_) => break,
        }
    }

    latest.and_then(|data| parse_input_report(&data, zcm2).ok())
}

#[cfg(test)]
mod tests {
    use super::*;

    // --- MAC address conversion tests ---

    #[test]
    fn test_mac_bytes_to_string_known() {
        // LSB-first: FF EE DD CC BB AA -> "AA:BB:CC:DD:EE:FF"
        assert_eq!(
            mac_bytes_to_string(&[0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA]).unwrap(),
            "AA:BB:CC:DD:EE:FF"
        );
    }

    #[test]
    fn test_mac_bytes_to_string_all_zeros() {
        assert_eq!(
            mac_bytes_to_string(&[0x00, 0x00, 0x00, 0x00, 0x00, 0x00]).unwrap(),
            "00:00:00:00:00:00"
        );
    }

    #[test]
    fn test_mac_bytes_to_string_broadcast() {
        assert_eq!(
            mac_bytes_to_string(&[0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]).unwrap(),
            "FF:FF:FF:FF:FF:FF"
        );
    }

    #[test]
    fn test_mac_bytes_to_string_sony_prefix() {
        // Sony PS Move prefix 00:06:F7:AA:BB:CC -> LSB: CC BB AA F7 06 00
        assert_eq!(
            mac_bytes_to_string(&[0xCC, 0xBB, 0xAA, 0xF7, 0x06, 0x00]).unwrap(),
            "00:06:F7:AA:BB:CC"
        );
    }

    #[test]
    fn test_mac_string_to_bytes_known() {
        assert_eq!(
            mac_string_to_bytes("AA:BB:CC:DD:EE:FF").unwrap(),
            [0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA]
        );
    }

    #[test]
    fn test_mac_string_to_bytes_all_zeros() {
        assert_eq!(
            mac_string_to_bytes("00:00:00:00:00:00").unwrap(),
            [0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        );
    }

    #[test]
    fn test_mac_string_to_bytes_broadcast() {
        assert_eq!(
            mac_string_to_bytes("FF:FF:FF:FF:FF:FF").unwrap(),
            [0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]
        );
    }

    #[test]
    fn test_mac_string_to_bytes_lowercase() {
        assert_eq!(
            mac_string_to_bytes("aa:bb:cc:dd:ee:ff").unwrap(),
            [0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA]
        );
    }

    #[test]
    fn test_mac_string_to_bytes_invalid_short() {
        assert!(mac_string_to_bytes("AA:BB:CC").is_err());
    }

    #[test]
    fn test_mac_roundtrip_bytes_to_string() {
        let original = [0x11u8, 0x22, 0x33, 0x44, 0x55, 0x66];
        let mac_str = mac_bytes_to_string(&original).unwrap();
        assert_eq!(mac_string_to_bytes(&mac_str).unwrap(), original);
    }

    #[test]
    fn test_mac_roundtrip_string_to_bytes() {
        let original = "DC:A6:32:AA:BB:CC";
        let mac_bytes = mac_string_to_bytes(original).unwrap();
        assert_eq!(mac_bytes_to_string(&mac_bytes).unwrap(), original);
    }

    // --- Feature report 0x04 parsing tests ---

    #[test]
    fn test_parse_btaddr_report_known() {
        // Controller: 00:06:F7:AA:BB:CC -> LSB: CC BB AA F7 06 00
        // Host: DC:A6:32:11:22:33 -> LSB: 33 22 11 32 A6 DC
        let mut report = [0u8; 16];
        report[0] = 0x04;
        report[1..7].copy_from_slice(&[0xCC, 0xBB, 0xAA, 0xF7, 0x06, 0x00]);
        // 7-9: padding
        report[10..16].copy_from_slice(&[0x33, 0x22, 0x11, 0x32, 0xA6, 0xDC]);

        let info = parse_btaddr_report(&report).unwrap();
        assert_eq!(info.controller_mac, "00:06:F7:AA:BB:CC");
        assert_eq!(info.host_mac, "DC:A6:32:11:22:33");
    }

    #[test]
    fn test_parse_btaddr_report_all_zeros() {
        let report = [0u8; 16];
        let info = parse_btaddr_report(&report).unwrap();
        assert_eq!(info.controller_mac, "00:00:00:00:00:00");
        assert_eq!(info.host_mac, "00:00:00:00:00:00");
    }

    #[test]
    fn test_parse_btaddr_report_too_short() {
        assert!(parse_btaddr_report(&[0x04, 0x00, 0x00]).is_err());
    }

    // Matches Python conftest.py SAMPLE_FEATURE_REPORT
    #[test]
    fn test_parse_btaddr_report_sample() {
        let report: [u8; 16] = [
            0x04, 0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA, 0x00, 0x00, 0x00, 0x66, 0x55, 0x44, 0x33,
            0x22, 0x11,
        ];
        let info = parse_btaddr_report(&report).unwrap();
        assert_eq!(info.controller_mac, "AA:BB:CC:DD:EE:FF");
        assert_eq!(info.host_mac, "11:22:33:44:55:66");
    }

    // --- Feature report 0x05 building tests ---

    #[test]
    fn test_build_set_btaddr_report_structure() {
        let report = build_set_btaddr_report("DC:A6:32:11:22:33").unwrap();
        assert_eq!(report.len(), FEATURE_REPORT_SET_SIZE);
        assert_eq!(report[0], 0x05);
        assert_eq!(&report[1..7], &[0x33, 0x22, 0x11, 0x32, 0xA6, 0xDC]);
        assert_eq!(&report[7..], &[0u8; 16]);
    }

    #[test]
    fn test_build_set_btaddr_report_all_zeros() {
        let report = build_set_btaddr_report("00:00:00:00:00:00").unwrap();
        let mut expected = [0u8; FEATURE_REPORT_SET_SIZE];
        expected[0] = 0x05;
        assert_eq!(report, expected);
    }

    #[test]
    fn test_build_set_btaddr_report_broadcast() {
        let report = build_set_btaddr_report("FF:FF:FF:FF:FF:FF").unwrap();
        assert_eq!(report[0], 0x05);
        assert_eq!(&report[1..7], &[0xFF; 6]);
        assert_eq!(&report[7..], &[0u8; 16]);
    }

    // --- Input report parsing tests ---

    /// Build a test input report with the psmoveapi byte layout.
    ///
    /// Encodes buttons using psmoveapi's PSMove_Data_Input_Common format:
    ///   Byte 0: report ID (0x01)
    ///   Byte 1: buttons1 (bits 8-15 of button mask)
    ///   Byte 2: buttons2 (bits 0-7 of button mask)
    ///   Byte 3: buttons3 (PS = bit 0)
    ///   Byte 4: buttons4 (Move/T in high nibble, seq in low nibble)
    fn make_input_report(
        buttons: u32,
        trigger2: u8,
        battery: u8,
        accel2: (u16, u16, u16),
        gyro2: (u16, u16, u16),
        temperature: u16,
    ) -> [u8; INPUT_REPORT_SIZE] {
        let mut data = [0u8; INPUT_REPORT_SIZE];
        data[0] = 0x01; // Report ID
        // Reverse the psmoveapi formula to encode buttons into raw bytes:
        //   result = buttons2 | (buttons1 << 8) | ((buttons3 & 0x01) << 16) | ((buttons4 & 0xF0) << 13)
        // So: buttons2 = result & 0xFF, buttons1 = (result >> 8) & 0xFF
        //     buttons3 bit 0 = (result >> 16) & 0x01
        //     buttons4 high nibble = (result >> 13) & 0xF0
        data[1] = ((buttons >> 8) & 0xFF) as u8;        // buttons1
        data[2] = (buttons & 0xFF) as u8;                // buttons2
        data[3] = ((buttons >> 16) & 0x01) as u8;        // buttons3
        data[4] = ((buttons >> 13) & 0xF0) as u8;        // buttons4 high nibble
        // Trigger frame 2: byte 6
        data[6] = trigger2;
        // Battery: byte 12
        data[12] = battery;
        // Accel frame 2: bytes 19-24 (little-endian u16)
        data[19..21].copy_from_slice(&accel2.0.to_le_bytes());
        data[21..23].copy_from_slice(&accel2.1.to_le_bytes());
        data[23..25].copy_from_slice(&accel2.2.to_le_bytes());
        // Gyro frame 2: bytes 31-36
        data[31..33].copy_from_slice(&gyro2.0.to_le_bytes());
        data[33..35].copy_from_slice(&gyro2.1.to_le_bytes());
        data[35..37].copy_from_slice(&gyro2.2.to_le_bytes());
        // Temperature: bytes 37-38
        data[37..39].copy_from_slice(&temperature.to_le_bytes());
        data
    }

    #[test]
    fn test_parse_input_report_zcm1_buttons() {
        let data = make_input_report(
            Button::MOVE | Button::T | Button::CROSS,
            128,  // trigger
            0x05, // battery MAX
            (0x8000, 0x8000, 0x9000), // accel at 0, 0, ~1g
            (0x8000, 0x8000, 0x8000), // gyro all zero
            300, // temperature
        );
        let report = parse_input_report(&data, false).unwrap();
        assert!(report.button_move());
        assert!(report.button_trigger());
        assert!(report.button_cross());
        assert!(!report.button_ps());
        assert!(!report.button_circle());
        assert_eq!(report.trigger, 128);
        assert_eq!(report.battery, 100);
        assert_eq!(report.temperature, 300);
    }

    #[test]
    fn test_parse_input_report_zcm1_accel() {
        // 0x9000 = 36864 unsigned, offset by 0x8000 = 4096 raw
        // 4096 / ACCEL_SCALE(4096.0) = 1.0g
        let data = make_input_report(
            0, 0, 0,
            (0x8000, 0x8000, 0x9000),
            (0x8000, 0x8000, 0x8000),
            0,
        );
        let report = parse_input_report(&data, false).unwrap();
        assert!((report.accel_x).abs() < 0.001);
        assert!((report.accel_y).abs() < 0.001);
        assert!((report.accel_z - 1.0).abs() < 0.001);
    }

    #[test]
    fn test_parse_input_report_zcm2_signed() {
        // ZCM2 uses signed 16-bit: 4096 = 0x1000
        let ax: i16 = 0;
        let ay: i16 = 0;
        let az: i16 = 4096; // ~1g
        let data = make_input_report(
            0, 0, 0,
            (ax as u16, ay as u16, az as u16),
            (0, 0, 0),
            0,
        );
        let report = parse_input_report(&data, true).unwrap();
        assert!((report.accel_x).abs() < 0.001);
        assert!((report.accel_z - 1.0).abs() < 0.001);
    }

    #[test]
    fn test_parse_input_report_too_short() {
        let data = [0u8; 10];
        assert!(parse_input_report(&data, false).is_err());
    }

    #[test]
    fn test_parse_input_report_strips_report_id() {
        // 50 bytes with leading 0x01
        let mut data = [0u8; 50];
        data[0] = 0x01;
        // Rest is a valid 49-byte report (all zeros)
        let report = parse_input_report(&data, false).unwrap();
        assert_eq!(report.buttons, 0);
    }

    #[test]
    fn test_parse_input_report_all_buttons() {
        let all = Button::TRIANGLE | Button::CIRCLE | Button::CROSS | Button::SQUARE
            | Button::SELECT | Button::START | Button::PS | Button::MOVE | Button::T;
        let data = make_input_report(all, 255, 0, (0x8000, 0x8000, 0x8000), (0x8000, 0x8000, 0x8000), 0);
        let report = parse_input_report(&data, false).unwrap();
        assert!(report.button_triangle());
        assert!(report.button_circle());
        assert!(report.button_cross());
        assert!(report.button_square());
        assert!(report.button_select());
        assert!(report.button_start());
        assert!(report.button_ps());
        assert!(report.button_move());
        assert!(report.button_trigger());
    }

    // --- Battery conversion tests ---

    #[test]
    fn test_battery_to_percent_levels() {
        assert_eq!(battery_to_percent(0x00), 0);
        assert_eq!(battery_to_percent(0x01), 20);
        assert_eq!(battery_to_percent(0x02), 40);
        assert_eq!(battery_to_percent(0x03), 60);
        assert_eq!(battery_to_percent(0x04), 80);
        assert_eq!(battery_to_percent(0x05), 100);
        assert_eq!(battery_to_percent(0xEE), 100); // Charging
        assert_eq!(battery_to_percent(0xEF), 100); // Charging done
        assert_eq!(battery_to_percent(0xFF), 50);  // Unknown
    }

    // --- Output report tests ---

    #[test]
    fn test_build_output_report_structure() {
        let report = build_output_report(255, 128, 64, 200);
        assert_eq!(report.len(), OUTPUT_REPORT_SIZE);
        assert_eq!(report[0], 0x06); // Report ID
        assert_eq!(report[1], 0x00); // Reserved
        assert_eq!(report[2], 255);  // R
        assert_eq!(report[3], 128);  // G
        assert_eq!(report[4], 64);   // B
        assert_eq!(report[5], 0x00); // Reserved
        assert_eq!(report[6], 200);  // Rumble
        assert_eq!(report[7], 0x00); // Padding
        assert_eq!(report[8], 0x00); // Padding
    }

    #[test]
    fn test_build_output_report_all_zeros() {
        let report = build_output_report(0, 0, 0, 0);
        let mut expected = [0u8; OUTPUT_REPORT_SIZE];
        expected[0] = 0x06;
        assert_eq!(report, expected);
    }

    // --- Serial normalization tests ---

    #[test]
    fn test_normalize_serial_mac() {
        assert_eq!(normalize_serial("AA:BB:CC:DD:EE:FF"), "AABBCCDDEEFF");
        assert_eq!(normalize_serial("aa:bb:cc:dd:ee:ff"), "AABBCCDDEEFF");
        assert_eq!(normalize_serial("AABBCCDDEEFF"), "AABBCCDDEEFF");
    }

    #[test]
    fn test_normalize_serial_non_mac() {
        assert_eq!(normalize_serial("mock_controller_0"), "mock_controller_0");
    }

    #[test]
    fn test_is_zcm2_detection() {
        assert!(!is_zcm2(PRODUCT_ID_ZCM1));
        assert!(is_zcm2(PRODUCT_ID_ZCM2));
        assert!(is_zcm2(PRODUCT_ID_ZCM2E));
    }

    // --- sysfs adapter resolution tests ---

    #[test]
    fn test_resolve_adapter_from_sysfs_extracts_name() {
        // This test verifies the path parsing logic without real sysfs.
        // On a non-Linux system or without /sys, it returns None (sysfs not found).
        let result = resolve_adapter_from_sysfs("/dev/hidraw99");
        // We can't assert a specific adapter without real sysfs,
        // but we can verify it doesn't panic and returns None when sysfs absent.
        assert!(result.is_none() || result.as_deref().unwrap().starts_with("hci"));
    }

    #[test]
    fn test_resolve_adapter_from_sysfs_empty_path() {
        assert!(resolve_adapter_from_sysfs("").is_none());
    }

    #[test]
    fn test_resolve_adapter_from_sysfs_nonexistent_device() {
        assert!(resolve_adapter_from_sysfs("/dev/hidraw99999").is_none());
    }
}
