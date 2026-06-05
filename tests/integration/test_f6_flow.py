"""
End-to-end coverage for the F6 intervention levers (#802, #766, #787).

F6 added two new state-shaped intervention flags plus a flagd ``targeting``
if-ladder on ``game.json``'s ``windows`` flag (preset selection by
``targetingKey``). The #730 levers are covered by ``test_intervention_flow.py``;
F6 shipped with unit tests only. This file extends the SAME e2e harness (compose
lifecycle, in-place flag-file mutation with snapshot/restore via the
``flag_files`` fixture, StreamGameEvents collector, live flagd RPC resolver) to
validate the F6 additions against the real services.

Observability routes (F6 effects are NOT exposed on the GetGameState proto):

- ``global_difficulty_factor`` (type ``adjust_global_difficulty``): a live float
  multiplied into every player's effective threshold by
  ``_compute_effective_thresholds``. It is not a proto field, so the observable
  signal is the unblocked ``agent_intervention`` event (the EventBus mirror of
  ``game_interventions_total``) on apply, a permission-block event on deny, and
  the quiet revert path (no new applied event) when reset to the neutral 1.0.
- ``pacing_profile`` (type ``set_pacing_profile``): swaps ``game.music_windows``
  (also not a proto field). Observable signal is the unblocked applied event,
  plus the LIVE flagd RPC resolution of the ``windows`` targeting ladder that the
  handler relies on (known preset -> preset windows; unknown -> default variant).
- The ``windows`` preset ladder lives on the ``game`` flag set; we evaluate it
  through the published flagd RPC port (8013) with ``selector=flagSetId=game``,
  exactly the path the game-coordinator handler uses, catching schema/targeting
  rejections the jsonlogic-direct Go unit tests cannot.

flag files are bind-mounted host files; the ``flag_files`` fixture (imported from
``test_intervention_flow``) snapshots and restores them byte-for-byte.
"""

import asyncio
import time

import pytest

