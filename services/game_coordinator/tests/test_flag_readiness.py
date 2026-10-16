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


def _patched(ready_domains, waits=None):
    """Patch init/wait/refresh: ``ready_domains`` become READY, the rest time out."""
    waits = waits if waits is not None else []

    async def _wait(domain, deadline_seconds):
        waits.append((domain, deadline_seconds))
        return domain in ready_domains

    return (
        patch.object(flag_readiness, "init_flag_domain"),
        patch.object(flag_readiness, "wait_for_provider_ready", side_effect=_wait),
        patch("services.game_coordinator.runtime_config.refresh_runtime_config"),
    )


def _events(span, name):
    return [c.kwargs["attributes"] for c in span.add_event.call_args_list if c.args and c.args[0] == name]


@pytest.mark.asyncio
@pytest.mark.uc("UC-010", flow="A5", steps=[1, 2], br=["BR-004"])
async def test_all_ready_waits_every_domain_and_refreshes_config():
    """UC-010 A5 steps 1-2: every coordinator domain is initialized and awaited, then config re-read."""
    span = _span()
    waits = []
    p_init, p_wait, p_refresh = _patched(set(COORDINATOR_FLAG_DOMAINS), waits)
    with p_init as init, p_wait, p_refresh as refresh:
        not_ready = await await_flag_providers_ready(span)

    assert not_ready == []
    assert [c.args[0] for c in init.call_args_list] == list(COORDINATOR_FLAG_DOMAINS)
    assert waits == [(d, FLAG_PROVIDER_READY_TIMEOUT_S) for d in COORDINATOR_FLAG_DOMAINS]
    assert {"system", "controller"} <= {d for d, _ in waits}  # the two that failed in dry run #2
    refresh.assert_called_once()
    assert _events(span, "feature_flag.providers_not_ready") == []
    keys = [c.args[0] for c in span.set_attribute.call_args_list]
    assert "feature_flag.providers_wait_ms" in keys


@pytest.mark.asyncio
@pytest.mark.uc("UC-010", flow="A5", steps=[3], br=["BR-004"])
async def test_not_ready_after_deadline_records_fallback():
    """UC-010 A5 step 3: domains still NOT_READY are recorded on the root span; the game still starts."""
    span = _span()
    p_init, p_wait, p_refresh = _patched({"game", "agent", "user", "interventions"})
    with p_init, p_wait, p_refresh:
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
    p_init, p_wait, _ = _patched(set(COORDINATOR_FLAG_DOMAINS))
    with (
        p_init,
        p_wait,
        patch("services.game_coordinator.runtime_config.refresh_runtime_config", side_effect=RuntimeError("boom")),
    ):
        assert await await_flag_providers_ready(span) == []


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
