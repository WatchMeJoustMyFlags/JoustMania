"""Tests for flagd-based interval configuration."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from psmove_pairing.config import (
    _DEFAULT_BT_MONITOR_INTERVAL,
    _DEFAULT_POLL_INTERVAL,
    get_adapter_routing_default,
    get_bluez_restart_mode,
    get_bt_monitor_interval,
    get_poll_interval,
    get_restart_quiet_period,
)


class TestGetPollInterval:
    """Tests for get_poll_interval()."""

    def test_returns_default_when_no_flag_client(self):
        """Falls back to default when flagd is unavailable."""
        with patch("psmove_pairing.config._system_client", None):
            assert get_poll_interval() == _DEFAULT_POLL_INTERVAL

    def test_returns_flag_value(self):
        """Returns value from flagd when available."""
        mock_client = MagicMock()
        mock_client.get_integer_value.return_value = 5

        with patch("psmove_pairing.config._system_client", mock_client):
            result = get_poll_interval()

        assert result == 5
        # Verify correct flag key and default were passed
        args = mock_client.get_integer_value.call_args
        assert args[0][0] == "pairing.poll_interval"
        assert args[0][1] == _DEFAULT_POLL_INTERVAL

    def test_returns_default_on_exception(self):
        """Falls back to default when flag evaluation fails."""
        mock_client = MagicMock()
        mock_client.get_integer_value.side_effect = RuntimeError("flagd down")

        with patch("psmove_pairing.config._system_client", mock_client):
            assert get_poll_interval() == _DEFAULT_POLL_INTERVAL


class TestGetBtMonitorInterval:
    """Tests for get_bt_monitor_interval()."""

    def test_returns_default_when_no_flag_client(self):
        """Falls back to default when flagd is unavailable."""
        with patch("psmove_pairing.config._system_client", None):
            assert get_bt_monitor_interval() == _DEFAULT_BT_MONITOR_INTERVAL

    def test_returns_flag_value(self):
        """Returns value from flagd when available."""
        mock_client = MagicMock()
        mock_client.get_integer_value.return_value = 2

        with patch("psmove_pairing.config._system_client", mock_client):
            result = get_bt_monitor_interval()

        assert result == 2
        # Verify correct flag key and default were passed
        args = mock_client.get_integer_value.call_args
        assert args[0][0] == "pairing.bt_monitor_interval"
        assert args[0][1] == _DEFAULT_BT_MONITOR_INTERVAL

    def test_returns_default_on_exception(self):
        """Falls back to default when flag evaluation fails."""
        mock_client = MagicMock()
        mock_client.get_integer_value.side_effect = RuntimeError("flagd down")

        with patch("psmove_pairing.config._system_client", mock_client):
            assert get_bt_monitor_interval() == _DEFAULT_BT_MONITOR_INTERVAL


class TestGetAdapterRoutingDefault:
    """Tests for get_adapter_routing_default()."""

    @pytest.mark.uc("UC-001", flow="main", steps=[4], br=["BR-003"])
    def test_returns_rust_when_no_flag_client(self):
        """Falls back to 'rust' (the flag's default) when flagd is unavailable (#1371)."""
        with patch("psmove_pairing.config._controller_client", None):
            assert get_adapter_routing_default() == "rust"

    def test_returns_flag_value(self):
        """Returns value from flagd when available."""
        mock_client = MagicMock()
        mock_client.get_string_value.return_value = "python"

        with patch("psmove_pairing.config._controller_client", mock_client):
            result = get_adapter_routing_default()

        assert result == "python"
        args = mock_client.get_string_value.call_args
        assert args[0][0] == "bluetooth_backend"
        assert args[0][1] == "rust"

    @pytest.mark.uc("UC-001", flow="main", steps=[4], br=["BR-003"])
    def test_returns_rust_on_exception(self):
        """Falls back to 'rust' when flag evaluation fails."""
        mock_client = MagicMock()
        mock_client.get_string_value.side_effect = RuntimeError("flagd down")

        with patch("psmove_pairing.config._controller_client", mock_client):
            assert get_adapter_routing_default() == "rust"


class TestInitPerformanceFlags:
    """Tests for init_performance_flags()."""

    @patch("psmove_pairing.config.logger")
    def test_init_success(self, mock_logger):
        """Successfully initializes flag clients for both domains."""
        mock_client = MagicMock()

        with (
            patch("lib.feature_flags.init_flag_domain") as mock_init,
            patch("lib.feature_flags.get_flag_client", return_value=mock_client) as mock_get,
            patch("psmove_pairing.config._system_client", None),
            patch("psmove_pairing.config._controller_client", None),
        ):
            from psmove_pairing.config import init_performance_flags

            init_performance_flags()
            mock_init.assert_any_call("system")
            mock_init.assert_any_call("controller")
            mock_get.assert_any_call("system")
            mock_get.assert_any_call("controller")

    @patch("psmove_pairing.config.logger")
    def test_init_failure_uses_defaults(self, mock_logger):
        """Falls back to defaults when init fails."""
        with (
            patch("psmove_pairing.config._system_client", None),
            patch("psmove_pairing.config._controller_client", None),
        ):
            with patch.dict("sys.modules", {"lib.feature_flags": None}):
                from psmove_pairing.config import init_performance_flags

                # Should not raise, just log warning
                init_performance_flags()

            # Intervals should still return defaults
            assert get_poll_interval() == _DEFAULT_POLL_INTERVAL
            assert get_bt_monitor_interval() == _DEFAULT_BT_MONITOR_INTERVAL


class TestGetBluezRestartMode:
    """pairing.bluez_restart_mode (UC-001 BR-005): read at use time, fails closed to never."""

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    def test_never_when_no_flag_client(self):
        with patch("psmove_pairing.config._system_client", None):
            assert get_bluez_restart_mode() == "never"

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    @pytest.mark.parametrize("mode", ["never", "always", "when_idle"])
    def test_returns_flag_value(self, mode):
        mock_client = MagicMock()
        mock_client.get_string_value.return_value = mode
        with patch("psmove_pairing.config._system_client", mock_client):
            assert get_bluez_restart_mode() == mode
        assert mock_client.get_string_value.call_args[0][:2] == ("pairing.bluez_restart_mode", "never")

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    @pytest.mark.parametrize("value", ["", "sometimes", "ALWAYS"])
    def test_unknown_value_is_never(self, value):
        mock_client = MagicMock()
        mock_client.get_string_value.return_value = value
        with patch("psmove_pairing.config._system_client", mock_client):
            assert get_bluez_restart_mode() == "never"

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    def test_never_on_exception(self):
        mock_client = MagicMock()
        mock_client.get_string_value.side_effect = RuntimeError("flagd down")
        with patch("psmove_pairing.config._system_client", mock_client):
            assert get_bluez_restart_mode() == "never"

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005"])
    def test_shipped_flag_variants_match_the_daemon(self):
        """Every shipped variant is a mode the daemon knows; the default is when_idle (#1466).

        Only when_idle lets a never-seen controller connect without ever restarting
        bluetoothd during a real game; the code's fallback stays never (fail closed).
        """
        import json
        from pathlib import Path

        from psmove_pairing.config import BLUEZ_RESTART_MODES

        flagd_dir = Path(__file__).resolve().parents[3] / "services" / "flagd"
        for flag_file in (flagd_dir / "system.json", flagd_dir / "ci" / "system.json"):
            flag = json.loads(flag_file.read_text())["flags"]["pairing.bluez_restart_mode"]
            assert set(flag["variants"].values()) == set(BLUEZ_RESTART_MODES), flag_file
            assert flag["variants"][flag["defaultVariant"]] == "when_idle", flag_file


class TestGetRestartQuietPeriod:
    """pairing.restart_quiet_period (UC-001 BR-005, #2091): read at use time, 20 s when unusable."""

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005"])
    def test_default_when_no_flag_client(self):
        with patch("psmove_pairing.config._system_client", None):
            assert get_restart_quiet_period() == 20

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005"])
    @pytest.mark.parametrize("seconds", [0, 10, 60])
    def test_returns_flag_value(self, seconds):
        mock_client = MagicMock()
        mock_client.get_integer_value.return_value = seconds
        with patch("psmove_pairing.config._system_client", mock_client):
            assert get_restart_quiet_period() == seconds
        assert mock_client.get_integer_value.call_args[0][:2] == ("pairing.restart_quiet_period", 20)

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005"])
    def test_negative_value_is_the_default(self):
        mock_client = MagicMock()
        mock_client.get_integer_value.return_value = -5
        with patch("psmove_pairing.config._system_client", mock_client):
            assert get_restart_quiet_period() == 20

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005"])
    def test_default_on_exception(self):
        mock_client = MagicMock()
        mock_client.get_integer_value.side_effect = RuntimeError("flagd down")
        with patch("psmove_pairing.config._system_client", mock_client):
            assert get_restart_quiet_period() == 20

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005"])
    def test_shipped_flag_default_is_20s(self):
        flagd_dir = Path(__file__).resolve().parents[3] / "services" / "flagd"
        for flag_file in (flagd_dir / "system.json", flagd_dir / "ci" / "system.json"):
            flag = json.loads(flag_file.read_text())["flags"]["pairing.restart_quiet_period"]
            assert flag["variants"][flag["defaultVariant"]] == 20, flag_file
            assert all(v >= 0 for v in flag["variants"].values()), flag_file


@pytest.mark.nfr("NFR-007")
def test_nfr_007_default_cadence_is_10s_usb_and_5s_bluetooth():
    """NFR-007: with no flag value the daemon polls USB every 10 s and Bluetooth every 5 s.

    Literals, not the constants, so a changed default fails here; the shipped flagd
    default variants must agree with the code fallback.
    """
    with patch("psmove_pairing.config._system_client", None):
        assert get_poll_interval() == 10
        assert get_bt_monitor_interval() == 5

    flags = json.loads((Path(__file__).parents[3] / "services/flagd/system.json").read_text())["flags"]
    for key, expected in (("pairing.poll_interval", 10), ("pairing.bt_monitor_interval", 5)):
        flag = flags[key]
        assert flag["variants"][flag["defaultVariant"]] == expected
