"""
Player-activated shield charge in the game coordinator (#1837 PR-3, FR-160).

UC-040 A30 (charge granted), A31 (grant while charged), A32 (charge ends unused),
BR-019 (activation mode, FFA allow-list), BR-020 (one charge), BR-021 (feedback);
UC-030 A6 (the player activates the charge), BR-005 (trigger press).

Every test runs on a fake clock (``base.time.time``) with the two flags patched.
"""

import asyncio
import math
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest
from opentelemetry.trace import INVALID_SPAN_CONTEXT, Span

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from lib.types import GameEvent, Sound
from proto import controller_manager_pb2
from services.game_coordinator import game_clock
from services.game_coordinator.game_factory import _GAME_CLASSES
from services.game_coordinator.games import base as base_module
from services.game_coordinator.games.base import (
    BaseGameMode,
    Player,
    resolve_charge_expiry,
    resolve_shield_activation,
)
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.nonstop_joust import NonstopJoustGame
from services.game_coordinator.intervention_errors import InterventionNotApplied

SHIELD = controller_manager_pb2.GAME_EFFECT_SHIELD
CHARGE = controller_manager_pb2.GAME_EFFECT_SHIELD_CHARGE
SWELL = controller_manager_pb2.GAME_EFFECT_SHIELD_SWELL
DEATH = controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH
T0 = 1000.0


@pytest.fixture(autouse=True)
def _no_shield_limits():
    """Shield limits off (#2044, UC-040 BR-022): these tests are not about the cap or the cooldown."""
    with patch.object(BaseGameMode, "_shield_limits", return_value=(0, 0)):
        yield


class RecordingStream:
    def __init__(self):
        self.writes = []

    async def write(self, message):
        self.writes.append(message)

    def effects(self):
        """(serial, effect, duration_ms) per game effect, without the help cue PULSE.

        The cue (UC-040 A42, BR-031) is pinned in test_shield_help_cue.py.
        """
        return [
            (m.game_effect.serial, m.game_effect.effect, m.game_effect.duration_ms)
            for m in self.writes
            if m.HasField("game_effect") and m.game_effect.effect != controller_manager_pb2.GAME_EFFECT_PULSE
        ]


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class Flags:
    """The two shield flags; any other key resolves to the caller's default."""

    def __init__(self):
        self.values = {"shield.activation": "trigger", "shield.charge_expiry_seconds": 20.0}
        self.reads = 0
        self.keys: list[str] = []

    def read(self, _set, key, default, **_kw):
        self.reads += 1
        self.keys.append(key)
        return self.values.get(key, default)


@pytest.fixture
def clock():
    c = Clock()
    # The game clock drives the deadlines (UC-030 BR-010); the wall clock the wire timestamps.
    with (
        patch.object(base_module.time, "time", side_effect=lambda: c.t),
        patch.object(game_clock, "now", side_effect=lambda: c.t),
    ):
        yield c


@pytest.fixture
def flags():
    f = Flags()
    with (
        patch.object(base_module, "read_string_flag", side_effect=f.read),
        patch.object(base_module, "read_float_flag", side_effect=f.read),
    ):
        yield f


def _make_ffa(events):
    g = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=2),
        event_publisher=events.publish,
        audio_client=None,
        game_id="test_shield_charge",
    )
    g.gameplay_stream = RecordingStream()
    for serial in ("p1", "p2"):
        g.players[serial] = Player(serial=serial, alive=True, color=(0, 255, 0))
    return g


@pytest.fixture
def events():
    return EventCollector()


@pytest.fixture
def game(clock, flags, events):  # noqa: ARG001 - clock and flags patch base for the test
    g = _make_ffa(events)
    with patch.object(g, "_play_sound", new_callable=AsyncMock):
        yield g


def frame(serial, trigger=False, accel=(0.0, 0.0, 1.0)):
    return controller_manager_pb2.GameplayData(
        serial=serial,
        accel=controller_manager_pb2.Vector3(x=accel[0], y=accel[1], z=accel[2]),
        trigger_pressed=trigger,
    )


async def feed(game, serial, *triggers, accel=(0.0, 0.0, 1.0)):
    for t in triggers:
        await game._process_controller_state(frame(serial, t, accel))


async def arm(game, serial="p1"):
    """One released frame: the trigger read released once in the game loop (BR-005)."""
    await feed(game, serial, False)


def effects_of(game, *kinds):
    return [e for e in game.gameplay_stream.effects() if e[1] in kinds]


