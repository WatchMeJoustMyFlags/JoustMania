"""Operator master volume (audio_volume.master, #2050 PR2).

UC-015 BR-006: the operator's gain multiplies everything the audio service plays;
SetVolume keeps its relative meaning (UC-031 step 5). Only the hardware edges are
replaced (SoundChannel.play opens a miniaudio device, MusicPlayer spawns ALSA).
"""

import array
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, create_autospec, patch

import pytest

from proto import audio_pb2
from services.audio import servicer as servicer_module
from services.audio.music_player import MusicPlayer
from services.audio.servicer import AudioManager, AudioServiceServicer, SoundChannel

ASSETS_DIR = Path(servicer_module.__file__).parent / "assets"


class _FakeUserFlagClient:
    def __init__(self, master: float = 1.0):
        self.master = master

    def get_boolean_value(self, flag_key, default_value, *_a, **_k):
        return default_value

    def get_string_value(self, flag_key, default_value, *_a, **_k):
        return default_value

    def get_float_value(self, flag_key, default_value, *_a, **_k):
        return self.master if flag_key == "audio_volume.master" else default_value


@pytest.fixture
def flags():
    client = _FakeUserFlagClient()
    with patch("lib.feature_flags.get_flag_client", return_value=client):
        yield client


@pytest.fixture
def live_servicer(monkeypatch, flags):  # noqa: ARG001 (flags patches the flag client)
    """A servicer with live (non-silent) AudioManager and stubbed hardware."""
    monkeypatch.setenv("AUDIO_ASSETS_DIR", str(ASSETS_DIR))

    def _fake_music_player(_name="music"):
        player = create_autospec(MusicPlayer, instance=True)
        player.start.return_value = "track-1"
        player.is_playing = False
        return player

    with (
        patch.object(AudioServiceServicer, "_load_play_audio_flag", return_value=True),
        patch.object(servicer_module, "MusicPlayer", side_effect=_fake_music_player),
        patch.object(SoundChannel, "play", autospec=True, return_value=True) as channel_play,
        patch.object(servicer_module.os.path, "exists", return_value=True),
    ):
        svc = AudioServiceServicer()
        svc.channel_play = channel_play
        yield svc


def _sound_volume(svc) -> float:
    return svc.channel_play.call_args.kwargs["volume"]


@pytest.mark.uc("UC-015", br=["BR-006"])
class TestMasterMultiplier:
    def test_play_sound_multiplied_by_master(self, live_servicer):
        manager = live_servicer.audio_manager
        manager.set_operator_gain(0.5)
        manager.master_volume = 0.8
        assert manager.play_sound("Joust/sounds/beep.ogg", volume=0.5) is True
        assert _sound_volume(live_servicer) == pytest.approx(0.5 * 0.8 * 0.5)

    def test_play_music_multiplied_by_master(self, live_servicer):
        manager = live_servicer.audio_manager
        manager.set_operator_gain(0.6)
        manager.master_volume = 0.5
        manager.play_music("Joust/music/*.ogg")
        manager.music_player.set_volume.assert_called_with(pytest.approx(0.3))

    def test_set_volume_multiplied_by_master(self, live_servicer):
        manager = live_servicer.audio_manager
        manager.set_operator_gain(0.6)
        manager.set_volume(0.5)
        manager.music_player.set_volume.assert_called_with(pytest.approx(0.3))

    def test_enable_hardware_applies_master(self, flags):
        with patch.object(servicer_module, "MusicPlayer") as player_cls:
            manager = AudioManager(silent=True)
            manager.set_operator_gain(0.3)
            manager.master_volume = 0.5
            assert manager.enable_hardware() is True
            player_cls.return_value.set_volume.assert_called_with(pytest.approx(0.15))

    def test_set_volume_after_master_change_keeps_multiplier(self, live_servicer):
        """UC-031 step 5: SetVolume stays relative to the operator level."""
        flags_client = _FakeUserFlagClient(master=0.3)
        with patch("lib.feature_flags.get_flag_client", return_value=flags_client):
            live_servicer._on_flags_changed(None)
            live_servicer.SetVolume(audio_pb2.SetVolumeRequest(volume=0.5), None)
        manager = live_servicer.audio_manager
        assert manager.master_volume == 0.5
        manager.music_player.set_volume.assert_called_with(pytest.approx(0.15))
        manager.play_sound("Joust/sounds/beep.ogg", volume=1.0)
        assert _sound_volume(live_servicer) == pytest.approx(0.15)


@pytest.mark.uc("UC-015", br=["BR-006"])
class TestFlagChange:
    def test_flag_change_reapplies_music_volume(self, live_servicer, flags):
        manager = live_servicer.audio_manager
        manager.master_volume = 0.5
        flags.master = 0.6
        live_servicer._on_flags_changed(None)
        manager.music_player.set_volume.assert_called_with(pytest.approx(0.3))
        flags.master = 0.0
        live_servicer._on_flags_changed(None)
        manager.music_player.set_volume.assert_called_with(0.0)

    def test_unreadable_flag_keeps_last_gain(self, live_servicer):
        live_servicer.audio_manager.set_operator_gain(0.6)
        with patch("lib.feature_flags.get_flag_client", side_effect=RuntimeError("flagd down")):
            live_servicer._refresh_operator_gain()
        assert live_servicer.audio_manager.operator_gain == 0.6


@pytest.mark.uc("UC-015", br=["BR-006"])
class TestMute:
    def test_effective_zero_plays_no_sound(self, live_servicer):
        live_servicer.audio_manager.set_operator_gain(0.0)
        assert live_servicer.audio_manager.play_sound("Joust/sounds/beep.ogg", volume=1.0) is True
        live_servicer.channel_play.assert_not_called()

    def test_effective_zero_from_set_volume_plays_no_sound(self, live_servicer):
        live_servicer.audio_manager.set_volume(0.0)
        live_servicer.audio_manager.play_sound("Joust/sounds/beep.ogg", volume=1.0)
        live_servicer.channel_play.assert_not_called()

    def test_music_keeps_running_silent_so_unmute_restores_it(self, live_servicer, flags):
        manager = live_servicer.audio_manager
        manager.set_operator_gain(0.0)
        assert manager.play_music("Joust/music/*.ogg") == "track-1"
        manager.music_player.set_volume.assert_called_with(0.0)
        flags.master = 0.8
        live_servicer._on_flags_changed(None)
        manager.music_player.set_volume.assert_called_with(pytest.approx(0.8))


@pytest.mark.uc("UC-031", flow="main", steps=[5])
def test_play_with_volume_scales_samples_with_numpy():
    """Scaling truncates toward zero like the old int() loop and keeps the sample count."""
    decoded = SimpleNamespace(
        samples=array.array("h", [1000, -1000, 3, -3, 32767, -32768]),
        sample_width=2,
        nchannels=1,
        sample_format=None,
        sample_rate=44100,
    )
    sent = {}
    device = MagicMock()
    device.__enter__.return_value = device

    def start(stream):
        next_chunk = stream.send(1000)
        sent["data"] = next_chunk

    device.start.side_effect = start
    channel = SoundChannel(0)
    channel._playing.set()
    with (
        patch.object(servicer_module.miniaudio, "decode_file", return_value=decoded),
        patch.object(servicer_module.miniaudio, "PlaybackDevice", return_value=device),
        patch.object(SoundChannel, "_wait_for_playback"),
    ):
        channel._play_with_volume("x.ogg", 0.5, 1.0)
    out = array.array("h")
    out.frombytes(sent["data"])
    assert list(out) == [500, -500, 1, -1, 16383, -16384]
