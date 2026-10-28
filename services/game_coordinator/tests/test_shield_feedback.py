"""
Shield feedback in the game coordinator (#1301, UC-040 A16 / A17, BR-007..BR-010).

grant_shield / grant_toughness_boost send ONE controller-rendered effect per
grant (no LED stream, no task), play the shield sound and publish
``player_shielded``; the game end clears running shield effects.
"""

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
from opentelemetry.trace import INVALID_SPAN_CONTEXT, Span

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import MockControllerManagerService

from lib.types import GameEvent, Sound
from proto import controller_manager_pb2
from services.game_coordinator.games.base import BaseGameMode, Player
from services.game_coordinator.games.ffa import FFAGame

SHIELD = controller_manager_pb2.GAME_EFFECT_SHIELD
TOUGHNESS_BOOST = controller_manager_pb2.GAME_EFFECT_TOUGHNESS_BOOST


@pytest.fixture(autouse=True)
def _no_shield_limits():
    """Shield limits off (#2044, UC-040 BR-022): these tests are not about the cap or the cooldown."""
    with patch.object(BaseGameMode, "_shield_limits", return_value=(0, 0)):
        yield


class RecordingStream:
    def __init__(self, fail=False):
        self.writes = []
        self.fail = fail

    async def write(self, message):
        if self.fail:
            raise RuntimeError("stream closed")
        self.writes.append(message)

    def effects(self):
        return [
            (m.game_effect.serial, m.game_effect.effect, m.game_effect.duration_ms)
            for m in self.writes
            if m.HasField("game_effect")
        ]


@pytest.fixture
def game():
    publisher = AsyncMock()
    g = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=2),
        event_publisher=publisher,
        audio_client=None,
        game_id="test_shield_feedback",
    )
    g.gameplay_stream = RecordingStream()
    for serial in ("p1", "p2"):
        g.players[serial] = Player(serial=serial, alive=True, color=(0, 255, 0))
    return g


def _shielded_events(game):
    return [c.args[1] for c in game.event_publisher.await_args_list if c.args[0] == GameEvent.PLAYER_SHIELDED]


