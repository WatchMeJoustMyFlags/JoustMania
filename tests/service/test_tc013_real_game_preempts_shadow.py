"""TC-013 step 8 on the service tier (T2): a real game preempts the agent's shadow game (#1404).

AGENT composition: the real agent (built at HEAD), menu, game coordinator and
controller manager (mock backend) from source, a fresh flag plane and the OtlpSink.
The agent runs one env-seeded experiment on the ``calm_long`` archetype (a game that
does not end on its own before the test acts, the first of the baseline ``all``), and ``shadow_policy`` = ``block`` is
served as a stack overlay.

While the experiment's first shadow game S1 runs, players ready up in the menu and
the real game R1 starts. The coordinator force-ends S1 with reason
``preempted_by_real_game`` (UC-043 A2, UC-046 A8); the agent releases S1 without a
fitness sample and reruns its pair (BR-015); while R1 runs, the agent's next shadow
start is refused ("Shadow games blocked while a real game is running", a spawn
failure, UC-043 A13) and no shadow game runs. Once R1 has ended, the pair runs again
in a new game.

The full-stack ``tests/integration/test_tc013_shadow_alongside_real.py`` proves the
coordinator side with a headless shadow game; this module adds the agent's side,
which was unit-tested only (``experiment_loop_test.go``,
``gamerunner/runner_test.go``).
"""

import json
import re
from dataclasses import replace

import pytest

from tests.integration.flag_service import FlagService
from tests.integration.flag_service import bind as bind_flag_service
from tests.integration.helpers import (
    GameEventCollector,
    async_poll_until,
    force_end_game_by_id,
    get_game_client,
    list_games,
    wait_until_running,
)
from tests.service.harness import AGENT
from tests.service.lobby_flow import ready_up_until_started, start_lobby, wait_running

pytestmark = pytest.mark.tier("service")

SEED_FLAG = "windows"
SEED_VALUE = (
    '{"min_music_fast_time":6,"max_music_fast_time":11,'
    '"min_music_slow_time":6,"max_music_slow_time":14,'
    '"end_min_music_fast_time":8,"end_max_music_fast_time":14,'
    '"end_min_music_slow_time":5,"end_max_music_slow_time":8}'
)
SERVICE_COMPOSITION = replace(
    AGENT,
    agent_env={
        "AGENT_EXPERIMENT_SEED_FLAG": SEED_FLAG,
        "AGENT_EXPERIMENT_SEED_VALUE": SEED_VALUE,
        "AGENT_EXPERIMENT_SEED_OBJECTIVE": "balanced",
        "AGENT_EXPERIMENT_SEED_TARGET_N": "2",
    },
)
TUNING = {
    "verdict_min_n": 1,
    "verdict_min_pairs": 1,
    "experiment_tick_seconds": 3,
    "experiment_max_games": 6,
    "experiment.max_game_seconds": 90,
}
# Served before the agent starts (Composition.agent_flags), so its first tick already reads them.
SERVICE_COMPOSITION = replace(
    SERVICE_COMPOSITION,
    agent_flags={
        "enabled": {"variant": "on"},
        "experiments_enabled": {"variant": "on"},
        "decision.backend": {"variant": "rules"},
        **{key: {"value": value} for key, value in TUNING.items()},
    },
)
PREEMPTED = "preempted_by_real_game"
BLOCKED = "Shadow games blocked while a real game is running"
# The second experiment tick comes within 5 s of start (first-tick cap, #1907); 90 s is margin.
ASSIGN_TIMEOUT = 90.0
EVENT_TIMEOUT = 20.0
# Spawn ticks are 3 s apart: a refused start shows within a few ticks.
BLOCKED_TIMEOUT = 20.0


def _agent_log(stack) -> str:
    return stack.agent.log_path.read_text(errors="replace")


def _lines(stack, needle: str) -> list[str]:
    return [line for line in _agent_log(stack).splitlines() if needle in line]


def _field(line: str, key: str) -> str:
    match = re.search(rf'\b{re.escape(key)}=("[^"]*"|\S+)', line)
    assert match, f"no {key}= in {line!r}"
    return match.group(1).strip('"')


def _assigned(stack) -> list[tuple[str, str, str]]:
    """(game_id, arm, pair_index) of every game the experiment bound, oldest first."""
    return [
        (_field(line, "game_id"), _field(line, "arm"), _field(line, "pair_index"))
        for line in _lines(stack, "experiment.game_assigned")
    ]


def _journal(stack) -> tuple[list[dict], dict]:
    dirs = list((stack.workdir / "experiments").glob("exp_*"))
    assert len(dirs) == 1, f"expected one experiment journal, found {dirs}"
    events = [json.loads(line) for line in (dirs[0] / "events.jsonl").read_text().splitlines() if line.strip()]
    return events, json.loads((dirs[0] / "summary.json").read_text())


@pytest.fixture(scope="module")
def preempt_stack(service_stack):
    client = FlagService.from_compose(service_stack)
    bind_flag_service(client)
    # Baseline variant ``all``: its first archetype, ``calm_long``, has no player die on
    # its own, so S1 is still running when the real game starts (overlays take
    # declared variants only).
    client.put("agent", "experiment.mock_archetypes", variant="all")
    client.put("game", "shadow_policy", variant="block")
    try:
        yield service_stack
    finally:
        bind_flag_service(None)
        client.cleanup()


