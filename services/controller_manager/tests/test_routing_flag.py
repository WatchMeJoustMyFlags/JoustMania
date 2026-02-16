"""
Tests for routing flag listener in servicer.

Tests the flagd integration for forcing rescans when controller_adapter_routing changes.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

import services.controller_manager.servicer as servicer_mod


class TestInitRoutingListener:
    """Tests for init_routing_listener registration."""

    def _reset(self):
        self._orig_initialized = servicer_mod._routing_listener_initialized
        self._orig_timer = servicer_mod._routing_rescan_timer
        servicer_mod._routing_listener_initialized = False
        servicer_mod._routing_rescan_timer = None

    def _restore(self):
        servicer_mod._routing_listener_initialized = self._orig_initialized
        servicer_mod._routing_rescan_timer = self._orig_timer

    @patch("lib.feature_flags.get_flag_client")
    def test_registers_handler_on_performance_client(self, mock_get_client):
        self._reset()
        try:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            timer = MagicMock()

            servicer_mod.init_routing_listener(timer)

            mock_get_client.assert_called_once_with("performance")
            mock_client.add_handler.assert_called_once()
            # Verify it registered for PROVIDER_CONFIGURATION_CHANGED
            from openfeature.provider import ProviderEvent

            call_args = mock_client.add_handler.call_args
            assert call_args[0][0] == ProviderEvent.PROVIDER_CONFIGURATION_CHANGED
        finally:
            self._restore()

    @patch("lib.feature_flags.get_flag_client")
    def test_stores_rescan_timer(self, mock_get_client):
        self._reset()
        try:
            mock_get_client.return_value = MagicMock()
            timer = MagicMock()

            servicer_mod.init_routing_listener(timer)

            assert servicer_mod._routing_rescan_timer is timer
        finally:
            self._restore()

    @patch("lib.feature_flags.get_flag_client")
    def test_idempotent_only_registers_once(self, mock_get_client):
        self._reset()
        try:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            timer = MagicMock()

            servicer_mod.init_routing_listener(timer)
            servicer_mod.init_routing_listener(timer)

            assert mock_client.add_handler.call_count == 1
        finally:
            self._restore()


class TestOnRoutingFlagChanged:
    """Tests for _on_routing_flag_changed handler."""

    def _reset(self):
        self._orig_timer = servicer_mod._routing_rescan_timer
        servicer_mod._routing_rescan_timer = MagicMock()

    def _restore(self):
        servicer_mod._routing_rescan_timer = self._orig_timer

    def test_forces_rescan_when_routing_flag_changed(self):
        """Handler forces rescan when controller_adapter_routing is in changed flags."""
        self._reset()
        try:
            event = MagicMock()
            event.flags_changed = ["controller_adapter_routing"]

            servicer_mod._on_routing_flag_changed(event)

            servicer_mod._routing_rescan_timer.force_next_rescan.assert_called_once()
        finally:
            self._restore()

    def test_forces_rescan_when_routing_flag_among_others(self):
        """Handler forces rescan when our flag is among multiple changed flags."""
        self._reset()
        try:
            event = MagicMock()
            event.flags_changed = ["sensitivity_mode", "controller_adapter_routing", "update_frequency_hz"]

            servicer_mod._on_routing_flag_changed(event)

            servicer_mod._routing_rescan_timer.force_next_rescan.assert_called_once()
        finally:
            self._restore()

    def test_skips_when_other_flag_changed(self):
        """Handler skips rescan when a different flag changed."""
        self._reset()
        try:
            event = MagicMock()
            event.flags_changed = ["sensitivity_mode"]

            servicer_mod._on_routing_flag_changed(event)

            servicer_mod._routing_rescan_timer.force_next_rescan.assert_not_called()
        finally:
            self._restore()

    def test_forces_rescan_when_flags_changed_is_none(self):
        """Handler forces rescan when event has no flags_changed (full reload)."""
        self._reset()
        try:
            servicer_mod._on_routing_flag_changed(None)

            servicer_mod._routing_rescan_timer.force_next_rescan.assert_called_once()
        finally:
            self._restore()

    def test_no_crash_when_timer_is_none(self):
        """Handler gracefully handles missing timer."""
        self._reset()
        servicer_mod._routing_rescan_timer = None
        try:
            # Should not raise
            servicer_mod._on_routing_flag_changed(None)
        finally:
            self._restore()
