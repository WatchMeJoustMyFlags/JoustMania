"""
Shield feedback in the game coordinator (#1301, UC-040 A14 / A15, BR-005..BR-008).

grant_shield / grant_partial_shield send ONE controller-rendered effect per
grant (no LED stream, no task), play the shield sound and publish
``player_shielded``; the game end clears running shield effects.
"""

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import MockControllerManagerService

from lib.types import GameEvent, Sound
from proto import controller_manager_pb2
from services.game_coordinator.games.base import Player
from services.game_coordinator.games.ffa import FFAGame

SHIELD = controller_manager_pb2.GAME_EFFECT_SHIELD
PARTIAL = controller_manager_pb2.GAME_EFFECT_PARTIAL_SHIELD


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
    """UC-040 A14: one effect, the shield sound and a player_shielded event per grant."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A14", br=["BR-005", "BR-006"])
    async def test_grant_sends_shield_effect_sound_and_event(self, game):
        """UC-040 A14: grant_shield -> GAME_EFFECT_SHIELD for the duration, shield_up, player_shielded."""
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
    @pytest.mark.uc("UC-040", flow="A14", br=["BR-007"])
    async def test_partial_shield_sends_weaker_variant_at_half_volume(self, game):
        """UC-040 BR-007: partial_shield -> GAME_EFFECT_PARTIAL_SHIELD and shield_up at 0.5."""
        with patch.object(game, "_play_sound", new_callable=AsyncMock) as play:
            assert await game.grant_partial_shield("p2", 5.0) is True

        [(serial, effect, duration_ms)] = game.gameplay_stream.effects()
        assert (serial, effect) == ("p2", PARTIAL)
        assert 4900 <= duration_ms <= 5000
        play.assert_awaited_once_with(Sound.SFX_SHIELD_UP, priority=2, volume=0.5)
        assert _shielded_events(game)[0]["kind"] == "partial"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A14", br=["BR-008"])
    async def test_extending_restarts_the_effect_with_the_new_remaining_time(self, game):
        """UC-040 BR-008: a longer grant re-sends the effect for the new remaining protection."""
        with patch.object(game, "_play_sound", new_callable=AsyncMock) as play:
            await game.grant_shield("p1", 3.0)
            await game.grant_shield("p1", 8.0)

        effects = game.gameplay_stream.effects()
        assert [e[1] for e in effects] == [SHIELD, SHIELD]
        assert effects[1][2] > effects[0][2]
        assert 7900 <= effects[1][2] <= 8000
        assert play.await_count == 2

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A14", br=["BR-008"])
    async def test_kept_grant_sends_no_feedback(self, game):
        """UC-040 BR-008: a shorter grant keeps the protection and sends nothing."""
        with patch.object(game, "_play_sound", new_callable=AsyncMock) as play:
            await game.grant_shield("p1", 8.0)
            await game.grant_shield("p1", 2.0)
            await game.grant_partial_shield("p2", 8.0)
            await game.grant_partial_shield("p2", 2.0)

        assert len(game.gameplay_stream.effects()) == 2
        assert play.await_count == 2
        assert len(_shielded_events(game)) == 2

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A14", br=["BR-008"])
    async def test_admin_invincibility_composition_holds(self, game):
        """UC-040 BR-008 / #817: effective grace = max; a covered grant is kept with no feedback."""
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
    @pytest.mark.uc("UC-040", flow="A14", br=["BR-008"])
    async def test_feedback_failure_never_fails_the_shield(self, game):
        """UC-040 BR-008: a broken stream loses the feedback, not the protection."""
        game.gameplay_stream = RecordingStream(fail=True)
        before = time.time()
        with patch.object(game, "_play_sound", new_callable=AsyncMock):
            assert await game.grant_shield("p1", 5.0) is True
        assert game.players["p1"].grace_until >= before + 4.5

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A14", br=["BR-006"])
    async def test_no_task_is_created_for_the_fade(self, game):
        """UC-040 BR-006: the controller manager renders the fade; the coordinator keeps no task."""
        before = set(asyncio.all_tasks())
        with patch.object(game, "_play_sound", new_callable=AsyncMock):
            await game.grant_shield("p1", 5.0)
            await game.grant_partial_shield("p2", 5.0)
        assert set(asyncio.all_tasks()) == before


class TestUC040ShieldFeedbackEnds:
    """UC-040 A15: game end clears running shield effects."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A15", br=["BR-008"])
    async def test_game_end_sends_zero_duration_shield_to_running_ones(self, game):
        """UC-040 A15: a zero-duration SHIELD goes to every still-running shield, then the record is clear."""
        with patch.object(game, "_play_sound", new_callable=AsyncMock):
            await game.grant_shield("p1", 5.0)
            await game.grant_partial_shield("p2", 5.0)
        game._shield_feedback_until["p2"] = time.time() - 1  # already expired
        game.gameplay_stream.writes.clear()

        await game._end_shield_feedback()

        assert game.gameplay_stream.effects() == [("p1", SHIELD, 0)]
        assert game._shield_feedback_until == {}

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A15", br=["BR-008"])
    async def test_game_end_without_shields_sends_nothing(self, game):
        """UC-040 A15: nothing to end, nothing sent."""
        await game._end_shield_feedback()
        assert game.gameplay_stream.effects() == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A15", br=["BR-008"])
    async def test_game_end_clear_survives_a_closed_stream(self, game):
        """UC-040 A15: the clear is best-effort and never raises into the teardown."""
        game._shield_feedback_until["p1"] = time.time() + 5
        game.gameplay_stream = RecordingStream(fail=True)
        await game._end_shield_feedback()
        assert game._shield_feedback_until == {}

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A15", br=["BR-008"])
    async def test_teardown_clears_shields_before_the_winner_effects(self, game):
        """UC-040 A15: the end-of-game clear runs before _end_game_impl (winner rainbow)."""
        import inspect

        from services.game_coordinator.games.base import BaseGameMode

        source = inspect.getsource(BaseGameMode)
        assert source.index("await self._end_shield_feedback()") < source.index("await self._end_game_impl()")
