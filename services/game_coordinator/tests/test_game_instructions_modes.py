"""Every mode's instruction clip (FR-014, UC-020 A5, #2151).

With ``game_instructions`` on, each mode plays its own recording before the
countdown: Teams and Random Teams (UC-021 A3), Traitor and Werewolf (UC-022 A3),
Zombies (UC-023 A2), Swapper (UC-024 A2), Tournament and Fight Club (UC-025 A5).
Non-Stop Joust has no recording (UC-026 A3). Fight Club's line exists in the ivy
voice only, so it plays the ivy file whatever ``menu_voice`` is.

The modes with their own ``run()`` (Werewolf, Zombies, Fight Club, Tournament)
play the clip after their intro, while the stream is drained, and before the
countdown; a force-end during the wait skips the start.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest
from opentelemetry import trace

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from run_fakes import event_order as _event_order
from run_fakes import name as _name
from test_ffa_uc020 import RecordingAudio
from test_random_teams_formation import SERIALS, QueueControllerManager, RecordingLog, _players, _stop, _wait_for

from lib.types import GameEvent, Sound
from proto import audio_pb2
from services.game_coordinator.games import fight_club as fight_club_mod
from services.game_coordinator.games import tournament as tournament_mod
from services.game_coordinator.games import werewolf as werewolf_mod
from services.game_coordinator.games import zombie as zombie_mod
from services.game_coordinator.games.base import BaseGameMode
from services.game_coordinator.games.fight_club import FIGHT_CLUB_INSTRUCTIONS, FightClubGame
from services.game_coordinator.games.nonstop_joust import NonstopJoustGame
from services.game_coordinator.games.random_teams import RandomTeamsGame
from services.game_coordinator.games.swapper import SwapperGame
from services.game_coordinator.games.teams import SimpleTeamsGame
from services.game_coordinator.games.tournament import TournamentGame
from services.game_coordinator.games.traitor import TraitorGame
from services.game_coordinator.games.werewolf import WerewolfGame
from services.game_coordinator.games.zombie import ZombieGame
from services.game_coordinator.runtime_config import GamePerformanceConfig, get_config_manager
from services.game_coordinator.tests.conftest import EventCollector, MockControllerManagerService

ASSETS_DIR = Path(__file__).resolve().parents[2] / "audio" / "assets"
CLIP_MARK = "test_instruction_clip"
INTRO_DONE = "test_intro_done"

# mode class -> (expected clip, uc, flow)
MODE_CLIPS = [
    pytest.param(
        SimpleTeamsGame,
        Sound.MENU_VOX_TEAMS_INSTRUCTIONS.value,
        id="teams",
        marks=pytest.mark.uc("UC-021", flow="A3", steps=[1]),
    ),
    pytest.param(
        RandomTeamsGame,
        Sound.MENU_VOX_TEAMS_INSTRUCTIONS.value,
        id="random_teams",
        marks=pytest.mark.uc("UC-021", flow="A3", steps=[1]),
    ),
    pytest.param(
        TraitorGame,
        Sound.MENU_VOX_TRAITOR_INSTRUCTIONS.value,
        id="traitor",
        marks=pytest.mark.uc("UC-022", flow="A3", steps=[1]),
    ),
    pytest.param(
        WerewolfGame,
        Sound.MENU_VOX_WEREWOLF_INSTRUCTIONS.value,
        id="werewolf",
        marks=pytest.mark.uc("UC-022", flow="A3", steps=[1]),
    ),
    pytest.param(
        ZombieGame,
        Sound.MENU_VOX_ZOMBIE_INSTRUCTIONS.value,
        id="zombie",
        marks=pytest.mark.uc("UC-023", flow="A2", steps=[1]),
    ),
    pytest.param(
        SwapperGame,
        Sound.MENU_VOX_SWAPPER_INSTRUCTIONS.value,
        id="swapper",
        marks=pytest.mark.uc("UC-024", flow="A2", steps=[1]),
    ),
    pytest.param(
        TournamentGame,
        Sound.MENU_VOX_TOURNAMENT_INSTRUCTIONS.value,
        id="tournament",
        marks=pytest.mark.uc("UC-025", flow="A5", steps=[1]),
    ),
    pytest.param(
        FightClubGame,
        "Menu/vox/ivy/Fightclub-instructions.ogg",
        id="fight_club",
        marks=pytest.mark.uc("UC-025", flow="A5", steps=[1]),
    ),
]


class ClipAudio(RecordingAudio):
    """Audio stub that reports a length for the instruction clip and marks it in the run log."""

    def __init__(self, clip: str, clip_seconds: float, log: RecordingLog | None = None):
        super().__init__()
        self.clip = clip
        self.clip_seconds = clip_seconds
        self.log = log
        self.on_clip = None  # optional callable run when the clip's PlaySound arrives

    async def PlaySound(self, request):  # noqa: N802 - gRPC stub name
        await super().PlaySound(request)
        if request.file_path != self.clip:
            return audio_pb2.PlaySoundResponse(success=True)
        if self.log is not None:
            self.log.entries.append(("event", CLIP_MARK))
        if self.on_clip is not None:
            self.on_clip()
        return audio_pb2.PlaySoundResponse(success=True, duration_seconds=self.clip_seconds)


def _game(mode_cls, audio, *, client=None, publisher=None):
    return mode_cls(
        controller_manager_client=client or MockControllerManagerService(num_controllers=4),
        event_publisher=publisher or EventCollector().publish,
        audio_client=audio,
        game_id=f"test_instructions_{mode_cls.__name__}",
        initial_players=_players(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode_cls", "clip"), MODE_CLIPS)
async def test_mode_plays_its_own_clip(mode_cls, clip):
    """Instructions on: the mode's clip plays once and the span records `played`."""
    audio = ClipAudio(clip, clip_seconds=0.0)
    game = _game(mode_cls, audio)
    game.running = True
    span = create_autospec(trace.Span, instance=True)

    with patch.object(BaseGameMode, "_instructions_enabled", autospec=True, return_value=True):
        await game._play_instructions(span)

    assert audio.sounds() == [clip]
    span.set_attribute.assert_called_once_with("game.instructions", "played")


