"""
Abstract base class for controller I/O adapters.

Adapters handle device handles and raw I/O only.
State tracking (LED colors, effects, rumble) lives in MultiplexerBackend.
All methods are sync (blocking) — called via asyncio.to_thread().
"""

from abc import ABC, abstractmethod


class ControllerIOAdapter(ABC):
    """Thin I/O adapter for controller communication.

    Adapters handle device handles and raw I/O only.
    State tracking (LED colors, effects) lives in MultiplexerBackend.
    All methods are sync (blocking) — called via asyncio.to_thread().
    """

    @property
    @abstractmethod
    def adapter_type(self) -> str:
        """Identifier: 'psmove', 'hidapi', 'mock'."""

    @abstractmethod
    def discover(self, force: bool = False, verify_only: bool = False) -> list[str]:
        """Scan for available controllers. Returns list of serials.

        Args:
            force: Force a full rescan, removing stale devices.
            verify_only: When True, only verify existing devices are
                accessible — skip expensive enumeration for new devices.
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

    def close_all(self) -> None:  # noqa: B027
        """Release all handles (shutdown). Default: no-op."""
