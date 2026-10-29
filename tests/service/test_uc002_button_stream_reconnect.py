"""UC-002 A5 on the service tier (T2): the menu's button stream drops and reconnects.

Real controller manager, game coordinator and menu from source (LOBBY, OTel
exported to the in-process OtlpSink). The menu reaches the controller manager
through a ``TcpProxy`` (its ``CONTROLLER_MANAGER_PORT``), so the test cuts the
connection without restarting either process. Three controllers sit in the
lobby, two of them READY. No button is pressed after the drop, so nothing but
the replayed connect events can register a controller again:

- step 2: the menu processes one connect event per controller on the new
  stream (``menu_button_frames_processed_total`` grows by 3; the counter
  counts every event of the stream since #2380: connect, disconnect and button);
- steps 1 and 3: the READY controllers show the dim lobby colour again
  (CONNECTED, un-readied), and every controller does.
"""

import asyncio
import dataclasses

import pytest

from tests.integration.helpers import async_poll_until, get_controller_color, get_mock_client, mark_controllers_ready
from tests.service.harness import LOBBY, SERVICES
from tests.service.harness.fakes import TcpProxy
from tests.service.lobby_flow import start_lobby

pytestmark = pytest.mark.tier("service")

MENU = SERVICES["menu"][2]
CONNECT_EVENTS_METRIC = "menu_button_frames_processed_total"
LED_TIMEOUT_SECONDS = 15.0
RECONNECT_TIMEOUT_SECONDS = 15.0
# The metric reader exports on an interval (5 s by default).
EXPORT_TIMEOUT_SECONDS = 30.0
CONTROLLERS = 3


@pytest.fixture(scope="module")
def proxied_stack(request):
    """A LOBBY stack whose menu dials the controller manager through a TcpProxy; returns (stack, proxy).

    The proxy binds here, before the stack starts: its port goes into this
    module's composition (``SERVICE_COMPOSITION``, read by ``service_stack``).
    The menu's first dials wait in the proxy's backlog until ``start``.
    """
    proxy = TcpProxy()
    try:
        request.module.SERVICE_COMPOSITION = dataclasses.replace(
            LOBBY, otlp=True, env={"menu": {"CONTROLLER_MANAGER_PORT": str(proxy.port)}}
        )
        stack = request.getfixturevalue("service_stack")
        proxy.start(stack.get_service_port("controller-manager", SERVICES["controller-manager"][1]))
        yield stack, proxy
    finally:
        proxy.stop()


async def _colors(mock_client, serials) -> dict[str, tuple[int, int, int]]:
    return {serial: await get_controller_color(mock_client, serial) for serial in serials}


@pytest.mark.uc("UC-002", flow="A5", steps=[1, 2, 3])
async def test_button_stream_reconnect_re_registers_every_controller(proxied_stack):
    stack, proxy = proxied_stack
    sink = stack.otlp_sink
    serials = await start_lobby(stack, count=CONTROLLERS)
    readied = serials[:2]
    mock_client, mock_channel = await get_mock_client(stack)
    try:
        dim = await _colors(mock_client, serials)
        # The lobby's own connects are exported before the drop (one per controller).
        connects_before = (
            await sink.wait_metric_async(
                CONNECT_EVENTS_METRIC,
                service=MENU,
                predicate=lambda p: p.value >= CONTROLLERS,
                timeout=EXPORT_TIMEOUT_SECONDS,
            )
        ).value

        # Two of three READY: their LEDs leave the dim lobby colour; no game starts.
        await mark_controllers_ready(mock_client, readied)

        async def readied_lit() -> bool:
            colors = await _colors(mock_client, readied)
            return all(colors[serial] != dim[serial] for serial in readied)

        assert await async_poll_until(readied_lit, timeout=LED_TIMEOUT_SECONDS), "READY never showed on the LEDs"

        # The button stream drops; the menu dials the controller manager again.
        dialled = proxy.accepted
        assert proxy.drop_connections() > 0, "the menu had no connection through the proxy"
        assert await asyncio.to_thread(proxy.wait_for_connections, dialled + 1, RECONNECT_TIMEOUT_SECONDS), (
            "the menu never reconnected"
        )

        # Step 2: a replayed connect per controller reached the menu (no press since the drop).
        replayed = await sink.wait_metric_async(
            CONNECT_EVENTS_METRIC,
            service=MENU,
            predicate=lambda p: p.value >= connects_before + CONTROLLERS,
            timeout=EXPORT_TIMEOUT_SECONDS,
        )
        assert replayed.value >= connects_before + CONTROLLERS

        # Steps 1 and 3: every controller is CONNECTED again with the dim lobby colour (READY cleared).
        last = {}

        async def all_dim() -> bool:
            last.update(await _colors(mock_client, serials))
            return last == dim

        assert await async_poll_until(all_dim, timeout=LED_TIMEOUT_SECONDS), (
            f"lobby not re-registered after the reconnect: {last} != {dim}"
        )
    finally:
        await mock_channel.close()
