"""TC-011 on the service tier (T2): a shadow experiment runs to a verdict (S9 of #1422).

AGENT composition: the real agent (built at HEAD), game coordinator and
controller manager (mock backend) from source, a fresh flag plane, FakeAudio and
the OtlpSink. The agent runs on the inference-absent path (``stub`` backend,
``decision.backend`` = ``rules``) with one seeded experiment (the ``experiment_seed`` flag); the verdict
minimums and tick are lowered with stack overlays, as the former
full-stack module did with compose overlays.

The agent shares the stack's one game coordinator: there is no second
agent coordinator. The stack
is the module's, so no other test competes for its session registry.

Per-module split (#1422 S9): what a unit or contract test can prove stays there;
this module runs the one thing only a running agent, coordinator and controller
manager prove together: the loop declares, spawns paired shadow games on
reserved pattern-driven mock controllers, scores them, reaches a verdict and
tears down. The agent's log is the observable (the agent has no state API).
"""

import re
from dataclasses import replace

import pytest

from tests.integration.flag_service import FlagService
from tests.integration.flag_service import bind as bind_flag_service
from tests.integration.helpers import (
    async_poll_until,
    get_game_client,
    list_games,
    list_mock_controllers_detailed,
    poll_until,
)
from tests.service.agent_flow import AGENT_SERVICE
from tests.service.harness import AGENT

pytestmark = pytest.mark.tier("service")

COORDINATOR = "game-coordinator-service"

SEED_FLAG = "windows"
SEED_VALUE = {
    "min_music_fast_time": 6,
    "max_music_fast_time": 11,
    "min_music_slow_time": 6,
    "max_music_slow_time": 14,
    "end_min_music_fast_time": 8,
    "end_max_music_fast_time": 14,
    "end_min_music_slow_time": 5,
    "end_max_music_slow_time": 8,
}
# The experiment seed is the experiment_seed flag (#1907): its declared variant
# windows_frantic is rewritten in the stack's baseline to this test's seed and served
# before ``enabled`` (the registry takes declared variants only).
SEED_VARIANT = "windows_frantic"
SEED = {"flag": SEED_FLAG, "value": SEED_VALUE, "objective": "balanced", "target_n": 2}
SERVICE_COMPOSITION = replace(
    AGENT,
    flag_variant_values={("agent", "experiment_seed", SEED_VARIANT): SEED},
)

# Agent overlays (flags.md section 13.2): bounded so the loop ends in about a minute.
TUNING = {
    "verdict_min_n": 1,
    "verdict_min_pairs": 1,
    "experiment_tick_seconds": 3,
    "experiment_max_games": 6,
    "experiment.mock_archetypes": "quick_cascade",
    "experiment.max_game_seconds": 90,
}
# Served before the agent starts (Composition.agent_flags), so its first tick already reads them.
SERVICE_COMPOSITION = replace(
    SERVICE_COMPOSITION,
    agent_flags={
        "experiment_seed": {"variant": SEED_VARIANT},  # before the gates
        "enabled": {"variant": "on"},
        "experiments_enabled": {"variant": "on"},
        "decision.backend": {"variant": "rules"},
        **{key: {"value": value} for key, value in TUNING.items()},
    },
)
# The second experiment tick comes within 5 s of start (first-tick cap, #1907); 90 s is margin.
DECLARE_TIMEOUT = 90.0
CONCLUDE_TIMEOUT = 300.0
# Absence window after teardown: several experiment ticks (3 s each).
NO_RESPAWN_WINDOW = 10.0
# A game's flag-service end is queued at its end (#1847); 3 s is well below the 15 s
# reconcile backstop, so a pass shows the end was reported, not reconciled away.
GAME_END_TIMEOUT = 3.0


def _agent_log(stack) -> str:
    return stack.agent.log_path.read_text(errors="replace")


def _lines(stack, needle: str) -> list[str]:
    return [line for line in _agent_log(stack).splitlines() if needle in line]


def _field(line: str, key: str) -> str:
    match = re.search(rf'\b{re.escape(key)}=("[^"]*"|\S+)', line)
    assert match, f"no {key}= in {line!r}"
    return match.group(1).strip('"')


def _wait_log(stack, needle: str, timeout: float) -> bool:
    return poll_until(lambda: needle in _agent_log(stack), timeout=timeout, interval=1.0)


@pytest.fixture(scope="module")
def experiment_stack(service_stack):
    """The module's stack with the agent enabled and the bounded tuning served.

    The tests below run in order against the one live loop: each waits for the
    transition it asserts, so a later test sees the log of the earlier ones.
    """
    client = FlagService.from_compose(service_stack)
    bind_flag_service(client)
    try:
        yield service_stack
    finally:
        bind_flag_service(None)
        client.cleanup()


