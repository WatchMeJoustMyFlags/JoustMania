"""End-to-end admin-mode integration tests (#823).

Admin mode is the controller-driven settings/force-start surface: a controller
holds all four face buttons (Cross+Circle+Square+Triangle) to ENTER, then uses
Move (cycle option), Select/Start (adjust value), face buttons (sensitivity /
game mode / etc.), a trigger-hold (force start), and PS (exit). It has rich unit
coverage (services/menu/tests/test_admin_handler.py) but no integration coverage
until now; admin mode "regularly breaks" precisely at the seams these unit tests
cannot reach — the mock-button -> controller-manager -> menu combo detection,
the FlagConfigWriter -> flagd file the writes actually land in, and the
menu->coordinator force-start handoff. These tests exercise all three against
the real docker-compose stack.

Mechanics (verified against the live code):

- Entry combo detection is INCREMENTAL in the menu (servicer.on_button): each
  face-button press updates a per-serial button_state, and entry fires on the
  press that completes the four-button set. So we press the four face buttons
  without releasing (helpers.press_admin_combo), then release all four so the
  ``combo_shown`` latch clears for the next entry.
- ADMIN_ENTER drives the controller LED to white (255,255,255); we poll the
  mock GetColor RPC with a deadline because the enter effect briefly flashes.
- Settings persist by FlagConfigWriter flipping a flag's ``defaultVariant`` in
  the CI flag files (services/flagd/ci/{game,user}.json) — the SAME files flagd
  serves in CI (#959/#962). The ``admin_flags`` fixture snapshots and restores
  them so no mutation leaks between tests or into the repo.
- Force start publishes a menu ``game_requested`` event (source
  ``admin_force_start``); the coordinator then emits ``game_started``.
- The 3s real-play trigger-hold is shortened to 0.6s in CI via
  ADMIN_FORCE_START_SECONDS (docker-compose.ci.yml) so a ~1.5s hold suffices.

The surviving controller-cycled surface after the #815 rationalization (PR
#1010) is sensitivity / num_teams / force_all_start, plus agent_control
(the agent kill-switch, appended last by #819) — admin.py option_names. These
tests target sensitivity / num_teams / force_all_start; the removed-from-
controller tunables are not tested here, and agent_control (last index) does not
shift their positions.
"""

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from openfeature.evaluation_context import EvaluationContext  # noqa: E402

from proto import (  # noqa: E402
    controller_manager_mock_pb2,
    menu_pb2,
)

from tests.integration.helpers import (  # noqa: E402
    ADMIN_WHITE,
    GameEventCollector,
    MenuEventCollector,
    active_flag_file,
    async_poll_until,
    enter_admin_mode,
    exit_admin_mode,
    flagd_probe_client,
    get_controller_color,
    get_controller_serials,
    get_game_client,
    get_menu_client,
    get_mock_client,
    hold_trigger_for_force_start,
    press_admin_button,
    press_admin_combo,
    select_game_mode,
    setup_mock_controllers,
    wait_for_lobby_colors,
)

GAME_FLAG_PATH = active_flag_file("game")
USER_FLAG_PATH = active_flag_file("user")

# How long a settings write is given to round-trip to the host flag file.
PERSIST_TIMEOUT_SECONDS = 5.0

_Button = controller_manager_mock_pb2.ButtonRequest.Button


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def admin_flags():
    """Snapshot game.json + user.json; restore byte-for-byte on teardown.

    Admin settings changes persist by flipping ``defaultVariant`` in these CI
    flag files (the files flagd serves). Snapshot/restore keeps each test
    isolated and prevents any mutation leaking into the repo or later tests.
    """
    with open(GAME_FLAG_PATH) as fh:
        game_backup = fh.read()
    with open(USER_FLAG_PATH) as fh:
        user_backup = fh.read()

    yield

    with open(GAME_FLAG_PATH, "w") as fh:
        fh.write(game_backup)
    with open(USER_FLAG_PATH, "w") as fh:
        fh.write(user_backup)


@pytest.fixture
async def menu_client(docker_compose):
    client, channel = await get_menu_client(docker_compose)
    yield client
    await channel.close()


@pytest.fixture
async def mock_client(docker_compose):
    client, channel = await get_mock_client(docker_compose)
    yield client
    await channel.close()


# =============================================================================
# Helpers local to admin tests
# =============================================================================


