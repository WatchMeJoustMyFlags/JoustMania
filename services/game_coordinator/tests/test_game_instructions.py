"""UC-020 A5: spoken game instructions before the countdown (#1550).

When the host switched ``game_instructions`` on (UC-012 A3, FR-014), the mode's
instruction clip plays and the countdown waits until it has finished. Off, the
main scenario runs unchanged. The other modes' clips: test_game_instructions_modes.py (#2151).
"""

import asyncio
import time
from unittest.mock import create_autospec, patch

import pytest
from openfeature.client import OpenFeatureClient
from opentelemetry import trace

from lib.types import GameEvent, Sound
from proto import audio_pb2
from services.game_coordinator import game_clock
from services.game_coordinator.games.base import BaseGameMode
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.nonstop_joust import NonstopJoustGame
from services.game_coordinator.tests.conftest import EventCollector, MockControllerManagerService
from services.game_coordinator.tests.test_ffa_uc020 import (
    RecordingAudio,
    RecordingControllerManager,
    _event_types,
    _initial_players,
    fast_config,  # noqa: F401 - pytest fixture
)

INSTRUCTIONS = Sound.MENU_VOX_FFA_INSTRUCTIONS.value
CLIP_SECONDS = 0.4


class TimedAudio(RecordingAudio):
    """Recording audio stub that timestamps calls and reports a length for the instruction clip."""

    def __init__(self, clip_seconds: float = CLIP_SECONDS):
        super().__init__()
        self.clip_seconds = clip_seconds
        self.times: list[tuple[str, str, float]] = []

    def _stamp(self, name: str, request) -> None:
        self.times.append((name, getattr(request, "file_path", ""), game_clock.now()))

    async def PlaySound(self, request):  # noqa: N802 - gRPC stub name
        self._stamp("PlaySound", request)
        await super().PlaySound(request)
        duration = self.clip_seconds if request.file_path == INSTRUCTIONS else 0.0
        return audio_pb2.PlaySoundResponse(success=True, duration_seconds=duration)

    async def PlayMusic(self, request):  # noqa: N802 - gRPC stub name
        self._stamp("PlayMusic", request)
        return await super().PlayMusic(request)

    def first(self, name: str, file_path: str = "") -> float:
        return next(t for n, f, t in self.times if n == name and (not file_path or f == file_path))


class FailingInstructionsAudio(TimedAudio):
    """Timed audio stub whose PlaySound raises for the instruction clip (audio unreachable)."""

    async def PlaySound(self, request):  # noqa: N802 - gRPC stub name
        if request.file_path == INSTRUCTIONS:
            raise ConnectionError("audio unreachable")
        return await super().PlaySound(request)


def _flag_client(value=None, error: Exception | None = None):
    """Autospec'd OpenFeature client whose boolean read returns ``value`` or raises ``error``."""
    client = create_autospec(OpenFeatureClient, instance=True)
    if error is not None:
        client.get_boolean_value.side_effect = error
    else:
        client.get_boolean_value.return_value = value
    return client


def _patch_flag(client):
    return (
        patch("lib.feature_flags.get_flag_client", autospec=True, return_value=client),
        patch("lib.feature_flags.init_flag_domain", autospec=True),
    )


def _ffa(audio, *, players: int = 3, cm=None) -> tuple[FFAGame, EventCollector]:
    events = EventCollector()
    game = FFAGame(
        controller_manager_client=cm or MockControllerManagerService(num_controllers=players),
        event_publisher=events.publish,
        audio_client=audio,
        game_id="test_uc020_a5",
        initial_players=_initial_players(players),
    )
    return game, events