# --------------------------------------------------------------------------- #
# UC-040 A30: the grant becomes a charge
# --------------------------------------------------------------------------- #
class TestUC040A30ChargeGranted:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-019", "BR-021"])
    async def test_trigger_grant_stores_a_charge_without_protection(self, game, events):
        """A30: grace_until unchanged, SHIELD_CHARGE for the expiry, player_shield_charged, no shield yet."""
        assert await game.grant_shield("p1", 5.0) is True

        player = game.players["p1"]
        assert player.grace_until == 0.0
        assert player.shield_charge_seconds == 5.0
        assert player.shield_charge_expires_at == T0 + 20.0
        assert game.gameplay_stream.effects() == [("p1", CHARGE, 20_000)]
        [charged] = events.get_events_of_type(GameEvent.PLAYER_SHIELD_CHARGED)
        assert charged == {"serial": "p1", "seconds": "5.0", "expires_ms": str(int((T0 + 20.0) * 1000))}
        assert events.get_events_of_type(GameEvent.PLAYER_SHIELDED) == []
        game._play_sound.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    async def test_never_expiring_charge_is_capped_at_120_s_and_reports_zero(self, game, flags, events):
        """A30 / BR-021: expiry 0 = never: the layer is capped at 120 s and expires_ms is "0"."""
        flags.values["shield.charge_expiry_seconds"] = 0
        await game.grant_shield("p1", 5.0)

        assert game.gameplay_stream.effects() == [("p1", CHARGE, 120_000)]
        assert events.get_events_of_type(GameEvent.PLAYER_SHIELD_CHARGED)[0]["expires_ms"] == "0"
        assert game.players["p1"].shield_charge_expires_at == 0.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30")
    async def test_span_event_and_charge_span(self, game, clock):
        """A30 step 3: shield_charge_granted on the player span; game.shield_charge ends at activation."""
        player_span = create_autospec(Span, instance=True)
        player_span.get_span_context.return_value = INVALID_SPAN_CONTEXT
        game.players["p1"].span = player_span
        charge_span = create_autospec(Span, instance=True)
        with patch.object(base_module.tracer, "start_span", return_value=charge_span) as start:
            await game.grant_shield("p1", 5.0)
        assert start.call_args.args[0] == "game.shield_charge"
        assert start.call_args.kwargs["attributes"]["shield.charge_expiry_s"] == 20.0
        granted = [
            c.kwargs["attributes"] for c in player_span.add_event.call_args_list if c.args[0] == "shield_charge_granted"
        ]
        assert granted == [{"shield.seconds": 5.0, "shield.charge_expiry_s": 20.0, "player.serial": "p1"}]

        await arm(game)
        clock.t += 2.5
        await feed(game, "p1", True)

        charge_span.set_attribute.assert_any_call("shield.charge_outcome", "activated")
        charge_span.set_attribute.assert_any_call("shield.charge_delay_ms", 2500)
        charge_span.end.assert_called_once()
        activated = [
            c.kwargs["attributes"] for c in player_span.add_event.call_args_list if c.args[0] == "shield_activated"
        ]
        assert activated[0]["shield.charge_delay_ms"] == 2500
        feedback = [
            c.kwargs["attributes"] for c in player_span.add_event.call_args_list if c.args[0] == "shield_feedback"
        ]
        assert feedback[0]["shield.activation"] == "trigger"