async def _start_menu_with_controllers(
    docker_compose, menu_client, game_mode: str = "JoustFFA", count: int = 4
) -> list[str]:
    """Start a fresh menu with ``count`` connected (not-ready) controllers.

    Stops then starts the menu so controller state is clean, selects a game
    mode, and waits until the menu has registered the controllers (each gets a
    lobby base color via the connection events). Returns the controller serials.
    Controllers are left CONNECTED (not ready) so the admin combo is detected in
    the CONNECTED state, the way a human enters admin from the lobby.
    """
    # Clear any running game and stale menu state, then start fresh.
    game_client, game_channel = await get_game_client(docker_compose)
    try:
        from proto import game_coordinator_pb2

        await game_client.ForceEndGame(
            game_coordinator_pb2.ForceEndGameRequest(reason="admin_test_setup")
        )
    finally:
        await game_channel.close()
    await asyncio.sleep(0.2)

    await setup_mock_controllers(docker_compose, count=count)

    await menu_client.StopMenu(menu_pb2.StopMenuRequest())
    await asyncio.sleep(0.1)
    start = await menu_client.StartMenu(menu_pb2.StartMenuRequest())
    assert start.success, f"failed to start menu: {start.error}"
    await asyncio.sleep(0.3)

    await select_game_mode(menu_client, game_mode)

    serials = await get_controller_serials(docker_compose)
    assert serials, "no controllers connected to menu"
    # Give the menu a moment to apply lobby base colors from connection events.
    await asyncio.sleep(0.3)
    return serials


def _default_variant(path: str, flag_key: str) -> str:
    with open(path) as fh:
        doc = json.load(fh)
    return doc["flags"][flag_key]["defaultVariant"]


def _set_default_variant(path: str, flag_key: str, variant: str) -> None:
    """Flip a flag's defaultVariant in the served CI flag file (idempotent).

    flagd hot-reloads the file; the ``admin_flags`` fixture restores it. Safe to
    call repeatedly as a ``poll_until`` rewrite to self-heal a coalesced
    file-watch event under CI load.
    """
    with open(path) as fh:
        doc = json.load(fh)
    if doc["flags"][flag_key]["defaultVariant"] == variant:
        return
    doc["flags"][flag_key]["defaultVariant"] = variant
    with open(path, "w") as fh:
        json.dump(doc, fh, indent=2)


async def _wait_default_variant_changes(
    path: str, flag_key: str, baseline: str, timeout: float = PERSIST_TIMEOUT_SECONDS
) -> str:
    """Poll the flag file until ``flag_key``'s defaultVariant differs from baseline."""
    deadline = asyncio.get_event_loop().time() + timeout
    current = baseline
    while asyncio.get_event_loop().time() < deadline:
        current = _default_variant(path, flag_key)
        if current != baseline:
            return current
        await asyncio.sleep(0.1)
    return current


# =============================================================================
# 1. Combo entry -> admin white LED
# =============================================================================


@pytest.mark.asyncio
async def test_combo_entry_lights_admin_white(docker_compose, menu_client, mock_client):
    """Holding the four face buttons enters admin mode: the controller LED
    settles to admin white (255,255,255)."""
    serials = await _start_menu_with_controllers(docker_compose, menu_client)
    admin_serial = serials[0]

    # enter_admin_mode presses the combo and polls until the LED is white.
    await enter_admin_mode(mock_client, admin_serial)
    assert await get_controller_color(mock_client, admin_serial) == ADMIN_WHITE

    # Clean up: leave admin mode so the controller doesn't stay latched.
    await exit_admin_mode(mock_client, admin_serial)


# =============================================================================
# 2. PS exit -> LED leaves admin white (returns to lobby)
# =============================================================================


@pytest.mark.asyncio
async def test_ps_exit_restores_lobby_color(docker_compose, menu_client, mock_client):
    """PS while in admin mode exits: the LED leaves admin white and returns to
    the (dim) lobby color the controller had before entering."""
    serials = await _start_menu_with_controllers(docker_compose, menu_client)
    admin_serial = serials[0]

    await enter_admin_mode(mock_client, admin_serial)
    assert await get_controller_color(mock_client, admin_serial) == ADMIN_WHITE

    await exit_admin_mode(mock_client, admin_serial)

    # ADMIN_EXIT restores the pre-admin base (lobby) color instantly; assert the
    # LED is no longer the admin white. The exact lobby color depends on the
    # connection state, so we assert "left admin white" rather than a literal.
    deadline = asyncio.get_event_loop().time() + 5.0
    color = await get_controller_color(mock_client, admin_serial)
    while color == ADMIN_WHITE and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.1)
        color = await get_controller_color(mock_client, admin_serial)
    assert color != ADMIN_WHITE, f"LED stayed admin-white after PS exit (={color})"


# =============================================================================
# 3. Partial combo (3 of 4 buttons) does NOT enter admin
# =============================================================================