class TestUC040ShieldFeedback:
    """UC-040 A16: one effect, the shield sound and a player_shielded event per grant."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-007", "BR-008"])
    @pytest.mark.uc("UC-031", flow="main", steps=[4])
    async def test_grant_sends_shield_effect_sound_and_event(self, game):
        """UC-040 A16: grant_shield -> GAME_EFFECT_SHIELD for the duration, shield_up, player_shielded."""
        with patch.object(game, "_play_sound", new_callable=AsyncMock) as play:
            assert await game.grant_shield("p1", 5.0) is True

        [(serial, effect, duration_ms)] = game.gameplay_stream.effects()
        assert (serial, effect) == ("p1", SHIELD)
        assert 4900 <= duration_ms <= 5000
        play.assert_awaited_once_with(Sound.SFX_SHIELD_UP, priority=2, volume=1.0)
        [event] = _shielded_events(game)
        assert event["serial"] == "p1"
        assert event["kind"] == "full"
        assert int(event["until_ms"]) == pytest.approx(game.players["p1"].grace_until * 1000, abs=50)
        assert "p1" in game._shield_feedback_until

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-009"])
    async def test_toughness_boost_sends_weaker_variant_at_half_volume(self, game):
        """UC-040 BR-009: toughness_boost -> GAME_EFFECT_TOUGHNESS_BOOST and shield_up at 0.5."""
        with patch.object(game, "_play_sound", new_callable=AsyncMock) as play:
            assert await game.grant_toughness_boost("p2", 5.0) is True

        [(serial, effect, duration_ms)] = game.gameplay_stream.effects()
        assert (serial, effect) == ("p2", TOUGHNESS_BOOST)
        assert 4900 <= duration_ms <= 5000
        play.assert_awaited_once_with(Sound.SFX_SHIELD_UP, priority=2, volume=0.5)
        assert _shielded_events(game)[0]["kind"] == "toughness_boost"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-010"])
    async def test_extending_restarts_the_effect_with_the_new_remaining_time(self, game):
        """UC-040 BR-010: a longer grant re-sends the effect for the new remaining protection."""
        with patch.object(game, "_play_sound", new_callable=AsyncMock) as play:
            await game.grant_shield("p1", 3.0)
            await game.grant_shield("p1", 8.0)

        effects = game.gameplay_stream.effects()
        assert [e[1] for e in effects] == [SHIELD, SHIELD]
        assert effects[1][2] > effects[0][2]
        assert 7900 <= effects[1][2] <= 8000
        assert play.await_count == 1  # the extension restarts the LED silently

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-010"])
    async def test_kept_grant_sends_no_feedback(self, game):
        """UC-040 BR-010: a shorter grant keeps the protection and sends nothing."""
        with patch.object(game, "_play_sound", new_callable=AsyncMock) as play:
            await game.grant_shield("p1", 8.0)
            await game.grant_shield("p1", 2.0)
            await game.grant_toughness_boost("p2", 8.0)
            await game.grant_toughness_boost("p2", 2.0)

        assert len(game.gameplay_stream.effects()) == 2
        assert play.await_count == 2
        assert len(_shielded_events(game)) == 2

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-010"])
    async def test_admin_invincibility_composition_holds(self, game):
        """UC-040 BR-010 / #817: effective grace = max; a covered grant is kept with no feedback."""
        player = game.players["p1"]
        player.invincible_until = time.time() + 10.0  # admin baseline (Tournament / Fight Club)

        with patch.object(game, "_play_sound", new_callable=AsyncMock):
            assert await game.grant_shield("p1", 4.0) is True  # kept
        assert player.grace_until == 0.0  # agent never shortens or overwrites the admin baseline
        assert game.gameplay_stream.effects() == []

        player.invincible_until = time.time() + 2.0
        with patch.object(game, "_play_sound", new_callable=AsyncMock):
            await game.grant_shield("p1", 6.0)
        assert game.effective_grace_until(player) == pytest.approx(player.grace_until)
        [(_, effect, duration_ms)] = game.gameplay_stream.effects()
        assert effect == SHIELD and 5900 <= duration_ms <= 6000

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-010"])
    async def test_feedback_failure_never_fails_the_shield(self, game):
        """UC-040 BR-010: a broken stream loses the feedback, not the protection."""
        game.gameplay_stream = RecordingStream(fail=True)
        before = time.time()
        with patch.object(game, "_play_sound", new_callable=AsyncMock):
            assert await game.grant_shield("p1", 5.0) is True
        assert game.players["p1"].grace_until >= before + 4.5

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-008"])
    async def test_no_task_is_created_for_the_fade(self, game):
        """UC-040 BR-008: the controller manager renders the fade; the coordinator keeps no task."""
        before = set(asyncio.all_tasks())
        with patch.object(game, "_play_sound", new_callable=AsyncMock):
            await game.grant_shield("p1", 5.0)
            await game.grant_toughness_boost("p2", 5.0)
        assert set(asyncio.all_tasks()) == before