# --------------------------------------------------------------------------- #
# UC-040 A31 / BR-020: one charge per player
# --------------------------------------------------------------------------- #
class TestUC040A31AlreadyCharged:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A31", br=["BR-020"])
    async def test_second_grant_applies_nothing(self, game, events):
        """A31 step 2: a grant while charged raises already_charged and sends nothing."""
        await game.grant_shield("p1", 5.0)
        game.gameplay_stream.writes.clear()

        with pytest.raises(InterventionNotApplied) as exc:
            await game.grant_shield("p1", 8.0)

        assert exc.value.reason == "already_charged"
        assert game.players["p1"].shield_charge_seconds == 5.0
        assert game.gameplay_stream.effects() == []
        assert len(events.get_events_of_type(GameEvent.PLAYER_SHIELD_CHARGED)) == 1

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A31", br=["BR-020"])
    async def test_handler_raises_for_the_targeted_serial(self, game):
        """A31: handle_shield_seconds lets already_charged reach the manager."""
        from services.game_coordinator.interventions import INTERVENTION_SPECS, InterventionContext
        from services.game_coordinator.lifecycle_handlers import handle_shield_seconds

        spec = next(s for s in INTERVENTION_SPECS if s.flag_key == "shield_seconds")
        ctx = InterventionContext(spec=spec, value=5.0, payload="", target_serial="p1", game=game, objective="balanced")
        manager = MagicMock()
        manager.resolve_player_targets.return_value = {"p1": 5.0}
        with patch("services.game_coordinator.lifecycle_handlers._grant_values", return_value={"p1": 5.0}):
            await handle_shield_seconds(ctx, manager)
            with pytest.raises(InterventionNotApplied):
                await handle_shield_seconds(ctx, manager)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A31", br=["BR-020"])
    async def test_fan_out_counts_a_refused_grant_as_not_applied(self, game):
        """A31: a dead serial (grant False) next to an already charged one still raises."""
        from services.game_coordinator.interventions import INTERVENTION_SPECS, InterventionContext
        from services.game_coordinator.lifecycle_handlers import handle_shield_seconds

        await game.grant_shield("p2", 5.0)  # p2 holds a charge
        game.players["p1"].alive = False  # grant_shield returns False for p1
        spec = next(s for s in INTERVENTION_SPECS if s.flag_key == "shield_seconds")
        ctx = InterventionContext(spec=spec, value=5.0, payload="", target_serial=None, game=game, objective="balanced")
        with (
            patch(
                "services.game_coordinator.lifecycle_handlers._grant_values",
                return_value={"p1": 5.0, "p2": 5.0},
            ),
            pytest.raises(InterventionNotApplied),
        ):
            await handle_shield_seconds(ctx, MagicMock())

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A31", br=["BR-020"])
    @pytest.mark.uc("UC-040", flow="A22")
    async def test_manager_records_already_charged_and_refunds_the_backstop(self):
        """A31 step 2 / A22: blocked=true, block_reason=already_charged, the reservation is refunded."""
        from services.game_coordinator.tests.event_list_helpers import edge_event
        from services.game_coordinator.tests.test_interventions import make_manager

        mgr, published = make_manager(interventions={"audio_cue": edge_event("audio_cue", "beep")})

        async def not_applied(_ctx):
            raise InterventionNotApplied("already_charged")

        mgr.register_handler("audio_cue", not_applied)
        with patch("services.game_coordinator.metrics.interventions_total"):
            await mgr.evaluate_all()

        cue = [d for et, d in published if et == GameEvent.AGENT_INTERVENTION and d["type"] == "play_audio_cue"]
        assert cue[0]["blocked"] == "true"
        assert cue[0]["block_reason"] == "already_charged"
        assert mgr._rate_limiter.current_weight() == 0.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-020"])
    async def test_grant_while_a_shield_is_active_waits_under_it(self, game, clock):
        """BR-020: a grant during an active shield is accepted; presses are ignored until it ends."""
        game.players["p1"].grace_until = T0 + 3.0
        assert await game.grant_shield("p1", 5.0) is True
        await arm(game)
        await feed(game, "p1", True, False)  # press while protected
        assert game.players["p1"].shield_charge_seconds == 5.0

        clock.t += 4.0
        await feed(game, "p1", True)
        assert game.players["p1"].shield_charge_seconds == 0.0
        assert game.players["p1"].grace_until == pytest.approx(clock.t + 5.0)


