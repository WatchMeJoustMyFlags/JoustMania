"""TC-005 on the service tier (T2): the host tunes the game in admin mode and force-starts.

Moved from tests/integration/test_admin_flow.py (#1562): its seven per-step
tests are chained into one journey, every assertion kept. LOBBY composition:
real menu, game coordinator and controller manager (mock backend) from source
at HEAD on a fresh flag plane (flag service + flagd); audio is FakeAudio, so the
UC-011 step 3 announcement is asserted on the recorded PlaySound calls.

The seams under test are the ones unit tests cannot reach: mock button ->
controller manager -> menu combo detection, the menu -> flag service -> flagd
path the admin writes take (persisted admin values, UC-053 A2), and the
menu -> coordinator force-start handoff (#1029).

Mechanics (from the live code):

- Entry combo detection is incremental (``servicer.on_button``): the four face
  buttons are pressed without releasing, then released so ``combo_shown`` clears.
- ADMIN_ENTER drives the LED to admin white after a brief flash; it is polled.
- Admin options cycle ``[sensitivity, num_teams, force_all_start,
  agent_control]`` from index 0; Cross cycles the game mode whatever the option.
- ``force_all_start_seconds`` is ``ci`` (0.6 s) in services/flagd/ci/game.json,
  read at use time, so a 1.5 s trigger hold force-starts (TC-005 says 3 s).
- UC-012 step 6 (the next game uses the new value) is read from the force-started
  game's ``game_sensitivity`` gauge, so the stack exports OTLP to its sink.
- UC-012 Post-S-1: the admin writes are persisted admin values on the flag
  service; the baseline flag files of the stack stay byte-for-byte unchanged.
- The test changes stack-wide state (the ``force_all_start`` overlay and
  persisted admin values), so it runs on ``fresh_stack``: a stack of its own,
  nothing leaks to another test, and no teardown of the admin values is needed
  (the ``flag_service`` fixture still deletes the stack overlay it set).
"""

import asyncio
import dataclasses

import pytest
from openfeature.evaluation_context import EvaluationContext

from proto import controller_manager_mock_pb2, game_coordinator_pb2
from tests.integration.flag_service import default as flag_service_default
from tests.integration.flag_service import variant_value
from tests.integration.helpers import (
    ADMIN_WHITE,
    GameEventCollector,
    MenuEventCollector,
    async_poll_until,
    enter_admin_mode,
    exit_admin_mode,
    flagd_probe_client,
    get_controller_color,
    get_game_client,
    get_menu_client,
    get_mock_client,
    hold_trigger_for_force_start,
    list_games,
    press_admin_button,
    press_admin_combo,
)
from tests.service.harness import LOBBY
from tests.service.lobby_flow import start_lobby

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = dataclasses.replace(LOBBY, otlp=True)

GAME = "game"
USER = "user"
START_MODE = "JoustFFA"
# The next playable mode after JoustFFA in the game registry (UC-011 step 2).
EXPECTED_MODE = "JoustTeams"
# The voice the menu asks the audio service for (services/menu/utils/audio.py
# GAME_MODE_VOICE, lib/types.py Sound.MENU_VOX_JOUST_TEAMS).
EXPECTED_ANNOUNCEMENT = "menu Joust Teams"
SENSITIVITY_ORDER = ["ultra_slow", "slow", "medium", "fast", "ultra_fast"]
NUM_TEAMS_ORDER = ["two", "three", "four", "five", "six"]

# How long a settings write is given to reach the flag service.
PERSIST_TIMEOUT_SECONDS = 5.0
LED_TIMEOUT_SECONDS = 5.0
EVENT_TIMEOUT_SECONDS = 10.0
START_TIMEOUT_SECONDS = 15.0
# A gauge reaches the sink at the next metrics export.
EXPORT_TIMEOUT_SECONDS = 30.0
COORDINATOR = "game-coordinator-service"
# Absence assertion: a partial combo must not enter admin. There is no event to
# await when the correct behaviour is "nothing happens", so the bounded wait IS
# the test.
PARTIAL_COMBO_WINDOW_SECONDS = 1.0

_Button = controller_manager_mock_pb2.ButtonRequest.Button


# --------------------------------------------------------------------------- #
# Flag service views
# --------------------------------------------------------------------------- #
def _default_variant(flag_set: str, flag_key: str) -> str:
    """The variant the flag service serves now (the effective default)."""
    return flag_service_default().get(flag_set, flag_key)["effective_default"]


def _persisted_admin_value(flag_set: str, flag_key: str) -> dict | None:
    """The persisted admin value of a flag (``admin_value`` in the view), if any."""
    return flag_service_default().get(flag_set, flag_key).get("admin_value")


async def _wait_default_variant_changes(flag_set: str, flag_key: str, baseline: str) -> str:
    """Poll the flag service until ``flag_key``'s effective default differs from baseline."""
    current = baseline

    def changed() -> bool:
        nonlocal current
        current = _default_variant(flag_set, flag_key)
        return current != baseline

    await async_poll_until(changed, timeout=PERSIST_TIMEOUT_SECONDS)
    return current


