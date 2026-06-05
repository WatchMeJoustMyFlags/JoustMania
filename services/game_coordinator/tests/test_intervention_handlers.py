"""
Unit tests for the ambient + session intervention handlers (#730, PR E).

Covers the real handlers wired by ``register_ambient_handlers``:
``audio_cue``, ``volume_override`` (apply + exact revert), ``controller_effect``
(broadcast vs per-serial routing, effect validation) and ``end_game`` (delegates
to the shared force-end path, no-ops without a game). Telemetry is disabled via
conftest; the intervention metric is patched per the repo convention.
"""

from unittest.mock import AsyncMock, patch

import pytest

from lib.types import GameEvent
from proto import controller_manager_pb2
from services.game_coordinator.games.base import GAME_VOLUME
from services.game_coordinator.interventions import (
    INTERVENTION_SPECS,
    InterventionContext,
    InterventionManager,
    _parse_effect_payload,
    _resolve_effect,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeFlagClient:
    def __init__(self, values=None):
        self.values = values or {}

    def set(self, key, value):
        self.values[key] = value

    def get_string_value(self, key, default, _ctx=None):
        return str(self.values.get(key, default))

    def get_integer_value(self, key, default, _ctx=None):
        return int(self.values.get(key, default))

    def get_float_value(self, key, default, _ctx=None):
        return float(self.values.get(key, default))

    def get_object_value(self, key, default, _ctx=None):
        return self.values.get(key, default)


class FakePlayer:
    def __init__(self, serial, alive=True):
        self.serial = serial
        self.alive = alive


class FakeGame:
    """Stand-in for a live BaseGameMode with an audio client + gameplay stream."""

    def __init__(self, name="Nonstop Joust", players=None, with_audio=True, with_stream=True):
        self._name = name
        self.players = players or {}
        self.audio_client = AsyncMock() if with_audio else None
        self.gameplay_stream = AsyncMock() if with_stream else None
        self.played_sounds = []

    def get_game_name(self):
        return self._name

    async def _play_sound(self, sound, priority=2, parent_context=None):
        self.played_sounds.append((sound, priority))


ALL_TYPES = {
    "play_audio_cue",
    "adjust_volume",
    "send_controller_effect",
    "end_game",
    "adjust_music_tempo",
    "adjust_global_sensitivity",
    "adjust_player_sensitivity",
    "grant_shield",
    "eliminate_player",
    "revive_player",
}


def make_manager(*, interventions=None, game=None, end_game_fn=None, budget=20):
    events = []

    async def publisher(event_type, data):
        events.append((event_type, data))

    agent_values = {
        "interventions_allowed": list(ALL_TYPES),
        "policy.max_interventions_per_minute": budget,
        "policy.battery_threshold": 20,
    }
    mgr = InterventionManager(
        event_publisher=publisher,
        get_game=lambda: game,
        end_game_fn=end_game_fn,
    )
    mgr._interventions_client = FakeFlagClient(interventions or {})
    mgr._agent_client = FakeFlagClient(agent_values)
    mgr._rate_limiter.set_budget(float(budget))
    mgr.register_ambient_handlers()
    return mgr, events


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_parse_effect_payload():
    assert _parse_effect_payload("ctrl_a:pulse") == ("ctrl_a", "pulse")
    assert _parse_effect_payload(":rumble") == ("", "rumble")
    assert _parse_effect_payload("ctrl_a:PULSE") == ("ctrl_a", "pulse")  # case-insensitive
    assert _parse_effect_payload("ctrl_a:") == ("ctrl_a", None)  # empty effect
    assert _parse_effect_payload("noeffect") == ("", None)  # no separator


def test_resolve_effect():
    assert _resolve_effect("rumble") == controller_manager_pb2.GAME_EFFECT_RUMBLE
    assert _resolve_effect("pulse") == controller_manager_pb2.GAME_EFFECT_PULSE
    assert _resolve_effect("flash") == controller_manager_pb2.GAME_EFFECT_FLASH
    assert _resolve_effect("show_battery") == controller_manager_pb2.GAME_EFFECT_SHOW_BATTERY
    assert _resolve_effect("player_death") is None  # not agent-addressable
    assert _resolve_effect("bogus") is None


# --------------------------------------------------------------------------- #
# audio_cue
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_audio_cue_plays_sound_via_game():
    game = FakeGame()
    mgr, _ = make_manager(interventions={"audio_cue": "1:wolfdown"}, game=game)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    assert game.played_sounds == [("wolfdown", 2)]


@pytest.mark.asyncio
async def test_audio_cue_applies_once_per_nonce():
    game = FakeGame()
    mgr, _ = make_manager(interventions={"audio_cue": "1:wolfdown"}, game=game)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
        await mgr.evaluate_all()  # same nonce -> no replay
    assert len(game.played_sounds) == 1
    with patch("services.game_coordinator.metrics.interventions_total"):
        mgr._interventions_client.set("audio_cue", "2:wolfdown")
        await mgr.evaluate_all()  # new nonce -> replay
    assert len(game.played_sounds) == 2


@pytest.mark.asyncio
async def test_audio_cue_empty_sound_is_defensive_noop():
    game = FakeGame()
    mgr, _ = make_manager(interventions={"audio_cue": "1: "}, game=game)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    assert game.played_sounds == []


@pytest.mark.asyncio
async def test_audio_cue_no_game_is_noop():
    mgr, events = make_manager(interventions={"audio_cue": "1:wolfdown"}, game=None)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    # Still dispatched (not blocked) — handler defensively returns.
    cue = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "play_audio_cue"]
    assert cue and cue[0]["blocked"] == "false"