# --------------------------------------------------------------------------- #
# UC-030 A6 / BR-005: the player activates the charge with the trigger
# --------------------------------------------------------------------------- #
class TestUC030A6Activation:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A6", br=["BR-005"])
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-021"])
    async def test_press_activates_the_shield_with_swell_sound_and_events(self, game, clock, events):
        """A6 steps 1-2: grace_until = now + s; CHARGE 0, SHIELD s, SWELL; shield_up; ended + shielded."""
        await game.grant_shield("p1", 5.0)
        await arm(game)
        game.gameplay_stream.writes.clear()
        clock.t += 3.0

        await feed(game, "p1", True)

        player = game.players["p1"]
        assert player.grace_until == T0 + 3.0 + 5.0
        assert player.shield_charge_seconds == 0.0
        assert game.gameplay_stream.effects() == [("p1", CHARGE, 0), ("p1", SHIELD, 5000), ("p1", SWELL, 0)]
        game._play_sound.assert_awaited_once_with(Sound.SFX_SHIELD_UP, priority=2, volume=1.0)
        assert events.get_events_of_type(GameEvent.PLAYER_SHIELD_CHARGE_ENDED) == [
            {"serial": "p1", "reason": "activated"}
        ]
        [shielded] = events.get_events_of_type(GameEvent.PLAYER_SHIELDED)
        assert shielded["activation"] == "trigger"
        assert shielded["charge_delay_ms"] == "3000"
        assert shielded["kind"] == "full"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A6", br=["BR-005"])
    async def test_sound_plays_even_while_a_toughness_boost_runs(self, game):
        """A6 step 2: the player acted, so shield_up plays even when already protected by a boost."""
        game.players["p1"].toughness_boost_until = T0 + 30.0
        await game.grant_shield("p1", 5.0)
        await arm(game)
        await feed(game, "p1", True)
        game._play_sound.assert_awaited_once_with(Sound.SFX_SHIELD_UP, priority=2, volume=1.0)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A6", br=["BR-005"])
    async def test_press_in_a_death_frame_activates_and_the_player_survives(self, game):
        """BR-005: the press is evaluated before the grace and death checks of the same frame."""
        await game.grant_shield("p1", 5.0)
        await arm(game)
        await feed(game, "p1", True, accel=(30.0, 30.0, 30.0))
        assert game.players["p1"].alive is True

        # Control: the same motion without a press kills.
        await feed(game, "p2", False, accel=(30.0, 30.0, 30.0))
        assert game.players["p2"].alive is False

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A6", br=["BR-005"])
    async def test_trigger_held_or_latched_from_the_lobby_is_not_a_press(self, game):
        """BR-005: armed only after one released frame; a lobby-latched or held press never activates."""
        await game.grant_shield("p1", 5.0)
        await feed(game, "p1", True, True, True)
        assert game.players["p1"].shield_charge_seconds == 5.0

        await feed(game, "p1", False, True)
        assert game.players["p1"].shield_charge_seconds == 0.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A6", br=["BR-005"])
    @pytest.mark.parametrize("window", ["startup_grace", "agent_shield", "admin_invincibility"])
    async def test_press_in_a_protection_window_is_ignored_and_not_remembered(self, game, clock, window):
        """BR-005: no activation in grace / shield / invincibility; still held when it ends = no press."""
        player = game.players["p1"]
        if window == "admin_invincibility":
            player.invincible_until = T0 + 1.0
        else:
            player.grace_until = T0 + (0.3 if window == "startup_grace" else 1.0)
        await game.grant_shield("p1", 5.0)
        await arm(game)
        await feed(game, "p1", True)
        assert player.shield_charge_seconds == 5.0

        clock.t += 2.0
        await feed(game, "p1", True)  # still held after the window: not an edge
        assert player.shield_charge_seconds == 5.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A6", br=["BR-005"])
    async def test_bouncing_press_activates_once(self, game):
        """BR-005: frames P, R, P, R, P give one activation (no time debounce needed)."""
        await game.grant_shield("p1", 5.0)
        await arm(game)
        await feed(game, "p1", True, False, True, False, True)
        assert len(effects_of(game, SWELL)) == 1

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A6", br=["BR-005"])
    async def test_dead_players_press_does_nothing(self, game):
        """BR-005: a dead player's frames are ignored."""
        await game.grant_shield("p1", 5.0)
        await arm(game)
        game.players["p1"].alive = False
        game.gameplay_stream.writes.clear()
        await feed(game, "p1", True)
        assert game.gameplay_stream.effects() == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A6", br=["BR-005"])
    async def test_press_without_a_charge_does_nothing(self, game, events):
        """BR-005: no charge, no effect and no event."""
        await arm(game)
        await feed(game, "p1", True)
        assert game.gameplay_stream.effects() == []
        assert events.events == []