@pytest.mark.uc("UC-040", flow="A1", tc="TC-011")
@pytest.mark.uc("UC-046", flow="main", steps=[7], tc="TC-011")
def test_agent_starts_inference_absent_and_decides_with_rules(experiment_stack):
    """Step 1: no inference backend is wired (``backend=stub``) and the first shadow game is
    decided by the rules backend; the agent does not panic."""
    stack = experiment_stack
    selected = _lines(stack, "Proposer/retro inference backend selected")
    assert selected and "backend=stub" in selected[0], selected
    assert _wait_log(stack, "decision_backend=rules", CONCLUDE_TIMEOUT), _agent_log(stack)[-3000:]
    assert "panic:" not in _agent_log(stack)


@pytest.mark.uc("UC-043", flow="main", steps=[1], tc="TC-011")
def test_experiment_is_declared(experiment_stack):
    """Step 2: the seeded experiment is declared on the seeded flag, then started."""
    stack = experiment_stack
    assert _wait_log(stack, "experiment.declared", DECLARE_TIMEOUT), _agent_log(stack)[-3000:]
    declared = _lines(stack, "experiment.declared")
    assert len(declared) == 1, declared
    assert _field(declared[0], "flag_key") == SEED_FLAG
    assert _wait_log(stack, "experiment.started", DECLARE_TIMEOUT)


@pytest.mark.asyncio
@pytest.mark.uc("UC-043", flow="main", steps=[2, 3], tc="TC-011")
@pytest.mark.uc("UC-046", flow="main", steps=[3], tc="TC-011")
@pytest.mark.uc("UC-003", flow="A2", tc="TC-011")
async def test_first_game_is_a_shadow_game_on_reserved_controllers(experiment_stack, flag_service):
    """Steps 3 and 4: the experimental arm runs as its own game on reserved mock controllers.

    The game has its own id and is registered with the flag service; its players are the
    reserved controllers the agent added, which the lobby (the non-reserved list) never sees.
    """
    stack = experiment_stack
    assert _wait_log(stack, "experiment.game_assigned", DECLARE_TIMEOUT), _agent_log(stack)[-3000:]
    assigned = _lines(stack, "experiment.game_assigned")[0]
    game_id = _field(assigned, "game_id")
    assert _field(assigned, "arm") == "experimental"  # one arm at a time, experimental first (UC-046 BR-010)

    game_client, channel = await get_game_client(stack)
    try:
        listed = {}

        async def running() -> bool:
            listed.update({g.game_id: g for g in await list_games(game_client)})
            return game_id in listed

        assert await async_poll_until(running, timeout=DECLARE_TIMEOUT), f"{game_id} not listed: {sorted(listed)}"
    finally:
        await channel.close()
    serials = [p.serial for p in listed[game_id].players]
    assert serials, f"{game_id} has no players"

    controllers = {c.serial: c for c in await list_mock_controllers_detailed(stack)}
    assert all(serial in controllers and controllers[serial].reserved for serial in serials), (
        f"players {serials} are not all reserved mock controllers: "
        f"{[(c.serial, c.reserved) for c in controllers.values()]}"
    )
    registered = flag_service.wait_game_registered(game_id)
    assert registered["experiment_id"] == _field(_lines(stack, "experiment.declared")[0], "experiment_id")
    assert registered["arm"] == "experimental"


@pytest.mark.uc("UC-043", flow="main", steps=[4], tc="TC-011")
@pytest.mark.uc("UC-041", flow="main", tc="TC-011")
def test_each_game_is_scored_for_its_arm(experiment_stack):
    """Steps 5 and 6: both arms conclude, each with a fitness in [0, 1] logged for its arm."""
    stack = experiment_stack
    assert poll_until(
        lambda: len(_lines(stack, "experiment.game_concluded")) >= 2, timeout=CONCLUDE_TIMEOUT, interval=1.0
    ), _agent_log(stack)[-4000:]
    concluded = _lines(stack, "experiment.game_concluded")
    assert sorted(_field(line, "arm") for line in concluded) == ["control", "experimental"], concluded
    for line in concluded:
        assert 0.0 <= float(_field(line, "fitness")) <= 1.0, line
    assert len({_field(line, "game_id") for line in concluded}) == 2, "the arms ran in two games"