def _flag_files(stack) -> dict[str, bytes]:
    """The stack's baseline flag files (the flag service's baseline directory), by name."""
    return {path.name: path.read_bytes() for path in sorted(stack.flag_plane.flag_dir.glob("*.json"))}


def _next(order: list[str], variant: str) -> str:
    return order[(order.index(variant) + 1) % len(order)]


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #
async def _serve_force_all_start(stack) -> None:
    """Precondition: ``force_all_start`` is ``on`` (a stack overlay), served by flagd."""
    flag_service_default().put(GAME, "force_all_start", variant="on")
    with flagd_probe_client(stack, GAME) as probe:
        served = await async_poll_until(
            lambda: probe.get_boolean_value("force_all_start", False, EvaluationContext()) is True,
            rewrite=lambda: flag_service_default().put(GAME, "force_all_start", variant="on"),
        )
    assert served, "flagd never served force_all_start=on"


async def _wait_led_leaves_admin_white(mock_client, serial: str) -> tuple[int, int, int]:
    """Poll until the LED is lit and not admin white.

    Off does not count: the force-start flash alternates white and (0, 0, 0),
    so an off-phase read is not yet the game colour.
    """
    color = await get_controller_color(mock_client, serial)

    async def left_white() -> bool:
        nonlocal color
        color = await get_controller_color(mock_client, serial)
        return color != ADMIN_WHITE and sum(color) > 0

    await async_poll_until(left_white, timeout=LED_TIMEOUT_SECONDS)
    return color


async def _end_all_games(stack) -> None:
    game_client, channel = await get_game_client(stack)
    try:
        await game_client.ForceEndGame(game_coordinator_pb2.ForceEndGameRequest(reason="tc005_teardown"))
    finally:
        await channel.close()


