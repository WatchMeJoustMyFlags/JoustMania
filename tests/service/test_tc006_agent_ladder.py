"""TC-006 on the service tier (T2): the host restricts the agent from the controller (steps 1 to 4).

LOBBY composition on a fresh flag plane: real menu, game coordinator and
controller manager (mock backend) from source at HEAD, flag service + flagd;
audio is FakeAudio, so the per-level voice feedback is asserted on the recorded
PlaySound calls.

The seam under test is the one the menu's unit tests cannot reach: mock button ->
controller manager -> menu admin handler -> flag service (persisted admin value
of ``interventions_allowed``, UC-053 A2) -> flagd. The journey walks the whole
ladder none, ambient, standard, full and back, and presses past both ends to
assert it does not wrap (the #1340 regression, clamped since #1373).

Stated gap (TC-006 step 5, S7 of #1422): the agent being blocked by the
permission needs the agent composition. Steps 6 and 7 stay covered by
tests/integration/test_intervention_flow.py. No LED colour feedback is
asserted (UC-013 step 4 colours, incl. the 800 ms red pulse, too short to read through
GetColor without a sleep); only the voice is checked.

The ladder writes the GLOBAL ``interventions_allowed``, so the test runs on
``fresh_stack``: a stack of its own, nothing leaks to another test.
"""

import pytest
from openfeature.evaluation_context import EvaluationContext

from proto import controller_manager_mock_pb2, menu_pb2
from tests.integration.flag_service import default as flag_service_default
from tests.integration.flag_service import variant_value
from tests.integration.helpers import (
    ADMIN_WHITE,
    async_poll_until,
    enter_admin_mode,
    flagd_probe_client,
    get_menu_client,
    get_mock_client,
    press_admin_button,
    select_game_mode,
    setup_mock_controllers,
    wait_for_lobby_colors,
)
from tests.service.harness import LOBBY

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = LOBBY

AGENT = "agent"
FLAG = "interventions_allowed"
START_MODE = "JoustFFA"
PERSIST_TIMEOUT_SECONDS = 5.0
VOICE_TIMEOUT_SECONDS = 5.0
LOBBY_TIMEOUT_SECONDS = 15.0

# The voice the menu asks the audio service for per level
# (services/menu/handlers/admin.py level_voices, lib/types.py Sound.MENU_VOX_ADMINOP_*).
LEVEL_VOICE = {"none": "adminop_off", "ambient": "adminop_1", "standard": "adminop_2", "full": "adminop_3"}

_Button = controller_manager_mock_pb2.ButtonRequest.Button


def _effective_variant() -> str:
    return flag_service_default().get(AGENT, FLAG)["effective_default"]


def _persisted_admin_variant() -> str | None:
    admin = flag_service_default().get(AGENT, FLAG).get("admin_value")
    return admin.get("variant") if admin else None


async def _start_lobby(stack, menu_client, mock_client) -> list[str]:
    """The menu runs with four CONNECTED controllers on JoustFFA.

    The menu is restarted and the mode selected BEFORE the controllers are added
    (StopMenu clears the controller states; controllers added first can connect
    before the restart and be lost, #1575).
    """
    await menu_client.StopMenu(menu_pb2.StopMenuRequest())
    start = await menu_client.StartMenu(menu_pb2.StartMenuRequest())
    assert start.success, f"failed to start menu: {start.error}"
    assert await select_game_mode(menu_client, START_MODE), f"could not select {START_MODE}"
    serials = await setup_mock_controllers(stack, count=4)
    # White does not count: the mock adapter starts every LED white, the same as admin.
    await wait_for_lobby_colors(mock_client, serials, timeout=LOBBY_TIMEOUT_SECONDS, exclude=ADMIN_WHITE)
    return serials


