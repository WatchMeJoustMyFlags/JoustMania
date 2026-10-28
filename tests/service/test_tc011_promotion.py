"""TC-011 on the service tier (T2): a ``promote`` verdict is promoted as a GitHub issue (#1404).

AGENT composition plus a fake GitHub (``stack.fake_github``): the real agent (built
at HEAD), game coordinator and controller manager (mock backend) from source, a
fresh flag plane and the OtlpSink. The agent's promoter has its GitHub env gate set
(``AGENT_CODE_IMPROVEMENT_ENABLED``, token, repository, target ``github``) with
``GITHUB_API_URL`` on the stub, so ``code_improvement.mode`` = ``issue`` opens the
issue there and never on github.com.

Synthetic shadow games carry no product signal, so the test makes the verdict
deterministic: the seeded experiment (the ``experiment_seed`` flag) scores the ``endurance`` objective (session
length over the baseline ``fitness.endurance.min_session_seconds``), conclusive from two
seed-matched pairs (``verdict_min_pairs`` = 2, two archetypes), and the test, acting like the agent's
``eliminate_player`` writer, ends each control game as soon as it runs and each
experimental game after ``EXPERIMENTAL_HOLD_SECONDS``. Every experimental game is
then longer than its control game, both pairs differ the same way, and the paired
effect clears the default thresholds: the verdict is ``promote``. The games still end
on their own terms (a winner after eliminations), so the agent scores them as usual.

``test_tc011_shadow_experiment.py`` runs the same loop to its synthetic
``inconclusive`` verdict; this module covers UC-043 steps 6 and 7 and tightens the
teardown checks (pairing by seed, no spawn after teardown, the arm targeting gone
from what flagd serves for the ``game`` set).
"""

import asyncio
import json
import re
from dataclasses import replace

import pytest
from openfeature.evaluation_context import EvaluationContext

from tests.integration.flag_service import INTERVENTIONS, FlagService
from tests.integration.flag_service import bind as bind_flag_service
from tests.integration.helpers import (
    async_poll_until,
    flagd_probe_client,
    get_game_client,
    list_games,
    list_mock_controllers_detailed,
    poll_until,
    wait_until_running,
)
from tests.service.harness import AGENT
from tests.service.harness.fakes.github import TOKEN

pytestmark = pytest.mark.tier("service")

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
SEED = {"flag": SEED_FLAG, "value": SEED_VALUE, "objective": "endurance", "target_n": 2}
SERVICE_COMPOSITION = replace(
    AGENT,
    fake_github=True,
    flag_variant_values={("agent", "experiment_seed", SEED_VARIANT): SEED},
)

