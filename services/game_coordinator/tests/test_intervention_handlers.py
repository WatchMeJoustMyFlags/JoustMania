"""
Unit tests for the ambient + session intervention handlers (#730, PR E).

Covers the real handlers wired by ``register_ambient_handlers``:
``audio_cue``, ``volume_override`` (apply + exact revert), ``controller_effect``
(broadcast vs per-serial routing, effect validation) and ``end_game`` (delegates
to the shared force-end path, no-ops without a game). Telemetry is disabled via
conftest; the intervention metric is patched per the repo convention.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from openfeature.flag_evaluation import FlagEvaluationDetails

from lib.types import GameEvent
from proto import controller_manager_pb2
from services.game_coordinator.games.base import GAME_VOLUME, Player
from services.game_coordinator.interventions import (
    INTERVENTION_SPECS,
    InterventionContext,
    InterventionManager,
    _parse_effect_payload,
    _resolve_effect,
)
from services.game_coordinator.tests.conftest import FakeInterventionGame
from services.game_coordinator.tests.event_list_helpers import edge_event, object_details


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

    def get_object_details(self, key, _default, _ctx=None):
        # One-shot flags are per-game event lists read via details (FR-137).
        return object_details(key, self.values.get(key))

    def get_string_details(self, key, default, _ctx=None):
        return FlagEvaluationDetails(flag_key=key, value=self.get_string_value(key, default, _ctx))

    def get_integer_value(self, key, default, _ctx=None):
        return int(self.values.get(key, default))

    def get_float_value(self, key, default, _ctx=None):
        return float(self.values.get(key, default))

    def get_object_value(self, key, default, _ctx=None):
        return self.values.get(key, default)


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
        # interventions_allowed is a STRING flag: comma-separated ids (#1127).
        "interventions_allowed": ",".join(sorted(ALL_TYPES)),
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
    game = FakeInterventionGame(players={}, audio_client=AsyncMock(), gameplay_stream=AsyncMock())
    mgr, _ = make_manager(interventions={"audio_cue": edge_event("audio_cue", "wolfdown")}, game=game)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    assert game.sounds == [("wolfdown", 2)]


@pytest.mark.asyncio
async def test_audio_cue_applies_once_per_event():
    game = FakeInterventionGame(players={}, audio_client=AsyncMock(), gameplay_stream=AsyncMock())
    mgr, _ = make_manager(interventions={"audio_cue": edge_event("audio_cue", "wolfdown")}, game=game)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
        await mgr.evaluate_all()  # same event list -> no replay
    assert len(game.sounds) == 1
    with patch("services.game_coordinator.metrics.interventions_total"):
        mgr._interventions_client.set("audio_cue", edge_event("audio_cue", "wolfdown", seq=2))
        await mgr.evaluate_all()  # new event -> plays again
    assert len(game.sounds) == 2


@pytest.mark.asyncio
async def test_audio_cue_empty_sound_is_defensive_noop():
    game = FakeInterventionGame(players={}, audio_client=AsyncMock(), gameplay_stream=AsyncMock())
    mgr, _ = make_manager(interventions={"audio_cue": edge_event("audio_cue", " ")}, game=game)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    assert game.sounds == []


@pytest.mark.asyncio
async def test_audio_cue_no_game_is_noop():
    mgr, events = make_manager(interventions={"audio_cue": edge_event("audio_cue", "wolfdown")}, game=None)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    # Still dispatched (not blocked) — handler defensively returns.
    cue = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "play_audio_cue"]
    assert cue and cue[0]["blocked"] == "false"


# --------------------------------------------------------------------------- #
# volume_override
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.uc("UC-031", flow="A3")
async def test_volume_override_applies_value():
    from proto import audio_pb2

    game = FakeInterventionGame(players={}, audio_client=AsyncMock(), gameplay_stream=AsyncMock())
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
@pytest.mark.uc("UC-031", flow="A3")
async def test_volume_override_restores_game_volume_on_revert():
    game = FakeInterventionGame(players={}, audio_client=AsyncMock(), gameplay_stream=AsyncMock())
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
    game = FakeInterventionGame(players={}, gameplay_stream=AsyncMock())
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
    players = {"ctrl_a": Player("ctrl_a"), "ctrl_b": Player("ctrl_b")}
    game = FakeInterventionGame(players=players, audio_client=AsyncMock(), gameplay_stream=AsyncMock())
    mgr, _ = make_manager(
        interventions={"controller_effect": edge_event("controller_effect", "ctrl_a:pulse")}, game=game
    )
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    game.gameplay_stream.write.assert_awaited_once()
    cmd = game.gameplay_stream.write.await_args.args[0]
    assert cmd.game_effect.serial == "ctrl_a"
    assert cmd.game_effect.effect == controller_manager_pb2.GAME_EFFECT_PULSE


@pytest.mark.asyncio
async def test_controller_effect_broadcast_to_alive_players():
    players = {
        "ctrl_a": Player("ctrl_a", alive=True),
        "ctrl_b": Player("ctrl_b", alive=True),
        "ctrl_c": Player("ctrl_c", alive=False),  # dead -> excluded
    }
    game = FakeInterventionGame(players=players, audio_client=AsyncMock(), gameplay_stream=AsyncMock())
    mgr, _ = make_manager(interventions={"controller_effect": edge_event("controller_effect", ":flash")}, game=game)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    sent = [c.args[0].game_effect.serial for c in game.gameplay_stream.write.await_args_list]
    assert set(sent) == {"ctrl_a", "ctrl_b"}


@pytest.mark.asyncio
async def test_controller_effect_unknown_effect_is_noop():
    players = {"ctrl_a": Player("ctrl_a")}
    game = FakeInterventionGame(players=players, audio_client=AsyncMock(), gameplay_stream=AsyncMock())
    mgr, _ = make_manager(
        interventions={"controller_effect": edge_event("controller_effect", "ctrl_a:disco")}, game=game
    )
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    game.gameplay_stream.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_controller_effect_malformed_payload_is_noop():
    players = {"ctrl_a": Player("ctrl_a")}
    game = FakeInterventionGame(players=players, audio_client=AsyncMock(), gameplay_stream=AsyncMock())
    mgr, _ = make_manager(
        interventions={"controller_effect": edge_event("controller_effect", "ctrl_a")}, game=game
    )  # no effect
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    game.gameplay_stream.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_controller_effect_no_stream_is_noop():
    players = {"ctrl_a": Player("ctrl_a")}
    game = FakeInterventionGame(players=players, audio_client=AsyncMock())
    mgr, _ = make_manager(
        interventions={"controller_effect": edge_event("controller_effect", "ctrl_a:pulse")}, game=game
    )
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()  # no stream -> defensive no-op (no crash)


def _effect_ctx(game, payload):
    return InterventionContext(
        spec=next(s for s in INTERVENTION_SPECS if s.flag_key == "controller_effect"),
        value=payload,
        payload=payload,
        target_serial=None,
        game=game,
        objective="balanced",
    )


@pytest.mark.uc("UC-030", br=["BR-006"])
@pytest.mark.asyncio
async def test_controller_effect_stops_when_the_game_end_closes_the_stream():
    """The game end cancels the call under the write (#1914): stop, do not raise."""
    players = {"ctrl_a": Player("ctrl_a"), "ctrl_b": Player("ctrl_b")}
    game = FakeInterventionGame(players=players, audio_client=AsyncMock(), gameplay_stream=AsyncMock())
    game.gameplay_stream.write.side_effect = asyncio.CancelledError()
    mgr, _ = make_manager(game=game)
    try:
        await mgr._handle_controller_effect(_effect_ctx(game, ":flash"))
    except asyncio.CancelledError:
        pytest.fail("a write on a closed gameplay stream raised CancelledError out of the handler")
    game.gameplay_stream.write.assert_awaited_once()


@pytest.mark.uc("UC-030", br=["BR-006"])
@pytest.mark.asyncio
async def test_controller_effect_propagates_a_cancel_of_its_own_task():
    """A cancel of the dispatching task itself is not swallowed."""
    players = {"ctrl_a": Player("ctrl_a")}
    game = FakeInterventionGame(players=players, audio_client=AsyncMock(), gameplay_stream=AsyncMock())
    writing = asyncio.Event()

    async def blocked_write(_cmd):
        writing.set()
        await asyncio.Event().wait()

    game.gameplay_stream.write.side_effect = blocked_write
    mgr, _ = make_manager(game=game)
    task = asyncio.create_task(mgr._handle_controller_effect(_effect_ctx(game, "ctrl_a:pulse")))
    await asyncio.wait_for(writing.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


@pytest.mark.uc("UC-030", br=["BR-006"])
@pytest.mark.asyncio
async def test_controller_effect_stops_on_a_stream_that_finished_before_the_write():
    """grpc.aio rejects a write on a finished call with InvalidStateError (#1913): stop, do not raise."""
    players = {"ctrl_a": Player("ctrl_a"), "ctrl_b": Player("ctrl_b")}
    game = FakeInterventionGame(players=players, audio_client=AsyncMock(), gameplay_stream=AsyncMock())
    game.gameplay_stream.write.side_effect = asyncio.InvalidStateError("RPC already finished")
    mgr, _ = make_manager(game=game)
    try:
        await mgr._handle_controller_effect(_effect_ctx(game, ":flash"))
    except asyncio.InvalidStateError:
        pytest.fail("a write on a finished gameplay call raised out of the handler")
    game.gameplay_stream.write.assert_awaited_once()


@pytest.mark.uc("UC-030", br=["BR-006"])
@pytest.mark.asyncio
async def test_controller_effect_writes_to_the_stream_a_reopen_swapped_in():
    """A reopen during a broadcast: the remaining writes go to the new call (#1913)."""
    players = {"ctrl_a": Player("ctrl_a"), "ctrl_b": Player("ctrl_b")}
    game = FakeInterventionGame(players=players, audio_client=AsyncMock(), gameplay_stream=AsyncMock())
    old_stream, new_stream = game.gameplay_stream, AsyncMock()

    async def write_then_reopen(_cmd):
        game.gameplay_stream = new_stream

    old_stream.write.side_effect = write_then_reopen
    mgr, _ = make_manager(game=game)
    await mgr._handle_controller_effect(_effect_ctx(game, ":flash"))
    old_stream.write.assert_awaited_once()
    new_stream.write.assert_awaited_once()


# --------------------------------------------------------------------------- #
# end_game
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_end_game_delegates_to_force_end():
    end = AsyncMock(return_value=(True, ""))
    game = FakeInterventionGame(players={}, audio_client=AsyncMock(), gameplay_stream=AsyncMock())
    mgr, _ = make_manager(interventions={"end_game": edge_event("end_game")}, game=game, end_game_fn=end)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    # Legacy single-primary path: the sentinel session id is "" (= primary).
    end.assert_awaited_once_with("", "agent_intervention")


@pytest.mark.asyncio
async def test_end_game_applies_once_per_event():
    end = AsyncMock(return_value=(True, ""))
    game = FakeInterventionGame(players={}, audio_client=AsyncMock(), gameplay_stream=AsyncMock())
    mgr, _ = make_manager(interventions={"end_game": edge_event("end_game")}, game=game, end_game_fn=end)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
        await mgr.evaluate_all()  # same event list -> not re-ended
    end.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.uc("UC-027", flow="A3")
async def test_end_game_no_game_is_safe_noop():
    # end_game_fn reports "no game in progress"; handler must not raise.
    end = AsyncMock(return_value=(False, "No game in progress"))
    mgr, events = make_manager(interventions={"end_game": edge_event("end_game")}, game=None, end_game_fn=end)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    # addressed to the (legacy primary, empty id) session with the agent reason
    end.assert_awaited_once_with("", "agent_intervention")
    eg = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "end_game"]
    assert eg and eg[0]["blocked"] == "false"  # dispatched; no-op handled internally


@pytest.mark.asyncio
async def test_end_game_no_fn_wired_is_noop():
    mgr, _ = make_manager(
        interventions={"end_game": edge_event("end_game")},
        game=FakeInterventionGame(players={}, audio_client=AsyncMock(), gameplay_stream=AsyncMock()),
        end_game_fn=None,
    )
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()  # no end_game_fn -> defensive no-op (no crash)


# --------------------------------------------------------------------------- #
# Revert handler bypasses enforcement (always safe to restore default)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_volume_revert_runs_even_when_not_allowed():
    """Restoring the default volume on revert is not gated by the allow-list."""
    game = FakeInterventionGame(players={}, audio_client=AsyncMock(), gameplay_stream=AsyncMock())
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
    game = FakeInterventionGame(players={}, audio_client=AsyncMock(), gameplay_stream=AsyncMock())
    mgr, _ = make_manager(game=game)
    spec = next(s for s in INTERVENTION_SPECS if s.flag_key == "volume_override")
    ctx = InterventionContext(spec=spec, value=-1, payload="", target_serial=None, game=game, objective="balanced")
    await mgr._handle_volume_revert(ctx)
    req = game.audio_client.SetVolume.await_args.args[0]
    assert abs(req.volume - GAME_VOLUME) < 1e-6