@pytest.mark.asyncio
async def test_partial_combo_does_not_enter_admin(
    docker_compose, menu_client, mock_client
):
    """Pressing only three of the four face buttons must NOT enter admin mode:
    the LED never goes admin-white."""
    serials = await _start_menu_with_controllers(docker_compose, menu_client)
    admin_serial = serials[0]

    # Press only 3 of the 4 face buttons (omit TRIANGLE).
    await press_admin_combo(
        mock_client,
        admin_serial,
        buttons=(_Button.CROSS, _Button.CIRCLE, _Button.SQUARE),
    )

    # Give the menu a window to (incorrectly) react, then assert NOT white. This
    # absence assertion is a bounded wait by design — there is no event to await
    # when the correct behavior is "nothing happens".
    await asyncio.sleep(1.0)
    color = await get_controller_color(mock_client, admin_serial)
    assert color != ADMIN_WHITE, (
        f"partial combo entered admin mode (LED={color}, expected non-white)"
    )


# =============================================================================
# 4. Sensitivity change (Circle) persists to game.json
# =============================================================================


@pytest.mark.asyncio
async def test_sensitivity_change_persists(
    docker_compose, menu_client, mock_client, admin_flags
):
    """In admin mode, Circle cycles sensitivity; the change persists by advancing
    the ``sensitivity`` flag's defaultVariant in the served game.json."""
    serials = await _start_menu_with_controllers(docker_compose, menu_client)
    admin_serial = serials[0]

    baseline = _default_variant(GAME_FLAG_PATH, "sensitivity")

    await enter_admin_mode(mock_client, admin_serial)
    # Circle = cycle sensitivity forward.
    await press_admin_button(mock_client, admin_serial, _Button.CIRCLE)

    new_variant = await _wait_default_variant_changes(
        GAME_FLAG_PATH, "sensitivity", baseline
    )
    assert new_variant != baseline, (
        f"sensitivity defaultVariant did not change (still {baseline})"
    )
    # It advanced to the next variant in the file's variant order.
    order = ["ultra_slow", "slow", "medium", "fast", "ultra_fast"]
    assert new_variant == order[(order.index(baseline) + 1) % len(order)], (
        f"sensitivity advanced to unexpected variant: {baseline} -> {new_variant}"
    )

    await exit_admin_mode(mock_client, admin_serial)


# =============================================================================
# 5. Option cycle (Move) + value adjust (Select) persists num_teams
# =============================================================================


@pytest.mark.asyncio
async def test_option_cycle_and_value_change_persists(
    docker_compose, menu_client, mock_client, admin_flags
):
    """Move cycles the selected option (sensitivity -> num_teams), then Select
    increments the now-selected option's value, persisted to game.json.

    The option order is ``[sensitivity, num_teams, force_all_start,
    agent_control]`` and starts at index 0 (sensitivity) on entry, so one Move
    lands on num_teams (agent_control is appended last and does not shift this).
    """
    serials = await _start_menu_with_controllers(docker_compose, menu_client)
    admin_serial = serials[0]

    baseline = _default_variant(GAME_FLAG_PATH, "num_teams")

    await enter_admin_mode(mock_client, admin_serial)
    # Move: sensitivity -> num_teams (current_option index 0 -> 1).
    await press_admin_button(mock_client, admin_serial, _Button.MOVE)
    # Select: increment the current option (num_teams) forward.
    await press_admin_button(mock_client, admin_serial, _Button.SELECT)

    new_variant = await _wait_default_variant_changes(
        GAME_FLAG_PATH, "num_teams", baseline
    )
    assert new_variant != baseline, (
        f"num_teams defaultVariant did not change after Move+Select (still {baseline})"
    )
    order = ["two", "three", "four", "five", "six"]
    assert new_variant == order[(order.index(baseline) + 1) % len(order)], (
        f"num_teams advanced to unexpected variant: {baseline} -> {new_variant}"
    )

    await exit_admin_mode(mock_client, admin_serial)


# =============================================================================
# 6. Trigger-hold force start -> menu game_requested + game game_started
# =============================================================================