# --------------------------------------------------------------------------- #
# UC-040 A32: the charge ends unused
# --------------------------------------------------------------------------- #
class TestUC040A32ChargeEnds:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A32", br=["BR-020"])
    async def test_expiry_drops_the_charge_on_the_first_frame_after_it(self, game, clock, events):
        """A32: expired -> SHIELD_CHARGE 0 and player_shield_charge_ended; a later press does nothing."""
        await game.grant_shield("p1", 5.0)
        await arm(game)
        clock.t += 19.9
        await feed(game, "p1", False)
        assert game.players["p1"].shield_charge_seconds == 5.0
        game.gameplay_stream.writes.clear()

        clock.t += 0.1
        await feed(game, "p1", False)
        assert game.gameplay_stream.effects() == [("p1", CHARGE, 0)]
        assert events.get_events_of_type(GameEvent.PLAYER_SHIELD_CHARGE_ENDED) == [
            {"serial": "p1", "reason": "expired"}
        ]

        await feed(game, "p1", True)
        assert game.players["p1"].grace_until == 0.0
        assert effects_of(game, SWELL) == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A32", br=["BR-021"])
    async def test_never_expiring_charge_is_resent_before_the_cap(self, game, clock, flags):
        """BR-021: the 120 s layer is re-sent before it runs out, while the charge is held."""
        flags.values["shield.charge_expiry_seconds"] = 0
        await game.grant_shield("p1", 5.0)
        await arm(game)
        game.gameplay_stream.writes.clear()

        clock.t += 100.0
        await feed(game, "p1", False)
        assert game.gameplay_stream.effects() == []

        clock.t += 11.0
        await feed(game, "p1", False, False)
        assert game.gameplay_stream.effects() == [("p1", CHARGE, 120_000)]

        clock.t += 1000.0
        await feed(game, "p1", False)
        assert game.players["p1"].shield_charge_seconds == 5.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A32", br=["BR-020"])
    async def test_expiring_charge_is_not_resent(self, game, clock):
        """BR-021: a layer that already covers the expiry is never re-sent."""
        await game.grant_shield("p1", 5.0)
        await arm(game)
        game.gameplay_stream.writes.clear()
        clock.t += 15.0
        await feed(game, "p1", False)
        assert game.gameplay_stream.effects() == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A32", br=["BR-020"])
    async def test_death_drops_the_charge_before_the_death_effect(self, game, events):
        """A32: death -> charge ended (death), SHIELD_CHARGE 0 ahead of the death effect."""
        await game.grant_shield("p1", 5.0)
        await arm(game)
        await feed(game, "p1", False, accel=(30.0, 30.0, 30.0))

        assert game.players["p1"].alive is False
        assert game.players["p1"].shield_charge_seconds == 0.0
        assert events.get_events_of_type(GameEvent.PLAYER_SHIELD_CHARGE_ENDED) == [{"serial": "p1", "reason": "death"}]
        kinds = [e[1] for e in game.gameplay_stream.effects() if e[1] in (CHARGE, DEATH)]
        assert kinds[:2] == [CHARGE, CHARGE] and (DEATH not in kinds or kinds.index(DEATH) > 1)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A32", br=["BR-020"])
    @pytest.mark.uc("UC-020", flow="A2")
    async def test_disconnect_drops_the_charge_as_death(self, game, events):
        """A32: a disconnect is a death in every mode."""
        await game.grant_shield("p1", 5.0)
        await game._handle_disconnect("p1")
        assert events.get_events_of_type(GameEvent.PLAYER_SHIELD_CHARGE_ENDED) == [{"serial": "p1", "reason": "death"}]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A32")
    @pytest.mark.uc("UC-040", flow="A17")
    async def test_game_end_drops_the_charge(self, game, events):
        """A32: game end -> SHIELD_CHARGE 0 and charge ended (game_end), the record is clear."""
        await game.grant_shield("p1", 5.0)
        game.gameplay_stream.writes.clear()

        await game._end_shield_feedback()

        assert game.gameplay_stream.effects() == [("p1", CHARGE, 0)]
        assert events.get_events_of_type(GameEvent.PLAYER_SHIELD_CHARGE_ENDED) == [
            {"serial": "p1", "reason": "game_end"}
        ]
        assert game.players["p1"].shield_charge_seconds == 0.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A32")
    async def test_error_teardown_ends_the_open_charge_span(self, game):
        """A32: an error teardown skips _end_shield_feedback; run()'s finally still ends the span."""
        charge_span = create_autospec(Span, instance=True)
        game.players["p1"].span = create_autospec(Span, instance=True)
        game.players["p1"].span.get_span_context.return_value = INVALID_SPAN_CONTEXT
        with patch.object(base_module.tracer, "start_span", return_value=charge_span):
            await game.grant_shield("p1", 5.0)

        game._close_shield_charge_spans()

        charge_span.set_attribute.assert_any_call("shield.charge_outcome", "game_end")
        charge_span.end.assert_called_once()
        assert game.players["p1"]._shield_charge_span is None


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A32")
@pytest.mark.uc("UC-040", flow="A17")
@pytest.mark.usefixtures("flags")
async def test_real_run_drops_a_held_charge_before_the_winner_effects():
    """A32 (real run()): a held charge ends with game_end before _end_game_impl."""
    events = EventCollector()
    game = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=3, infinite=True),
        event_publisher=events.publish,
        audio_client=None,
        game_id="test_a32_run_order",
        initial_players=[type("P", (), {"serial": f"mock_controller_{i}"})() for i in range(3)],
    )
    end_impl = game._end_game_impl
    seen: list[list[dict]] = []

    async def recording_end_impl():
        seen.append(events.get_events_of_type(GameEvent.PLAYER_SHIELD_CHARGE_ENDED))
        await end_impl()

    game._end_game_impl = recording_end_impl
    task = asyncio.create_task(game.run())
    for _ in range(200):
        if events.get_events_of_type(GameEvent.GAME_STARTED) and game.players:
            break
        await asyncio.sleep(0.05)
    assert await game.grant_shield("mock_controller_0", 5.0) is True

    game.force_end("uc040_a32")
    await asyncio.wait_for(task, timeout=10)

    assert seen == [[{"serial": "mock_controller_0", "reason": "game_end"}]]


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A32")
@pytest.mark.usefixtures("flags")
async def test_error_teardown_ends_the_charge_span_even_when_cleanup_raises():
    """A32 (real run()): run()'s finally ends the open charge span before cleanup, which may raise."""
    game = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=3, infinite=True),
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id="test_a32_cleanup_raises",
        initial_players=[type("P", (), {"serial": f"mock_controller_{i}"})() for i in range(3)],
    )
    charge_span = create_autospec(Span, instance=True)

    async def failing_gameplay():
        player_span = create_autospec(Span, instance=True)
        player_span.get_span_context.return_value = INVALID_SPAN_CONTEXT
        game.players["mock_controller_0"].span = player_span
        with patch.object(base_module.tracer, "start_span", return_value=charge_span):
            assert await game.grant_shield("mock_controller_0", 5.0) is True
        raise RuntimeError("gameplay broke")

    game._run_gameplay_phase = failing_gameplay
    game.cleanup = AsyncMock(side_effect=RuntimeError("cleanup broke"))

    with pytest.raises(RuntimeError, match="cleanup broke"):
        await asyncio.wait_for(game.run(), timeout=10)

    charge_span.set_attribute.assert_any_call("shield.charge_outcome", "game_end")
    charge_span.end.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A32")
