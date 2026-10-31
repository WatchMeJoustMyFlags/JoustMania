"""
Writes during a disconnect and feedback left over after one (#2471).

UC-002 A1 (disconnect), A4 / BR-008 (reconnect LED):
- H1: an LED or rumble write for a serial with no adapter route is kept and sent
  once the serial is routed again; the owner's newer write still wins;
- the output from before the disconnect is still dropped.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))

from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend
from services.controller_manager.tests.backend_doubles import autospec_adapter

SERIAL = "AA:AA"


def _adapter(serials):
    adapter = autospec_adapter()
    adapter.adapter_type = "mock"
    adapter.discover.return_value = list(serials)
    adapter.open.return_value = True
    adapter.poll.return_value = None
    adapter.set_output.return_value = True
    adapter.get_adapter_for_serial.return_value = None
    adapter.inner = None
    return adapter


@pytest.fixture(autouse=True)
def _metrics():
    with patch("services.controller_manager.multiplexer.multiplexer_backend.metrics"):
        yield


@pytest.fixture
def routed():
    """A backend with SERIAL routed and lit green, then SERIAL gone (no route)."""
    adapter = _adapter([SERIAL])
    mux = MultiplexerBackend(adapters=[adapter])
    mux.get_connected_controllers()
    mux._led_colors[SERIAL] = (0, 255, 0)
    adapter.discover.return_value = []
    mux.get_connected_controllers()
    adapter.set_output.reset_mock()
    return mux, adapter


def _reroute(mux, adapter):
    adapter.discover.return_value = [SERIAL]
    mux.get_connected_controllers()


class TestUC002H1WritesDuringADisconnect:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A4", br=["BR-008"])
    async def test_a_colour_written_while_unrouted_is_sent_after_the_reroute(self, routed):
        """The death's black written during the disconnect reaches the controller once it is back."""
        mux, adapter = routed
        assert await mux.set_led_color(SERIAL, 0, 0, 0) is False
        adapter.set_output.assert_not_called()

        mux.get_connected_controllers()  # further passes while it is away keep the write
        mux.get_connected_controllers()
        _reroute(mux, adapter)
        mux.update_all_leds()

        adapter.set_output.assert_called_once_with(SERIAL, 0, 0, 0, 0)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A4", br=["BR-008"])
    async def test_a_rumble_written_while_unrouted_is_sent_after_the_reroute(self, routed):
        """A rumble write (a stop at 0 included) is kept like a colour, with the colour it goes with."""
        mux, adapter = routed
        assert await mux.set_led_color(SERIAL, 255, 0, 0) is False
        assert await mux.set_rumble(SERIAL, 0) is False

        _reroute(mux, adapter)
        mux.update_all_leds()

        adapter.set_output.assert_called_once_with(SERIAL, 255, 0, 0, 0)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A1")
    async def test_the_output_from_before_the_disconnect_is_still_dropped(self, routed):
        """Without a write during the disconnect nothing is replayed: the pre-disconnect green is gone."""
        mux, adapter = routed
        _reroute(mux, adapter)
        mux.update_all_leds()

        adapter.set_output.assert_not_called()
        assert SERIAL not in mux._led_colors

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A4", br=["BR-008"])
    async def test_the_owners_write_after_the_reroute_wins(self, routed):
        """A write kept from the disconnect never overrides the colour the owner writes on reconnect."""
        mux, adapter = routed
        await mux.set_led_color(SERIAL, 0, 255, 0)  # a stale game colour, written while away
        _reroute(mux, adapter)
        assert await mux.set_led_color(SERIAL, 0, 0, 0) is True  # the owner's colour on (re)registration
        adapter.set_output.reset_mock()

        mux.update_all_leds()

        adapter.set_output.assert_not_called()  # already sent; the stale green never goes out
        assert mux._led_colors[SERIAL] == (0, 0, 0)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A1")
    async def test_shutdown_forgets_unrouted_writes(self, routed):
        mux, _ = routed
        await mux.set_led_color(SERIAL, 0, 0, 0)
        await mux.shutdown()
        assert SERIAL not in mux._led_colors
        assert not mux._unrouted_output