class TestUC040ShieldFeedbackChannels:
    """UC-040 BR-010: sound only on unprotected -> protected; channels fail independently."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-007", "BR-010"])
    async def test_sound_only_on_the_unprotected_to_protected_transition(self, game):
        """UC-040 BR-010: first grant plays shield_up; extensions resend the LED with no sound."""
        with patch.object(game, "_play_sound", new_callable=AsyncMock) as play:
            await game.grant_shield("p1", 3.0)
            await game.grant_shield("p1", 6.0)
            await game.grant_toughness_boost("p1", 9.0)  # still protected: silent too

        assert [e[1] for e in game.gameplay_stream.effects()] == [SHIELD, SHIELD, TOUGHNESS_BOOST]
        play.assert_awaited_once_with(Sound.SFX_SHIELD_UP, priority=2, volume=1.0)
        assert len(_shielded_events(game)) == 3

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-007", "BR-010"])
    async def test_sound_again_after_the_protection_lapsed(self, game):
        """UC-040 BR-010: a grant after the previous shield expired is a new transition."""
        player = game.players["p1"]
        player.grace_until = time.time() - 1  # an earlier shield has expired
        with patch.object(game, "_play_sound", new_callable=AsyncMock) as play:
            await game.grant_shield("p1", 3.0)
        play.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-010"])
    async def test_led_failure_still_plays_sound_and_publishes(self, game):
        """UC-040 BR-010: a broken stream skips only the LED channel."""
        game.gameplay_stream = RecordingStream(fail=True)
        with patch.object(game, "_play_sound", new_callable=AsyncMock) as play:
            assert await game.grant_shield("p1", 5.0) is True
        play.assert_awaited_once()
        assert len(_shielded_events(game)) == 1

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-010"])
    async def test_sound_failure_still_sends_led_and_publishes(self, game):
        """UC-040 BR-010: a failing audio call skips only the sound channel."""
        with patch.object(game, "_play_sound", new_callable=AsyncMock, side_effect=RuntimeError("audio down")):
            assert await game.grant_shield("p1", 5.0) is True
        assert [e[1] for e in game.gameplay_stream.effects()] == [SHIELD]
        assert len(_shielded_events(game)) == 1

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-010"])
    async def test_event_failure_does_not_fail_the_shield(self, game):
        """UC-040 BR-010: a failing publisher skips only the event."""
        game.event_publisher.side_effect = RuntimeError("bus closed")
        with patch.object(game, "_play_sound", new_callable=AsyncMock) as play:
            assert await game.grant_shield("p1", 5.0) is True
        play.assert_awaited_once()
        assert [e[1] for e in game.gameplay_stream.effects()] == [SHIELD]


class TestUC040ShieldFeedbackEnds:
    """UC-040 A17: game end clears running shield effects."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A17", br=["BR-010"])
    async def test_game_end_sends_zero_duration_shield_to_running_ones(self, game):
        """UC-040 A17: a zero-duration SHIELD goes to every still-running shield, then the record is clear."""
        with patch.object(game, "_play_sound", new_callable=AsyncMock):
            await game.grant_shield("p1", 5.0)
            await game.grant_toughness_boost("p2", 5.0)
        game._shield_feedback_until["p2"] = time.time() - 1  # already expired
        game.gameplay_stream.writes.clear()

        await game._end_shield_feedback()

        assert game.gameplay_stream.effects() == [("p1", SHIELD, 0)]
        assert game._shield_feedback_until == {}

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A17", br=["BR-010"])
    async def test_shorter_toughness_boost_does_not_hide_a_running_full_shield(self, game):
        """UC-040 A17: a short toughness boost grant after a long full one must not drop the full one from the clear."""
        clock = [1000.0]
        with (
            patch("services.game_coordinator.game_clock.now", side_effect=lambda: clock[0]),
            patch.object(game, "_play_sound", new_callable=AsyncMock),
        ):
            await game.grant_shield("p1", 10.0)
            await game.grant_toughness_boost("p1", 1.0)
            sent = [(serial, effect) for serial, effect, _ in game.gameplay_stream.effects()]
            assert sent == [("p1", SHIELD), ("p1", TOUGHNESS_BOOST)]
            game.gameplay_stream.writes.clear()

            clock[0] += 5.0  # toughness boost expired, full still running
            await game._end_shield_feedback()

        assert game.gameplay_stream.effects() == [("p1", SHIELD, 0)]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A17", br=["BR-010"])
    async def test_game_end_without_shields_sends_nothing(self, game):
        """UC-040 A17: nothing to end, nothing sent."""
        await game._end_shield_feedback()
        assert game.gameplay_stream.effects() == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A17", br=["BR-010"])
    async def test_game_end_clear_survives_a_closed_stream(self, game):
        """UC-040 A17: the clear is best-effort and never raises into the teardown."""
        game._shield_feedback_until["p1"] = time.time() + 5
        game.gameplay_stream = RecordingStream(fail=True)
        await game._end_shield_feedback()
        assert game._shield_feedback_until == {}

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A17", br=["BR-010"])
    async def test_teardown_clears_shields_before_the_winner_effects(self, game):
        """UC-040 A17: the end-of-game clear runs before _end_game_impl (winner rainbow)."""
        import inspect

        from services.game_coordinator.games.base import BaseGameMode

        source = inspect.getsource(BaseGameMode)
        assert source.index("await self._end_shield_feedback()") < source.index("await self._end_game_impl()")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A17", br=["BR-010"])
    async def test_real_run_ends_running_shields_before_the_winner_effects(self):
        """UC-040 A17 (real run()): a game that ends with a shield running clears it before _end_game_impl."""
        from conftest import EventCollector

        events = EventCollector()
        game = FFAGame(
            controller_manager_client=MockControllerManagerService(num_controllers=3, infinite=True),
            event_publisher=events.publish,
            audio_client=None,
            game_id="test_a17_run_order",
            initial_players=[type("P", (), {"serial": f"mock_controller_{i}"})() for i in range(3)],
        )
        order: list[str] = []
        end_shields, end_impl = game._end_shield_feedback, game._end_game_impl

        async def recording_end_shields():
            order.append(f"shields_cleared:{sorted(game._shield_feedback_until)}")
            await end_shields()

        async def recording_end_impl():
            order.append(f"end_game_impl:{sorted(game._shield_feedback_until)}")
            await end_impl()

        game._end_shield_feedback = recording_end_shields
        game._end_game_impl = recording_end_impl

        task = asyncio.create_task(game.run())
        for _ in range(200):  # up to 10 s for the game to reach RUNNING
            if events.get_events_of_type(GameEvent.GAME_STARTED) and game.players:
                break
            await asyncio.sleep(0.05)
        assert events.get_events_of_type(GameEvent.GAME_STARTED), "game never started"
        assert await game.grant_shield("mock_controller_0", 20.0) is True

        game.force_end("uc040_a17")
        await asyncio.wait_for(task, timeout=10)

        assert order == ["shields_cleared:['mock_controller_0']", "end_game_impl:[]"]