@pytest.mark.uc("UC-040", flow="A42", br=["BR-031"])
@pytest.mark.usefixtures("flags")
async def test_a_grant_arriving_during_cleanup_leaves_no_charge_span_open():
    """A32 (#1868 nit), A42 step 3 (#2395): a grant that lands while cleanup() awaits is refused
    ``game_ending``, so no charge is stored and no charge span is left open."""
    game = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=3, infinite=True),
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id="test_a32_grant_in_cleanup",
        initial_players=[type("P", (), {"serial": f"mock_controller_{i}"})() for i in range(3)],
    )
    late_span = create_autospec(Span, instance=True)

    async def failing_gameplay():
        raise RuntimeError("gameplay broke")

    async def cleanup_with_late_grant():
        player_span = create_autospec(Span, instance=True)
        player_span.get_span_context.return_value = INVALID_SPAN_CONTEXT
        game.players["mock_controller_0"].span = player_span
        with (
            patch.object(base_module.tracer, "start_span", return_value=late_span) as start_span,
            pytest.raises(InterventionNotApplied) as refused,
        ):
            await game.grant_shield("mock_controller_0", 5.0)
        assert refused.value.reason == "game_ending"
        start_span.assert_not_called()

    game._run_gameplay_phase = failing_gameplay
    game.cleanup = AsyncMock(side_effect=cleanup_with_late_grant)

    with pytest.raises(RuntimeError, match="gameplay broke"):
        await asyncio.wait_for(game.run(), timeout=10)

    game.cleanup.assert_awaited_once()
    assert game.players["mock_controller_0"].shield_charge_seconds == 0
    late_span.end.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A32")
@pytest.mark.usefixtures("flags")
async def test_a_grant_during_cleanup_of_a_cancelled_run_closes_its_charge_span():
    """A32 (#1868 nit): a cancelled run keeps its state RUNNING and sets no outcome latch, so a
    grant that lands while cleanup() awaits is applied; the second close ends its charge span."""
    game = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=3, infinite=True),
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id="test_a32_cancel_grant_in_cleanup",
        initial_players=[type("P", (), {"serial": f"mock_controller_{i}"})() for i in range(3)],
    )
    late_span = create_autospec(Span, instance=True)
    granted: list[bool] = []

    async def cancelled_gameplay():
        raise asyncio.CancelledError

    async def cleanup_with_late_grant():
        player_span = create_autospec(Span, instance=True)
        player_span.get_span_context.return_value = INVALID_SPAN_CONTEXT
        game.players["mock_controller_0"].span = player_span
        with patch.object(base_module.tracer, "start_span", return_value=late_span):
            granted.append(await game.grant_shield("mock_controller_0", 5.0))

    game._run_gameplay_phase = cancelled_gameplay
    game.cleanup = AsyncMock(side_effect=cleanup_with_late_grant)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(game.run(), timeout=10)

    game.cleanup.assert_awaited_once()
    assert granted == [True]
    late_span.end.assert_called_once()


