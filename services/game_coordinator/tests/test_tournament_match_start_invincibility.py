"""A Tournament match's fighters are invincible from its first await on (#2025 S3, UC-025 step 2, BR-006).

``_start_match`` seats the fighters, then awaits ``match_start`` and the two colour
writes. The kill gate must already see their match invincibility at each of those
awaits: an ``eliminate_player`` (or a kill) that the game loop runs there is refused.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService
from test_tournament_run import _frame, _RecordingStream

from services.game_coordinator.games.tournament import TournamentGame


@pytest.mark.uc("UC-025", flow="main", steps=[2, 3], br=["BR-006"])
@pytest.mark.asyncio
async def test_fighters_are_invincible_at_every_await_of_the_match_start():
    mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={}, max_duration=10.0)
    events = EventCollector()
    game = TournamentGame(
        controller_manager_client=mock_cm,
        event_publisher=events.publish,
        audio_client=None,
        game_id="test_2025_s3",
        invincibility_seconds=4.0,
    )
    await game._initialize_players_impl(mock_cm.controllers)
    game._play_sound = AsyncMock()
    game.running = True
    game.gameplay_stream = _RecordingStream()
    game._time_between_matches = 0.0

    seen: list[tuple[str, bool]] = []

    def record(where: str) -> None:
        match = game.current_match
        if match is not None:
            for serial in (match.player1_serial, match.player2_serial):
                seen.append((where, game._accepts_kill(serial)))

    real_publish = game.event_publisher

    async def publish(event_type, data):
        if event_type == "match_start":
            record("match_start")
        return await real_publish(event_type, data)

    real_set_color = game._set_base_color

    async def set_color(serial, color):
        record("set_base_color")
        return await real_set_color(serial, color)

    game.event_publisher = publish
    game._set_base_color = set_color

    # The first frame starts the first match.
    assert await game._process_gameplay_update(_frame()) is False
    assert game.current_match is not None

    assert seen, "the probe saw no await of _start_match"
    assert [where for where, accepted in seen if accepted] == [], f"kill accepted during the match start: {seen}"
