"""Unit tests for per-channel volume flags (F7, #766).

Characterization: defaults reproduce GAME_VOLUME / COUNTDOWN_MUSIC_VOLUME.
Fallback: malformed flag values revert to the hardcoded defaults.
"""

from unittest.mock import MagicMock

from services.game_coordinator.games.base import (
    COUNTDOWN_MUSIC_VOLUME,
    GAME_VOLUME,
    _read_volume_flag,
)


def _patch_user_client(monkeypatch, client):
    monkeypatch.setattr("lib.feature_flags.init_flag_domain", lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr("lib.feature_flags.get_flag_client", lambda *_a, **_k: client, raising=False)


def test_default_constants_unchanged():
    assert GAME_VOLUME == 0.7
    assert COUNTDOWN_MUSIC_VOLUME == 0.15


def test_read_volume_returns_valid_value(monkeypatch):
    client = MagicMock()
    client.get_float_value.return_value = 0.5
    _patch_user_client(monkeypatch, client)
    assert _read_volume_flag("audio_volume.game", GAME_VOLUME) == 0.5


def test_read_volume_rejects_out_of_range(monkeypatch):
    client = MagicMock()
    client.get_float_value.return_value = 1.7
    _patch_user_client(monkeypatch, client)
    assert _read_volume_flag("audio_volume.game", GAME_VOLUME) == GAME_VOLUME

    client.get_float_value.return_value = -0.2
    assert _read_volume_flag("audio_volume.game", GAME_VOLUME) == GAME_VOLUME


def test_read_volume_rejects_non_numeric(monkeypatch):
    client = MagicMock()
    client.get_float_value.return_value = "loud"
    _patch_user_client(monkeypatch, client)
    assert _read_volume_flag("audio_volume.game", GAME_VOLUME) == GAME_VOLUME


def test_read_volume_rejects_bool(monkeypatch):
    client = MagicMock()
    client.get_float_value.return_value = True
    _patch_user_client(monkeypatch, client)
    assert _read_volume_flag("audio_volume.game", GAME_VOLUME) == GAME_VOLUME


def test_read_volume_falls_back_on_error(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("no flagd")

    monkeypatch.setattr("lib.feature_flags.init_flag_domain", _boom, raising=False)
    assert _read_volume_flag("audio_volume.countdown_music", COUNTDOWN_MUSIC_VOLUME) == COUNTDOWN_MUSIC_VOLUME


def test_base_init_uses_default_volumes_without_flagd(monkeypatch):
    """A constructed game mode exposes instance volumes equal to the constants.

    With flagd unavailable, _read_volume_flag returns its default, so the
    instance attributes match the original module constants (behavior-neutral).
    """
    from services.game_coordinator.games.ffa import FFAGame
    from services.game_coordinator.tests.test_base_game import (
        MockControllerManagerService,
        async_noop,
    )

    def _boom(*_a, **_k):
        raise RuntimeError("no flagd")

    monkeypatch.setattr("lib.feature_flags.init_flag_domain", _boom, raising=False)

    game = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=2),
        event_publisher=async_noop,
        audio_client=None,
        game_id="test_volume",
    )

    assert game.game_volume == GAME_VOLUME
    assert game.countdown_music_volume == COUNTDOWN_MUSIC_VOLUME
