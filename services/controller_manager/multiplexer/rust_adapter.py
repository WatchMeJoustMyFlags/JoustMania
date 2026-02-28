"""Stub adapter for a future Rust gRPC controller I/O service.

Will eventually connect to a separate Rust service via gRPC for
controller polling, LED output, and discovery. All methods raise
NotImplementedError until #612 adds the Rust service.
"""

from services.controller_manager.multiplexer.adapter import ControllerIOAdapter


class RustServiceAdapter(ControllerIOAdapter):
    """Stub I/O adapter for a Rust gRPC backend service."""

    @property
    def adapter_type(self) -> str:
        return "rust"

    def discover(self, force: bool = False, verify_only: bool = False) -> list[str]:
        raise NotImplementedError("Rust I/O service not yet implemented — see #612")

    def open(self, serial: str) -> bool:
        raise NotImplementedError("Rust I/O service not yet implemented — see #612")

    def poll(self, serial: str) -> dict | None:
        raise NotImplementedError("Rust I/O service not yet implemented — see #612")

    def set_output(self, serial: str, r: int, g: int, b: int, rumble: int) -> bool:
        raise NotImplementedError("Rust I/O service not yet implemented — see #612")

    def close(self, serial: str) -> None:
        raise NotImplementedError("Rust I/O service not yet implemented — see #612")