@pytest.mark.asyncio
@pytest.mark.uc("UC-043", flow="main", steps=[5, 8], tc="TC-011")
@pytest.mark.uc("UC-043", flow="A3", tc="TC-011")
async def test_verdict_is_inconclusive_and_the_experiment_is_torn_down(experiment_stack, flag_service):
    """Steps 7 and 8 and the validations.

    One synthetic pair cannot reach a paired SD (needs >= 2 pairs), so the verdict is
    inconclusive and the experiment is discarded. Both games of the pair share one seed
    (validation 1), no game is spawned after the teardown (validation 3), the experiment's
    arm values are gone from the flag service and its reserved controllers are removed.
    """
    stack = experiment_stack
    assert _wait_log(stack, "experiment.torn_down", CONCLUDE_TIMEOUT), _agent_log(stack)[-4000:]
    concluded = _lines(stack, "experiment.concluded")
    assert len(concluded) == 1 and _field(concluded[0], "outcome") == "inconclusive", concluded
    torn_down = _lines(stack, "experiment.torn_down")
    assert len(torn_down) == 1 and _field(torn_down[0], "status") == "discarded", torn_down
    assert "code_improvement.promoted" not in _agent_log(stack), "an inconclusive verdict was promoted"

    # Validation 1: every concluded experimental game has a control game with the same seed.
    spawned = _lines(stack, "experiment: shadow game spawned")
    seeds = {_field(line, "arm"): _field(line, "seed") for line in spawned}
    assert set(seeds) == {"experimental", "control"}, spawned
    assert seeds["experimental"] == seeds["control"] != "0", f"arms not paired by seed: {spawned}"

    # Validation 3: the experiment spawns nothing further (absence window of several 3 s ticks).
    count = len(spawned)
    assert not await async_poll_until(
        lambda: len(_lines(stack, "experiment: shadow game spawned")) != count, NO_RESPAWN_WINDOW
    ), "a game was spawned after the experiment was torn down"

    # Postconditions: the journal holds the concluded experiment; the arm values and the
    # reserved controllers are gone.
    assert list((stack.workdir / "experiments").glob("exp_*/summary.json")), "experiment not journaled"
    # The coordinator reports each game's end as it ends (UC-053 step 7, #1847); the
    # DELETE is queued, so poll instead of reading once.
    spawned_ids = {_field(line, "game_id") for line in spawned}
    assert await async_poll_until(
        lambda: not {g["game_id"] for g in flag_service.games()} & spawned_ids, GAME_END_TIMEOUT, interval=0.5
    ), f"shadow games still registered: {sorted({g['game_id'] for g in flag_service.games()} & spawned_ids)}"
    assert [c.serial for c in await list_mock_controllers_detailed(stack) if c.reserved] == []
    experiment_id = _field(_lines(stack, "experiment.declared")[0], "experiment_id")
    view = flag_service.get("game", SEED_FLAG)
    assert experiment_id not in str(view), f"arm value of {experiment_id} still on the flag service: {view}"


@pytest.mark.asyncio
@pytest.mark.uc("UC-043", flow="main", br=["BR-001"], tc="TC-011")
async def test_no_real_game_ran_with_the_experiment(experiment_stack):
    """Validation 2: every game the agent summarised was a shadow game, and the coordinator holds no other."""
    stack = experiment_stack
    summaries = _lines(stack, "agent.game.summary_built")
    assert summaries and all(_field(line, "game_kind") == "shadow" for line in summaries), summaries
    game_client, channel = await get_game_client(stack)
    try:
        live = {g.game_id for g in await list_games(game_client)}
    finally:
        await channel.close()
    spawned = {_field(line, "game_id") for line in _lines(stack, "experiment: shadow game spawned")}
    assert live <= spawned, f"a game the experiment did not spawn exists (a real game?): {live - spawned}"


@pytest.mark.uc("UC-043", flow="main", steps=[4], tc="TC-011")
def test_shadow_game_is_its_own_trace_linked_to_the_experiment(experiment_stack):
    """#2223: with telemetry.experiment_game_parent at its default (`link`) a shadow game
    roots its own trace and carries a Link `link.kind=experiment` to the experiment span.

    The agent.experiment span is exported when the experiment ends, so this runs after
    the teardown test above (the module's tests run in order).
    """
    stack = experiment_stack
    sink = stack.otlp_sink
    experiment_id = _field(_lines(stack, "experiment.declared")[0], "experiment_id")
    experiment_span = sink.wait_span(
        "agent.experiment", {"experiment.id": experiment_id}, service=AGENT_SERVICE, timeout=30
    )
    game_roots = sink.spans(None, {"experiment.id": experiment_id}, service=COORDINATOR, root=True)
    assert game_roots, "no shadow game root span carries the experiment id"
    for root in game_roots:
        assert root.trace_id != experiment_span.trace_id, "the shadow game joined the experiment trace"
        experiment_links = [link for link in root.links if link.attributes.get("link.kind") == "experiment"]
        assert [(link.trace_id, link.span_id) for link in experiment_links] == [
            (experiment_span.trace_id, experiment_span.span_id)
        ], root.links
