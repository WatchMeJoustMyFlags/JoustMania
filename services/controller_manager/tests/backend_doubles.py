"""Shared backend doubles: the servicer calls the mock-only hooks without probing."""

from unittest.mock import MagicMock, create_autospec

from services.controller_manager.backend import ControllerBackend
from services.controller_manager.mock_hooks import MockFeatureHooks


class InertBackendHooks(MockFeatureHooks):
    """Mix into a hand-written backend double to inherit the inert mock-only hooks."""

    def pattern_serials(self) -> set[str]:
        return set()


def autospec_backend():
    """An autospec'd ControllerBackend whose mock-only hooks are inert."""
    backend = create_autospec(ControllerBackend, instance=True)
    backend.pattern_serials.return_value = set()
    backend.pattern_frame.return_value = None
    backend.pattern_stream_rate.return_value = None
    backend.consume_trigger_press.return_value = False
    inert_hooks(backend)
    return backend


class AdapterAsBackend(InertBackendHooks):
    """Serve a MockAdapter's mock-only hooks through the backend interface the servicer calls."""

    def __init__(self, adapter):
        self._adapter = adapter

    def pattern_serials(self) -> set[str]:
        return set(self._adapter.pattern_serials)

    def consume_death_frame(self, serial):
        self._adapter.consume_death_frame(serial)

    def consume_trigger_press(self, serial):
        return self._adapter.consume_trigger_press(serial)

    def pattern_frame(self, serial, subscriber_id, explicit):
        return self._adapter.pattern_frame(serial, subscriber_id, explicit)

    def pattern_stream_rate(self, serials):
        return self._adapter.pattern_stream_rate(serials)

    def record_pattern_overrun(self, subscriber_id):
        return self._adapter.record_pattern_overrun(subscriber_id)

    def release_pattern_stream(self, subscriber_id):
        self._adapter.release_pattern_stream(subscriber_id)


def inert_hooks(backend):
    """Give a ``MagicMock(spec=[...])`` backend the inert defaults of MockFeatureHooks the discovery loop calls."""
    backend.get_adapter_type = MagicMock(return_value="")
    backend.get_controller_metadata = MagicMock(return_value=None)
    backend.rollout_summary = MagicMock(return_value=("", 0))
    backend.rollout_strategy = MagicMock(return_value="")
    backend.take_led_failures = MagicMock(return_value=0)
    return backend
