"""Operator control API on the pairing daemon's HTTP port (UC-005).

``POST /controllers/{address}/unpair`` removes every PS Move registration of
the address on every adapter. There is no authentication (UC-005 BR-003): the
port is bound on the host network (full stack) or the Compose network (lite
stack), and the dashboard reaches it through the connect-proxy.

The HTTP server runs in a thread; the unpair coroutine runs on the daemon's
event loop, which owns the shared D-Bus connection.
"""

import asyncio
import logging
import re
from urllib.parse import unquote

from lib.bluez_dbus import DBusError

from .registrations import normalize_address
from .usb_pairing import NotRegisteredError, PairingInProgressError, USBPairing

logger = logging.getLogger("psmove-pairing")

UNPAIR_PATH_RE = re.compile(r"^/controllers/([^/]+)/unpair/?$")
UNPAIR_TIMEOUT_SECONDS = 30.0


async def unpair_response(usb_pairing: USBPairing, address: str) -> tuple[int, dict]:
    """Run the unpair and map its outcome to an HTTP status and JSON body."""
    try:
        removed = await usb_pairing.unpair(address)
    except ValueError as e:
        return 400, {"error": str(e)}
    except PairingInProgressError:
        return 409, {"error": "This controller is being paired over USB; unpair it once pairing has finished."}
    except NotRegisteredError as e:
        return 404, {"error": f"Controller {e} is not registered on any adapter.", "address": str(e)}
    except DBusError as e:
        logger.error(f"Unpair of {address} failed: BlueZ error {e.error_name}: {e}")
        return 503, {"error": f"BlueZ unavailable: {e}"}
    return 200, {"address": normalize_address(address), "removed": len(removed), "adapters": removed}


def handle_post(path: str, usb_pairing: USBPairing, loop: asyncio.AbstractEventLoop | None) -> tuple[int, dict] | None:
    """Dispatch a POST from the HTTP thread; None when the path is not ours."""
    match = UNPAIR_PATH_RE.match(path.split("?", 1)[0])
    if match is None:
        return None
    if loop is None or not loop.is_running():
        return 503, {"error": "pairing daemon not started"}
    address = unquote(match.group(1))
    future = asyncio.run_coroutine_threadsafe(unpair_response(usb_pairing, address), loop)
    try:
        return future.result(timeout=UNPAIR_TIMEOUT_SECONDS)
    except TimeoutError:
        future.cancel()
        return 503, {"error": "unpair timed out"}
