"""A late-ending session resets ``game_players_alive`` only while it owns it (#2244).

After a force-end during game creation the old session's thread can finish after
the next game already started. Its cleanup used to zero the global aggregate,
overwriting the new game's live count until that game's next death.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

test_dir = Path(__file__).parent
project_root = test_dir.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from test_game_session import _make_session, _tracer_mock

from services.game_coordinator import metrics
from services.game_coordinator.game_session import GAME_KIND_PRIMARY

GAME_A = "game_abc123"  # _make_session's game id
GAME_B = "game_B"


@pytest.fixture(autouse=True)
def _no_owner(monkeypatch):
    monkeypatch.setattr(metrics, "_players_alive_last", None)


@pytest.fixture
def gauge(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(metrics, "players_alive", fake)
    return fake


def _last_value(gauge):
    return gauge.set.call_args_list[-1].args[0]


async def _run_session_a(game_run):
    session = _make_session(game_kind=GAME_KIND_PRIMARY)
    assert session.game_id == GAME_A
    game = MagicMock()
    game.run = AsyncMock(side_effect=game_run)
    with (
        patch("services.game_coordinator.game_session.tracer") as mock_tracer,
        patch("services.game_coordinator.game_session.GameFactory") as mock_factory,
    ):
        mock_tracer.start_as_current_span = _tracer_mock().start_as_current_span
        mock_factory.create_game.return_value = game
        await session._run_game_loop_async()


@pytest.mark.uc("UC-027", flow="main", br=["BR-002"])
class TestLateSessionKeepsNextGameCount:
    @pytest.mark.asyncio
    async def test_game_b_started_before_a_session_ends_keeps_b_value(self, gauge):
        """Game A wrote the gauge, game B started and wrote it, then A's session ends."""

        async def a_then_b_starts():
            metrics.set_players_alive(GAME_A, 3)
            metrics.set_players_alive(GAME_B, 4)

        await _run_session_a(a_then_b_starts)

        assert _last_value(gauge) == 4
        assert 0 not in [c.args[0] for c in gauge.set.call_args_list]

    @pytest.mark.asyncio
    async def test_a_never_wrote_keeps_b_value(self, gauge):
        """A was force-ended during creation (never wrote); B already runs."""

        async def only_b_writes():
            metrics.set_players_alive(GAME_B, 4)

        await _run_session_a(only_b_writes)

        assert _last_value(gauge) == 4

    @pytest.mark.asyncio
    async def test_a_still_owner_resets_to_zero(self, gauge):
        async def only_a_writes():
            metrics.set_players_alive(GAME_A, 3)

        await _run_session_a(only_a_writes)

        assert _last_value(gauge) == 0


@pytest.mark.uc("UC-027", flow="main", br=["BR-002"])
class TestResetIfOwner:
    def test_b_claims_between_check_and_zero_gets_its_value_back(self, gauge):
        metrics.set_players_alive(GAME_A, 3)

        def b_claims_on_zero(value):
            if value == 0:
                gauge.set.side_effect = None
                metrics.set_players_alive(GAME_B, 4)

        gauge.set.side_effect = b_claims_on_zero

        assert metrics.reset_players_alive_if_owner(GAME_A) is True
        assert _last_value(gauge) == 4

    def test_not_owner_does_not_touch_gauge(self, gauge):
        metrics.set_players_alive(GAME_B, 4)
        gauge.reset_mock()

        assert metrics.reset_players_alive_if_owner(GAME_A) is False
        gauge.set.assert_not_called()

    def test_no_writer_yet_does_not_touch_gauge(self, gauge):
        assert metrics.reset_players_alive_if_owner(GAME_A) is False
        gauge.set.assert_not_called()
