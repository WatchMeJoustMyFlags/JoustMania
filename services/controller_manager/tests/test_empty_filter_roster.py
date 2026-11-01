"""
A game stream consumes game signals only for its own roster (UC-035 BR-006, #1610).

An empty alive filter reaches _build_gameplay_update as None ("every controller").
It must not make the stream drain the death latch, the trigger latch or the
health counters of controllers that belong to another game (a shadow game's mocks).
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

test_dir = Path(__file__).parent
project_root = test_dir.parent.parent.parent
sys.path.insert(0, str(project_root))

from services.controller_manager.state_builder import FrameInputs
from services.controller_manager.tests.backend_doubles import (
    autospec_backend,
    autospec_discovery_loop,
    stub_frame_inputs,
    stub_method,
)

MINE = "mock_controller_0"
THEIRS = "mock_controller_1"


def _state(serial: str) -> FrameInputs:
    return stub_frame_inputs(
        serial=serial,
        move_num=0,
        battery=80,
        team=0,
        color=(255, 0, 0),
        accel=(0.0, 0.0, 1.0),
        gyro=(0.0, 0.0, 0.0),
        rssi=0,
        name=serial,
        trigger_pressed=False,
    )


@pytest.fixture
def rig():
    """Servicer whose backend latches a death frame and a trigger press for THEIRS."""
    death = {THEIRS: 3}
    trigger = {THEIRS: True}
    health = {THEIRS: (2, 1, 0)}

    def consume_death(serial):
        if death.get(serial):
            death[serial] -= 1

    def consume_trigger(serial):
        return trigger.pop(serial, False)

    def drain_health(serial):
        return health.pop(serial, (0, 0, 0))

    backend = autospec_backend()
    backend.consume_death_frame.side_effect = consume_death
    backend.consume_trigger_press.side_effect = consume_trigger

    with (
        patch("services.controller_manager.servicer.create_backend", return_value=backend),
        patch("services.controller_manager.servicer.DiscoveryLoop"),
    ):
        from services.controller_manager.servicer import ControllerManagerServicer

        servicer = ControllerManagerServicer()

    servicer.discovery_loop = autospec_discovery_loop()
    servicer.discovery_loop.drain_disconnects.return_value = []
    servicer.discovery_loop.drain_health_counters.side_effect = drain_health
    stub_method(servicer.state_builder, "frame_inputs", side_effect=lambda serial, _info: _state(serial))
    stub_method(servicer, "_connection_quality_msg", return_value=None)
    servicer.tracked_controllers[MINE] = {}
    servicer.tracked_controllers[THEIRS] = {}
    return servicer, death, trigger, health


@pytest.mark.uc("UC-035", br=["BR-006"])
def test_empty_filter_game_stream_leaves_other_games_latches(rig):
    servicer, death, trigger, health = rig
    servicer._open_game_stream("mine", {MINE})
    servicer._open_game_stream("theirs", {THEIRS})

    # "mine" has nobody alive: _process_filter_update maps the empty list to None.
    servicer._build_gameplay_update(None, "mine")

    assert death[THEIRS] == 3
    assert trigger == {THEIRS: True}
    assert health == {THEIRS: (2, 1, 0)}

    # The owning game's stream still gets the latched death, trigger and health.
    frame = servicer._build_gameplay_update({THEIRS}, "theirs")
    assert death[THEIRS] == 2
    (controller,) = frame.controllers
    assert controller.trigger_pressed is True
    assert (controller.health.poll_drops, controller.health.poll_errors) == (2, 1)


@pytest.mark.uc("UC-035", br=["BR-006"])
def test_empty_filter_game_stream_still_drains_its_own_roster(rig):
    servicer, death, trigger, health = rig
    servicer._open_game_stream("theirs", {THEIRS})

    frame = servicer._build_gameplay_update(None, "theirs")

    assert death[THEIRS] == 2
    assert trigger == {}
    assert health == {}
    assert any(c.serial == THEIRS and c.trigger_pressed for c in frame.controllers)