@pytest.mark.asyncio
@pytest.mark.uc("UC-043", flow="A2", tc="TC-013")
@pytest.mark.uc("UC-046", flow="A8", tc="TC-013")
@pytest.mark.uc("UC-043", flow="A13", tc="TC-013")
async def test_real_game_preempts_shadow_game_without_a_sample(preempt_stack):
    """A real start preempts the experiment's live shadow game, which yields no fitness
    sample; no shadow game starts while the real game runs; afterwards the pair reruns."""
    stack = preempt_stack
    assert await async_poll_until(lambda: _assigned(stack), ASSIGN_TIMEOUT), _agent_log(stack)[-3000:]
    s1, arm, pair = _assigned(stack)[0]
    assert arm == "experimental", _assigned(stack)

    game_client, channel = await get_game_client(stack)
    shadow = GameEventCollector(game_client, game_id=s1)
    primary = GameEventCollector(game_client)
    r1 = None
    try:
        await shadow.start()
        await primary.start()
        assert s1 in await wait_until_running(game_client, {s1}, timeout=EVENT_TIMEOUT), f"{s1} never RUNNING"

        # The real game R1 from the menu, on four lobby controllers, while S1 runs.
        lobby = await start_lobby(stack, count=4)
        assert s1 in {g.game_id for g in await list_games(game_client)}, "S1 ended before the real game started"
        r1 = await ready_up_until_started(stack, lobby, primary, known_ids={s1})
        await wait_running(game_client, r1)

        # Coordinator: S1 is force-ended for the real game and declares no winner.
        assert await async_poll_until(lambda: shadow.get_events("game_force_ended"), EVENT_TIMEOUT), (
            f"S1 not force-ended: {[(e.event_type, dict(e.data)) for e in shadow.get_events()]}"
        )
        assert [e.data.get("reason") for e in shadow.get_events("game_force_ended")] == [PREEMPTED]
        assert not shadow.get_events("game_winner"), "a preempted shadow game declared a winner"

        # Agent: S1 is released without a sample and its pair reruns (UC-046 BR-015).
        assert await async_poll_until(
            lambda: any(_field(line, "game_id") == s1 for line in _lines(stack, "experiment.game_released")),
            EVENT_TIMEOUT,
        ), _agent_log(stack)[-4000:]
        s1_released = [line for line in _lines(stack, "experiment.game_released") if _field(line, "game_id") == s1]
        assert PREEMPTED in _field(s1_released[0], "reason"), s1_released
        rerun = _lines(stack, "experiment.pattern_pair_rerun_preempted")
        assert rerun and _field(rerun[0], "pair_index") == pair, rerun

        # shadow_policy=block: while R1 runs the next spawn is refused and no shadow game runs.
        assert await async_poll_until(
            lambda: any(BLOCKED in line for line in _lines(stack, "experiment.spawn_failed")), BLOCKED_TIMEOUT
        ), f"no refused shadow start while R1 runs: {_lines(stack, 'experiment.spawn_failed')}"
        live = {g.game_id for g in await list_games(game_client)}
        assert live == {r1}, f"a shadow game runs next to the real game under block: {live}"

        # Every game the experiment bound so far (S1, and a rerun the agent may have
        # started while R1 was being admitted) was preempted and released without a
        # sample: no game_concluded in the log or the journal, every arm count 0.
        bound = [gid for gid, _, _ in _assigned(stack)]
        released = {
            _field(line, "game_id"): _field(line, "reason") for line in _lines(stack, "experiment.game_released")
        }
        assert all(PREEMPTED in released.get(gid, "") for gid in bound), f"bound {bound}, released {released}"
        assert not _lines(stack, "experiment.game_concluded"), _lines(stack, "experiment.game_concluded")
        events, summary = _journal(stack)
        assert not [e for e in events if e.get("kind") == "game_concluded"], "a preempted game gave a sample"
        assert sorted(e.get("game_id") for e in events if e.get("kind") == "game_released") == sorted(bound)
        assert all(arm_stat.get("count", 0) == 0 for arm_stat in summary["arms"].values()), summary["arms"]

        # R1 ends: the pair runs again, experimental first, in a new game (UC-043 A2 step 2).
        assert await force_end_game_by_id(game_client, r1, reason="tc013_real_game_over")
        r1 = None
        assert await async_poll_until(lambda: len(_assigned(stack)) > len(bound), ASSIGN_TIMEOUT), _agent_log(stack)[
            -3000:
        ]
        rerun_id, rerun_arm, rerun_pair = _assigned(stack)[len(bound)]
        assert rerun_id not in bound and rerun_arm == "experimental" and rerun_pair == pair, _assigned(stack)
        assert await wait_until_running(game_client, {rerun_id}, timeout=EVENT_TIMEOUT) == {rerun_id}
    finally:
        if r1 is not None:
            await force_end_game_by_id(game_client, r1, reason="tc013_cleanup")
        for collector in (shadow, primary):
            await collector.stop()
        await channel.close()
