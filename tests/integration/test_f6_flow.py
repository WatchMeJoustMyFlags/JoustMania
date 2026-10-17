"""
End-to-end coverage for the F6 intervention levers (#802, #766, #787).

F6 added two new state-shaped intervention flags plus a flagd ``targeting``
if-ladder on ``game.json``'s ``windows`` flag (preset selection by
``targetingKey``). The #730 levers are covered by ``test_intervention_flow.py``;
F6 shipped with unit tests only. This file extends the SAME e2e harness (compose
lifecycle, in-place flag-file mutation with snapshot/restore via the
``flag_files`` fixture, StreamGameEvents collector) to validate the F6
additions against the real services.

Observability routes (F6 effects are NOT exposed on the GetGameState proto):

- ``global_difficulty_factor`` (type ``adjust_global_difficulty``): a live float
  multiplied into every player's effective threshold by
  ``_compute_effective_thresholds``. It is not a proto field, so the observable
  signal is the unblocked ``agent_intervention`` event (the EventBus mirror of
  ``game_interventions_total``) on apply, a permission-block event on deny, and
  the quiet revert path (no new applied event) when reset to the neutral 1.0.
- ``pacing_profile`` (type ``set_pacing_profile``): swaps ``game.music_windows``
  (also not a proto field). Observable signal is the unblocked applied event.
  The ``windows`` targeting ladder the handler relies on (known preset -> preset
  windows; unknown -> default variant) is resolved against a real flagd in the
  contract tier (tests/contract/test_flagd_resolution.py, #1422).

flag files are bind-mounted host files; the ``flag_files`` fixture (imported from
``test_intervention_flow``) snapshots and restores them byte-for-byte.
"""

import asyncio
import time

import pytest

from tests.integration.helpers import (  # noqa: E402
    GameEventCollector,
    get_game_client,
    setup_mock_controllers,
    start_game_via_menu,
)
from tests.integration.test_intervention_flow import (  # noqa: E402
    APPLY_TIMEOUT_SECONDS,
    RELOAD_SETTLE_SECONDS,
    _intervention_events,
    _wait_for_intervention_event,
    flag_files,  # re-exported fixture (snapshot/restore interventions.json + agent.json)
    game_client,  # re-exported fixture (game-coordinator client)
    set_interventions_allowed,
    write_state,
)

# Keep both fixtures referenced so linters don't flag the imports as unused.
__all__ = ["flag_files", "game_client"]


# =============================================================================
# Scenario 1: global_difficulty_factor permission-block (sequential)
#
# The apply+revert happy path was moved to the gameId-scoped parallel batch
# (test_parallel_intervention_flow.py::_scenario_global_difficulty). This
# permission-BLOCK negative stays sequential: it sets the GLOBAL
# ``interventions_allowed=standard`` (not gameId-scopeable) which would clobber
# a concurrent ``full`` batch.
# =============================================================================


@pytest.mark.asyncio
async def test_global_difficulty_factor_blocked_without_permission(flag_files, docker_compose, game_client):
    """Permission-block negative for an F6 lever: with interventions_allowed set
    to ``standard`` (which omits ``adjust_global_difficulty``), the factor write
    is blocked (block_reason=not_allowed) and never reaches the live game.
    """
    set_interventions_allowed("standard")  # adjust_global_difficulty NOT in standard
    await setup_mock_controllers(docker_compose, count=4)

    game_client_obj, channel = await get_game_client(docker_compose)
    try:
        collector = GameEventCollector(game_client_obj)
        async with collector:
            await start_game_via_menu(
                docker_compose, game_mode="JoustFFA", event_collector=collector
            )

            write_state("global_difficulty_factor", 1.5)

            evt = await _wait_for_intervention_event(
                collector, "adjust_global_difficulty", blocked="true"
            )
            assert evt.data.get("block_reason") == "not_allowed", (
                f"unexpected block_reason: {evt.data.get('block_reason')}"
            )

            # No applied event ever fired — the lever did not reach the game.
            applied = [
                e for e in _intervention_events(collector, "adjust_global_difficulty")
                if e.data.get("blocked") == "false"
            ]
            assert not applied, "blocked global_difficulty_factor still applied"
    finally:
        await channel.close()


# =============================================================================
# Scenario 2: pacing_profile apply (calm / frantic) + revert
# =============================================================================


@pytest.mark.asyncio
async def test_pacing_profile_applies_and_reverts(flag_files, docker_compose, game_client):
    """pacing_profile e2e: writing ``calm`` then ``frantic`` each fires an
    unblocked ``set_pacing_profile`` event (the handler swaps
    ``game.music_windows`` to the resolved preset). Reverting to ``none`` runs
    the revert handler (restores the init windows) WITHOUT a new applied event.

    music_windows is not on the GetGameState proto; the observable signal is the
    applied event on apply and the quiet revert path on reset. The preset->windows
    resolution itself is asserted against a real flagd in the contract tier
    (tests/contract/test_flagd_resolution.py
    ``test_windows_preset_ladder_resolves_against_live_flagd``).
    """
    set_interventions_allowed("standard")  # set_pacing_profile is in standard+
    # Two MEDIUM pacing writes fit under the suite-wide budget pinned by the
    # flag_files fixture (SUITE_RATE_LIMIT_BUDGET); no per-test bump needed.
    await setup_mock_controllers(docker_compose, count=4)

    game_client_obj, channel = await get_game_client(docker_compose)
    try:
        collector = GameEventCollector(game_client_obj)
        async with collector:
            await start_game_via_menu(
                docker_compose, game_mode="JoustFFA", event_collector=collector
            )

            def _applied_count() -> int:
                return len(
                    [e for e in _intervention_events(collector, "set_pacing_profile")
                     if e.data.get("blocked") == "false"]
                )

            async def _wait_applied_count(at_least: int) -> None:
                deadline = time.time() + APPLY_TIMEOUT_SECONDS
                while time.time() < deadline:
                    if _applied_count() >= at_least:
                        return
                    await asyncio.sleep(0.2)
                raise AssertionError(
                    f"expected >={at_least} applied set_pacing_profile events, "
                    f"got {_applied_count()}"
                )

            # calm preset -> first applied event.
            write_state("pacing_profile", "calm")
            await _wait_applied_count(1)

            # frantic preset (distinct value change) -> a second applied event.
            # No settle needed (#894): the applied event above proves the calm
            # write completed the whole flagd-reload -> coordinator-apply chain,
            # so the frantic write cannot coalesce with it.
            write_state("pacing_profile", "frantic")
            await _wait_applied_count(2)

            applied_after_apply = _applied_count()

            # Revert to neutral: revert handler restores init windows, no new event.
            # Absence assertion — bounded wait by design (#894): there is no
            # event to wait for when the correct behavior is "no new event".
            write_state("pacing_profile", "none")
            await asyncio.sleep(RELOAD_SETTLE_SECONDS + 1.0)
            applied_after_revert = len(
                [e for e in _intervention_events(collector, "set_pacing_profile")
                 if e.data.get("blocked") == "false"]
            )
            assert applied_after_revert == applied_after_apply, (
                "revert to neutral spuriously fired a new applied set_pacing_profile event"
            )
    finally:
        await channel.close()
