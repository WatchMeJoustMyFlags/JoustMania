"""Shared backend doubles: the servicer calls the mock-only hooks without probing."""

from unittest.mock import MagicMock, create_autospec

import grpc
import grpc.aio
from openfeature.client import OpenFeatureClient
from opentelemetry import trace

from services.controller_manager.backend import ControllerBackend
from services.controller_manager.button_detector import ButtonDetector
from services.controller_manager.discovery import PeriodicRescanTimer
from services.controller_manager.discovery_loop import DiscoveryLoop
from services.controller_manager.event_publisher import EventPublisher
from services.controller_manager.feedback_manager import FeedbackManager
from services.controller_manager.mock_hooks import MockFeatureHooks
from services.controller_manager.monitoring import ControllerMonitoring
from services.controller_manager.multiplexer.adapter import ControllerIOAdapter


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
    backend.get_adapter_type.return_value = ""
    backend.get_controller_metadata.return_value = None
    backend.rollout_summary.return_value = ("", 0)
    backend.rollout_strategy.return_value = ""
    backend.take_led_failures.return_value = 0
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


def autospec_button_detector():
    """An autospec'd ButtonDetector: a call the real class does not have raises."""
    return create_autospec(ButtonDetector, instance=True)


def autospec_feedback_manager():
    """An autospec'd FeedbackManager."""
    return create_autospec(FeedbackManager, instance=True)


def autospec_monitoring():
    """An autospec'd ControllerMonitoring (``last_battery_check`` is an instance attribute, so seed it)."""
    monitoring = create_autospec(ControllerMonitoring, instance=True)
    monitoring.last_battery_check = 0
    return monitoring


def autospec_publisher():
    """An autospec'd EventPublisher."""
    return create_autospec(EventPublisher, instance=True)


def autospec_adapter():
    """An autospec'd ControllerIOAdapter."""
    return create_autospec(ControllerIOAdapter, instance=True)


def autospec_rescan_timer():
    """An autospec'd PeriodicRescanTimer."""
    return create_autospec(PeriodicRescanTimer, instance=True)


def autospec_discovery_loop():
    """An autospec'd DiscoveryLoop."""
    return create_autospec(DiscoveryLoop, instance=True)


def autospec_span():
    """An autospec'd OpenTelemetry Span."""
    return create_autospec(trace.Span, instance=True)


def autospec_flag_client():
    """An autospec'd OpenFeature client."""
    return create_autospec(OpenFeatureClient, instance=True)


def autospec_servicer_context():
    """An autospec'd grpc.aio.ServicerContext."""
    return create_autospec(grpc.aio.ServicerContext, instance=True)


def autospec_channel():
    """An autospec'd grpc.Channel."""
    return create_autospec(grpc.Channel, instance=True)
