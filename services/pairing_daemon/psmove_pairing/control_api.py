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

from .usb_pairing import NotRegisteredError, PairingInProgressError, UnpairResult, USBPairing

logger = logging.getLogger("psmove-pairing")

UNPAIR_PATH_RE = re.compile(r"^/controllers/([^/]+)/unpair/?$")
UNPAIR_TIMEOUT_SECONDS = 30.0


def _result_body(unpair: UnpairResult) -> dict:
    results = unpair.results
    return {
        "address": unpair.address,
        "removed": sum(r.outcome == "removed" for r in results),
        "already_absent": sum(r.outcome == "already_absent" for r in results),
        "failed": sum(not r.ok for r in results),
        "adapters": [r.registration.adapter_address for r in results if r.outcome == "removed"],
        "results": [
            {"adapter": r.registration.adapter_address, "outcome": r.outcome, **({"error": r.error} if r.error else {})}
            for r in results
        ],
        "on_usb": unpair.on_usb,
    }


async def unpair_response(usb_pairing: USBPairing, address: str) -> tuple[int, dict]:
    """Run the unpair and map its outcome to an HTTP status and JSON body.

    200: every registration found is gone ("removed" or "already_absent": unpair is
    idempotent). 404: none found (A4). 409: a pairing or another unpair runs (A6).
    503: any removal failed (partial result in the body) or BlueZ / D-Bus failed (A7).
    """
    try:
        unpair = await usb_pairing.unpair(address)
    except ValueError as e:
        return 400, {"error": str(e)}
    except PairingInProgressError:
        return 409, {"error": "A USB pairing or another unpair is running; try again in a few seconds."}
    except NotRegisteredError as e:
        return 404, {"error": f"Controller {e} is not registered on any adapter.", "address": str(e)}
    except DBusError as e:
        logger.error(f"Unpair of {address} failed: BlueZ error {e.error_name}: {e}")
        return 503, {"error": f"BlueZ error {e.error_name}: {e}"}
    except Exception as e:  # D-Bus socket missing, permissions, ...: never a bare 502 from the proxy
        logger.exception(f"Unpair of {address} failed")
        return 503, {"error": f"Unpair failed: {type(e).__name__}: {e}"}

    body = _result_body(unpair)
    if body["failed"]:
        body["error"] = f"{body['failed']} registration(s) could not be removed"
        return 503, body
    return 200, body


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