# --------------------------------------------------------------------------- #
# volume_override
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_volume_override_applies_value():
    from proto import audio_pb2

    game = FakeGame()
    mgr, _ = make_manager(interventions={"volume_override": -1}, game=game)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()  # prime baseline (none)
        mgr._interventions_client.set("volume_override", 0.3)
        await mgr.evaluate_all()
    game.audio_client.SetVolume.assert_awaited_once()
    req = game.audio_client.SetVolume.await_args.args[0]
    assert isinstance(req, audio_pb2.SetVolumeRequest)
    assert abs(req.volume - 0.3) < 1e-6


@pytest.mark.asyncio
async def test_volume_override_restores_game_volume_on_revert():
    game = FakeGame()
    mgr, _ = make_manager(interventions={"volume_override": 0.3}, game=game)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()  # 0.3 applied
        mgr._interventions_client.set("volume_override", -1)
        await mgr.evaluate_all()  # revert -> restore GAME_VOLUME
    # First call set 0.3, second restored exactly GAME_VOLUME.
    calls = game.audio_client.SetVolume.await_args_list
    assert len(calls) == 2
    assert abs(calls[1].args[0].volume - GAME_VOLUME) < 1e-6


@pytest.mark.asyncio
async def test_volume_override_no_audio_client_is_noop():
    game = FakeGame(with_audio=False)
    mgr, _ = make_manager(interventions={"volume_override": -1}, game=game)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
        mgr._interventions_client.set("volume_override", 0.3)
        await mgr.evaluate_all()  # no client -> defensive no-op (no crash)


# --------------------------------------------------------------------------- #
# controller_effect
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_controller_effect_per_serial_routing():
    players = {"ctrl_a": FakePlayer("ctrl_a"), "ctrl_b": FakePlayer("ctrl_b")}
    game = FakeGame(players=players)
    mgr, _ = make_manager(interventions={"controller_effect": "1:ctrl_a:pulse"}, game=game)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    game.gameplay_stream.write.assert_awaited_once()
    cmd = game.gameplay_stream.write.await_args.args[0]
    assert cmd.game_effect.serial == "ctrl_a"
    assert cmd.game_effect.effect == controller_manager_pb2.GAME_EFFECT_PULSE


@pytest.mark.asyncio
async def test_controller_effect_broadcast_to_alive_players():
    players = {
        "ctrl_a": FakePlayer("ctrl_a", alive=True),
        "ctrl_b": FakePlayer("ctrl_b", alive=True),
        "ctrl_c": FakePlayer("ctrl_c", alive=False),  # dead -> excluded
    }
    game = FakeGame(players=players)
    mgr, _ = make_manager(interventions={"controller_effect": "1::flash"}, game=game)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    sent = [c.args[0].game_effect.serial for c in game.gameplay_stream.write.await_args_list]
    assert set(sent) == {"ctrl_a", "ctrl_b"}


