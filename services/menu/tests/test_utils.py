"""Unit tests for menu utility classes."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import grpc.aio
import pytest

from lib.colors import Colors
from lib.types import Sound
from services.menu.utils.audio import GAME_MODE_VOICE, AudioHelper
from services.menu.utils.led import DEFAULT_COLOR, GAME_MODE_COLORS, LedController
from services.menu.utils.settings import GAME_MODES, get_next_game_mode, is_valid_game_mode


class TestAudioHelper:
    """Tests for AudioHelper."""

    @pytest.fixture
    def audio(self):
        """Create AudioHelper instance."""
        return AudioHelper(MagicMock())

    def test_initialization(self, audio):
        """AudioHelper should initialize correctly."""
        assert audio.lobby_music_track_id is None

    def test_default_volumes_without_client(self, audio):
        """Without a user-prefs client, volumes equal the hardcoded defaults (F7, #766)."""
        from services.menu.utils.audio import (
            DEFAULT_LOBBY_MUSIC_VOLUME,
            DEFAULT_SOUND_VOLUME,
            DEFAULT_VOICE_VOLUME,
        )

        assert audio.sound_volume == DEFAULT_SOUND_VOLUME == 0.8
        assert audio.voice_volume == DEFAULT_VOICE_VOLUME == 0.9
        assert audio.lobby_music_volume == DEFAULT_LOBBY_MUSIC_VOLUME == 0.4

    def test_valid_volume_overrides_from_flags(self):
        """Valid user-domain flag values override the channel volumes."""
        values = {
            "audio_volume.menu_sound": 0.6,
            "audio_volume.menu_voice": 1.0,
            "audio_volume.lobby_music": 0.2,
        }
        client = MagicMock()
        client.get_float_value.side_effect = lambda name, default: values.get(name, default)

        audio = AudioHelper(MagicMock(), user_prefs_client=client)
        assert audio.sound_volume == 0.6
        assert audio.voice_volume == 1.0
        assert audio.lobby_music_volume == 0.2

    def test_malformed_volume_falls_back_to_default(self):
        """Out-of-range / non-numeric flag values fall back to defaults."""
        from services.menu.utils.audio import DEFAULT_SOUND_VOLUME, DEFAULT_VOICE_VOLUME

        bad = {"audio_volume.menu_sound": 5.0, "audio_volume.menu_voice": "loud"}
        client = MagicMock()
        client.get_float_value.side_effect = lambda name, default: bad.get(name, default)

        audio = AudioHelper(MagicMock(), user_prefs_client=client)
        assert audio.sound_volume == DEFAULT_SOUND_VOLUME
        assert audio.voice_volume == DEFAULT_VOICE_VOLUME

    @pytest.mark.asyncio
    async def test_play_sound_uses_configured_volume(self):
        """play_sound with no explicit volume uses the configured sound volume."""
        client = MagicMock()
        client.get_float_value.side_effect = lambda name, default: (
            0.55 if name == "audio_volume.menu_sound" else default
        )
        audio = AudioHelper(MagicMock(), user_prefs_client=client)

        with patch("proto.audio_pb2_grpc.AudioServiceStub") as mock_stub_class:
            mock_stub = MagicMock()
            mock_stub.PlaySound = AsyncMock()
            mock_stub_class.return_value = mock_stub

            await audio.play_sound("test/sound.ogg")

            assert mock_stub.PlaySound.call_args.args[0].volume == pytest.approx(0.55)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-031", flow="main", steps=[1])
    async def test_lobby_music_starts_at_lobby_music_volume(self):
        """UC-031 step 1: lobby music plays at audio_volume.lobby_music (volume set before the music starts)."""
        from proto import audio_pb2

        client = MagicMock()
        client.get_float_value.side_effect = lambda name, default: (
            0.25 if name == "audio_volume.lobby_music" else default
        )
        audio = AudioHelper(MagicMock(), user_prefs_client=client)
        calls: list = []

        class RecordingStub:
            def __init__(self, _channel):
                pass

            async def SetVolume(self, request):  # noqa: N802 - gRPC stub name
                calls.append(("SetVolume", request.volume))
                return audio_pb2.SetVolumeResponse(success=True)

            async def PlayMusic(self, request):  # noqa: N802 - gRPC stub name
                calls.append(("PlayMusic", request.file_pattern))
                return audio_pb2.PlayMusicResponse(track_id="lobby-1", success=True)

        with patch("proto.audio_pb2_grpc.AudioServiceStub", RecordingStub):
            await audio.start_lobby_music()

        assert calls == [("SetVolume", pytest.approx(0.25)), ("PlayMusic", "Menu/music/*.ogg")]
        assert audio.lobby_music_track_id == "lobby-1"

    @pytest.mark.asyncio
    async def test_play_sound_string(self, audio):
        """play_sound should handle string sound paths."""
        with patch("proto.audio_pb2_grpc.AudioServiceStub") as mock_stub_class:
            mock_stub = MagicMock()
            mock_stub.PlaySound = AsyncMock()
            mock_stub_class.return_value = mock_stub

            await audio.play_sound("test/sound.ogg", volume=0.5)

            mock_stub.PlaySound.assert_called_once()

    @pytest.mark.asyncio
    async def test_play_game_mode_voice(self, audio):
        """play_game_mode_voice should play correct voice for game mode."""
        audio.play_voice = AsyncMock()

        await audio.play_game_mode_voice("JoustFFA")

        audio.play_voice.assert_called_once()
        assert audio.play_voice.call_args[0][0] == GAME_MODE_VOICE["JoustFFA"]

    @pytest.mark.asyncio
    async def test_play_game_mode_voice_unknown(self, audio):
        """play_game_mode_voice should handle unknown game modes."""
        audio.play_voice = AsyncMock()

        await audio.play_game_mode_voice("UnknownGame")

        audio.play_voice.assert_not_called()


class TestLedController:
    """Tests for LedController."""

    @pytest.fixture
    def led(self):
        """Create LedController instance."""
        return LedController(MagicMock())

    def test_get_game_color_known(self, led):
        """get_game_color should return correct color for known game."""
        color = led.get_game_color("JoustFFA")
        assert color == GAME_MODE_COLORS["JoustFFA"]

    def test_get_game_color_unknown(self, led):
        """get_game_color should return default for unknown game."""
        color = led.get_game_color("UnknownGame")
        assert color == DEFAULT_COLOR

    def test_dim_color(self, led):
        """dim_color should reduce brightness."""
        color = (100, 200, 255)
        dimmed = led.dim_color(color, 0.5)
        assert dimmed == (50, 100, 127)

    def test_set_stream(self, led):
        """set_stream should store queue reference."""
        queue = asyncio.Queue()
        led.set_stream(queue)
        assert led._stream_queue is queue

    @pytest.mark.asyncio
    async def test_send_base_color_no_stream(self, led):
        """send_base_color should return False when no stream."""
        result = await led.send_base_color("serial1", (255, 0, 0))
        assert result is False

    @pytest.mark.asyncio
    async def test_send_base_color_with_stream(self, led):
        """send_base_color should send message to stream queue."""
        queue = asyncio.Queue()
        led.set_stream(queue)

        result = await led.send_base_color("serial1", (255, 0, 0))

        assert result is True
        assert not queue.empty()

    @pytest.mark.asyncio
    async def test_set_connected_color(self, led):
        """set_connected_color should set dimmed game mode color."""
        led.set_color = AsyncMock(return_value=True)

        await led.set_connected_color("serial1", "JoustFFA")

        led.set_color.assert_called_once()
        call_color = led.set_color.call_args[0][1]
        expected = led.dim_color(GAME_MODE_COLORS["JoustFFA"])
        assert call_color == expected

    @pytest.mark.asyncio
    async def test_set_ready_color(self, led):
        """set_ready_color should set bright game mode color."""
        led.set_color = AsyncMock(return_value=True)

        await led.set_ready_color("serial1", "JoustFFA")

        led.set_color.assert_called_once_with("serial1", GAME_MODE_COLORS["JoustFFA"])


class TestSettingsModuleFunctions:
    """Tests for settings module-level functions (replaced SettingsHelper)."""

    def test_get_next_game_mode_forward(self):
        """get_next_game_mode should cycle forward."""
        from lib.types import Games

        current_idx = GAME_MODES.index("JoustFFA")
        expected_name = GAME_MODES[(current_idx + 1) % len(GAME_MODES)]

        result = get_next_game_mode(Games.JoustFFA, forward=True)

        assert result.name == expected_name

    def test_get_next_game_mode_backward(self):
        """get_next_game_mode should cycle backward."""
        from lib.types import Games

        current_idx = GAME_MODES.index("JoustFFA")
        expected_name = GAME_MODES[(current_idx - 1) % len(GAME_MODES)]

        result = get_next_game_mode(Games.JoustFFA, forward=False)

        assert result.name == expected_name

    def test_is_valid_game_mode(self):
        """is_valid_game_mode should validate game modes."""
        from lib.types import Games

        assert is_valid_game_mode(Games.JoustFFA) is True
        assert is_valid_game_mode(Games.Werewolf) is True


class TestGameModesConstant:
    """Test GAME_MODES constant."""

    def test_game_modes_not_empty(self):
        """GAME_MODES should not be empty."""
        assert len(GAME_MODES) > 0

    def test_game_modes_are_unique(self):
        """All game modes should be unique."""
        assert len(GAME_MODES) == len(set(GAME_MODES))

    @pytest.mark.uc("UC-011", flow="main", steps=[2], br=["BR-001"])
    def test_random_not_in_game_modes(self):
        """Random should not be in GAME_MODES (it's meta)."""
        assert "Random" not in GAME_MODES


class TestUC011ModeSelectionRules:
    """UC-011 BR-001 (playable modes only) and A3 (Zombies / NonStop feedback)."""

    @pytest.mark.uc("UC-011", flow="main", steps=[2], br=["BR-001"])
    def test_ten_playable_modes_never_commander_ninja_random(self):
        """Exactly the ten implemented modes are offered; Commander, Ninja and Random never are."""
        from lib.types import Games

        assert len(GAME_MODES) == 10
        for excluded in (Games.Commander, Games.Ninja, Games.Random):
            assert excluded.name not in GAME_MODES
        # Cycling from every playable mode never lands on an excluded one.
        for name in GAME_MODES:
            assert get_next_game_mode(Games[name]).name in GAME_MODES
            assert get_next_game_mode(Games[name], forward=False).name in GAME_MODES

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="A3")
    @pytest.mark.parametrize(
        ("mode", "voice", "color"),
        [
            ("Zombies", Sound.MENU_VOX_ZOMBIES, Colors.White40.value),
            ("NonStop", Sound.MENU_VOX_NONSTOP_JOUST, Colors.Pink.value),
        ],
    )
    async def test_zombies_and_nonstop_feedback(self, mode, voice, color):
        """UC-011 A3: Zombies and NonStop use their existing voice and lobby colour."""
        channel = create_autospec(grpc.aio.Channel, instance=True, spec_set=True)
        audio = AudioHelper(channel)
        led = LedController(channel)

        with patch.object(AudioHelper, "play_voice", autospec=True) as play_voice:
            await audio.play_game_mode_voice(mode)
            play_voice.assert_awaited_once_with(audio, voice)

        assert led.get_game_color(mode) == color
        assert color != DEFAULT_COLOR

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-011", flow="main", steps=[3])
    async def test_all_playable_modes_have_voice_and_colour(self):
        """UC-011 main step 3: every playable enum name has voice and colour mappings."""
        channel = create_autospec(grpc.aio.Channel, instance=True, spec_set=True)
        audio = AudioHelper(channel)
        led = LedController(channel)

        assert set(GAME_MODES) <= GAME_MODE_VOICE.keys()
        assert set(GAME_MODES) <= GAME_MODE_COLORS.keys()
        with patch.object(AudioHelper, "play_voice", autospec=True) as play_voice:
            for mode in GAME_MODES:
                await audio.play_game_mode_voice(mode)
                play_voice.assert_awaited_once_with(audio, GAME_MODE_VOICE[mode])
                assert led.get_game_color(mode) == GAME_MODE_COLORS[mode]
                play_voice.reset_mock()
