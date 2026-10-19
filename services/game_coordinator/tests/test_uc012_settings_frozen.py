"""UC-012 BR-001: a running game keeps the settings it started with.

The menu reads ``sensitivity`` once, at game start, and sends it in the
StartGameConfig. An admin change while the game runs (the flag now serves
another level) must not reach the running game: it applies from the next game.
"""

import asyncio
from unittest.mock import create_autospec, patch

import pytest
from openfeature.client import OpenFeatureClient

from lib.types import GameEvent, Sensitivity
from services.game_coordinator.games import base as base_module
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.tests.conftest import EventCollector
from services.game_coordinator.tests.test_ffa_uc020 import (
    RecordingControllerManager,
    _initial_players,
    fast_config,  # noqa: F401 - pytest fixture
)


def _flags_with_admin_change(sensitivity: int):
    """Autospec'd flag client: ``sensitivity`` serves the admin's new level, every other flag its default."""
    client = create_autospec(OpenFeatureClient, instance=True)

    def integer_value(flag_key, default, *_args, **_kwargs):
        return sensitivity if flag_key == "sensitivity" else default

    def default_value(_flag_key, default, *_args, **_kwargs):
        return default

    client.get_integer_value.side_effect = integer_value
    for name in ("get_boolean_value", "get_float_value", "get_string_value", "get_object_value"):
        getattr(client, name).side_effect = default_value
    return client


@pytest.mark.asyncio
@pytest.mark.uc("UC-012", br=["BR-001"])
async def test_running_game_keeps_its_start_sensitivity(fast_config):  # noqa: F811, ARG001
    """Started at slow; the admin switches to ultra_fast while it runs; the game stays at slow to its end."""
    events = EventCollector()
    cm = RecordingControllerManager(num_controllers=3, death_schedule={0.5: 0, 0.8: 1}, max_duration=6.0)
    game = FFAGame(
        controller_manager_client=cm,
        event_publisher=events.publish,
        audio_client=None,
        game_id="test_uc012_br001",
        initial_players=_initial_players(3),
        sensitivity=Sensitivity.SLOW.value,
    )
    client = _flags_with_admin_change(Sensitivity.ULTRA_FAST.value)
    contexts = create_autospec(base_module.set_game_transaction_context)

    with (
        patch("lib.feature_flags.get_flag_client", autospec=True, return_value=client),
        patch("lib.feature_flags.init_flag_domain", autospec=True),
        patch.object(base_module, "set_game_transaction_context", contexts),
    ):
        await asyncio.wait_for(game.run(), timeout=15.0)

    assert events.get_events_of_type(GameEvent.GAME_WINNER), "the game did not run to its end"
    assert game.sensitivity == Sensitivity.SLOW
    assert game.baseline_sensitivity == Sensitivity.SLOW
    # Flag evaluations during the game carry the start level, not the admin's new one.
    assert contexts.call_args_list, "the game never set its flag evaluation context"
    assert {c.kwargs["sensitivity"] for c in contexts.call_args_list} == {Sensitivity.SLOW.value}