@pytest.mark.asyncio
async def test_controller_effect_unknown_effect_is_noop():
    players = {"ctrl_a": FakePlayer("ctrl_a")}
    game = FakeGame(players=players)
    mgr, _ = make_manager(interventions={"controller_effect": "1:ctrl_a:disco"}, game=game)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    game.gameplay_stream.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_controller_effect_malformed_payload_is_noop():
    players = {"ctrl_a": FakePlayer("ctrl_a")}
    game = FakeGame(players=players)
    mgr, _ = make_manager(interventions={"controller_effect": "1:ctrl_a"}, game=game)  # no effect
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    game.gameplay_stream.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_controller_effect_no_stream_is_noop():
    players = {"ctrl_a": FakePlayer("ctrl_a")}
    game = FakeGame(players=players, with_stream=False)
    mgr, _ = make_manager(interventions={"controller_effect": "1:ctrl_a:pulse"}, game=game)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()  # no stream -> defensive no-op (no crash)


# --------------------------------------------------------------------------- #
# end_game
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_end_game_delegates_to_force_end():
    end = AsyncMock(return_value=(True, ""))
    game = FakeGame()
    mgr, _ = make_manager(interventions={"end_game": "1"}, game=game, end_game_fn=end)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    end.assert_awaited_once_with("agent_intervention")


@pytest.mark.asyncio
async def test_end_game_applies_once_per_nonce():
    end = AsyncMock(return_value=(True, ""))
    game = FakeGame()
    mgr, _ = make_manager(interventions={"end_game": "1"}, game=game, end_game_fn=end)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
        await mgr.evaluate_all()  # same nonce -> not re-ended
    end.assert_awaited_once()


@pytest.mark.asyncio
async def test_end_game_no_game_is_safe_noop():
    # end_game_fn reports "no game in progress"; handler must not raise.
    end = AsyncMock(return_value=(False, "No game in progress"))
    mgr, events = make_manager(interventions={"end_game": "1"}, game=None, end_game_fn=end)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    end.assert_awaited_once()
    eg = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "end_game"]
    assert eg and eg[0]["blocked"] == "false"  # dispatched; no-op handled internally


@pytest.mark.asyncio
async def test_end_game_no_fn_wired_is_noop():
    mgr, _ = make_manager(interventions={"end_game": "1"}, game=FakeGame(), end_game_fn=None)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()  # no end_game_fn -> defensive no-op (no crash)


# --------------------------------------------------------------------------- #
# Revert handler bypasses enforcement (always safe to restore default)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_volume_revert_runs_even_when_not_allowed():
    """Restoring the default volume on revert is not gated by the allow-list."""
    game = FakeGame()
    mgr, _ = make_manager(interventions={"volume_override": 0.3}, game=game)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()  # apply 0.3
    # Now disallow everything; revert should still restore.
    mgr._agent_client.set("interventions_allowed", [])
    with patch("services.game_coordinator.metrics.interventions_total"):
        mgr._interventions_client.set("volume_override", -1)
        await mgr.evaluate_all()
    calls = game.audio_client.SetVolume.await_args_list
    assert abs(calls[-1].args[0].volume - GAME_VOLUME) < 1e-6


def test_register_ambient_handlers_wires_all_four():
    mgr, _ = make_manager()
    assert mgr._handlers["audio_cue"] == mgr._handle_audio_cue
    assert mgr._handlers["volume_override"] == mgr._handle_volume_override
    assert mgr._handlers["controller_effect"] == mgr._handle_controller_effect
    assert mgr._handlers["end_game"] == mgr._handle_end_game
    assert mgr._revert_handlers["volume_override"] == mgr._handle_volume_revert


@pytest.mark.asyncio
async def test_handle_volume_revert_direct():
    """Direct unit test of the revert handler restoring GAME_VOLUME."""
    game = FakeGame()
    mgr, _ = make_manager(game=game)
    spec = next(s for s in INTERVENTION_SPECS if s.flag_key == "volume_override")
    ctx = InterventionContext(spec=spec, value=-1, payload="", target_serial=None, game=game, objective="balanced")
    await mgr._handle_volume_revert(ctx)
    req = game.audio_client.SetVolume.await_args.args[0]
    assert abs(req.volume - GAME_VOLUME) < 1e-6
