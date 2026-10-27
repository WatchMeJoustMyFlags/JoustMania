"""
Unit tests for SoundChannel.
"""

import threading
import time
from unittest.mock import patch

import pytest

from proto import audio_pb2
from services.audio.servicer import SoundChannel


class TestSoundChannel:
    """Tests for SoundChannel class."""

    def test_init_state(self):
        channel = SoundChannel(channel_id=3)
        assert channel.channel_id == 3
        assert channel.is_playing is False
        assert channel.priority == 0

    def test_stop_sets_not_playing(self):
        channel = SoundChannel(channel_id=0)
        channel._playing.set()
        assert channel.is_playing is True
        channel.stop()
        assert channel.is_playing is False

    def test_play_nonexistent_file_returns_false(self):
        channel = SoundChannel(channel_id=0)
        result = channel.play("/nonexistent/path/to/file.ogg", volume=1.0, priority=2)
        assert result is False
        assert channel.is_playing is False

    def test_stop_idempotent(self):
        channel = SoundChannel(channel_id=0)
        channel.stop()
        channel.stop()
        assert channel.is_playing is False


# --- clip groups (#2107): PlaySoundRequest.group / StopSoundRequest.group ---------


def _fake_playback(self, _file_path, _duration, playing=None):
    """Stand-in for the device loop: 'plays' until this playback's flag is cleared."""
    playing = playing if playing is not None else self._playing
    deadline = time.monotonic() + 5.0
    while playing.is_set() and time.monotonic() < deadline:
        time.sleep(0.005)


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    return predicate()


@pytest.fixture
def playing_manager():
    """An AudioManager with three real channels and a fake device (no hardware)."""
    from services.audio.servicer import AudioManager, SoundChannel

    info = type("Info", (), {"duration": 3.0})()
    with (
        patch("services.audio.servicer.miniaudio.get_file_info", return_value=info),
        patch.object(SoundChannel, "_play_full_volume", _fake_playback),
        patch("services.audio.servicer.os.path.exists", return_value=True),
    ):
        manager = AudioManager(silent=True)
        manager.silent = False
        manager.channels = [SoundChannel(i) for i in range(3)]
        yield manager
        for channel in manager.channels:
            channel.stop()


def _playing_files(manager):
    return sorted(c.group for c in manager.channels if c.is_playing)


class TestSoundChannelPlaybackFlag:
    @pytest.mark.uc("UC-031", br=["BR-007"])
    def test_stopped_clip_cannot_end_the_next_clip_on_the_same_channel(self):
        """Each play has its own flag: the stopped clip's thread ending must not clear the new clip's flag."""
        from services.audio.servicer import SoundChannel

        info = type("Info", (), {"duration": 3.0})()
        started: list[threading.Event] = []

        def recording_playback(self, _path, _duration, playing=None):
            playing = playing if playing is not None else self._playing
            started.append(playing)
            gate = len(started)
            # The first clip lingers after its stop (a device closing), then its thread ends.
            deadline = time.monotonic() + 5.0
            while playing.is_set() and time.monotonic() < deadline:
                time.sleep(0.005)
            if gate == 1:
                time.sleep(0.05)

        channel = SoundChannel(0)
        with (
            patch("services.audio.servicer.miniaudio.get_file_info", return_value=info),
            patch.object(SoundChannel, "_play_full_volume", recording_playback),
        ):
            assert channel.play("a.ogg")
            assert _wait_until(lambda: len(started) == 1)
            channel.stop()
            assert channel.play("b.ogg")
            assert _wait_until(lambda: len(started) == 2)
            first, second = started
            assert not first.is_set()
            time.sleep(0.15)  # the first clip's thread has ended by now
            assert second.is_set(), "the stopped clip's thread cleared the new clip's flag"
            assert channel.is_playing
            channel.stop()


class TestClipGroups:
    @pytest.mark.uc("UC-031", br=["BR-007"])
    def test_second_clip_of_a_group_stops_the_first(self, playing_manager):
        assert playing_manager.play_sound("Menu/vox/gen_female/high.ogg", group="pad_preview")
        first = next(c for c in playing_manager.channels if c.is_playing)
        assert playing_manager.play_sound("Menu/vox/gen_female/low.ogg", group="pad_preview")
        playing = [c for c in playing_manager.channels if c.is_playing]
        assert len(playing) == 1
        assert playing[0].group == "pad_preview"
        assert first is playing[0] or not first.is_playing

    @pytest.mark.uc("UC-031", br=["BR-007"])
    def test_other_groups_and_ungrouped_clips_keep_playing(self, playing_manager):
        assert playing_manager.play_sound("Joust/sounds/beep_loud.ogg")
        assert playing_manager.play_sound("Menu/vox/gen_female/high.ogg", group="other")
        assert playing_manager.play_sound("Menu/vox/gen_female/low.ogg", group="pad_preview")
        assert playing_manager.play_sound("Menu/vox/gen_female/medium.ogg", group="pad_preview")
        assert _playing_files(playing_manager) == ["", "other", "pad_preview"]

    @pytest.mark.uc("UC-031", br=["BR-007"])
    def test_stop_sounds_with_a_group_stops_only_that_group(self, playing_manager):
        playing_manager.play_sound("Joust/sounds/beep_loud.ogg")
        playing_manager.play_sound("Menu/vox/gen_female/high.ogg", group="pad_preview")
        assert playing_manager.stop_sounds(group="pad_preview") == 1
        assert _playing_files(playing_manager) == [""]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-031", br=["BR-007"])
    async def test_stop_sound_rpc_passes_the_group(self, mock_audio_servicer, mock_grpc_context, playing_manager):
        mock_audio_servicer.audio_manager = playing_manager
        playing_manager.play_sound("Joust/sounds/beep_loud.ogg")
        playing_manager.play_sound("Menu/vox/gen_female/high.ogg", group="pad_preview")
        response = await mock_audio_servicer.StopSound(
            audio_pb2.StopSoundRequest(group="pad_preview"), mock_grpc_context
        )
        assert response.success and response.stopped == 1
        assert _playing_files(playing_manager) == [""]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[2])
    async def test_empty_stop_sound_still_stops_every_effect(
        self, mock_audio_servicer, mock_grpc_context, playing_manager
    ):
        """UC-020 A5 regression: a StopSound without a group stops grouped and ungrouped clips alike."""
        mock_audio_servicer.audio_manager = playing_manager
        playing_manager.play_sound("Joust/sounds/beep_loud.ogg")
        playing_manager.play_sound("Menu/vox/gen_female/high.ogg", group="pad_preview")
        response = await mock_audio_servicer.StopSound(audio_pb2.StopSoundRequest(), mock_grpc_context)
        assert response.success and response.stopped == 2
        assert _playing_files(playing_manager) == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-031", br=["BR-007"])
    async def test_play_sound_rpc_passes_the_group(self, mock_audio_servicer, mock_grpc_context):
        from unittest.mock import MagicMock

        mock_audio_servicer.audio_manager.play_sound = MagicMock(return_value=True)
        request = audio_pb2.PlaySoundRequest(file_path="high", group="pad_preview")
        await mock_audio_servicer.PlaySound(request, mock_grpc_context)
        assert mock_audio_servicer.audio_manager.play_sound.call_args.kwargs["group"] == "pad_preview"
