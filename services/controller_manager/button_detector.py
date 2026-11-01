"""
Button transition detection for ControllerManager.

Detects button press/release transitions and publishes button events
to stream subscribers. Also handles connection/disconnection events.

Phase 41: Button event streaming.
"""

import asyncio
import logging
import time

from lib.controller_constants import ButtonKey, ButtonTrackingKey, ControllerInfoKey
from proto import controller_manager_pb2
from services.controller_manager import metrics
from services.controller_manager.event_publisher import EventPublisher

logger = logging.getLogger(__name__)

# Built once at import, not per 100 Hz call (#2353). Order is the event order within one poll:
# (tracking key, state-dict key, proto ButtonType).
_BUTTONS: tuple[tuple[ButtonTrackingKey, ButtonKey, int], ...] = (
    (ButtonTrackingKey.TRIGGER, ButtonKey.TRIGGER, controller_manager_pb2.BUTTON_TRIGGER),
    (ButtonTrackingKey.MOVE, ButtonKey.MOVE, controller_manager_pb2.BUTTON_MOVE),
    (ButtonTrackingKey.CROSS, ButtonKey.CROSS, controller_manager_pb2.BUTTON_CROSS),
    (ButtonTrackingKey.CIRCLE, ButtonKey.CIRCLE, controller_manager_pb2.BUTTON_CIRCLE),
    (ButtonTrackingKey.SQUARE, ButtonKey.SQUARE, controller_manager_pb2.BUTTON_SQUARE),
    (ButtonTrackingKey.TRIANGLE, ButtonKey.TRIANGLE, controller_manager_pb2.BUTTON_TRIANGLE),
    (ButtonTrackingKey.PS, ButtonKey.PS, controller_manager_pb2.BUTTON_PS),
    (ButtonTrackingKey.SELECT, ButtonKey.SELECT, controller_manager_pb2.BUTTON_SELECT),
    (ButtonTrackingKey.START, ButtonKey.START, controller_manager_pb2.BUTTON_START),
)

# Template for a newly tracked controller: every button released. Copied, never shared.
_ALL_RELEASED: dict[str, bool] = dict.fromkeys((tracking_key for tracking_key, _, _ in _BUTTONS), False)


