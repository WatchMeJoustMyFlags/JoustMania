"""#2378: a hung audio service or controller manager never stalls the menu (UC-010 BR-013, UC-014 A2).

These tests use the real ``AudioHelper`` over a fake audio stub: the handler tests elsewhere
mock ``AudioHelper`` itself, so they cannot see whether the helper waits for the RPC.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import grpc
import pytest

from lib.flag_service_writer import FlagServiceWriter
from lib.types import Sound
from proto import menu_pb2
from services.menu.handlers.base import ControllerState
from services.menu.handlers.connected import ConnectedHandler
from services.menu.state_manager import StateManager
from services.menu.utils import audio as audio_module
from services.menu.utils.audio import AudioHelper
from services.menu.utils.led import LedController

FAST_DEADLINE = 0.05


class _HungAudioStub:
    """An audio service that never answers: a call returns only when its gRPC deadline passes."""

    calls: list[tuple[str, float | None]] = []

    def __init__(self, _channel):
        pass

    async def _hang(self, rpc: str, deadline: float | None):
        type(self).calls.append((rpc, deadline))
        if deadline is None:
            await asyncio.Event().wait()  # no deadline: hangs for good
        await asyncio.sleep(deadline)
        raise grpc.aio.AioRpcError(
            grpc.StatusCode.DEADLINE_EXCEEDED, grpc.aio.Metadata(), grpc.aio.Metadata(), details="Deadline Exceeded"
        )

    async def PlaySound(self, _request, **kwargs):  # noqa: N802 - gRPC stub name
        await self._hang("PlaySound", kwargs.get("timeout"))

    async def StopMusic(self, _request, **kwargs):  # noqa: N802 - gRPC stub name
        await self._hang("StopMusic", kwargs.get("timeout"))

    async def SetVolume(self, _request, **kwargs):  # noqa: N802 - gRPC stub name
        await self._hang("SetVolume", kwargs.get("timeout"))

    async def PlayMusic(self, _request, **kwargs):  # noqa: N802 - gRPC stub name
        await self._hang("PlayMusic", kwargs.get("timeout"))


@pytest.fixture
def hung_audio(monkeypatch):
    """A real AudioHelper whose audio service never answers; deadlines shortened for the test."""
    _HungAudioStub.calls = []
    monkeypatch.setattr(audio_module, "AUDIO_RPC_TIMEOUT_SECONDS", FAST_DEADLINE, raising=False)
    with patch("proto.audio_pb2_grpc.AudioServiceStub", _HungAudioStub):
        helper = AudioHelper(MagicMock())
        yield helper
        for task in list(getattr(helper, "_sound_tasks", ())):
            task.cancel()


@pytest.fixture
def failures():
    with patch("services.menu.metrics.audio_rpc_failures_total") as counter:
        yield counter


@pytest.mark.asyncio
@pytest.mark.uc("UC-010", flow="main", br=["BR-013"])
async def test_a_hung_voice_line_does_not_hold_the_next_button_event(hung_audio, failures):
    """SELECT plays the mode's voice line; with audio wedged the next controller's trigger still readies."""
    manager = StateManager(
        led=create_autospec(LedController, instance=True, spec_set=True),
        audio=hung_audio,
        game_settings_writer=create_autospec(FlagServiceWriter, instance=True, spec_set=True),
        user_prefs_writer=create_autospec(FlagServiceWriter, instance=True, spec_set=True),
        publish_event=AsyncMock(),
    )
    handler = ConnectedHandler()
    manager.register_handler(handler)
    handler._debouncer.should_process = MagicMock(return_value=True)

    await asyncio.wait_for(handler.handle_button("s1", "select"), timeout=1.0)
    await asyncio.wait_for(handler.handle_button("s2", "trigger"), timeout=1.0)

    assert manager.get_controller_state("s2") == ControllerState.READY
    # The voice line was still sent, with a deadline, and its timeout is counted.
    await asyncio.wait_for(asyncio.gather(*hung_audio._sound_tasks), timeout=1.0)
    assert ("PlaySound", FAST_DEADLINE) in _HungAudioStub.calls
    failures.labels.assert_any_call(rpc="PlaySound", reason="timeout")


@pytest.mark.asyncio
@pytest.mark.uc("UC-010", flow="main", steps=[5], br=["BR-013"])
async def test_a_hung_stop_music_does_not_hold_the_game_start(hung_audio, failures):
    """The game start stops the lobby music; a wedged audio service delays it by the deadline only."""
    from services.menu.servicer import MenuServicer
    from services.menu.tests.test_servicer import FakeMenuServicer

    servicer = FakeMenuServicer()
    servicer.audio = hung_audio
    servicer.idle_monitor = MagicMock()
    servicer.stop_button_monitor = AsyncMock()

    await asyncio.wait_for(MenuServicer._prepare_game_start(servicer), timeout=1.0)

    assert servicer.state == menu_pb2.MenuState.GAME_STARTING
    servicer.stop_button_monitor.assert_awaited_once()
    assert _HungAudioStub.calls == [("StopMusic", FAST_DEADLINE)]
    failures.labels.assert_called_once_with(rpc="StopMusic", reason="timeout")


@pytest.mark.asyncio
@pytest.mark.uc("UC-010", flow="main", steps=[8], br=["BR-013"])
async def test_a_hung_lobby_music_start_ends_at_its_deadline(hung_audio, failures):
    await asyncio.wait_for(hung_audio.start_lobby_music(), timeout=1.0)

    assert _HungAudioStub.calls == [("SetVolume", FAST_DEADLINE)]
    failures.labels.assert_called_once_with(rpc="SetVolume", reason="timeout")


@pytest.mark.asyncio
@pytest.mark.uc("UC-010", flow="main", br=["BR-013"])
async def test_a_failing_sound_is_counted_as_error(failures):
    stub = MagicMock()
    stub.PlaySound = AsyncMock(side_effect=RuntimeError("audio down"))
    with patch("proto.audio_pb2_grpc.AudioServiceStub", return_value=stub):
        helper = AudioHelper(MagicMock())
        await helper.play_voice(Sound.MENU_VOX_ADMIN_NOT_SAVED)
        await asyncio.gather(*helper._sound_tasks)

    failures.labels.assert_called_once_with(rpc="PlaySound", reason="error")


@pytest.mark.asyncio
@pytest.mark.uc("UC-014", flow="A2")
async def test_the_live_roster_lookup_has_a_deadline(monkeypatch):
    """A controller manager that never answers falls back to the menu roster (UC-014 A2)."""
    from services.menu import settings_ops as settings_ops_module
    from services.menu.settings_ops import SettingsOps

    monkeypatch.setattr(settings_ops_module, "LIVE_ROSTER_RPC_TIMEOUT_SECONDS", FAST_DEADLINE, raising=False)
    seen: list[float | None] = []

    class HungControllerManager:
        def __init__(self, _channel):
            pass

        async def GetConnectedControllers(self, _request, **kwargs):  # noqa: N802 - gRPC stub name
            deadline = kwargs.get("timeout")
            seen.append(deadline)
            if deadline is None:
                await asyncio.Event().wait()
            await asyncio.sleep(deadline)
            raise grpc.aio.AioRpcError(grpc.StatusCode.DEADLINE_EXCEEDED, grpc.aio.Metadata(), grpc.aio.Metadata())

    ops = SettingsOps(get_state_manager=lambda: None, controller_channel=MagicMock(), metrics=MagicMock())
    with patch("proto.controller_manager_pb2_grpc.ControllerManagerServiceStub", HungControllerManager):
        roster = await asyncio.wait_for(ops.prune_ghosts(["s1", "s2"], None, MagicMock()), timeout=1.0)

    assert roster == ["s1", "s2"]
    assert seen == [FAST_DEADLINE]
    ops.metrics.force_start_reconcile_failed_total.inc.assert_called_once()
