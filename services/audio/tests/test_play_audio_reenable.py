"""Regression tests for #1352: re-enabling play_audio after a muted start.

UC-031 A1: "turning it off at runtime stops the music, and turning it on again
resumes audio". These tests drive the real AudioServiceServicer and the real
AudioManager; only the hardware edges are replaced (SoundChannel.play would open
a miniaudio device, MusicPlayer spawns an ALSA subprocess).
"""

import threading
from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest

from proto import audio_pb2
from services.audio import servicer as servicer_module
from services.audio.music_player import DummyMusicPlayer, MusicPlayer
from services.audio.servicer import AudioManager, AudioServiceServicer, SoundChannel

ASSETS_DIR = Path(servicer_module.__file__).parent / "assets"


class _FakeUserFlagClient:
    """Stands in for the OpenFeature 'user' domain client: a mutable play_audio value."""

    def __init__(self, play_audio: bool):
        self.play_audio = play_audio

    def get_boolean_value(self, flag_key, default_value, *_args, **_kwargs):
        return self.play_audio if flag_key == "play_audio" else default_value

    def get_string_value(self, flag_key, default_value, *_args, **_kwargs):
        return "gen_female" if flag_key == "menu_voice" else default_value


@pytest.fixture
def flag_client():
    client = _FakeUserFlagClient(play_audio=False)
    with patch("lib.feature_flags.get_flag_client", return_value=client):
        yield client


@pytest.fixture
def muted_start_servicer(monkeypatch):
    """A servicer that started with play_audio=false (silent AudioManager)."""
    monkeypatch.setenv("AUDIO_ASSETS_DIR", str(ASSETS_DIR))

    def _fake_music_player(_name="music"):
        player = create_autospec(MusicPlayer, instance=True)
        player.start.return_value = "track-1"
        player.is_playing = False
        return player

    with (
        patch.object(AudioServiceServicer, "_load_play_audio_flag", return_value=False),
        patch.object(servicer_module, "MusicPlayer", side_effect=_fake_music_player) as music_player_cls,
        patch.object(SoundChannel, "play", autospec=True, return_value=True) as channel_play,
    ):
        svc = AudioServiceServicer()
        svc.channel_play = channel_play
        svc.music_player_cls = music_player_cls
        yield svc