async def _press_and_expect(stack, mock_client, host: str, button, expected: str) -> None:
    """Press ``button`` in admin mode and assert the persisted level and the voice feedback."""
    stack.fake_audio.clear()
    await press_admin_button(mock_client, host, button)
    assert await async_poll_until(lambda: _persisted_admin_variant() == expected, timeout=PERSIST_TIMEOUT_SECONDS), (
        f"after {_Button.Name(button)}: persisted {FLAG} is {_persisted_admin_variant()!r}, expected {expected!r}"
    )
    assert _effective_variant() == expected
    voice = LEVEL_VOICE[expected]
    assert await async_poll_until(
        lambda: any(c.request.get("file_path") == voice for c in stack.fake_audio.calls_of("PlaySound")),
        timeout=VOICE_TIMEOUT_SECONDS,
    ), f"no {voice!r} feedback for {expected}; PlaySound: {[c.request for c in stack.fake_audio.calls_of('PlaySound')]}"


async def _flagd_serves(stack, variant: str) -> bool:
    expected = variant_value(AGENT, FLAG, variant)
    with flagd_probe_client(stack, AGENT) as probe:
        return await async_poll_until(
            lambda: probe.get_string_value(FLAG, "unset", EvaluationContext()) == expected,
            timeout=PERSIST_TIMEOUT_SECONDS,
        )


@pytest.mark.asyncio
@pytest.mark.uc("UC-012", flow="main", steps=[1, 2], tc="TC-006")
@pytest.mark.uc("UC-012", flow="A6", tc="TC-006")
@pytest.mark.uc("UC-013", flow="main", steps=[1, 2, 3], tc="TC-006")
@pytest.mark.uc("UC-013", flow="A1", tc="TC-006")
@pytest.mark.uc("UC-053", flow="A2", tc="TC-006")
@pytest.mark.usefixtures("flag_service")  # binds the flag-service client to this stack
async def test_host_walks_the_agent_ladder_from_the_controller(fresh_stack):
    """The host enters admin mode, selects agent_control and walks the ladder
    up and down; the presses past both ends clamp (TC-006 steps 1 to 4)."""
    stack = fresh_stack
    menu_client, menu_channel = await get_menu_client(stack)
    mock_client, mock_channel = await get_mock_client(stack)
    try:
        serials = await _start_lobby(stack, menu_client, mock_client)
        host = serials[0]

        # UC-013 A1: a value off the ladder (probe) jumps to none on Start.
        flag_service_default().put(AGENT, FLAG, variant="probe")
        assert await async_poll_until(lambda: _effective_variant() == "probe", timeout=PERSIST_TIMEOUT_SECONDS)

        # Step 1 (UC-012 steps 1, 2): the combo enters admin mode.
        await enter_admin_mode(mock_client, host)

        # Step 2 (UC-013 step 1): three Move presses select agent_control (index 0 -> 3).
        for _ in range(3):
            await press_admin_button(mock_client, host, _Button.MOVE)

        # A1: Start at probe lands on none.
        await _press_and_expect(stack, mock_client, host, _Button.START, "none")
        # Clamp at the bottom: Start at none stays none (the #1340 wrap would give full).
        await _press_and_expect(stack, mock_client, host, _Button.START, "none")

        # Escalate up the whole ladder with Select, then past the top.
        for level in ("ambient", "standard", "full"):
            await _press_and_expect(stack, mock_client, host, _Button.SELECT, level)
        await _press_and_expect(stack, mock_client, host, _Button.SELECT, "full")

        # Step 3: restrict twice from standard (full -> standard -> ambient -> none).
        for level in ("standard", "ambient", "none"):
            await _press_and_expect(stack, mock_client, host, _Button.START, level)

        # Step 4 / validation 2: flagd serves none; the admin value is persisted.
        assert await _flagd_serves(stack, "none"), "flagd did not serve interventions_allowed=none"
        assert _persisted_admin_variant() == "none"
    finally:
        for channel in (menu_channel, mock_channel):
            await channel.close()