class TestUC020InstructionsOn:
    """UC-020 A5: instructions on."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[1, 2, 4])
    @pytest.mark.uc("UC-012", flow="A3", steps=[2])
    async def test_ffa_plays_clip_and_countdown_waits_for_it(self, fast_config, monkeypatch):  # noqa: F811
        """A5: the FFA clip plays first and the music and the countdown start only after it finished."""
        monkeypatch.setattr(BaseGameMode, "_INSTRUCTIONS_WAIT_SCALE", 1.0)  # the full clip length, on game time
        cm = RecordingControllerManager(num_controllers=3, death_schedule={1.0: 0, 1.6: 1}, max_duration=8.0)
        audio = TimedAudio()
        game, events = _ffa(audio, cm=cm)
        client = _flag_client(True)
        flag_patch, init_patch = _patch_flag(client)

        with flag_patch, init_patch:
            await asyncio.wait_for(game.run(), timeout=15.0)

        # The flag is read at use time, from the user set, defaulting to off.
        flag_key, default = client.get_boolean_value.call_args.args[:2]
        assert (flag_key, default) == ("game_instructions", False)
        # The clip is the first sound, played before the music and the countdown beeps.
        assert audio.sounds()[0] == INSTRUCTIONS
        assert audio.sounds().count(INSTRUCTIONS) == 1
        clip_at = audio.first("PlaySound", INSTRUCTIONS)
        # The countdown waits for the clip: music and the first beep start after its full length.
        assert audio.first("PlayMusic") - clip_at >= CLIP_SECONDS - 0.02
        assert audio.first("PlaySound", Sound.SFX_BEEP_LOUD.value) - clip_at >= CLIP_SECONDS - 0.02
        # Step 4: the game then continues at main step 2 and ends normally.
        types = _event_types(events)
        assert types.index(GameEvent.GAME_STARTED) < types.index(GameEvent.COUNTDOWN_START)
        assert events.get_events_of_type(GameEvent.GAME_WINNER) == [{"serial": "mock_controller_2"}]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[2, 3])
    async def test_records_played_attribute_and_event(self):
        """A5 step 3: game.instructions=played and a game.instructions_played event with clip and length."""
        audio = TimedAudio(clip_seconds=0.05)
        game, _ = _ffa(audio)
        game.running = True
        span = create_autospec(trace.Span, instance=True)
        flag_patch, init_patch = _patch_flag(_flag_client(True))

        with flag_patch, init_patch:
            await game._play_instructions(span)

        span.set_attribute.assert_called_once_with("game.instructions", "played")
        span.add_event.assert_called_once_with(
            "game.instructions_played",
            {"instructions.clip": INSTRUCTIONS, "instructions.duration_seconds": pytest.approx(0.05)},
        )
        request = audio.calls[0][1]
        assert request.file_path == INSTRUCTIONS
        assert request.priority == audio_pb2.HIGH

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[2])
    async def test_force_end_stops_the_wait(self):
        """A5 step 2: a force-end during the clip stops the wait at once."""
        audio = TimedAudio(clip_seconds=30.0)
        game, _ = _ffa(audio)
        game.running = True
        span = create_autospec(trace.Span, instance=True)
        flag_patch, init_patch = _patch_flag(_flag_client(True))

        with flag_patch, init_patch:
            task = asyncio.create_task(game._play_instructions(span))
            await asyncio.sleep(0.15)
            game.force_end("test")
            started = time.monotonic()
            await asyncio.wait_for(task, timeout=2.0)

        assert time.monotonic() - started < 0.5

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[2])
    async def test_clip_not_heard_means_no_wait(self):
        """A5 step 2: a clip the audio service reports as 0 s (muted, silent) does not hold the game."""
        audio = TimedAudio(clip_seconds=0.0)
        game, _ = _ffa(audio)
        game.running = True
        span = create_autospec(trace.Span, instance=True)
        flag_patch, init_patch = _patch_flag(_flag_client(True))

        started = time.monotonic()
        with flag_patch, init_patch:
            await game._play_instructions(span)

        assert time.monotonic() - started < 0.1
        assert audio.sounds() == [INSTRUCTIONS]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[2])
    async def test_absurd_duration_is_capped(self):
        """A5 step 2: the wait is capped at MAX_INSTRUCTIONS_WAIT_SECONDS (60 s)."""
        audio = TimedAudio(clip_seconds=10_000.0)
        game, _ = _ffa(audio)
        game.running = True
        span = create_autospec(trace.Span, instance=True)
        flag_patch, init_patch = _patch_flag(_flag_client(True))

        async def stop_after_first_poll(_seconds):
            game.running = False

        with (
            flag_patch,
            init_patch,
            patch("services.game_coordinator.games.base.asyncio.sleep", side_effect=stop_after_first_poll),
        ):
            await game._play_instructions(span)

        event_attrs = span.add_event.call_args.args[1]
        assert event_attrs["instructions.duration_seconds"] == 60.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[2, 3])
    async def test_play_sound_failure_records_failed_and_does_not_wait(self):
        """A5 step 3: PlaySound raising (audio unreachable) records failed, no played event, no wait."""
        audio = FailingInstructionsAudio()
        game, _ = _ffa(audio)
        game.running = True
        span = create_autospec(trace.Span, instance=True)
        flag_patch, init_patch = _patch_flag(_flag_client(True))

        started = time.monotonic()
        with flag_patch, init_patch:
            await game._play_instructions(span)

        assert time.monotonic() - started < 0.1
        span.set_attribute.assert_called_once_with("game.instructions", "failed")
        span.add_event.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[2, 4])
    async def test_play_sound_failure_still_starts_the_game(self, fast_config):  # noqa: F811
        """A5 step 4: a failed clip does not hold the game: it starts and ends normally."""
        cm = RecordingControllerManager(num_controllers=3, death_schedule={1.0: 0, 1.6: 1}, max_duration=8.0)
        audio = FailingInstructionsAudio()
        game, events = _ffa(audio, cm=cm)
        flag_patch, init_patch = _patch_flag(_flag_client(True))

        with flag_patch, init_patch:
            await asyncio.wait_for(game.run(), timeout=15.0)

        assert GameEvent.GAME_STARTED in _event_types(events)
        assert events.get_events_of_type(GameEvent.GAME_WINNER) == [{"serial": "mock_controller_2"}]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[2], br=["BR-003"])
    async def test_force_end_during_wait_skips_the_start(self, fast_config):  # noqa: F811
        """A5 step 2: force-ended during the clip, the game never starts and ends once as force_ended."""
        cm = RecordingControllerManager(num_controllers=3, infinite=True, max_duration=30.0)
        audio = TimedAudio(clip_seconds=30.0)
        game, events = _ffa(audio, cm=cm)
        flag_patch, init_patch = _patch_flag(_flag_client(True))

        with flag_patch, init_patch:
            task = asyncio.create_task(game.run())
            for _ in range(100):  # until the clip has been requested
                if INSTRUCTIONS in audio.sounds():
                    break
                await asyncio.sleep(0.02)
            game.force_end("admin_stop")
            await asyncio.wait_for(task, timeout=3.0)

        types = _event_types(events)
        assert GameEvent.GAME_STARTED not in types
        assert GameEvent.COUNTDOWN_START not in types
        assert cm.stream is None  # no gameplay stream
        assert not [name for name, _ in audio.calls if name == "PlayMusic"]
        assert audio.sounds() == [INSTRUCTIONS]  # no countdown beeps
        ended = events.get_events_of_type(GameEvent.GAME_ENDED)
        assert len(ended) == 1
        assert ended[0]["outcome"] == "force_ended"
        assert ended[0]["reason"] == "admin_stop"
        assert game.game_outcome() == "force_ended"
        assert GameEvent.GAME_WINNER not in types

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[2], br=["BR-003"])
    async def test_force_end_during_wait_stops_the_clip(self, fast_config):  # noqa: F811
        """A5 step 2 (#1586): a force-end during the clip sends StopSound once, after the clip started."""
        cm = RecordingControllerManager(num_controllers=3, infinite=True, max_duration=30.0)
        audio = TimedAudio(clip_seconds=30.0)
        game, _events = _ffa(audio, cm=cm)
        flag_patch, init_patch = _patch_flag(_flag_client(True))

        with flag_patch, init_patch:
            task = asyncio.create_task(game.run())
            for _ in range(100):
                if INSTRUCTIONS in audio.sounds():
                    break
                await asyncio.sleep(0.02)
            game.force_end("admin_stop")
            await asyncio.wait_for(task, timeout=3.0)

        names = [name for name, _ in audio.calls]
        assert names.count("StopSound") == 1
        assert names.index("PlaySound") < names.index("StopSound")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[2], br=["BR-003"])
    async def test_stop_sound_failure_does_not_block_the_end(self, fast_config):  # noqa: F811
        """A5 step 2 (#1586): StopSound failing is logged; the game still ends as force_ended."""

        class StopFailsAudio(TimedAudio):
            async def StopSound(self, request):  # noqa: N802 - gRPC stub name
                raise ConnectionError("audio unreachable")

        cm = RecordingControllerManager(num_controllers=3, infinite=True, max_duration=30.0)
        audio = StopFailsAudio(clip_seconds=30.0)
        game, events = _ffa(audio, cm=cm)
        flag_patch, init_patch = _patch_flag(_flag_client(True))

        with flag_patch, init_patch:
            task = asyncio.create_task(game.run())
            for _ in range(100):
                if INSTRUCTIONS in audio.sounds():
                    break
                await asyncio.sleep(0.02)
            game.force_end("admin_stop")
            await asyncio.wait_for(task, timeout=3.0)

        assert events.get_events_of_type(GameEvent.GAME_ENDED)[0]["outcome"] == "force_ended"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[2], br=["BR-003"])
    async def test_stop_sound_that_never_answers_is_bounded(self, fast_config):  # noqa: F811
        """A5 step 2 (#1586): a wedged audio service cannot stall the end; it times out and force_ended is recorded."""

        class StopHangsAudio(TimedAudio):
            async def StopSound(self, request):  # noqa: N802 - gRPC stub name
                await asyncio.sleep(3600)

        cm = RecordingControllerManager(num_controllers=3, infinite=True, max_duration=30.0)
        audio = StopHangsAudio(clip_seconds=30.0)
        game, events = _ffa(audio, cm=cm)
        flag_patch, init_patch = _patch_flag(_flag_client(True))

        with flag_patch, init_patch, patch("services.game_coordinator.games.base.STOP_SOUND_TIMEOUT_SECONDS", 0.1):
            task = asyncio.create_task(game.run())
            for _ in range(100):
                if INSTRUCTIONS in audio.sounds():
                    break
                await asyncio.sleep(0.02)
            game.force_end("admin_stop")
            await asyncio.wait_for(task, timeout=3.0)

        assert events.get_events_of_type(GameEvent.GAME_ENDED)[0]["outcome"] == "force_ended"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[2])
    async def test_clip_that_finishes_is_not_stopped(self, fast_config):  # noqa: F811
        """A5 step 2 (#1586): without a force-end the clip plays out and no StopSound is sent."""
        audio = TimedAudio(clip_seconds=0.1)
        game, _events = _ffa(audio)
        flag_patch, init_patch = _patch_flag(_flag_client(True))

        with flag_patch, init_patch:
            await asyncio.wait_for(game.run(), timeout=10.0)

        assert "StopSound" not in [name for name, _ in audio.calls]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A4", steps=[1, 3], br=["BR-003"])
    async def test_force_end_during_colour_assignment_skips_the_start(self, fast_config):  # noqa: F811
        """A4: a force-end before the start (here during colour assignment) skips it; one game_ended, force_ended."""
        cm = RecordingControllerManager(num_controllers=3, infinite=True, max_duration=30.0)
        audio = TimedAudio()
        game, events = _ffa(audio, cm=cm)
        flag_patch, init_patch = _patch_flag(_flag_client(False))
        assign_colors = game._assign_ffa_colors

        async def force_end_while_assigning():
            await assign_colors()
            game.force_end("admin_stop")

        with flag_patch, init_patch, patch.object(game, "_assign_ffa_colors", force_end_while_assigning):
            await asyncio.wait_for(game.run(), timeout=5.0)

        types = _event_types(events)
        assert GameEvent.GAME_STARTED not in types
        assert GameEvent.COUNTDOWN_START not in types
        assert cm.stream is None
        assert not [name for name, _ in audio.calls if name == "PlayMusic"]
        ended = events.get_events_of_type(GameEvent.GAME_ENDED)
        assert len(ended) == 1
        assert ended[0]["outcome"] == "force_ended"


