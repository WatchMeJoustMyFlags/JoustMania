"""
Abstract base class for controller I/O adapters.

Adapters handle device handles and raw I/O only.
State tracking (LED colors, effects, rumble) lives in MultiplexerBackend.
All methods are sync (blocking) — called via asyncio.to_thread().
"""

from abc import ABC, abstractmethod


class DiscoveryFailedError(Exception):
    """A discovery pass could not run (backend unreachable or timed out).

    Distinct from an empty result: "no controllers" drops the adapter's routes,
    a failed pass keeps them for a few passes (UC-002 A1, #2125).
    """


class ControllerIOAdapter(ABC):
    """Thin I/O adapter for controller communication.

    Adapters handle device handles and raw I/O only.
    State tracking (LED colors, effects) lives in MultiplexerBackend.
    All methods are sync (blocking) — called via asyncio.to_thread().
    """

    @property
    @abstractmethod
    def adapter_type(self) -> str:
        """Identifier: 'hidapi', 'mock', 'rust'."""

    @abstractmethod
    def discover(
        self,
        force: bool = False,
        exclude_serials: list[str] | None = None,
    ) -> list[str]:
        """Scan for available controllers. Returns list of serials.

        Args:
            force: Force a full rescan, removing stale devices.
            exclude_serials: Serials claimed by another adapter — the adapter
                should avoid opening HID handles for these devices.

        Raises:
            DiscoveryFailedError: the scan could not run, which is not the same
                as finding no controllers.
        """

    @abstractmethod
    def open(self, serial: str) -> bool:
        """Open a handle to a controller. Returns True on success."""

    @abstractmethod
    def poll(self, serial: str) -> dict | None:
        """Read current sensor/button data. Returns state dict or None."""

    @abstractmethod
    def set_output(self, serial: str, r: int, g: int, b: int, rumble: int) -> bool:
        """Write LED color and rumble to controller hardware."""

    @abstractmethod
    def close(self, serial: str) -> None:
        """Release controller handle."""

    def consume_trigger_press(self, serial: str) -> bool:  # noqa: ARG002
        """Whether the trigger was pressed since the last call, then clear (#1837).

        The gameplay send path calls this once per delivered game frame, so a press
        shorter than the frame interval is not lost. Default: no latch.
        """
        return False

    def get_adapter_for_serial(self, serial: str) -> str | None:  # noqa: ARG002
        """Return BT adapter name (e.g. 'hci0') for this serial, if known."""
        return None

    def close_all(self) -> None:  # noqa: B027
        """Release all handles (shutdown). Default: no-op."""
