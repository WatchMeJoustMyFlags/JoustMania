"""BlueZ Bluetooth helpers — dispatches to legacy or modern backend.

Reads the ``dbus_backend`` feature flag from the ``performance`` domain
to select between dbus-python (legacy) and dbus-fast (modern).

The backend is resolved lazily on first function call, allowing the flagd
provider to initialize before evaluation.
"""

import logging
import os

logger = logging.getLogger(__name__)

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

    logger.info(f"D-Bus backend: {_backend_name}")
    return _backend_name


async def enable_adapter(hci: str) -> bool:
    """Power on the Bluetooth adapter, unblocking rfkill if needed."""
    if _resolve_backend() == "dbus_fast":
        from services.controller_manager.bluetooth_modern import enable_adapter as _impl
    else:
        from services.controller_manager.bluetooth_legacy import enable_adapter as _impl
    return await _impl(hci)


async def get_attached_addresses(hci: str) -> list[str]:
    """Get MAC addresses of devices known to the adapter."""
    if _resolve_backend() == "dbus_fast":
        from services.controller_manager.bluetooth_modern import get_attached_addresses as _impl
    else:
        from services.controller_manager.bluetooth_legacy import get_attached_addresses as _impl
    return await _impl(hci)


async def get_hci_dict() -> dict[str, str]:
    """Get dictionary mapping hci name to Bluetooth address."""
    if _resolve_backend() == "dbus_fast":
        from services.controller_manager.bluetooth_modern import get_hci_dict as _impl
    else:
        from services.controller_manager.bluetooth_legacy import get_hci_dict as _impl
    return await _impl()


def rfkill_unblock(hci: str) -> None:
    """Unblock a Bluetooth adapter via rfkill."""
    if _resolve_backend() == "dbus_fast":
        from services.controller_manager.bluetooth_modern import rfkill_unblock as _impl
    else:
        from services.controller_manager.bluetooth_legacy import rfkill_unblock as _impl
    _impl(hci)