# Agent overlays (flags.md section 13.2). experiment.mock_archetypes stays on its
# baseline variant ``all`` (six archetypes, one pair each, UC-046; the overlay takes
# declared variants only): the paired verdict is conclusive from the second pair
# (verdict_min_pairs = 2, the minimum for a paired SD), so the promote verdict
# concludes the experiment after four games.
TUNING = {
    "verdict_min_n": 2,
    "verdict_min_pairs": 2,
    "experiment_tick_seconds": 3,
    "experiment_max_games": 12,
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
# The experimental game runs this long before the test ends it; the control game ends
# at once. With the baseline fitness.endurance.min_session_seconds (120 s, not an
# overlay) that is a per-pair fitness gap of about 0.07, over three times
# verdict_min_raw_effect (0.02). code_improvement.mode is the baseline ``issue``
# (read-only on the flag service).
EXPERIMENTAL_HOLD_SECONDS = 8.0
# The second experiment tick comes within 5 s of start (first-tick cap, #1907); 90 s is margin.
DECLARE_TIMEOUT = 90.0
CONCLUDE_TIMEOUT = 240.0
RUNNING_TIMEOUT = 20.0
# Absence window after teardown: several experiment ticks (3 s each).
NO_RESPAWN_WINDOW = 10.0
SHADOW_CONTEXT = {"game_kind": "shadow", "arm": "experimental"}


def _agent_log(stack) -> str:
    return stack.agent.log_path.read_text(errors="replace")


def _lines(stack, needle: str) -> list[str]:
    return [line for line in _agent_log(stack).splitlines() if needle in line]


def _field(line: str, key: str) -> str:
    match = re.search(rf'\b{re.escape(key)}=("[^"]*"|\S+)', line)
    assert match, f"no {key}= in {line!r}"
    return match.group(1).strip('"')


def _experiment_id(stack) -> str:
    return _field(_lines(stack, "experiment.declared")[0], "experiment_id")


def _journal_events(stack) -> list[dict]:
    files = list((stack.workdir / "experiments").glob("exp_*/events.jsonl"))
    assert len(files) == 1, f"expected one experiment journal, found {files}"
    return [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]


def _served_windows(probe, experiment_id: str):
    """What flagd serves for ``windows`` to a shadow game of the experiment's experimental arm."""
    context = EvaluationContext(attributes={**SHADOW_CONTEXT, "experiment_id": experiment_id})
    return probe.get_object_value(SEED_FLAG, {}, context)


async def _eliminate_all_but_one(game_client, client: FlagService, game_id: str) -> None:
    """End the game with a winner, as the agent's ``eliminate_player`` writer would."""
    games = {g.game_id: g for g in await list_games(game_client)}
    if game_id not in games:
        return  # already ended on its own
    serials = [p.serial for p in games[game_id].players]
    for serial in serials[1:]:
        client.post(INTERVENTIONS, "eliminate_player", game_id=game_id, payload={"serial": serial})


@pytest.fixture(scope="module")
def promotion_stack(service_stack):
    """The module's stack with the agent, experiments, the bounded tuning and issue mode served."""
    client = FlagService.from_compose(service_stack)
    bind_flag_service(client)
    # eliminate_player passes the coordinator's allow-list only under `full`.
    client.put("agent", "interventions_allowed", variant="full")
    client.put("agent", "experiment.mock_archetypes", variant="all")
    try:
        yield service_stack, client
    finally:
        bind_flag_service(None)
        client.cleanup()


@pytest.mark.asyncio
@pytest.mark.uc("UC-043", flow="main", steps=[1, 2, 3, 4, 5], tc="TC-011")
async def test_paired_games_reach_a_promote_verdict(promotion_stack):
    """Steps 1 to 5: two seed-matched pairs, each experimental game longer than its control
    game, conclude with ``promote``. While the experiment runs, flagd serves the arm value
    to its experimental shadow games (the precondition of the teardown check below)."""
    stack, client = promotion_stack
    assert await async_poll_until(lambda: _lines(stack, "experiment.started"), DECLARE_TIMEOUT), _agent_log(stack)[
        -3000:
    ]
    experiment_id = _experiment_id(stack)
    with flagd_probe_client(stack, "game") as probe:
        assert await async_poll_until(lambda: _served_windows(probe, experiment_id) == SEED_VALUE, DECLARE_TIMEOUT), (
            f"flagd does not serve the arm value of {experiment_id}: {_served_windows(probe, experiment_id)}"
        )

    game_client, channel = await get_game_client(stack)
    handled: set[str] = set()
    pending: list[asyncio.Task] = []

    async def end_game(game_id: str, arm: str) -> None:
        running = await wait_until_running(game_client, {game_id}, timeout=RUNNING_TIMEOUT)
        if game_id not in running:
            return
        if arm == "experimental":
            await asyncio.sleep(EXPERIMENTAL_HOLD_SECONDS)
        await _eliminate_all_but_one(game_client, client, game_id)

    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONCLUDE_TIMEOUT
        while not _lines(stack, "experiment.concluded") and loop.time() < deadline:
            for line in _lines(stack, "experiment.game_assigned"):
                game_id = _field(line, "game_id")
                if game_id not in handled:
                    handled.add(game_id)
                    pending.append(asyncio.create_task(end_game(game_id, _field(line, "arm"))))
            await asyncio.sleep(0.5)
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await channel.close()

    concluded = _lines(stack, "experiment.concluded")
    assert len(concluded) == 1, _agent_log(stack)[-5000:]
    games = [e for e in _journal_events(stack) if e.get("kind") == "game_concluded"]
    assert _field(concluded[0], "outcome") == "promote", f"verdict not promote; concluded games: {games}"
    assert len(games) == 4 and sorted(e["arm"] for e in games) == ["control"] * 2 + ["experimental"] * 2, games


@pytest.mark.uc("UC-043", flow="main", steps=[6, 7, 8], tc="TC-011")
def test_promote_verdict_opens_one_issue(promotion_stack):
    """Steps 6 and 7: the promote verdict is routed to the ``issue`` promoter, which opens
    exactly one issue (on the stub) for the flag under test; the promotion is journaled
    as ``applied`` with the issue URL and the experiment ends ``done`` (step 8)."""
    stack, _client = promotion_stack
    assert poll_until(lambda: _lines(stack, "experiment.torn_down"), timeout=30.0, interval=1.0), _agent_log(stack)[
        -4000:
    ]

    issues = stack.fake_github.requests("issues")
    assert len(issues) == 1, stack.fake_github.log_tail()
    assert issues[0]["token_ok"], "the promoter did not send its GITHUB_TOKEN"
    assert issues[0]["body"]["title"] == f"Flag experiment: {SEED_FLAG}", issues[0]["body"]
    assert stack.fake_github.requests("pulls") == [], "issue mode opened a PR"
    url = stack.fake_github.html_url("issues", 1)

    promoted = _lines(stack, "code_improvement.promoted")
    assert len(promoted) == 1, promoted
    assert _field(promoted[0], "gate") == "issue" and _field(promoted[0], "outcome") == "applied", promoted
    assert _field(promoted[0], "url") == url, promoted
    assert not _lines(stack, "experiment.promote_failed")

    promotions = [e for e in _journal_events(stack) if e.get("kind") == "promotion"]
    assert len(promotions) == 1, promotions
    assert promotions[0]["promotion"]["outcome"] == "applied", promotions
    assert promotions[0]["promotion"]["reference"] == url, promotions
    torn_down = _lines(stack, "experiment.torn_down")
    assert len(torn_down) == 1 and _field(torn_down[0], "status") == "done", torn_down
    assert TOKEN not in _agent_log(stack), "the agent logged its GitHub token"


@pytest.mark.uc("UC-043", flow="main", steps=[2], tc="TC-011")
def test_each_pair_shares_one_seed(promotion_stack):
    """Validation 1: both games of a pair run with the pair's seed; the two pairs
    (two archetypes) have different seeds."""
    stack, _client = promotion_stack
    pair_of = {
        _field(line, "game_id"): _field(line, "pair_index") for line in _lines(stack, "experiment.game_assigned")
    }
    seeds: dict[str, dict[str, str]] = {}
    for line in _lines(stack, "experiment: shadow game spawned"):
        pair = pair_of[_field(line, "game_id")]
        seeds.setdefault(pair, {})[_field(line, "arm")] = _field(line, "seed")
    assert sorted(seeds) == ["0", "1"], seeds
    for pair, arms in seeds.items():
        assert set(arms) == {"experimental", "control"}, f"pair {pair}: {arms}"
        assert arms["experimental"] == arms["control"] != "0", f"pair {pair} not paired by seed: {arms}"
    assert seeds["0"]["control"] != seeds["1"]["control"], f"both pairs ran one seed: {seeds}"


@pytest.mark.asyncio
@pytest.mark.uc("UC-043", flow="main", steps=[8], tc="TC-011")
async def test_teardown_removes_targeting_and_spawns_nothing(promotion_stack, flag_service):
    """Validation 3 and the postconditions: after the teardown no game is spawned, the
    shadow games and reserved controllers are gone, and flagd serves the baseline
    ``windows`` again to the experiment's experimental arm (its targeting is removed)."""
    stack, _client = promotion_stack
    count = len(_lines(stack, "experiment: shadow game spawned"))
    assert count == 4, _lines(stack, "experiment: shadow game spawned")
    assert not await async_poll_until(
        lambda: len(_lines(stack, "experiment: shadow game spawned")) != count, NO_RESPAWN_WINDOW
    ), "a game was spawned after the experiment was torn down"

    experiment_id = _experiment_id(stack)
    baseline = flag_service.get("game", SEED_FLAG)
    assert experiment_id not in json.dumps(baseline), f"arm targeting of {experiment_id} still on: {baseline}"
    with flagd_probe_client(stack, "game") as probe:
        real = {}
        assert await async_poll_until(
            lambda: (
                real.update(probe.get_object_value(SEED_FLAG, {}, EvaluationContext(attributes={"game_kind": "real"})))
                or real
            ),
            10.0,
        ), "flagd serves no baseline windows"
        assert real != SEED_VALUE
        assert await async_poll_until(lambda: _served_windows(probe, experiment_id) == real, 10.0), (
            f"flagd still serves {_served_windows(probe, experiment_id)} to the experimental arm, baseline {real}"
        )

    spawned = {_field(line, "game_id") for line in _lines(stack, "experiment: shadow game spawned")}
    game_client, channel = await get_game_client(stack)
    try:
        running = await wait_until_running(game_client, set(), timeout=0)
    finally:
        await channel.close()
    assert not running & spawned, f"shadow games still running: {running & spawned}"
    assert not {g["game_id"] for g in flag_service.games()} & spawned, "shadow games still registered"
    assert [c.serial for c in await list_mock_controllers_detailed(stack) if c.reserved] == []