class TestUC020InstructionsOff:
    """UC-020 A5 step 1: off, unreadable, shadow or no clip: nothing plays, no wait."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[1])
    async def test_off_plays_nothing_and_main_scenario_is_unchanged(self, fast_config):  # noqa: F811
        """A5 step 1: with the flag off, no clip plays and the music starts right away."""
        cm = RecordingControllerManager(num_controllers=3, death_schedule={1.0: 0, 1.6: 1}, max_duration=8.0)
        audio = TimedAudio()
        game, events = _ffa(audio, cm=cm)
        flag_patch, init_patch = _patch_flag(_flag_client(False))

        with flag_patch, init_patch:
            await asyncio.wait_for(game.run(), timeout=15.0)

        assert INSTRUCTIONS not in audio.sounds()
        assert audio.calls[0][0] == "SetVolume"  # main step 2: music at the countdown volume first
        assert events.get_events_of_type(GameEvent.GAME_WINNER) == [{"serial": "mock_controller_2"}]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[1, 3])
    @pytest.mark.parametrize(
        ("client", "expected"),
        [
            (_flag_client(False), "off"),
            (_flag_client(error=RuntimeError("flagd down")), "off"),  # fails closed
            (_flag_client("on"), "off"),  # a non-bool value is not "on"
        ],
        ids=["off", "read-error", "non-bool"],
    )
    async def test_off_or_unreadable_records_off(self, client, expected):
        """A5 step 1: off or unreadable means off: no PlaySound, game.instructions=off."""
        audio = TimedAudio()
        game, _ = _ffa(audio)
        game.running = True
        span = create_autospec(trace.Span, instance=True)
        flag_patch, init_patch = _patch_flag(client)

        with flag_patch, init_patch:
            await game._play_instructions(span)

        assert audio.calls == []
        span.set_attribute.assert_called_once_with("game.instructions", expected)
        span.add_event.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[1, 3])
    async def test_shadow_game_skips_instructions(self):
        """A5 step 1: a shadow game has no audience: no clip, game.instructions=shadow, flag not read."""
        audio = TimedAudio()
        game, _ = _ffa(audio)
        game.game_kind = "shadow"
        span = create_autospec(trace.Span, instance=True)
        client = _flag_client(True)
        flag_patch, init_patch = _patch_flag(client)

        with flag_patch, init_patch:
            await game._play_instructions(span)

        assert audio.calls == []
        client.get_boolean_value.assert_not_called()
        span.set_attribute.assert_called_once_with("game.instructions", "shadow")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[3])
    async def test_mode_without_clip_records_no_clip(self):
        """A5: a mode without a clip (Non-Stop Joust, no recording) plays nothing and does not wait."""
        audio = TimedAudio()
        game = NonstopJoustGame(
            controller_manager_client=MockControllerManagerService(num_controllers=2),
            event_publisher=EventCollector().publish,
            audio_client=audio,
            game_id="test_uc020_a5_nonstop",
        )
        game.running = True
        span = create_autospec(trace.Span, instance=True)
        flag_patch, init_patch = _patch_flag(_flag_client(True))

        with flag_patch, init_patch:
            await game._play_instructions(span)

        assert game.get_instruction_clip() is None
        assert audio.calls == []
        span.set_attribute.assert_called_once_with("game.instructions", "no_clip")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[3])
    async def test_no_audio_client_records_no_audio(self):
        """A5 step 3: without an audio client nothing plays: game.instructions=no_audio, no wait."""
        game, _ = _ffa(None)
        game.running = True
        span = create_autospec(trace.Span, instance=True)
        flag_patch, init_patch = _patch_flag(_flag_client(True))

        with flag_patch, init_patch:
            await game._play_instructions(span)

        span.set_attribute.assert_called_once_with("game.instructions", "no_audio")
        span.add_event.assert_not_called()
