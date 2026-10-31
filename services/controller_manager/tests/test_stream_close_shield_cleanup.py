"""
A closed game stream ends its players' shield layers after a short delay (#1837, UC-040 A32).

A charge lasts minutes: a coordinator crash must not leave a breathing controller in the
lobby. A coordinator reconnect re-claims the controllers within the delay and keeps them.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from test_identify_controller import SERIAL, _game_config, servicer  # noqa: F401  (pytest fixture)

from proto import controller_manager_pb2
from services.controller_manager import servicer as servicer_module

CHARGE = controller_manager_pb2.GAME_EFFECT_SHIELD_CHARGE


from services.controller_manager.tests.backend_doubles import autospec_span


class _Context:
    cancelled_flag = False

    def cancelled(self):
        return self.cancelled_flag


async def _run_stream(svc, stop_after_frames=1):
    """Open a game stream (colours config), take a frame, close it like a cancelled RPC."""
    config = controller_manager_pb2.GameplayStreamControl(config=_game_config(SERIAL))

    async def requests():
        yield config
        await asyncio.Event().wait()

    context = _Context()
    seen = 0
    async for _ in svc.StreamGameplayData(requests(), context):
        if svc._in_game(SERIAL):
            seen += 1
            if seen >= stop_after_frames:
                context.cancelled_flag = True
    assert seen


@pytest.fixture
def stream_servicer(servicer, monkeypatch):  # noqa: F811
    servicer.state_builder.build_state = MagicMock(
        side_effect=lambda serial, _info: controller_manager_pb2.ControllerState(serial=serial)
    )
    servicer.discovery_loop.drain_health_counters.return_value = (0, 0, 0)
    monkeypatch.setattr(servicer_module, "STREAM_CLOSE_SHIELD_CLEANUP_DELAY_S", 0.05)
    servicer.feedback_manager.end_shield_effect = AsyncMock(return_value=True)
    return servicer


async def _wait_cleanups(svc):
    if svc._shield_cleanup_tasks:
        await asyncio.gather(*svc._shield_cleanup_tasks, return_exceptions=True)


class TestUC040StreamCloseCleanup:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A32", br=["BR-021"])
    async def test_closed_stream_ends_the_layers_of_its_roster_after_the_delay(self, stream_servicer):
        """UC-040 A32: roster snapshotted BEFORE the release; layers end ~2 s (here 50 ms) later."""
        await _run_stream(stream_servicer)

        assert stream_servicer._game_stream_serials == {}  # released ...
        stream_servicer.feedback_manager.end_shield_effect.assert_not_called()  # ... but not ended yet
        await _wait_cleanups(stream_servicer)

        stream_servicer.feedback_manager.end_shield_effect.assert_awaited_once_with(SERIAL, restore=True)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A32", br=["BR-021"])
    async def test_a_new_game_stream_claiming_the_serial_cancels_the_cleanup(self, stream_servicer):
        """UC-040 A32: the coordinator reconnect re-claims the controller; its charge layer survives."""
        await _run_stream(stream_servicer)
        # The reconnected stream sends its colours config within the delay.
        await stream_servicer._process_gameplay_config(_game_config(SERIAL), "game_stream_2", autospec_span())

        await _wait_cleanups(stream_servicer)

        stream_servicer.feedback_manager.end_shield_effect.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A32", br=["BR-021"])
    async def test_observer_stream_close_does_not_touch_shield_layers(self, stream_servicer):
        """UC-040 A32: a stream that never sent colours owns no layers."""

        class Ctx(_Context):
            pass

        async def requests():
            await asyncio.Event().wait()
            yield  # pragma: no cover

        context = Ctx()
        frames = 0
        async for _ in stream_servicer.StreamGameplayData(requests(), context):
            frames += 1
            context.cancelled_flag = True
        assert frames

        await _wait_cleanups(stream_servicer)
        stream_servicer.feedback_manager.end_shield_effect.assert_not_called()
        assert not stream_servicer._shield_cleanup_tasks


class TestUC040CleanupOutlastsReconnectBackoff:
    @pytest.mark.uc("UC-040", flow="A32", br=["BR-021"])
    def test_delay_covers_the_coordinator_reconnect_backoff(self):
        """The coordinator retries after 1, 2, 3 s (cumulative 6 s): the delay must outlast that."""
        assert servicer_module.STREAM_CLOSE_SHIELD_CLEANUP_DELAY_S >= 7.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A32", br=["BR-021"])
    async def test_late_reconnect_claim_inside_the_delay_keeps_the_layers(self, stream_servicer, monkeypatch):
        """UC-040 A32: a second reconnect attempt claims mid-delay (scaled: 0.2 s of 0.4 s); layers kept."""
        monkeypatch.setattr(servicer_module, "STREAM_CLOSE_SHIELD_CLEANUP_DELAY_S", 0.4)
        await _run_stream(stream_servicer)
        await asyncio.sleep(0.2)
        stream_servicer.feedback_manager.end_shield_effect.assert_not_called()

        await stream_servicer._process_gameplay_config(_game_config(SERIAL), "game_stream_2", autospec_span())
        await _wait_cleanups(stream_servicer)

        stream_servicer.feedback_manager.end_shield_effect.assert_not_called()