@pytest.mark.parametrize(("mode_cls", "clip"), MODE_CLIPS)
def test_clip_file_exists_in_every_voice_it_plays_in(mode_cls, clip):
    """Each clip resolves to a recording in aaron and ivy; Fight Club's pins the ivy file."""
    resolved = _game(mode_cls, None).get_instruction_clip()
    assert getattr(resolved, "value", resolved) == clip
    if "/" in clip:
        # A full path with a voice folder: the audio service plays it as-is (no menu_voice).
        assert clip.startswith("Menu/vox/ivy/"), clip
        assert (ASSETS_DIR / clip).is_file(), clip
    else:
        for voice in ("aaron", "ivy"):
            assert (ASSETS_DIR / "Menu" / "vox" / voice / f"{clip}.ogg").is_file(), (voice, clip)


@pytest.mark.uc("UC-025", flow="A5", steps=[1])
def test_fight_club_clip_is_the_ivy_recording_because_aaron_has_none():
    """Fight Club's line is ivy only: a menu_voice path would play silence for aaron."""
    assert FIGHT_CLUB_INSTRUCTIONS == "Menu/vox/ivy/Fightclub-instructions.ogg"
    assert not (ASSETS_DIR / "Menu/vox/aaron/Fightclub-instructions.ogg").exists()