def _span_events(span, name: str) -> list[dict]:
    """Attributes of every ``add_event(name, attributes=...)`` call on an autospec'd span."""
    return [c.kwargs.get("attributes", {}) for c in span.add_event.call_args_list if c.args and c.args[0] == name]


class TestUC040ShieldSpanEvents:
    """UC-040 A16: the span events a shield grant records on the player span (#1362)."""

    @pytest.fixture
    def span(self, game):
        span = create_autospec(Span, instance=True)
        span.get_span_context.return_value = INVALID_SPAN_CONTEXT  # trace-context injection formats its ids
        game.players["p1"].span = span
        return span

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16")
    async def test_full_shield_records_shield_granted_and_shield_feedback(self, game, span):
        """UC-040 A16: grant_shield records shield_granted and shield_feedback (kind full, shield_up sound)."""
        assert await game.grant_shield("p1", 3.0) is True

        assert _span_events(span, "shield_granted")[0]["shield_seconds"] == 3.0
        feedback = _span_events(span, "shield_feedback")
        assert len(feedback) == 1
        assert feedback[0]["shield.kind"] == "full"
        assert feedback[0]["shield.sound"] == Sound.SFX_SHIELD_UP.value
        assert 2900 <= feedback[0]["shield.duration_ms"] <= 3000

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16")
    async def test_toughness_boost_records_toughness_boost_armed_and_shield_feedback(self, game, span):
        """UC-040 A16: grant_toughness_boost records toughness_boost_armed + shield_feedback (kind toughness_boost)."""
        assert await game.grant_toughness_boost("p1", 2.0) is True

        assert _span_events(span, "toughness_boost_armed")
        feedback = _span_events(span, "shield_feedback")
        assert len(feedback) == 1
        assert feedback[0]["shield.kind"] == "toughness_boost"
        assert feedback[0]["shield.sound"] == Sound.SFX_SHIELD_UP.value
