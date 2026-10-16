"""Flag-provider readiness at game admission (#1329, UC-010 A5 / BR-004).

Dry run #2 (trace c8e9aa27347f60d5a5ac1649e8dd4233) showed two
``ProviderNotReadyError`` exceptions on the game root span at t=0
(``game_loop.update_frequency_hz``, ``poll_drop_threshold``): the coordinator
evaluated its start flags before its ``system`` / ``controller`` providers were
READY. A session now waits, bounded, before creating the game.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest
from openfeature.provider import ProviderStatus
from opentelemetry import trace

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from services.game_coordinator import flag_readiness
from services.game_coordinator.flag_readiness import (
    COORDINATOR_FLAG_DOMAINS,
    FLAG_PROVIDER_READY_TIMEOUT_S,
    await_flag_providers_ready,
)


def _span():
    return create_autospec(trace.Span, instance=True)


def _patched(statuses, ready_after_wait, waits=None):
    """Patch status/init/wait/refresh.

    ``statuses``: domain -> ProviderStatus on entry (missing = READY).
    ``ready_after_wait``: domains whose wait succeeds; the others time out.
    """
    waits = waits if waits is not None else []

    async def _wait(domain, deadline_seconds):
        waits.append((domain, deadline_seconds))
        return domain in ready_after_wait

    return (
        patch.object(flag_readiness, "init_flag_domain"),
        patch.object(
            flag_readiness.api.provider_registry,
            "get_provider_status",
            side_effect=lambda d: statuses.get(d, ProviderStatus.READY),
        ),
        patch.object(flag_readiness, "wait_for_provider_ready", side_effect=_wait),
        patch("services.game_coordinator.runtime_config.refresh_runtime_config"),
    )


def _events(span, name):
    return [c.kwargs["attributes"] for c in span.add_event.call_args_list if c.args and c.args[0] == name]


ALL_NOT_READY = dict.fromkeys(COORDINATOR_FLAG_DOMAINS, ProviderStatus.NOT_READY)


@pytest.mark.asyncio
@pytest.mark.uc("UC-010", flow="A5", steps=[1, 2], br=["BR-004"])
async def test_not_ready_domains_are_awaited_then_config_refreshed():
    """UC-010 A5 steps 1-2: every NOT_READY coordinator domain is awaited, then the config is re-read."""
    span = _span()
    waits = []
    p_init, p_status, p_wait, p_refresh = _patched(ALL_NOT_READY, set(COORDINATOR_FLAG_DOMAINS), waits)
    with p_init as init, p_status, p_wait, p_refresh as refresh:
        not_ready = await await_flag_providers_ready(span)

    assert not_ready == []
    assert [c.args[0] for c in init.call_args_list] == list(COORDINATOR_FLAG_DOMAINS)
    assert waits == [(d, FLAG_PROVIDER_READY_TIMEOUT_S) for d in COORDINATOR_FLAG_DOMAINS]
    refresh.assert_called_once()
    assert _events(span, "feature_flag.providers_not_ready") == []
    assert "feature_flag.providers_wait_ms" in [c.args[0] for c in span.set_attribute.call_args_list]


@pytest.mark.asyncio
@pytest.mark.uc("UC-010", flow="A5", br=["BR-004"])
async def test_all_ready_no_wait_no_refresh():
    """UC-010 BR-004: providers already READY -> no wait and no config refresh (nothing was cached as defaults)."""
    span = _span()
    waits = []
    p_init, p_status, p_wait, p_refresh = _patched({}, set(), waits)
    with p_init, p_status, p_wait, p_refresh as refresh:
        assert await await_flag_providers_ready(span) == []
    assert waits == []
    refresh.assert_not_called()
    assert _events(span, "feature_flag.providers_not_ready") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [ProviderStatus.STALE, ProviderStatus.ERROR])
@pytest.mark.uc("UC-010", flow="A5", steps=[3], br=["BR-004"])
async def test_stale_or_error_is_not_waited_on_and_not_labelled_code_defaults(status):
    """UC-010 A5 step 3: a flagd outage (STALE/ERROR still serves last-synced flags) adds no wait or fallback label."""
    span = _span()
    waits = []
    p_init, p_status, p_wait, p_refresh = _patched({"system": status, "controller": status}, set(), waits)
    with p_init, p_status, p_wait, p_refresh as refresh:
        assert await await_flag_providers_ready(span) == []
    assert waits == []
    refresh.assert_not_called()
    assert _events(span, "feature_flag.providers_not_ready") == []


@pytest.mark.asyncio
@pytest.mark.uc("UC-010", flow="A5", steps=[3], br=["BR-004"])
async def test_not_ready_after_deadline_records_fallback():
    """UC-010 A5 step 3: domains still NOT_READY after the wait are recorded on the root span; the game still starts."""
    span = _span()
    statuses = {"system": ProviderStatus.NOT_READY, "controller": ProviderStatus.NOT_READY}
    p_init, p_status, p_wait, p_refresh = _patched(statuses, set())
    with p_init, p_status, p_wait, p_refresh:
        not_ready = await await_flag_providers_ready(span, deadline_seconds=0.2)

    assert not_ready == ["system", "controller"]
    (event,) = _events(span, "feature_flag.providers_not_ready")
    assert event == {
        "feature_flag.domains": ["system", "controller"],
        "feature_flag.wait_seconds": 0.2,
        "feature_flag.fallback": "code_defaults",
    }


@pytest.mark.asyncio
@pytest.mark.uc("UC-010", flow="A5", br=["BR-004"])
async def test_refresh_failure_never_blocks_the_start():
    """UC-010 BR-004: a failing config refresh is logged, not raised."""
    span = _span()
    p_init, p_status, p_wait, _ = _patched(ALL_NOT_READY, set(COORDINATOR_FLAG_DOMAINS))
    with (
        p_init,
        p_status,
        p_wait,
        patch("services.game_coordinator.runtime_config.refresh_runtime_config", side_effect=RuntimeError("boom")),
    ):
        assert await await_flag_providers_ready(span) == []


@pytest.mark.asyncio
@pytest.mark.uc("UC-010", flow="A5", br=["BR-004"])
async def test_wait_failure_never_becomes_game_error():
    """UC-010 BR-004: an exception inside the wait is logged and the start goes on."""
    span = _span()
    with patch.object(flag_readiness, "init_flag_domain", side_effect=RuntimeError("sdk boom")):
        assert await await_flag_providers_ready(span) == []


@pytest.mark.asyncio
@pytest.mark.uc("UC-010", flow="A5", steps=[2], br=["BR-004"])
async def test_refresh_runs_outside_the_shadow_session_context():
    """UC-010 A5 step 2: the process-wide config refresh never sees a shadow/experiment transaction context.

    Probe flag targeted on game_kind: shadow -> 9999, otherwise 1000 (the reviewer's
    winner_rainbow_duration_ms probe). Inside a shadow session the refresh must still read 1000.
    """
    from openfeature import api
    from openfeature import transaction_context as tc
    from openfeature.flag_evaluation import FlagResolutionDetails, Reason
    from openfeature.provider.in_memory_provider import InMemoryFlag, InMemoryProvider

    from lib.feature_flags import set_game_session_kind_context

    def _by_kind(flag, ctx):
        kind = ctx.attributes.get("game_kind")
        variant = "shadow" if kind == "shadow" else "real"
        return FlagResolutionDetails(value=flag.variants[variant], variant=variant, reason=Reason.TARGETING_MATCH)

    api.set_provider(
        InMemoryProvider(
            {
                "winner_rainbow_duration_ms": InMemoryFlag(
                    "real", {"real": 1000, "shadow": 9999}, context_evaluator=_by_kind
                )
            }
        ),
        domain="probe_1329",
    )
    probe = api.get_client(domain="probe_1329")
    seen = []

    def _refresh():
        seen.append(probe.get_integer_value("winner_rainbow_duration_ms", 0))

    previous = tc._evaluation_transaction_context_propagator
    api.set_transaction_context_propagator(tc.ContextVarsTransactionContextPropagator())
    try:
        set_game_session_kind_context("shadow", experiment_id="exp_1", arm="experimental")
        # Sanity: in the session context the probe resolves the shadow variant.
        assert probe.get_integer_value("winner_rainbow_duration_ms", 0) == 9999

        span = _span()
        p_init, p_status, p_wait, _ = _patched({"system": ProviderStatus.NOT_READY}, {"system"})
        with (
            p_init,
            p_status,
            p_wait,
            patch("services.game_coordinator.runtime_config.refresh_runtime_config", side_effect=_refresh),
        ):
            await await_flag_providers_ready(span)
    finally:
        api.set_transaction_context_propagator(previous)

    assert seen == [1000]


@pytest.mark.asyncio
@pytest.mark.uc("UC-010", flow="A5", steps=[1], br=["BR-004"])
@patch("services.game_coordinator.game_session.GameFactory")
@patch("services.game_coordinator.game_session.tracer")
async def test_session_waits_for_providers_before_creating_the_game(mock_tracer_mod, mock_factory):
    """UC-010 A5 step 1: the session awaits readiness on the game root span BEFORE GameFactory.create_game."""
    from test_game_session import _make_session, _tracer_mock

    order = []
    session = _make_session()
    tracer_mock = _tracer_mock()
    mock_tracer_mod.start_as_current_span = tracer_mock.start_as_current_span
    root_span = tracer_mock.start_as_current_span.return_value.__enter__.return_value

    async def _ready(span):
        order.append(("ready", span))
        return []

    mock_game = MagicMock()
    mock_game.run = AsyncMock()

    def _create(**_kwargs):
        order.append(("create", None))
        return mock_game

    mock_factory.create_game.side_effect = _create
    with patch("services.game_coordinator.game_session.await_flag_providers_ready", side_effect=_ready):
        await session._run_game_loop_async()

    assert [o[0] for o in order] == ["ready", "create"]
    assert order[0][1] is root_span


@pytest.mark.uc("UC-010", flow="A5", steps=[2], br=["BR-004"])
def test_refresh_runtime_config_rereads_existing_manager():
    """UC-010 A5 step 2: an existing runtime-config manager re-reads its flags; none yet -> created."""
    from services.game_coordinator import runtime_config

    existing = MagicMock()
    with patch.object(runtime_config, "_global_config_manager", existing):
        runtime_config.refresh_runtime_config()
        existing._refresh_from_flags.assert_called_once()

    with (
        patch.object(runtime_config, "_global_config_manager", None),
        patch.object(runtime_config, "RuntimeConfigManager") as manager_cls,
    ):
        runtime_config.refresh_runtime_config()
        manager_cls.assert_called_once()
        assert runtime_config._global_config_manager is manager_cls.return_value


@pytest.mark.asyncio
@pytest.mark.uc("UC-010", flow="A5", steps=[1], br=["BR-004"])
@patch("services.game_coordinator.game_session.GameFactory")
@patch("services.game_coordinator.game_session.tracer")
async def test_force_end_during_the_wait_creates_no_orphan_game(mock_tracer_mod, mock_factory):
    """UC-010 A5 / UC-027: a force-end while the session waits for providers -> no game is created or run."""
    from test_game_session import _make_session, _tracer_mock

    from proto import game_coordinator_pb2

    session = _make_session()
    session.game_running = True  # admitted (start_thread sets this)
    mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span

    async def _force_ended_while_waiting(_span):
        session.game_running = False  # what GameSession.force_end does first
        return []

    with patch(
        "services.game_coordinator.game_session.await_flag_providers_ready", side_effect=_force_ended_while_waiting
    ):
        await session._run_game_loop_async()

    mock_factory.create_game.assert_not_called()
    assert session.current_game is None
    assert session.game_state == game_coordinator_pb2.GameState.ENDED
