"""
Tests for the tempo-change pacing pathway.

``_get_music_change_time`` -> ``_decide_next_change_delay`` is the ONE pacing
pathway: a random draw inside the game-progression-lerped ``music_windows``.
The "agent" tempo-schedule mode (#1109 stub, #1103 setter never called) was
removed in #2361; the agent shapes tempo through ``music_tempo_override`` and
``tempo_ramp`` (test_ramp_tempo.py, test_intensity_ramp_uc031.py).
"""

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector

from services.game_coordinator.games.ffa import FFAGame


def _make_ffa(game_id: str = ""):
    return FFAGame(
        controller_manager_client=None,
        event_publisher=EventCollector().publish,
        sensitivity=2,
        game_id=game_id,
    )


def _arm_game(game, *, players=2, dead=0, speed_up=True):
    game.players = {chr(ord("a") + i): object() for i in range(players)}
    game.dead_count = dead
    game.speed_up = speed_up


def test_get_music_change_time_is_now_plus_delay():
    """``_get_music_change_time`` == now + the drawn delay (single pathway)."""
    game = _make_ffa()
    _arm_game(game)
    with patch.object(game, "_decide_next_change_delay", return_value=7.5):
        before = time.time()
        result = game._get_music_change_time()
    assert abs(result - (before + 7.5)) < 0.05


@pytest.mark.uc("UC-031", flow="main", steps=[3])
def test_default_rule_stays_within_windows():
    """The delay draw stays within the default window ranges."""
    from services.game_coordinator.games.base import (
        MAX_MUSIC_FAST_TIME,
        MAX_MUSIC_SLOW_TIME,
        MIN_MUSIC_FAST_TIME,
        MIN_MUSIC_SLOW_TIME,
    )

    game = _make_ffa()
    _arm_game(game, players=2, dead=0)
    for speed_up, lo, hi in (
        (True, MIN_MUSIC_SLOW_TIME, MAX_MUSIC_SLOW_TIME),
        (False, MIN_MUSIC_FAST_TIME, MAX_MUSIC_FAST_TIME),
    ):
        game.speed_up = speed_up
        for _ in range(200):
            delay = game._decide_next_change_delay()
            assert lo - 0.001 <= delay <= hi + 0.001