from tests.integration.helpers import (  # noqa: E402
    GameEventCollector,
    get_game_client,
    get_mock_controller_serials,
    setup_mock_controllers,
    start_game_via_menu,
)
from tests.integration.test_intervention_flow import (  # noqa: E402
    APPLY_TIMEOUT_SECONDS,
    RELOAD_SETTLE_SECONDS,
    _get_state,
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
# Scenario 1: global_difficulty_factor apply + permission-block + revert
# =============================================================================


@pytest.mark.asyncio
async def test_global_difficulty_factor_applies_and_reverts(flag_files, docker_compose, game_client):
    """global_difficulty_factor e2e: writing a non-neutral factor fires an
    unblocked ``adjust_global_difficulty`` event (the lever reaches the live game
    where it shifts every player's effective thresholds); reverting to the
    neutral 1.0 runs the revert handler WITHOUT firing a new applied event.

    The factor itself is not on the GetGameState proto, so the observable signal
    is the applied event on the StreamGameEvents stream (apply) and the absence
    of a further applied event (revert).
    """
    set_interventions_allowed("full")  # adjust_global_difficulty is full-only
    # Rate-limit headroom is provided suite-wide by the flag_files fixture
    # (SUITE_RATE_LIMIT_BUDGET in test_intervention_flow); no per-test bump needed.
    await setup_mock_controllers(docker_compose, count=4)

    game_client_obj, channel = await get_game_client(docker_compose)
    try:
        collector = GameEventCollector(game_client_obj)
        async with collector:
            await start_game_via_menu(
                docker_compose, game_mode="JoustFFA", event_collector=collector
            )

            # Baseline: players present at the neutral per-player factor.
            info = await _get_state(game_client)
            assert info.players, "no players in game state"

            # Agent writes a harder global difficulty (1.5).
            write_state("global_difficulty_factor", 1.5)

            # Observable: the lever applied (reached the live game), not blocked.
            evt = await _wait_for_intervention_event(
                collector, "adjust_global_difficulty", blocked="false"
            )
            assert evt.data.get("blocked") == "false"

            applied_after_apply = len(
                [e for e in _intervention_events(collector, "adjust_global_difficulty")
                 if e.data.get("blocked") == "false"]
            )

            # Revert to the neutral 1.0: runs the revert handler, no new applied
            # event (reverting to neutral is not an enforced intervention).
            write_state("global_difficulty_factor", 1.0)
            await asyncio.sleep(RELOAD_SETTLE_SECONDS + 1.0)
            applied_after_revert = len(
                [e for e in _intervention_events(collector, "adjust_global_difficulty")
                 if e.data.get("blocked") == "false"]
            )
            assert applied_after_revert == applied_after_apply, (
                "revert to neutral 1.0 spuriously fired a new applied event"
            )
    finally:
        await channel.close()


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
    resolution itself is asserted against live flagd in
    ``test_windows_preset_ladder_resolves_against_live_flagd``.
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
            await asyncio.sleep(RELOAD_SETTLE_SECONDS)
            write_state("pacing_profile", "frantic")
            await _wait_applied_count(2)

            applied_after_apply = _applied_count()

            # Revert to neutral: revert handler restores init windows, no new event.
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


# =============================================================================
# Scenario 3: windows preset targeting ladder via LIVE flagd RPC resolver
# =============================================================================


@pytest.mark.asyncio
async def test_windows_preset_ladder_resolves_against_live_flagd(flag_files, docker_compose):
    """The F6 ``windows`` targeting if-ladder evaluated through the LIVE flagd RPC
    resolver: a known preset key (``calm`` / ``frantic``) resolves the matching
    preset window object; an unknown key falls through the ladder to the
    ``default`` variant. This is the schema/targeting-rejection risk the
    jsonlogic-direct Go unit tests cannot catch — the game-coordinator's
    pacing_profile handler resolves the same flag the same way.

    Note: ``windows`` lives on the ``game`` flag set (game.json), so the provider
    selector is ``flagSetId=game`` (vs the interventions selector used by the
    #730 per-player targeting test).
    """
    from openfeature import api
    from openfeature.contrib.provider.flagd import FlagdProvider
    from openfeature.contrib.provider.flagd.config import ResolverType
    from openfeature.evaluation_context import EvaluationContext

    host = docker_compose.get_service_host("flagd", 8013)
    port = int(docker_compose.get_service_port("flagd", 8013))

    provider = FlagdProvider(
        host=host,
        port=port,
        resolver_type=ResolverType.RPC,
        selector="flagSetId=game",
    )
    api.set_provider(provider, domain="it_game_windows")
    client = api.get_client("it_game_windows")

    def _resolve(preset: str) -> dict:
        return client.get_object_value(
            "windows", {}, EvaluationContext(targeting_key=preset)
        )

    # Poll until the provider has connected and served the committed flag set.
    deadline = time.time() + APPLY_TIMEOUT_SECONDS
    while time.time() < deadline:
        if _resolve("calm").get("min_music_fast_time") is not None:
            break
        time.sleep(0.3)

    try:
        calm = _resolve("calm")
        frantic = _resolve("frantic")
        default = _resolve("default")
        unknown = _resolve("SER_NOT_A_PRESET")

        # Known presets resolve to their distinct preset window objects.
        assert calm.get("min_music_slow_time") == 13, f"calm resolved wrong: {calm}"
        assert frantic.get("min_music_fast_time") == 6, f"frantic resolved wrong: {frantic}"
        # The presets are genuinely different objects (ladder branches, not a
        # single shared variant).
        assert calm != frantic, "calm and frantic resolved to the same object"

        # Unknown key falls through the if-ladder to the default variant.
        assert unknown == default, (
            f"unknown preset did not fall back to default: {unknown} != {default}"
        )
        assert unknown.get("min_music_fast_time") == 4, (
            f"default variant has unexpected values: {unknown}"
        )
    finally:
        provider.shutdown()
