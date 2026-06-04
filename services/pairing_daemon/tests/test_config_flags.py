"""Tests for flagd-based interval configuration."""

from unittest.mock import MagicMock, patch

from psmove_pairing.config import (
    _DEFAULT_BT_MONITOR_INTERVAL,
    _DEFAULT_POLL_INTERVAL,
    get_adapter_routing_default,
    get_bt_monitor_interval,
    get_poll_interval,
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

    def test_returns_hidapi_when_no_flag_client(self):
        """Falls back to 'hidapi' when flagd is unavailable."""
        with patch("psmove_pairing.config._controller_client", None):
            assert get_adapter_routing_default() == "hidapi"

    def test_returns_flag_value(self):
        """Returns value from flagd when available."""
        mock_client = MagicMock()
        mock_client.get_string_value.return_value = "rust"

        with patch("psmove_pairing.config._controller_client", mock_client):
            result = get_adapter_routing_default()

        assert result == "rust"
        args = mock_client.get_string_value.call_args
        assert args[0][0] == "bluetooth_backend"
        assert args[0][1] == "hidapi"

    def test_returns_hidapi_on_exception(self):
        """Falls back to 'hidapi' when flag evaluation fails."""
        mock_client = MagicMock()
        mock_client.get_string_value.side_effect = RuntimeError("flagd down")

        with patch("psmove_pairing.config._controller_client", mock_client):
            assert get_adapter_routing_default() == "hidapi"


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