@pytest.mark.asyncio
@pytest.mark.uc("UC-026", flow="A3", steps=[1])
@pytest.mark.uc("UC-020", flow="A5", steps=[3])
async def test_nonstop_has_no_clip_and_does_not_wait():
    """Non-Stop Joust has no recording: no PlaySound, `no_clip`."""
    audio = ClipAudio("", clip_seconds=5.0)
    game = _game(NonstopJoustGame, audio)
    game.running = True
    span = create_autospec(trace.Span, instance=True)

    with patch.object(BaseGameMode, "_instructions_enabled", autospec=True, return_value=True):
        await asyncio.wait_for(game._play_instructions(span), timeout=1.0)

    assert game.get_instruction_clip() is None
    assert audio.calls == []
    span.set_attribute.assert_called_once_with("game.instructions", "no_clip")


# --- Modes with their own run(): the clip plays after the intro, before the countdown ---

INTRO_S = 0.2
CUSTOM_RUN_MODES = [
    pytest.param(
        WerewolfGame,
        Sound.MENU_VOX_WEREWOLF_INSTRUCTIONS.value,
        id="werewolf",
        marks=pytest.mark.uc("UC-022", flow="A3", steps=[1, 2]),
    ),
    pytest.param(
        ZombieGame,
        Sound.MENU_VOX_ZOMBIE_INSTRUCTIONS.value,
        id="zombie",
        marks=pytest.mark.uc("UC-023", flow="A2", steps=[1, 2]),
    ),
    pytest.param(
        TournamentGame,
        Sound.MENU_VOX_TOURNAMENT_INSTRUCTIONS.value,
        id="tournament",
        marks=pytest.mark.uc("UC-025", flow="A5", steps=[1, 2]),
    ),
    pytest.param(
        FightClubGame,
        "Menu/vox/ivy/Fightclub-instructions.ogg",
        id="fight_club",
        marks=pytest.mark.uc("UC-025", flow="A5", steps=[1, 2]),
    ),
]


@pytest.fixture(autouse=True)
def fast_start():
    """No countdown, a short rainbow and short intros."""
    config = GamePerformanceConfig(countdown_phase_duration_ms=0, winner_rainbow_duration_ms=50)
    with (
        patch.object(get_config_manager(), "get_config", return_value=config),
        patch.object(werewolf_mod, "WEREWOLF_INTRO_DURATION_S", INTRO_S),
        patch.object(zombie_mod, "ZOMBIE_INTRO_DURATION_S", INTRO_S),
        patch.object(tournament_mod, "TOURNAMENT_INTRO_DURATION_S", INTRO_S),
        patch.object(fight_club_mod, "FC_INTRO_DURATION_S", INTRO_S),
    ):
        yield config


class _MarkedPhase:
    """Wraps a phase so the run log shows when it finished."""

    def __init__(self, phase, log: RecordingLog, after=None):
        self.name = phase.name
        self._phase = phase
        self._log = log
        self._after = after

    async def execute(self):
        await self._phase.execute()
        self._log.entries.append(("event", INTRO_DONE))
        if self._after is not None:
            self._after()


def _run_game(mode_cls, clip, clip_seconds, *, after_intro=None):
    log = RecordingLog()
    client = QueueControllerManager(log)
    audio = ClipAudio(clip, clip_seconds, log)
    game = _game(mode_cls, audio, client=client, publisher=log.publish)
    real_phases = game._get_additional_phases
    game._get_additional_phases = lambda: [_MarkedPhase(p, log, after_intro) for p in real_phases()]
    return log, client, audio, game


def _drain_running(game) -> bool:
    task = game._early_drain_task
    return task is not None and not task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode_cls", "clip"), CUSTOM_RUN_MODES)
