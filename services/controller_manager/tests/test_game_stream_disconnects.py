"""
A game stream only gets the disconnects that happen while it is open (UC-002 BR-004, #1607).

Disconnects are queued by the discovery loop and drained by game streams. One
queued between games (no game stream open) must not reach the next game's
first frame, or a controller that dropped, reconnected and joined that game
would be eliminated at frame 1 (UC-020 A2).
"""

import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from test_observer_stream import SERIAL, rig  # noqa: F401  (pytest fixture)

from proto import controller_manager_pb2
from services.controller_manager.feedback_manager import FeedbackManager
from services.controller_manager.tests.backend_doubles import autospec_span


def _game_config() -> controller_manager_pb2.GameplayStreamConfig:
    return controller_manager_pb2.GameplayStreamConfig(
        update_frequency_hz=30,
        colors=[controller_manager_pb2.ControllerColorConfig(serial=SERIAL)],
    )


async def _configure(servicer, subscriber_id: str) -> None:
    """Open a game stream the way StreamGameplayData does: send its colours config."""
    feedback = create_autospec(FeedbackManager, instance=True)
    feedback.base_colors = {}
    servicer.feedback_manager = feedback
    await servicer._process_gameplay_config(_game_config(), subscriber_id, autospec_span())


def _disconnects(update) -> list[str]:
    return [d.serial for d in update.disconnects]


@pytest.mark.asyncio
@pytest.mark.uc("UC-002", flow="A1", br=["BR-004"])
async def test_disconnect_before_the_game_stream_opens_is_not_delivered(rig):  # noqa: F811
    servicer, _ = rig
    servicer.discovery_loop._gameplay_disconnects.append(SERIAL)  # between games

    await _configure(servicer, "game")
    first = servicer._build_gameplay_update({SERIAL}, "game")

    assert _disconnects(first) == []


@pytest.mark.asyncio
@pytest.mark.uc("UC-002", flow="A1", br=["BR-004"])
async def test_disconnect_during_the_game_stream_is_delivered_once(rig):  # noqa: F811
    servicer, _ = rig
    await _configure(servicer, "game")
    servicer._build_gameplay_update({SERIAL}, "game")

    servicer.discovery_loop._gameplay_disconnects.append(SERIAL)
    frame = servicer._build_gameplay_update({SERIAL}, "game")
    later = servicer._build_gameplay_update({SERIAL}, "game")

    assert _disconnects(frame) == [SERIAL]
    assert _disconnects(later) == []


@pytest.mark.asyncio
@pytest.mark.uc("UC-002", flow="A1", br=["BR-004"])
async def test_a_second_game_stream_does_not_take_the_first_streams_disconnects(rig):  # noqa: F811
    """A shadow game opening mid-game leaves the running game's pending disconnect to it."""
    servicer, _ = rig
    await _configure(servicer, "primary")
    servicer.discovery_loop._gameplay_disconnects.append(SERIAL)

    await _configure(servicer, "shadow")
    shadow = servicer._build_gameplay_update({SERIAL}, "shadow")
    primary = servicer._build_gameplay_update({SERIAL}, "primary")

    assert _disconnects(shadow) == []
    assert _disconnects(primary) == [SERIAL]


@pytest.mark.asyncio
@pytest.mark.uc("UC-002", flow="A1", br=["BR-004"])
async def test_every_open_game_stream_gets_a_disconnect(rig):  # noqa: F811
    servicer, _ = rig
    await _configure(servicer, "primary")
    await _configure(servicer, "shadow")

    servicer.discovery_loop._gameplay_disconnects.append(SERIAL)

    assert _disconnects(servicer._build_gameplay_update({SERIAL}, "primary")) == [SERIAL]
    assert _disconnects(servicer._build_gameplay_update({SERIAL}, "shadow")) == [SERIAL]


@pytest.mark.uc("UC-002", flow="A1", br=["BR-004"])
def test_a_closed_game_stream_drops_its_pending_disconnects(rig):  # noqa: F811
    servicer, _ = rig
    servicer._open_game_stream("game", {SERIAL})
    servicer.discovery_loop._gameplay_disconnects.append(SERIAL)

    servicer._release_game_stream("game")
    assert "game" not in servicer._pending_disconnects
    servicer._open_game_stream("next", {SERIAL})

    assert _disconnects(servicer._build_gameplay_update({SERIAL}, "next")) == []


@pytest.mark.asyncio
@pytest.mark.uc("UC-002", flow="A1", br=["BR-004"])
async def test_a_roster_controller_gone_when_the_stream_opens_is_delivered(rig):  # noqa: F811
    """A player who dropped before the stream opened (instructions clip, stream reconnect) is still reported."""
    servicer, _ = rig
    servicer.discovery_loop._gameplay_disconnects.append(SERIAL)
    del servicer.tracked_controllers[SERIAL]  # dropped and not back

    await _configure(servicer, "game")
    first = servicer._build_gameplay_update({SERIAL}, "game")
    later = servicer._build_gameplay_update({SERIAL}, "game")

    assert _disconnects(first) == [SERIAL]
    assert _disconnects(later) == []


@pytest.mark.asyncio
@pytest.mark.uc("UC-002", flow="A1", br=["BR-004"])
async def test_a_roster_controller_that_reconnected_before_the_stream_is_not_delivered(rig):  # noqa: F811
    """Dropped, reconnected and joined: tracked again at open, so no disconnect (#1607)."""
    servicer, _ = rig
    servicer.discovery_loop._gameplay_disconnects.append(SERIAL)
    assert SERIAL in servicer.tracked_controllers  # back before the game

    await _configure(servicer, "game")

    assert _disconnects(servicer._build_gameplay_update({SERIAL}, "game")) == []


@pytest.mark.uc("UC-002", flow="A1", br=["BR-004"])
def test_a_resent_config_keeps_the_pending_disconnects(rig):  # noqa: F811
    servicer, _ = rig
    servicer._open_game_stream("game", {SERIAL})
    servicer.discovery_loop._gameplay_disconnects.append(SERIAL)

    servicer._open_game_stream("game", {SERIAL})

    assert _disconnects(servicer._build_gameplay_update({SERIAL}, "game")) == [SERIAL]


@pytest.mark.uc("UC-002", flow="A1", br=["BR-004"])
def test_a_roster_controller_not_discovered_yet_is_not_delivered(rig):  # noqa: F811
    """A freshly added controller the discovery loop has not tracked yet is not a disconnect."""
    servicer, _ = rig
    del servicer.tracked_controllers[SERIAL]  # never seen, never dropped

    servicer._open_game_stream("game", {SERIAL})

    assert _disconnects(servicer._build_gameplay_update({SERIAL}, "game")) == []