class TestPlayAudioReenableAfterMutedStart:
    """UC-031 A1: muted at start, unmuted at runtime → audio resumes without a restart."""

    def test_starts_silent_when_flag_off(self, muted_start_servicer):
        """Precondition: play_audio=false at startup opens no audio hardware."""
        assert muted_start_servicer.audio_enabled is False
        assert muted_start_servicer.audio_manager.silent is True
        assert muted_start_servicer.audio_manager.channels == []
        assert isinstance(muted_start_servicer.audio_manager.music_player, DummyMusicPlayer)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-031", flow="A1", cond=["Pre-1"])
    async def test_flag_change_event_opens_hardware_and_play_sound_reaches_a_channel(
        self, muted_start_servicer, flag_client, mock_grpc_context
    ):
        """UC-031 A1: flipping play_audio on (change event) makes PlaySound reach a channel."""
        flag_client.play_audio = True
        muted_start_servicer._on_flags_changed(None)

        manager = muted_start_servicer.audio_manager
        assert muted_start_servicer.audio_enabled is True
        assert manager.silent is False
        assert len(manager.channels) == AudioManager.MAX_CHANNELS

        request = audio_pb2.PlaySoundRequest(file_path="beep_loud", volume=1.0, priority=2)
        response = await muted_start_servicer.PlaySound(request, mock_grpc_context)

        assert response.success is True
        muted_start_servicer.channel_play.assert_called_once()
        played_path = muted_start_servicer.channel_play.call_args.args[1]
        assert played_path.endswith("beep_loud.ogg")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-031", flow="A1")
    async def test_flag_read_at_use_time_without_change_event(
        self, muted_start_servicer, flag_client, mock_grpc_context
    ):
        """UC-031 A1: a missed change event still unmutes, because PlaySound reads play_audio at use time."""
        flag_client.play_audio = True

        request = audio_pb2.PlaySoundRequest(file_path="beep_loud", volume=1.0, priority=2)
        response = await muted_start_servicer.PlaySound(request, mock_grpc_context)

        assert response.success is True
        assert muted_start_servicer.audio_manager.silent is False
        muted_start_servicer.channel_play.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-031", flow="A1")
    async def test_play_music_after_unmute_uses_real_player_at_current_volume(
        self, muted_start_servicer, flag_client, mock_grpc_context
    ):
        """UC-031 A1: after unmuting, PlayMusic starts the real music player at the master volume set while muted."""
        muted_start_servicer.audio_manager.set_volume(0.4)  # SetVolume arrives while muted
        flag_client.play_audio = True

        request = audio_pb2.PlayMusicRequest(file_pattern="Joust/music/*.ogg", loop=True, tempo=1.0)
        response = await muted_start_servicer.PlayMusic(request, mock_grpc_context)

        player = muted_start_servicer.audio_manager.music_player
        assert response.success is True
        assert not isinstance(player, DummyMusicPlayer)
        assert response.track_id == "track-1"
        player.start.assert_called_once()
        player.set_volume.assert_any_call(0.4)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-031", flow="A1")
    async def test_muted_again_stays_silent_and_hardware_is_reused(
        self, muted_start_servicer, flag_client, mock_grpc_context
    ):
        """UC-031 A1: off → on → off → on keeps one set of channels and plays nothing while off."""
        flag_client.play_audio = True
        muted_start_servicer._on_flags_changed(None)
        channels = muted_start_servicer.audio_manager.channels

        flag_client.play_audio = False
        request = audio_pb2.PlaySoundRequest(file_path="beep_loud", volume=1.0, priority=2)
        response = await muted_start_servicer.PlaySound(request, mock_grpc_context)
        assert response.success is True
        muted_start_servicer.channel_play.assert_not_called()

        flag_client.play_audio = True
        muted_start_servicer._on_flags_changed(None)
        assert muted_start_servicer.audio_manager.channels is channels

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-031", flow="A1")
    async def test_failed_hardware_open_stays_muted_and_retries(
        self, muted_start_servicer, flag_client, mock_grpc_context
    ):
        """UC-031 A1: if opening the hardware fails, the service stays muted and the next call retries."""
        fake_player_factory = muted_start_servicer.music_player_cls.side_effect
        muted_start_servicer.music_player_cls.side_effect = [RuntimeError("no ALSA device"), fake_player_factory()]
        flag_client.play_audio = True
        request = audio_pb2.PlaySoundRequest(file_path="beep_loud", volume=1.0, priority=2)

        first = await muted_start_servicer.PlaySound(request, mock_grpc_context)
        assert first.success is True  # muted requests still succeed
        assert muted_start_servicer.audio_enabled is False
        assert muted_start_servicer.audio_manager.silent is True
        muted_start_servicer.channel_play.assert_not_called()

        second = await muted_start_servicer.PlaySound(request, mock_grpc_context)
        assert second.success is True
        assert muted_start_servicer.audio_enabled is True
        assert muted_start_servicer.audio_manager.silent is False
        muted_start_servicer.channel_play.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-031", flow="A1")
    async def test_use_time_unmute_opens_hardware_off_the_event_loop(
        self, muted_start_servicer, flag_client, mock_grpc_context
    ):
        """UC-031 A1: the music player spawn on a use-time unmute runs in a worker thread, not on the RPC loop."""
        fake_player_factory = muted_start_servicer.music_player_cls.side_effect
        spawn_threads: list[int] = []

        def _recording_factory(*args, **kwargs):
            spawn_threads.append(threading.get_ident())
            return fake_player_factory(*args, **kwargs)

        muted_start_servicer.music_player_cls.side_effect = _recording_factory
        flag_client.play_audio = True

        request = audio_pb2.PlaySoundRequest(file_path="beep_loud", volume=1.0, priority=2)
        await muted_start_servicer.PlaySound(request, mock_grpc_context)

        assert len(spawn_threads) == 1
        assert spawn_threads[0] != threading.get_ident()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-031", flow="A1")
    async def test_unreadable_flag_keeps_last_known_value(self, muted_start_servicer, mock_grpc_context):
        """UC-031 A1: when flagd cannot be read, the last known play_audio value stays in force."""
        request = audio_pb2.PlaySoundRequest(file_path="beep_loud", volume=1.0, priority=2)
        with patch("lib.feature_flags.get_flag_client", side_effect=Exception("flagd unavailable")):
            response = await muted_start_servicer.PlaySound(request, mock_grpc_context)

        assert response.success is True
        assert muted_start_servicer.audio_enabled is False
        assert muted_start_servicer.audio_manager.silent is True
        muted_start_servicer.channel_play.assert_not_called()