async def test_custom_run_plays_clip_after_intro_and_before_countdown(mode_cls, clip):
    """The clip plays after the intro on the open stream; the countdown waits for it."""
    log, client, audio, game = _run_game(mode_cls, clip, clip_seconds=0.3)
    drain_at_clip: list[bool] = []
    audio.on_clip = lambda: drain_at_clip.append(_drain_running(game))

    with patch.object(BaseGameMode, "_instructions_enabled", autospec=True, return_value=True):
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: GameEvent.GAME_STARTED in [v for _, v in log.entries])
        await _stop(game, task, client)

    assert audio.sounds().count(clip) == 1
    # The drain still reads (and discards) the stream while the clip plays, so
    # motion from the clip never queues up for the warm-up (UC-021 A1).
    assert drain_at_clip == [True]
    order = _event_order(log)
    stream_open = log.writes("config")[0][0]
    clip_at = log.entries.index(("event", CLIP_MARK))
    assert stream_open < clip_at, "the stream is open (and drained) while the clip plays"
    positions = [
        order.index(_name(e)) for e in (INTRO_DONE, CLIP_MARK, GameEvent.COUNTDOWN_START, GameEvent.GAME_STARTED)
    ]
    assert positions == sorted(positions), order
    assert len(client.streams) == 1, "the stream stays open through the clip"


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode_cls", "clip"), CUSTOM_RUN_MODES)
async def test_custom_run_force_end_during_clip_skips_the_start(mode_cls, clip):
    """A force-end during the wait: no countdown, no game_started, StopSound sent."""
    log, client, audio, game = _run_game(mode_cls, clip, clip_seconds=5.0)

    with patch.object(BaseGameMode, "_instructions_enabled", autospec=True, return_value=True):
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: ("event", CLIP_MARK) in log.entries)
        await _stop(game, task, client)

    order = _event_order(log)
    assert GameEvent.COUNTDOWN_START.value not in order
    assert GameEvent.GAME_STARTED.value not in order
    assert any(name == "StopSound" for name, _ in audio.calls)
    assert not any(name == "PlayMusic" for name, _ in audio.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode_cls", "clip"), CUSTOM_RUN_MODES)
async def test_custom_run_drain_discards_frames_and_keeps_disconnect_during_clip(mode_cls, clip):
    """Frames sent during the clip are read by the drain; a disconnect during it is kept (UC-002)."""
    log, client, audio, game = _run_game(mode_cls, clip, clip_seconds=0.4)
    dropped = SERIALS[0]
    seq_at_clip: list[int] = []

    def drop_controller():
        stream = client.streams[-1]
        seq_at_clip.append(stream.seq)
        # From now on the controller sends no frames, and its disconnect is in the next one.
        stream.gaps[dropped] = (stream.seq, None)
        stream.disconnect_at[stream.seq] = dropped

    audio.on_clip = drop_controller
    kept: list[list[str]] = []
    queued_at_drain_stop: list[int] = []
    real_stop = game._stop_early_stream_drain

    async def stop_drain(reopen=True):
        queued_at_drain_stop.append(client.streams[-1].queue.qsize())
        await real_stop(reopen=reopen)
        kept.append(list(game._warmup_disconnects))

    game._stop_early_stream_drain = stop_drain

    with patch.object(BaseGameMode, "_instructions_enabled", autospec=True, return_value=True):
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: bool(kept))
        await _stop(game, task, client)

    assert seq_at_clip, "the clip played"
    assert kept[0] == [dropped], "the disconnect seen during the clip waits for the game loop"
    # The 0.4 s clip produces ~40 frames; the drain read them, so they did not queue up.
    assert queued_at_drain_stop[0] <= 3, queued_at_drain_stop


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode_cls", "clip"), CUSTOM_RUN_MODES)
async def test_custom_run_force_end_during_intro_plays_no_clip(mode_cls, clip):
    """A force-end during the intro skips the clip: no PlaySound for it, no StopSound needed."""
    holder: list = []
    log, client, audio, game = _run_game(
        mode_cls, clip, clip_seconds=5.0, after_intro=lambda: holder[0].force_end("test_intro")
    )
    holder.append(game)

    with patch.object(BaseGameMode, "_instructions_enabled", autospec=True, return_value=True):
        task = asyncio.create_task(game.run())
        await asyncio.wait_for(task, timeout=10)
    for stream in client.streams:
        if stream.producer:
            stream.producer.cancel()

    assert INTRO_DONE in _event_order(log)
    assert clip not in audio.sounds()
    assert GameEvent.COUNTDOWN_START.value not in _event_order(log)