class ButtonDetector:
    """
    Detects button state transitions and publishes events.

    Tracks button states per controller and generates events when
    buttons are pressed or released.
    """

    def __init__(self, event_publisher: EventPublisher):
        """
        Initialize button detector.

        Args:
            event_publisher: EventPublisher for cross-thread event publishing
        """
        self.event_publisher = event_publisher

        # Button state tracking: {serial: {button_name: pressed}}
        self._button_states: dict[str, dict[str, bool]] = {}

        # Button event subscribers (set by servicer)
        self._subscribers: dict[str, asyncio.Queue] = {}

    @property
    def button_states(self) -> dict[str, dict[str, bool]]:
        """Get the button states dict."""
        return self._button_states

    def set_subscribers(self, subscribers: dict[str, asyncio.Queue]) -> None:
        """Set reference to button event subscribers dict."""
        self._subscribers = subscribers

    def clear_controller(self, serial: str) -> None:
        """Clear button state tracking for a controller."""
        if serial in self._button_states:
            del self._button_states[serial]

    def detect_transitions_from_state(self, serial: str, state: dict, tracked_controllers: dict) -> None:
        """
        Detect button transitions from polled state dict.

        Called from discovery loop for immediate button detection at polling frequency.
        This ensures button events are detected as fast as we poll, not limited by
        gRPC stream frequency.

        Args:
            serial: Controller serial number
            state: State dict from backend polling (read only, never mutated)
            tracked_controllers: Dict of tracked controller info
        """
        if not state:
            return

        info = tracked_controllers.get(serial, {})
        self._detect_button_transitions(serial, info, state)

    def _detect_button_transitions(self, serial: str, info: dict, state: dict) -> None:
        """
        Detect button press/release transitions and publish button events (Phase 41).

        Args:
            serial: Controller serial number
            info: Controller info dict (for battery, name, reserved)
            state: Polled state dict; a missing button key reads as released
        """
        prev_states = self._button_states.get(serial)
        if prev_states is None:
            # Initialize button state tracking for this controller
            prev_states = self._button_states[serial] = dict(_ALL_RELEASED)
            logger.info(
                f"Initialized button tracking for {serial} "
                f"(current: move={state.get(ButtonKey.MOVE, False)}, trigger={state.get(ButtonKey.TRIGGER, False)})"
            )

        # Detect transitions and create events
        events = []
        for button_name, state_key, button_type in _BUTTONS:
            current_pressed = state.get(state_key, False)

            if current_pressed != prev_states[button_name]:
                # State changed - create event
                action = (
                    controller_manager_pb2.ACTION_PRESS if current_pressed else controller_manager_pb2.ACTION_RELEASE
                )

                event = controller_manager_pb2.ButtonEvent(
                    serial=serial,
                    timestamp=int(time.time() * 1000),
                    button=button_type,
                    action=action,
                    battery=info.get(ControllerInfoKey.BATTERY, 0),
                    color=controller_manager_pb2.RGB(r=0, g=0, b=255),
                    name=info.get(ControllerInfoKey.NAME, ""),
                )
                events.append(event)

                # Track button event (Phase 38)
                action_str = "press" if current_pressed else "release"
                metrics.button_events_total.labels(serial=serial, button=button_name, action=action_str).inc()

                # Update tracked state
                prev_states[button_name] = current_pressed

        # Log all button states when any transition occurred
        if events:
            get = state.get
            logger.info(
                f"Button event {serial}: T={get(ButtonKey.TRIGGER, False)} M={get(ButtonKey.MOVE, False)} "
                f"X={get(ButtonKey.CROSS, False)} O={get(ButtonKey.CIRCLE, False)} []={get(ButtonKey.SQUARE, False)} "
                f"/\\={get(ButtonKey.TRIANGLE, False)} PS={get(ButtonKey.PS, False)}"
            )

        # UC-002 BR-002: a reserved mock controller's buttons never reach the menu.
        # Publish events to all subscribers — but never announce reserved
        # controllers to button-stream consumers (the menu). #784 suppressed
        # reserved controllers from connect/disconnect events and the
        # connected_serials roster, but left this regular button-event path
        # un-filtered. That asymmetry let a reserved controller's button press
        # auto-register it in the menu's connected set (StateManager registers
        # any controller it receives a button for), while the reconcile path —
        # driven by the reserved-free roster — could prune it again. The
        # resulting nondeterministic connected/ready mismatch broke
        # all_ready() (ready_count == connected_count) and intermittently
        # prevented game start (#805). Reserved controllers stay fully usable
        # via the gameplay-data stream and explicit-serial commands.
        if events and not info.get(ControllerInfoKey.RESERVED, False):
            for event in events:
                self.event_publisher.publish_to_subscribers(self._subscribers, event, "button")

    def publish_connection_event(
        self,
        serial: str,
        is_connect: bool,
        battery: int = 0,
        name: str = "",
        connected_serials: list[str] | None = None,
    ) -> None:
        """
        Publish a connection or disconnection event to all button event subscribers.

        This allows the menu service to track controller connections via the same
        stream used for button events, eliminating the need for a separate polling loop.

        Args:
            serial: Controller serial number
            is_connect: True for connect, False for disconnect
            battery: Battery level (only available for connect events)
            name: Human-readable controller name (Issue #7)
            connected_serials: All currently connected serials (for reconciliation)
        """
        event_type = controller_manager_pb2.EVENT_CONNECT if is_connect else controller_manager_pb2.EVENT_DISCONNECT

        event = controller_manager_pb2.ButtonEvent(
            serial=serial,
            timestamp=int(time.time() * 1000),
            battery=battery,
            event_type=event_type,
            name=name,
            connected_serials=connected_serials or [],
            # button and action fields are left unset for connection events
        )

        self.event_publisher.publish_to_subscribers(self._subscribers, event, "connection")

        action_str = "connected" if is_connect else "disconnected"
        logger.info(f"Published connection event: {serial} ({name}) {action_str}")