# --------------------------------------------------------------------------- #
# UC-040 BR-019: activation mode
# --------------------------------------------------------------------------- #
class TestUC040BR019ActivationMode:
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-019"])
    @pytest.mark.parametrize("mode", sorted(_GAME_CLASSES, key=lambda m: m.name), ids=lambda m: m.name)
    def test_trigger_mode_is_an_ffa_only_allow_list(self, mode):
        """BR-019: only Free-for-All may turn a grant into a charge; every other mode is instant."""
        assert _GAME_CLASSES[mode].SHIELD_TRIGGER_ALLOWED is (_GAME_CLASSES[mode] is FFAGame)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-019"])
    async def test_other_mode_with_trigger_flag_gets_the_instant_shield(self, clock, flags):
        """BR-019: a non-FFA mode ignores shield.activation=trigger."""
        game = NonstopJoustGame(
            controller_manager_client=MockControllerManagerService(num_controllers=2),
            event_publisher=AsyncMock(),
            audio_client=None,
            game_id="test_nonstop_instant",
        )
        game.gameplay_stream = RecordingStream()
        game.players["p1"] = Player(serial="p1", alive=True)
        flags.reads = 0  # construction reads its own flags
        with patch.object(game, "_play_sound", new_callable=AsyncMock):
            await game.grant_shield("p1", 5.0)
        assert game.players["p1"].grace_until == T0 + 5.0
        assert [e[1] for e in game.gameplay_stream.effects()] == [SHIELD]
        # Only the shield end grace, latched in every mode (UC-040 BR-033); never shield.activation.
        assert flags.keys[-flags.reads :] == ["shield.end_grace_seconds"]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-019"])
    async def test_shadow_game_is_instant(self, game, flags):
        """BR-019: a shadow game is always instant: no charge, no swell."""
        game.game_kind = "shadow"
        flags.reads = 0
        await game.grant_shield("p1", 5.0)
        assert game.players["p1"].grace_until == T0 + 5.0
        assert effects_of(game, CHARGE, SWELL) == []
        # Only the shield end grace, latched in every mode (UC-040 BR-033); never shield.activation.
        assert flags.keys[-flags.reads :] == ["shield.end_grace_seconds"]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-019"])
    async def test_instant_flag_keeps_todays_shield(self, game, flags):
        """BR-019: shield.activation=instant (the shipped default) is today's shield."""
        flags.values["shield.activation"] = "instant"
        await game.grant_shield("p1", 5.0)
        assert game.players["p1"].grace_until == T0 + 5.0
        assert [e[1] for e in game.gameplay_stream.effects()] == [SHIELD]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-019", "BR-020"])
    async def test_flip_to_instant_keeps_a_held_charge_activatable(self, game, flags):
        """BR-019: after a flip to instant a held charge still activates, and an instant grant leaves it alone."""
        await game.grant_shield("p1", 5.0)
        flags.values["shield.activation"] = "instant"

        assert await game.grant_shield("p1", 2.0) is True  # instant: applied at once
        assert game.players["p1"].grace_until == T0 + 2.0
        assert game.players["p1"].shield_charge_seconds == 5.0

        game.players["p1"].grace_until = 0.0  # the instant shield ran out
        await arm(game)
        await feed(game, "p1", True)
        assert game.players["p1"].shield_charge_seconds == 0.0
        assert len(effects_of(game, SWELL)) == 1

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("trigger", "trigger"),
            (" Trigger ", "trigger"),
            ("instant", "instant"),
            ("bogus", "instant"),
            ("", "instant"),
            (None, "instant"),
            (1, "instant"),
        ],
    )
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-019"])
    def test_activation_value_is_validated(self, value, expected):
        """BR-019: a malformed shield.activation gives instant."""
        assert resolve_shield_activation(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (20, 20.0),
            (0, 0.0),
            (45.5, 45.5),
            (500, 120.0),
            (math.inf, 120.0),
            (-1, 20.0),
            (math.nan, 20.0),
            ("10", 20.0),
            (True, 20.0),
            (None, 20.0),
        ],
    )
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-020"])
    def test_expiry_value_is_validated_and_clamped(self, value, expected):
        """flags.md §4: non-number, NaN or negative gives 20; clamped to 0..120."""
        assert resolve_charge_expiry(value) == expected