# --------------------------------------------------------------------------- #
# TC-005
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.uc("UC-012", flow="main", steps=[1, 2], tc="TC-005")
@pytest.mark.uc("UC-012", flow="main", steps=[3, 4, 5], tc="TC-005")
@pytest.mark.uc("UC-012", flow="main", steps=[6], br=["BR-001"], tc="TC-005")
@pytest.mark.uc("UC-012", flow="main", steps=[7, 8], tc="TC-005")
@pytest.mark.uc("UC-012", flow="A1", tc="TC-005")
@pytest.mark.uc("UC-012", cond=["Post-S-1"], tc="TC-005")
# UC-011 step 3: only the announcement is asserted, not the colour pulse (TC-005 gap).
@pytest.mark.uc("UC-011", flow="main", steps=[1, 2, 4], tc="TC-005")
@pytest.mark.uc("UC-014", flow="main", steps=[1, 2, 3, 5], tc="TC-005")
@pytest.mark.uc("UC-053", flow="A2", tc="TC-005")
@pytest.mark.usefixtures("flag_service")  # binds the flag-service client to this stack
async def test_host_tunes_and_force_starts(fresh_stack):
    """The host enters admin mode, changes sensitivity, num_teams and the game
    mode, leaves and re-enters admin, and force-starts a game in the selected mode."""
    stack = fresh_stack
    menu_client, menu_channel = await get_menu_client(stack)
    mock_client, mock_channel = await get_mock_client(stack)
    game_client, game_channel = await get_game_client(stack)
    try:
        flag_files_before = _flag_files(stack)
        assert flag_files_before, "the stack has no baseline flag files"
        await _serve_force_all_start(stack)
        serials = await start_lobby(stack, count=4, game_mode=START_MODE)
        host = serials[0]
        sensitivity_baseline = _default_variant(GAME, "sensitivity")
        num_teams_baseline = _default_variant(GAME, "num_teams")
        assert sensitivity_baseline == "medium", "TC-005 precondition: sensitivity serves medium"

        # UC-012 step 1 (negative): three of the four face buttons do NOT enter admin.
        await press_admin_combo(mock_client, host, buttons=(_Button.CROSS, _Button.CIRCLE, _Button.SQUARE))
        await asyncio.sleep(PARTIAL_COMBO_WINDOW_SECONDS)
        color = await get_controller_color(mock_client, host)
        assert color != ADMIN_WHITE, f"partial combo entered admin mode (LED={color}, expected non-white)"

        # Step 1 (UC-012 steps 1, 2): the full combo enters admin; the LED settles to white.
        await enter_admin_mode(mock_client, host)
        assert await get_controller_color(mock_client, host) == ADMIN_WHITE

        # Step 2 (UC-012 A1): Circle advances sensitivity, persisted as an admin value (UC-053 A2).
        await press_admin_button(mock_client, host, _Button.CIRCLE)
        sensitivity = await _wait_default_variant_changes(GAME, "sensitivity", sensitivity_baseline)
        assert sensitivity == _next(SENSITIVITY_ORDER, sensitivity_baseline), (
            f"sensitivity advanced to unexpected variant: {sensitivity_baseline} -> {sensitivity}"
        )
        admin = _persisted_admin_value(GAME, "sensitivity")
        assert admin and admin.get("variant") == sensitivity, (
            f"sensitivity change was not persisted as an admin value: {admin}"
        )

        # Step 3: flagd serves the new level.
        expected_level = variant_value(GAME, "sensitivity", sensitivity)
        with flagd_probe_client(stack, GAME) as probe:
            assert await async_poll_until(
                lambda: probe.get_integer_value("sensitivity", -1, EvaluationContext()) == expected_level
            ), f"flagd did not serve sensitivity={expected_level}"

        # UC-012 steps 3 to 5: Move selects num_teams (index 0 -> 1), Select increments it.
        await press_admin_button(mock_client, host, _Button.MOVE)
        await press_admin_button(mock_client, host, _Button.SELECT)
        num_teams = await _wait_default_variant_changes(GAME, "num_teams", num_teams_baseline)
        assert num_teams == _next(NUM_TEAMS_ORDER, num_teams_baseline), (
            f"num_teams advanced to unexpected variant: {num_teams_baseline} -> {num_teams}"
        )

        async with (
            MenuEventCollector(menu_client) as menu_events,
            GameEventCollector(game_client) as game_events,
        ):
            # Step 4 (UC-011 steps 1, 2 and the step 3 announcement): Cross
            # advances the mode and announces it.
            stack.fake_audio.clear()
            await press_admin_button(mock_client, host, _Button.CROSS)
            selection = await menu_events.wait_for_event("selection_changed", timeout=EVENT_TIMEOUT_SECONDS)
            assert selection.data.get("game_name") == EXPECTED_MODE, (
                f"admin Cross did not advance selection to {EXPECTED_MODE}: {dict(selection.data)}"
            )
            announced = await async_poll_until(
                lambda: any(
                    call.request.get("file_path") == EXPECTED_ANNOUNCEMENT
                    for call in stack.fake_audio.calls_of("PlaySound")
                ),
                timeout=LED_TIMEOUT_SECONDS,
            )
            assert announced, (
                f"UC-011 step 3: no {EXPECTED_ANNOUNCEMENT!r} announcement; "
                f"PlaySound calls: {[c.request for c in stack.fake_audio.calls_of('PlaySound')]}"
            )
            # Validation 2: the mode is persisted as an admin value too.
            assert await async_poll_until(
                lambda: (_persisted_admin_value(USER, "current_game") or {}).get("variant") == EXPECTED_MODE,
                timeout=PERSIST_TIMEOUT_SECONDS,
            ), f"current_game not persisted as an admin value: {_persisted_admin_value(USER, 'current_game')}"

            # UC-012 steps 7, 8: PS exits admin; the LED leaves admin white.
            await exit_admin_mode(mock_client, host)
            color = await _wait_led_leaves_admin_white(mock_client, host)
            assert color != ADMIN_WHITE, f"LED stayed admin-white after PS exit (={color})"

            # Step 5 (UC-014 steps 1 to 3): re-enter admin and hold the trigger.
            await enter_admin_mode(mock_client, host)
            await hold_trigger_for_force_start(mock_client, host, hold_seconds=1.5)

            # Step 6: the menu requests the admin-selected mode (UC-014 step 5) ...
            requested = await menu_events.wait_for_event("game_requested", timeout=EVENT_TIMEOUT_SECONDS)
            assert requested.data.get("source") == "admin_force_start", (
                f"unexpected force-start source: {dict(requested.data)}"
            )
            assert requested.data.get("game_name") == EXPECTED_MODE, (
                f"force start requested {requested.data.get('game_name')}, expected admin-cycled {EXPECTED_MODE}"
            )
            # ... and the coordinator starts it (#1029: the start path is invoked).
            await game_events.wait_for_event("game_started", timeout=START_TIMEOUT_SECONDS)

        # Validation 1: the started game runs in the mode selected in step 4.
        games = await list_games(game_client)
        assert games, "no game after force start"
        started = [game for game in games if game.game_mode == EXPECTED_MODE]
        assert started, f"started game mode {[g.game_mode for g in games]}, expected {EXPECTED_MODE}"
        # Step 5: the roster is every connected controller.
        roster = {player.serial for player in started[0].players}
        assert roster == set(serials), f"force-start roster {sorted(roster)}, expected {sorted(serials)}"

        # Step 7: the host's controller shows a game colour: lit and not admin white.
        color = await _wait_led_leaves_admin_white(mock_client, host)
        assert color != ADMIN_WHITE and sum(color) > 0, f"host LED not a game colour after the start (={color})"

        # UC-012 step 6: the next game runs with the sensitivity changed in admin mode.
        gauge = await stack.otlp_sink.wait_metric_async(
            "game_sensitivity",
            {"game_id": started[0].game_id},
            service=COORDINATOR,
            timeout=EXPORT_TIMEOUT_SECONDS,
        )
        assert gauge.value == expected_level, (
            f"the force-started game runs at sensitivity {gauge.value}, expected the admin's {expected_level}"
        )

        # UC-012 Post-S-1: the settings live on the flag service; the flag files are unchanged.
        assert _flag_files(stack) == flag_files_before, "an admin write changed a baseline flag file"
    finally:
        await _end_all_games(stack)
        for channel in (menu_channel, mock_channel, game_channel):
            await channel.close()
