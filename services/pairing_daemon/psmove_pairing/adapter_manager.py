"""Bluetooth adapter management — dispatches to legacy or modern backend.

Reads the ``dbus_backend`` feature flag from the ``performance`` domain
to select between dbus-python (legacy) and dbus-fast (modern).

The backend is resolved lazily on first use, allowing the flagd
provider to initialize before evaluation.
"""

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("psmove-pairing")

_backend_name: str | None = None


def _resolve_backend() -> str:
    """Determine which D-Bus backend to use (cached after first call)."""
    global _backend_name
    if _backend_name is not None:
        return _backend_name

    try:
        from lib.feature_flags import get_flag_client, init_flag_domain

        init_flag_domain("performance")
        client = get_flag_client("performance")
        _backend_name = client.get_string_value("dbus_backend", "dbus_python")
    except Exception:
        _backend_name = os.environ.get("DBUS_BACKEND", "dbus_python")

    logger.info(f"D-Bus adapter manager backend: {_backend_name}")
    return _backend_name


@dataclass
class AdapterInfo:
    """Information about a Bluetooth adapter."""

    hci: str
    address: str
    name: str
    device_count: int


class AdapterManager:
    """Manages Bluetooth adapters — delegates to the active backend.

    Creates the real backend-specific AdapterManager on first use,
    after the feature flag has been resolved.
    """

    def __init__(self):
        self._delegate = None

    def _get_delegate(self):
        if self._delegate is not None:
            return self._delegate

        if _resolve_backend() == "dbus_fast":
            from psmove_pairing.adapter_manager_modern import AdapterManager as ModernManager

            logger.info("Using dbus-fast (modern) adapter manager")
            self._delegate = ModernManager()
        else:
            from psmove_pairing.adapter_manager_legacy import AdapterManager as LegacyManager

            logger.info("Using dbus-python (legacy) adapter manager")
            self._delegate = LegacyManager()

        return self._delegate

    async def refresh_adapters(self) -> dict[str, list[str]]:
        """Refresh the list of adapters and their paired devices."""
        return await self._get_delegate().refresh_adapters()

    def get_lowest_bt_device(self) -> str:
        """Get the address of the adapter with the fewest paired devices."""
        return self._get_delegate().get_lowest_bt_device()

    def select_least_loaded_adapter(self) -> AdapterInfo | None:
        """Select the adapter with the fewest paired devices."""
        result = self._get_delegate().select_least_loaded_adapter()
        if result is None:
            return None
        # Re-wrap in our AdapterInfo to keep a consistent type
        return AdapterInfo(hci=result.hci, address=result.address, name=result.name, device_count=result.device_count)

    def check_if_not_paired(self, controller_addr: str) -> bool:
        """Check if a controller is not yet paired to any adapter."""
        return self._get_delegate().check_if_not_paired(controller_addr)


async def restart_systemd_unit(unit: str, mode: str = "replace") -> None:
    """Restart a systemd unit — dispatches to active backend."""
    if _resolve_backend() == "dbus_fast":
        from lib.bluez_dbus import restart_systemd_unit as _impl

        await _impl(unit, mode)
    else:
        from psmove_pairing.adapter_manager_legacy import restart_systemd_unit as _impl

        await _impl(unit, mode)