# --------------------------------------------------------------------------- #
# Hot path and reconnect
# --------------------------------------------------------------------------- #
class TestShieldChargeHotPathAndReconnect:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A6", br=["BR-005"])
    async def test_no_flag_is_read_per_frame(self, game, clock, flags):
        """BR-005 / NFR-001: frames with and without a charge, and an activation, read no flag."""
        flags.values["shield.charge_expiry_seconds"] = 0
        await game.grant_shield("p1", 5.0)
        reads_after_grant = flags.reads
        with (
            patch.object(base_module, "read_object_flag") as obj,
            patch.object(base_module, "read_string_flag") as string,
            patch.object(base_module, "read_float_flag") as flt,
        ):
            for _ in range(50):
                clock.t += 1 / 60
                await feed(game, "p1", False)
                await feed(game, "p2", True)
            await feed(game, "p1", True)
            for _ in range(50):
                clock.t += 1 / 60
                await feed(game, "p1", False)
        assert game.players["p1"].shield_charge_seconds == 0.0  # it activated
        obj.assert_not_called()
        string.assert_not_called()
        flt.assert_not_called()
        assert flags.reads == reads_after_grant

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    async def test_every_reconnect_resends_charges_and_running_shields(self, game, clock):
        """BR-021: each _create_gameplay_stream re-sends SHIELD_CHARGE and SHIELD with the time left."""
        await game.grant_shield("p1", 5.0)  # charge, expires at T0 + 20
        game.players["p2"].grace_until = T0 + 8.0
        game._shield_feedback_until["p2"] = T0 + 8.0
        streams = []

        def new_stream():
            streams.append(RecordingStream())
            return streams[-1]

        game.controller_client = MagicMock()
        game.controller_client.StreamGameplayData.side_effect = new_stream

        clock.t += 2.0
        await game._create_gameplay_stream()
        clock.t += 2.0
        await game._create_gameplay_stream()

        assert streams[0].effects() == [("p1", CHARGE, 18_000), ("p2", SHIELD, 6000)]
        assert streams[1].effects() == [("p1", CHARGE, 16_000), ("p2", SHIELD, 4000)]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    async def test_first_stream_sends_no_shield_layers(self, game):
        """BR-021: nothing held at game start, so the first stream gets no shield commands."""
        stream = RecordingStream()
        game.controller_client = MagicMock()
        game.controller_client.StreamGameplayData.return_value = stream
        await game._create_gameplay_stream()
        assert stream.effects() == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A6", br=["BR-005"])
    @pytest.mark.uc("UC-040", flow="A32", br=["BR-021"])
    async def test_frame_path_awaits_only_when_charge_work_is_due(self, game, clock, flags):
        """BR-005: a held charge costs compares per frame; the coroutine runs only on expiry, re-send or a press."""
        flags.values["shield.charge_expiry_seconds"] = 0  # never: layer 120 s, re-send at 110 s
        await game.grant_shield("p1", 5.0)
        await game.grant_shield("p2", 5.0)
        game.players["p2"].shield_charge_expires_at = T0 + 30.0  # p2 expires at 30 s
        with patch.object(game, "_process_shield_charge", wraps=game._process_shield_charge) as process:
            for _ in range(100):  # 0 to ~1.7 s: nothing due
                clock.t += 1 / 60
                await feed(game, "p1", False)
                await feed(game, "p2", False)
            assert process.await_count == 0

            clock.t = T0 + 30.0  # p2's expiry
            await feed(game, "p2", False)
            assert process.await_count == 1
            assert game.players["p2"].shield_charge_seconds == 0.0

            clock.t = T0 + 110.0  # p1's re-send is due
            await feed(game, "p1", False)
            assert process.await_count == 2
            clock.t += 1 / 60  # re-sent: nothing due again
            await feed(game, "p1", False)
            assert process.await_count == 2

            await feed(game, "p1", True)  # a press
            assert process.await_count == 3
            assert game.players["p1"].shield_charge_seconds == 0.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    async def test_reconnect_resends_a_running_toughness_boost(self, game, clock):
        """BR-021: a reconnect re-sends TOUGHNESS_BOOST with the time left, next to SHIELD."""
        game.players["p2"].grace_until = T0 + 8.0
        game.players["p2"].toughness_boost_until = T0 + 6.0
        game._shield_feedback_until["p2"] = T0 + 8.0
        stream = RecordingStream()
        game.controller_client = MagicMock()
        game.controller_client.StreamGameplayData.return_value = stream

        clock.t += 2.0
        await game._create_gameplay_stream()

        boost = controller_manager_pb2.GAME_EFFECT_TOUGHNESS_BOOST
        assert stream.effects() == [("p2", SHIELD, 6000), ("p2", boost, 4000)]