@pytest.mark.asyncio
async def test_force_start_requests_game(
    docker_compose, menu_client, mock_client, admin_flags
):
    """Holding the trigger in admin mode force-starts the game: the menu publishes
    a ``game_requested`` event (source=admin_force_start) AND actually invokes the
    coordinator start path (#1029).

    Previously ``handle_force_start`` only published the menu event and set
    GAME_STARTING; it never invoked the menu's ``start_game`` coordinator path, so
    the menu lit up but NO start was ever attempted. The fix wires force-start
    through ``MenuServicer.start_game`` with the force_all_start-aware roster.

    We enable ``force_all_start`` so the roster is the menu's CONNECTED
    controllers (no dependence on cross-test ready state) and poll flagd until the
    write is served. The decisive #1029 signal is that the menu now *invokes* the
    coordinator start: it either reaches ``game_started`` (roster intact) or, if
    the shared-session roster has churned below the minimum, surfaces
    ``game_start_cancelled`` from inside ``_start_game`` — BOTH happen only because
    ``start_game`` is now called. Before the fix neither occurred (the menu sat in
    GAME_STARTING with no coordinator contact at all).
    """
    # Enable force_all_start and confirm flagd is serving it (rewrite self-heal
    # covers a coalesced file-watch event under CI load) before force-starting.
    _set_default_variant(GAME_FLAG_PATH, "force_all_start", "on")
    with flagd_probe_client(docker_compose, "game") as probe:
        served = await async_poll_until(
            lambda: (
                probe.get_boolean_value("force_all_start", False, EvaluationContext())
                is True
            ),
            rewrite=lambda: _set_default_variant(
                GAME_FLAG_PATH, "force_all_start", "on"
            ),
        )
    assert served, "flagd never served force_all_start=on"

    serials = await _start_menu_with_controllers(docker_compose, menu_client)
    admin_serial = serials[0]

    # Wait until the controllers are registered in the lobby (they only get a
    # lobby color once the menu sees them connected).
    await wait_for_lobby_colors(mock_client, serials, timeout=15.0)

    game_client, game_channel = await get_game_client(docker_compose)
    try:
        async with (
            MenuEventCollector(menu_client) as menu_collector,
            GameEventCollector(game_client) as game_collector,
        ):
            await enter_admin_mode(mock_client, admin_serial)

            # Hold the trigger past the (CI-shortened) force-start threshold.
            await hold_trigger_for_force_start(
                mock_client, admin_serial, hold_seconds=1.5
            )

            requested = await menu_collector.wait_for_event(
                "game_requested", timeout=10.0
            )
            assert requested.data.get("source") == "admin_force_start", (
                f"unexpected force-start source: {dict(requested.data)}"
            )
            assert requested.data.get("game_name"), (
                f"force-start request carried no game_name: {dict(requested.data)}"
            )

            # #1029: force-start must INVOKE the coordinator start path. Accept
            # either a real coordinator start (game_started) or the menu's
            # start-aborted signal (game_start_cancelled) — both prove start_game
            # was called; before the fix, neither ever fired.
            started_task = asyncio.create_task(
                game_collector.wait_for_event("game_started", timeout=15.0)
            )
            cancelled_task = asyncio.create_task(
                menu_collector.wait_for_event("game_start_cancelled", timeout=15.0)
            )
            done, pending = await asyncio.wait(
                {started_task, cancelled_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            invoked = any(not t.cancelled() and t.exception() is None for t in done)
            assert invoked, "force-start did not invoke the coordinator start path"
    finally:
        await game_channel.close()


# =============================================================================
# 7. Force start respects the admin-selected game mode (Cross cycles mode)
# =============================================================================


@pytest.mark.asyncio
async def test_force_start_respects_selected_game_mode(
    docker_compose, menu_client, mock_client, admin_flags
):
    """Admin Cross cycles the game mode (#1030) and force start respects it:
    starting from JoustFFA, one Cross press in admin advances the menu selection
    to JoustTeams (the next entry in the game registry), and a subsequent force
    start publishes a ``game_requested`` naming JoustTeams.

    Previously the Cross path was DEAD: ``get_game_options()`` always returned []
    so Cross logged "No game options available" and changed nothing. The fix
    populates ``MenuServicer.game_options`` from the game registry and applies the
    selection through the menu, so admin Cross cycling is now live.
    """
    # Start on JoustFFA; one Cross press advances to the next registry entry.
    serials = await _start_menu_with_controllers(
        docker_compose, menu_client, game_mode="JoustFFA"
    )
    admin_serial = serials[0]
    expected_mode = "JoustTeams"  # GAME_MODES[index(JoustFFA) + 1]

    async with MenuEventCollector(menu_client) as menu_collector:
        await enter_admin_mode(mock_client, admin_serial)

        # Cross: cycle the game mode forward. This must actually change the menu's
        # selection now (not just log "No game options available").
        await press_admin_button(mock_client, admin_serial, _Button.CROSS)
        selection = await menu_collector.wait_for_event(
            "selection_changed", timeout=10.0
        )
        assert selection.data.get("game_name") == expected_mode, (
            f"admin Cross did not advance selection to {expected_mode}: {dict(selection.data)}"
        )

        # Force start: the requested mode must be the admin-cycled selection.
        await hold_trigger_for_force_start(mock_client, admin_serial, hold_seconds=1.5)
        requested = await menu_collector.wait_for_event("game_requested", timeout=10.0)
        assert requested.data.get("source") == "admin_force_start"
        assert requested.data.get("game_name") == expected_mode, (
            f"force start requested {requested.data.get('game_name')}, "
            f"expected admin-cycled {expected_mode}"
        )
