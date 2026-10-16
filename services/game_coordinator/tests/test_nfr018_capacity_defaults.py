"""NFR-018 real/shadow capacity: the shipped defaults are 8 live games, 4 shadow, policy block.

The admission behaviour (caps, the block gate, real-start preemption) is asserted in
test_servicer_sessions.py (tests tagged ``nfr("NFR-018")``). This file pins the
numbers NFR-018 states: the coordinator's fail-open constants and the defaults the
production flag file (services/flagd/game.json) serves must both be 8 / 4 / block,
so a change to either is a visible spec change.
"""

import json
from pathlib import Path

import pytest

from services.game_coordinator.servicer import (
    DEFAULT_MAX_CONCURRENT_GAMES,
    DEFAULT_MAX_SHADOW_GAMES,
    DEFAULT_SHADOW_POLICY,
)

GAME_FLAGS = Path(__file__).resolve().parents[3] / "services" / "flagd" / "game.json"
EXPECTED = {"max_concurrent_games": 8, "max_shadow_games": 4, "shadow_policy": "block"}


def _served_default(flag_key: str):
    flag = json.loads(GAME_FLAGS.read_text())["flags"][flag_key]
    return flag["variants"][flag["defaultVariant"]]


@pytest.mark.nfr("NFR-018")
def test_fail_open_constants_match_nfr():
    assert {
        "max_concurrent_games": DEFAULT_MAX_CONCURRENT_GAMES,
        "max_shadow_games": DEFAULT_MAX_SHADOW_GAMES,
        "shadow_policy": DEFAULT_SHADOW_POLICY,
    } == EXPECTED


@pytest.mark.nfr("NFR-018")
@pytest.mark.parametrize("flag_key", sorted(EXPECTED))
def test_production_flag_defaults_match_nfr(flag_key):
    assert _served_default(flag_key) == EXPECTED[flag_key]
