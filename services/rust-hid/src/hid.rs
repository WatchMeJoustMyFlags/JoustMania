//! HID protocol for PS Move controllers.
//!
//! Ports the USB pairing subset of `lib/psmove_hid.py`:
//! - Enumerate USB-connected PS Move controllers
//! - Read/write BT addresses via feature reports 0x04/0x05
//! - MAC address conversion (LSB-first bytes <-> colon string)

use hidapi::HidApi;
use tracing::{debug, warn};

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
}
