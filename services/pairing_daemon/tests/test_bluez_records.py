"""BlueZ device record for a controller BlueZ does not know (UC-001 A7, A8, BR-008; #1466)."""

import configparser
import stat
from pathlib import Path

import pytest

from psmove_pairing.bluez_records import (
    HID_SDP_RECORD,
    RecordOutcome,
    write_device_record,
)
from psmove_pairing.registrations import _saved_registration_is_psmove

ADAPTER = "AA:BB:CC:DD:EE:01"
CONTROLLER = "00:06:F7:AA:BB:CC"


@pytest.fixture
def bluetooth_dir(tmp_path: Path) -> Path:
    (tmp_path / ADAPTER).mkdir(mode=0o700)
    return tmp_path


def _read(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # BlueZ keys are case-sensitive
    parser.read(path)
    return parser


class TestWriteDeviceRecord:
    @pytest.mark.uc("UC-001", flow="A7", br=["BR-008"])
    @pytest.mark.parametrize("product_id", [0x03D5, 0x0C5E])
    def test_writes_trusted_info_and_hid_sdp_cache(self, bluetooth_dir, product_id):
        outcome = write_device_record(ADAPTER, CONTROLLER, product_id, bluetooth_dir)

        assert outcome is RecordOutcome.WRITTEN
        assert outcome.needs_reload
        info = _read(bluetooth_dir / ADAPTER / CONTROLLER / "info")
        assert dict(info["General"]) == {
            "Name": "Motion Controller",
            "Class": "0x002508",
            "SupportedTechnologies": "BR/EDR;",
            "Trusted": "true",
            "Blocked": "false",
            "Services": "00001124-0000-1000-8000-00805f9b34fb;",
        }
        assert dict(info["DeviceID"]) == {
            "Source": "1",
            "Vendor": "1356",
            "Product": str(product_id),
            "Version": "1",
        }
        cache = _read(bluetooth_dir / ADAPTER / "cache" / CONTROLLER)
        assert cache["ServiceRecords"]["0x00010000"] == HID_SDP_RECORD

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-008"])
    def test_record_counts_as_a_saved_ps_move_registration(self, bluetooth_dir):
        """The registration reset and unpair recognise it (same matcher, UC-005 BR-001)."""
        write_device_record(ADAPTER, CONTROLLER, 0x03D5, bluetooth_dir)

        assert _saved_registration_is_psmove(bluetooth_dir / ADAPTER / CONTROLLER / "info")

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-008"])
    def test_files_are_private_like_bluez_storage(self, bluetooth_dir):
        write_device_record(ADAPTER, CONTROLLER, 0x03D5, bluetooth_dir)

        def mode(path: Path) -> int:
            return stat.S_IMODE(path.stat().st_mode)

        assert mode(bluetooth_dir / ADAPTER / CONTROLLER) == 0o700
        assert mode(bluetooth_dir / ADAPTER / CONTROLLER / "info") == 0o600
        assert mode(bluetooth_dir / ADAPTER / "cache" / CONTROLLER) == 0o600

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-008"])
    def test_address_case_is_normalised(self, bluetooth_dir):
        write_device_record(ADAPTER.lower(), CONTROLLER.lower(), 0x03D5, bluetooth_dir)

        assert (bluetooth_dir / ADAPTER / CONTROLLER / "info").is_file()

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-008"])
    def test_existing_info_is_never_overwritten(self, bluetooth_dir):
        """It may hold BlueZ's own pairing data (a ZCM2 link key); still not loaded -> reload."""
        device_dir = bluetooth_dir / ADAPTER / CONTROLLER
        device_dir.mkdir()
        own = "[General]\nName=Motion Controller\n\n[LinkKey]\nKey=0123456789ABCDEF0123456789ABCDEF\n"
        (device_dir / "info").write_text(own)

        outcome = write_device_record(ADAPTER, CONTROLLER, 0x0C5E, bluetooth_dir)

        assert outcome is RecordOutcome.EXISTS
        assert outcome.needs_reload
        assert (device_dir / "info").read_text() == own

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-008"])
    def test_existing_cache_is_kept(self, bluetooth_dir):
        cache_dir = bluetooth_dir / ADAPTER / "cache"
        cache_dir.mkdir()
        (cache_dir / CONTROLLER).write_text("[General]\nName=Motion Controller\n")

        assert write_device_record(ADAPTER, CONTROLLER, 0x03D5, bluetooth_dir) is RecordOutcome.WRITTEN
        assert (cache_dir / CONTROLLER).read_text() == "[General]\nName=Motion Controller\n"

    @pytest.mark.uc("UC-001", flow="A8", br=["BR-006", "BR-008"])
    @pytest.mark.parametrize("product_id", [0, 0x042F, 0x05C4])
    def test_only_ps_moves_get_a_record(self, bluetooth_dir, product_id):
        """Unknown product (e.g. an older rust-hid without product_id), Navigation, DualShock: nothing written."""
        outcome = write_device_record(ADAPTER, CONTROLLER, product_id, bluetooth_dir)

        assert outcome is RecordOutcome.UNSUPPORTED
        assert not outcome.needs_reload
        assert list((bluetooth_dir / ADAPTER).iterdir()) == []

    @pytest.mark.uc("UC-001", flow="A8", br=["BR-008"])
    def test_no_storage_directory_for_the_adapter(self, tmp_path):
        """BlueZ creates /var/lib/bluetooth/<adapter>; the daemon never invents one."""
        outcome = write_device_record(ADAPTER, CONTROLLER, 0x03D5, tmp_path)

        assert outcome is RecordOutcome.NO_ADAPTER_STORAGE
        assert not outcome.needs_reload
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.uc("UC-001", flow="A8", br=["BR-008"])
    @pytest.mark.parametrize(("adapter", "controller"), [("../etc", CONTROLLER), (ADAPTER, "../../x"), (ADAPTER, "")])
    def test_rejects_anything_but_bluetooth_addresses(self, bluetooth_dir, adapter, controller):
        outcome = write_device_record(adapter, controller, 0x03D5, bluetooth_dir)

        assert outcome is RecordOutcome.FAILED
        assert list((bluetooth_dir / ADAPTER).iterdir()) == []

    @pytest.mark.uc("UC-001", flow="A8", br=["BR-008"])
    def test_write_error_is_reported_not_raised(self, bluetooth_dir):
        (bluetooth_dir / ADAPTER).chmod(0o500)  # read-only adapter directory
        try:
            outcome = write_device_record(ADAPTER, CONTROLLER, 0x03D5, bluetooth_dir)
        finally:
            (bluetooth_dir / ADAPTER).chmod(0o700)

        if outcome is RecordOutcome.WRITTEN:  # running as root ignores the mode
            pytest.skip("root can write a read-only directory")
        assert outcome is RecordOutcome.FAILED
        assert not outcome.needs_reload
