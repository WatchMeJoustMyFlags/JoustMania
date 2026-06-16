"""End-to-end integration test of the agent experiment COHORT LOOP on the
INFERENCE-ABSENT path (issue #1069).

This codifies the manual M8 dry-run as a standing CI gate. It proves the full
cohort loop runs end-to-end WITHOUT any inference backend reachable — the
demo/CI reality (the LLM is default-off; the agent degrades to the stub/rules
path):

    experiment declared  ->  paired shadow games spawn (CRN pairs, concurrent)
    ->  games conclude with fitness  ->  paired-difference verdict
    ->  experiment terminates (concluded / torn_down)

DEDICATED COORDINATOR (#1163) — WHY THE FULL SOAK IS NOW A HARD GATE
--------------------------------------------------------------------
The full declare -> verdict -> teardown SOAK used to be xfail(strict=False): the
SHARED suite coordinator runs a single session registry (capped at
GAME_MAX_CONCURRENT_GAMES=4) that the suite's per-test ``coordinator_idle``
autouse fixture force-ends before EVERY test, so the cohort loop's in-flight
paired shadow games were repeatedly wiped and contended for slots with the
warmup/residual real game — only ~3 games concluded in a brief free window, then
the loop starved before its games-budget/target-N backstop fired.

This module now layers ``docker-compose.agent-coordinator.yml`` on top of the
dry-run overlay, which adds a DEDICATED game-coordinator
(``agent-game-coordinator``) with its OWN session registry, and points the agent
at it via ``GAME_COORDINATOR_ADDR``. The suite's ``coordinator_idle`` fixture
dials the SHARED ``game-coordinator`` by service name, so it never touches this
one — the cohort loop gets exclusive, uninterrupted coordinator time and the
soak completes deterministically. That is why the verdict+teardown test below is
a HARD assertion (no longer xfail).

WHY THIS TEST IS STRUCTURED THE WAY IT IS
-----------------------------------------
The agent service runs under ``profiles: [agent]`` and is NOT started by the
default integration harness (docker-compose.ci.yml gates it out). The shared
session ``docker_compose`` fixture has no way to activate a profile. So this
module brings the agent container up ON TOP of the already-running session
stack, into the SAME compose project (same network, same flagd), by shelling
out to ``docker compose`` reusing the live fixture's compose-file command and
layering ``docker-compose.dry-run.yml`` + ``--profile agent`` — exactly the
overlay ``make dry-run`` uses (it carries the #999 agent<->flagd flag-dir fix:
the agent writes the SAME ``services/flagd/ci`` dir flagd serves).

FORCING THE INFERENCE-ABSENT PATH HERMETICALLY
----------------------------------------------
``AGENT_INFERENCE_BACKEND=stub`` (the compose default; pinned explicitly here)
makes ``selectInferenceBackend`` return a nil delegate — NO network inference
client is ever constructed (services/agent/main.go: only the literal value
``openai`` activates the real HTTP client). The cohort loop declares from the
env seed and drives synthetic mock-controller shadow games against the live
game-coordinator; it never calls an LLM. There is therefore no dependency on an
external Ollama/OpenAI endpoint — the test is hermetic in CI.

OBSERVABLE
----------
The agent exposes no gRPC/HTTP state API (only ``/healthz``) and the experiment
package emits NO Prometheus metrics; its lifecycle is surfaced as stable,
greppable structured ``slog`` lines on stdout (services/agent/experiment/
registry.go). ``docker logs`` is the most robust observable for a
docker-compose test (no OTEL-collector / Jaeger dependency, no flakiness from
the metrics pipeline). We assert on the exact lifecycle strings, POLLING the
log rather than sleeping a wall clock.

DETERMINISM / BOUNDED RUNTIME
-----------------------------
Per the issue ("small N, short tick, assert KEY observable transitions, not a
long soak") the loop is driven DOWN via the LIVE agent.json tuning flags
(verdict min-N/min-pairs=1, fast tick, a small games-budget backstop) plus a
small seed target-N. The HARD gates assert the reliably-observable core:
inference-absent stub backend, graceful degradation to ``mode=rules``,
experiment.declared, and paired (CRN control + experimental) shadow games that
each conclude WITH a fitness scalar.

The full verdict + teardown SOAK is the FINAL test in this module and is now a
HARD gate (#1163): with the dedicated coordinator above, the cohort loop owns
its registry, so spawning is no longer throttled by the shared-suite contention
that previously starved it (ErrSpawnBackpressure) and the loop reaches a verdict
and tears down deterministically within the timeout. With synthetic mock games
producing tiny, noisy deltas the verdict is expected to be inconclusive (#1042
practical-significance floor) — so we assert the loop REACHES a verdict and TEARS
DOWN, not any particular outcome.
"""

import json
import os
import subprocess

import pytest

from tests.integration.helpers import (
    active_flag_file,
    flagd_probe_client,
    poll_until,
)


def _load(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def _dump_in_place(path: str, doc: dict) -> None:
    """In-place truncate+write (matches the Go ActionSink / admin-mode writer).

    No temp+rename: rename over a bind mount flagd watches triggers EBUSY. Same
    invariant as the suite's other flag writers (test_intervention_flow.py)."""
    text = json.dumps(doc, indent=2) + "\n"
    with open(path, "w") as fh:
        fh.write(text)

# Fixed container name from docker-compose.yml (container_name: joustmania-agent).
AGENT_CONTAINER = "joustmania-agent"
AGENT_SERVICE = "agent"

AGENT_FLAG_PATH = active_flag_file("agent")

# The dry-run overlay carries the #999 agent flag-dir fix + the agent profile.
DRY_RUN_COMPOSE_FILE = "docker-compose.dry-run.yml"

# The #1163 dedicated-coordinator overlay: adds ``agent-game-coordinator`` (its own
# session registry) and points the agent's GAME_COORDINATOR_ADDR at it, so the
# cohort loop gets exclusive coordinator time (the suite's coordinator_idle
# autouse fixture only force-ends the SHARED ``game-coordinator``). This is what
# promotes the full verdict+teardown soak from xfail to a HARD gate.
AGENT_COORDINATOR_COMPOSE_FILE = "docker-compose.agent-coordinator.yml"

# Dedicated coordinator service name (docker-compose.agent-coordinator.yml). Started
# alongside the agent under --profile agent so the cohort loop spawns against it.
AGENT_COORDINATOR_SERVICE = "agent-game-coordinator"

# Exact lifecycle log strings emitted by services/agent (load-bearing assertions).
LOG_BACKEND_SELECTED = "Inference backend selected"  # main.go:573 (#1048)
LOG_BACKEND_STUB = "backend=stub"  # graceful degradation: no inference wired
LOG_MODE_RULES = "mode=rules"  # agent.evaluate degrades to rules with no backend
LOG_DECLARED = "experiment.declared"  # registry.go:511
LOG_GAME_CONCLUDED = "experiment.game_concluded"  # registry.go:1027 (carries fitness=)
LOG_CONCLUDED = "experiment.concluded"  # registry.go (always logged; carries outcome=)
LOG_TORN_DOWN = "experiment.torn_down"  # registry.go teardown (discard/inconclusive/abort)
# The PROMOTE verdict routes conclude -> promote (status DONE) -> teardown, but the
# teardown idempotency guard early-returns on the already-terminal DONE status WITHOUT
# logging experiment.torn_down (registry.go: end-span-then-guard). So on a promote
# outcome the terminal teardown is surfaced by the promotion log line instead. The
# cohort soak's hard terminal assertion accepts EITHER terminal marker so it is
# deterministic regardless of which verdict the synthetic games happen to produce.
LOG_PROMOTED = "code_improvement.promoted"  # promote routed (target/local sink)
LOG_PROMOTE_FAILED = "experiment.promote_failed"  # promote attempted, sink unavailable

# Polling budgets (seconds). Generous worst-case bounds under CI load, NOT paid
# waits — each poll returns as soon as the line appears.
DECLARE_TIMEOUT = 60.0
CONCLUDE_TIMEOUT = 180.0


# LIVE numeric tuning flags (#1044). These take PRECEDENCE over the AGENT_*
# env defaults when present in agent.json — and they ARE present in
# services/flagd/ci/agent.json with production-scale defaults (verdict_min_n=8,
# verdict_min_pairs=5, experiment_tick_seconds=30, experiment_max_games=50).
# Left at those defaults the loop would run 16-50 shadow games over many minutes
# before reaching a terminal verdict (the effective target N per arm is
# max(seed target_n, verdict_min_n) — see registry.shouldGiveUp), blowing past
# CONCLUDE_TIMEOUT. So the test must DRIVE THESE LIVE FLAGS DOWN (not just the
# env), exactly as it drives the boolean gates. We write a bespoke ``it`` variant
# rather than reuse an existing one so the values are explicit and small; flagd
# tolerates extra variant keys (precedent: rewrite_edge_value / the suite's other
# numeric flag writers). intFlag/durationFlag read these via the TYPED getters
# (IntValue/FloatValue) off the in-process resolver, so file values are honored.
_TUNING = {
    # effective target N per arm = max(seed target_n=2, verdict_min_n) => keep small
    "verdict_min_n": 1,
    "verdict_min_pairs": 1,
    # fast refill so the few shadow games spin up promptly
    "experiment_tick_seconds": 3,
    # absolute games-budget backstop (#1001): guarantees termination even if the
    # paired arms never both reach target N — a hard bound well under CONCLUDE_TIMEOUT.
    "experiment_max_games": 6,
}


def _set_agent_flag_on(flag_key: str) -> None:
    """Flip an agent.json boolean flag's defaultVariant to ``on`` in place.

    Idempotent: safe to call as a poll ``rewrite`` to self-heal a missed flagd
    inotify reload (same pattern as the suite's other flag writers)."""
    doc = _load(AGENT_FLAG_PATH)
    doc["flags"][flag_key]["variants"]["on"] = True
    doc["flags"][flag_key]["defaultVariant"] = "on"
    _dump_in_place(AGENT_FLAG_PATH, doc)


def _set_agent_numeric(flag_key: str, value) -> None:
    """Point an agent.json numeric flag's defaultVariant at a bespoke ``it``
    variant carrying ``value``. Idempotent (safe as a poll ``rewrite``)."""
    doc = _load(AGENT_FLAG_PATH)
    doc["flags"][flag_key]["variants"]["it"] = value
    doc["flags"][flag_key]["defaultVariant"] = "it"
    _dump_in_place(AGENT_FLAG_PATH, doc)


def _enable_experiments_live() -> None:
    """Make the LIVE flagd flags match what the bounded cohort loop needs.

    The cohort loop self-gates on the LIVE flagd flags (post-#1044): the master
    kill-switch ``enabled`` AND ``experiments_enabled`` must both be ``on`` for
    the loop to act, regardless of the AGENT_* env defaults. The numeric verdict/
    tick/budget knobs are ALSO live flags that override the env, so we floor them
    here too so the loop terminates fast and deterministically. Idempotent: this
    is reused verbatim as the poll ``rewrite`` so a missed flagd reload self-heals."""
    _set_agent_flag_on("enabled")
    _set_agent_flag_on("experiments_enabled")
    for key, value in _TUNING.items():
        _set_agent_numeric(key, value)


def _compose_prefix(docker_compose) -> list[str]:
    """The live session fixture's ``docker compose -f ... -f ...`` command,
    extended with the dry-run overlay. Reusing the fixture's command keeps us in
    the SAME compose project (testcontainers sets no -p, so the project name is
    derived from the working dir — identical because we run from the same cwd),
    so the agent joins the same network and the same flagd."""
    prefix = list(docker_compose.docker_compose_command())
    prefix += ["-f", DRY_RUN_COMPOSE_FILE]
    prefix += ["-f", AGENT_COORDINATOR_COMPOSE_FILE]
    return prefix


def _agent_env() -> dict:
    """Environment for the agent container bring-up.

    INFERENCE-ABSENT: backend=stub (no network client constructed).
    BOUNDED LOOP: a small seed target_n + a fast tick so declare -> conclude ->
    torn_down completes within CONCLUDE_TIMEOUT.

    IMPORTANT — only env vars EXPLICITLY listed in the agent service's compose
    ``environment:`` block reach the container; compose silently drops the rest.
    So every var set here must be one the agent service passes through
    (docker-compose.yml / docker-compose.dry-run.yml). The verdict gates and the
    games budget are NOT in that passthrough — they are driven instead via the
    LIVE agent.json flags in ``_TUNING`` (which take precedence anyway, #1044).
    """
    env = dict(os.environ)
    env.update(
        {
            # --- inference-absent path (hermetic; no external endpoint) ---
            "AGENT_INFERENCE_BACKEND": "stub",
            # --- master opt-in (env defaults; the LIVE flags are the real gate) ---
            "AGENT_EXPERIMENTS_ENABLED": "true",
            # --- seed exactly ONE experiment on a real FFA lever (the dry-run seed) ---
            "AGENT_EXPERIMENT_SEED_FLAG": "windows",
            "AGENT_EXPERIMENT_SEED_VALUE": (
                '{"min_music_fast_time":6,"max_music_fast_time":11,'
                '"min_music_slow_time":6,"max_music_slow_time":14,'
                '"end_min_music_fast_time":8,"end_max_music_fast_time":14,'
                '"end_min_music_slow_time":5,"end_max_music_slow_time":8}'
            ),
            "AGENT_EXPERIMENT_SEED_OBJECTIVE": "balanced",
            # --- drive the loop SMALL + FAST so it terminates quickly ---
            # Both of these ARE in the agent compose passthrough. The bounded
            # verdict-N/min-pairs/tick/budget knobs are NOT (compose would drop
            # them) — they are set via the authoritative LIVE flags in _TUNING.
            "AGENT_EXPERIMENT_SEED_TARGET_N": "2",
            "AGENT_EXPERIMENT_TICK_SECONDS": "3",
            "AGENT_EXPERIMENT_DYNAMIC_ENABLED": "false",
            # keep logs greppable
            "LOG_LEVEL": "info",
        }
    )
    return env


def _agent_logs() -> str:
    """Current stdout/stderr of the agent container (cheap; safe to poll)."""
    result = subprocess.run(
        ["docker", "logs", AGENT_CONTAINER],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return (result.stdout or "") + (result.stderr or "")


@pytest.fixture(scope="module")
def agent_experiment_stack(docker_compose):
    """Bring up the agent container with the experiment cohort loop ENABLED on
    the inference-absent path, layered onto the running session stack.

    Snapshots and restores ``agent.json`` byte-for-byte, and stops/removes the
    agent container on teardown so it never leaks into other test modules
    (which assume the agent is absent)."""
    cwd = str(docker_compose.context)

    with open(AGENT_FLAG_PATH) as fh:
        agent_backup = fh.read()

    prefix = _compose_prefix(docker_compose)
    env = _agent_env()

    # 1. Flip the LIVE gating flags ON and confirm flagd SERVES them before the
    #    agent boots, so the very first tick is already enabled.
    _enable_experiments_live()
    from openfeature.evaluation_context import EvaluationContext

    with flagd_probe_client(docker_compose, "agent") as client:
        served = poll_until(
            lambda: client.get_boolean_value(
                "experiments_enabled", False, EvaluationContext()
            )
            and client.get_boolean_value("enabled", False, EvaluationContext())
            # Confirm the floored numeric knobs are SERVED too (typed getter, the
            # #903 numeric-flag path) so the very first tick uses the bounded
            # target/tick, not the production-scale agent.json defaults.
            and client.get_integer_value("verdict_min_n", 99, EvaluationContext())
            == _TUNING["verdict_min_n"],
            rewrite=_enable_experiments_live,
        )
    assert served, (
        "flagd did not serve the enabled+experiments_enabled+bounded-tuning flags"
    )

    # 2. Start the agent (--profile agent + dry-run overlay) in the same project.
    #    Mirror the session fixture's image policy: prebuilt/dev-mounts pull the
    #    image; a plain build run must build the agent image (it is profile-gated,
    #    so the session bring-up never built it).
    use_prebuilt = os.getenv("USE_PREBUILT_IMAGES", "false").lower() == "true"
    use_dev_mounts = os.getenv("USE_DEV_MOUNTS", "false").lower() == "true"
    # Bring up BOTH the dedicated coordinator (#1163) and the agent. The dedicated
    # coordinator reuses the SAME game-coordinator-service image the session stack
    # already built/pulled, so no extra build cost — only the profile-gated agent
    # image may need a build in build mode. --wait blocks until both are healthy so
    # the agent's first shadow spawn lands on a ready coordinator.
    up_cmd = [*prefix, "--profile", AGENT_SERVICE, "up", "-d", "--wait"]
    if not use_prebuilt and not use_dev_mounts:
        up_cmd.append("--build")
    up_cmd += [AGENT_COORDINATOR_SERVICE, AGENT_SERVICE]
    up = subprocess.run(
        up_cmd,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert up.returncode == 0, (
        "failed to start agent + dedicated coordinator containers:\n"
        f"STDOUT:{up.stdout}\nSTDERR:{up.stderr}"
    )

    try:
        yield docker_compose
    finally:
        # 3. Stop + remove the agent AND the dedicated coordinator so other modules
        #    see the agent ABSENT again and the dedicated coordinator does not leak.
        subprocess.run(
            [
                *prefix,
                "--profile",
                AGENT_SERVICE,
                "rm",
                "-sf",
                AGENT_SERVICE,
                AGENT_COORDINATOR_SERVICE,
            ],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        # 4. Restore agent.json byte-for-byte and wait until flagd serves the
        #    baseline (kill-switch back to default off) so no later test inherits
        #    an enabled agent gate.
        def _restore() -> None:
            with open(AGENT_FLAG_PATH, "w") as fh:
                fh.write(agent_backup)

        _restore()
        with flagd_probe_client(docker_compose, "agent") as client:
            poll_until(
                lambda: client.get_boolean_value(
                    "experiments_enabled", True, EvaluationContext()
                )
                is False,
                rewrite=_restore,
            )


def _poll_log_contains(needle: str, timeout: float) -> bool:
    """Poll the agent log until ``needle`` appears or ``timeout`` elapses."""
    return poll_until(
        lambda: needle in _agent_logs(),
        timeout=timeout,
        interval=1.0,
    )


def test_inference_absent_backend_is_stub(agent_experiment_stack):
    """The agent comes up on the inference-absent path: the startup line reports
    ``backend=stub`` — proving NO inference client is wired (graceful
    degradation to rules, no network dependency)."""
    assert _poll_log_contains(LOG_BACKEND_SELECTED, DECLARE_TIMEOUT), (
        "agent never logged the inference-backend selection line"
    )
    logs = _agent_logs()
    assert LOG_BACKEND_STUB in logs, (
        "expected backend=stub (inference-absent path); agent log was:\n"
        + logs[-2000:]
    )


def test_experiment_is_declared(agent_experiment_stack):
    """The env-seeded experiment is DECLARED once the loop is live — the entry
    transition of the cohort loop, reachable without any inference backend."""
    assert _poll_log_contains(LOG_DECLARED, DECLARE_TIMEOUT), (
        "agent never logged experiment.declared; log tail:\n"
        + _agent_logs()[-3000:]
    )


def test_inference_absent_degrades_to_rules(agent_experiment_stack):
    """The no-inference graceful-degradation guarantee, asserted explicitly
    (issue #1069 note): with no backend wired, the agent runs its decision loop
    in ``mode=rules`` — it does NOT crash, hang, or skip decisions waiting on an
    LLM. The shadow games the cohort loop spawns drive agent.evaluate cycles, so
    a ``mode=rules`` line appears once the first game is live."""
    assert _poll_log_contains(LOG_MODE_RULES, CONCLUDE_TIMEOUT), (
        "agent never reached mode=rules (no-inference degradation); log tail:\n"
        + _agent_logs()[-3000:]
    )
    assert "panic:" not in _agent_logs(), "agent panicked on the rules path"


def test_cohort_loop_spawns_paired_games_that_conclude_with_fitness(
    agent_experiment_stack,
):
    """The cohort loop runs end-to-end on the inference-absent path THROUGH game
    conclusion: declared -> paired shadow games spawn (CRN control + experimental
    arms) -> each concludes WITH a fitness scalar -> the per-game fitness feeds
    the verdict seam. This is the reliably-observable core of the loop and the
    standing CI gate for the inference-absent guarantee — the full verdict +
    teardown SOAK is the next (HARD) test, see its docstring."""
    assert _poll_log_contains(LOG_DECLARED, DECLARE_TIMEOUT), (
        "experiment was never declared"
    )

    # Poll until BOTH CRN arms (#1003) have a concluded game carrying fitness —
    # proving the loop spawns PAIRED games (a control + an experimental) off the
    # seeded experiment, not a single arm. Polling (not a one-shot snapshot)
    # absorbs the lag between the first arm concluding and its sibling: the two
    # games of a pair finish a few seconds apart.
    def _both_arms_concluded() -> bool:
        log = _agent_logs()
        return (
            LOG_GAME_CONCLUDED in log
            and "fitness=" in log
            and "arm=control" in log
            and "arm=experimental" in log
        )

    assert poll_until(_both_arms_concluded, timeout=CONCLUDE_TIMEOUT, interval=1.0), (
        "did not observe both CRN arms (control + experimental) conclude with "
        "fitness; log tail:\n" + _agent_logs()[-4000:]
    )
    assert "panic:" not in _agent_logs(), "agent panicked during the cohort loop"


# The full verdict + teardown SOAK is now a HARD gate (#1163). It used to be
# xfail(strict=False) because the cohort loop competed for the SHARED suite
# coordinator (single session registry, GAME_MAX_CONCURRENT_GAMES=4) with the
# warmup/residual real game AND had its in-flight paired shadow games force-ended
# by the per-test ``coordinator_idle`` autouse fixture (conftest.py) — each spawn
# while the coordinator was busy/quiesced returned gamerunner.ErrGameInProgress
# -> ErrSpawnBackpressure (shadow_spawner.go) and backed off, so the loop won only
# a brief free window (~3 games concluded) then starved before the games-budget /
# target-N backstop fired (diagnosed from CI run 27610202170).
#
# THE FIX: docker-compose.agent-coordinator.yml gives the agent its OWN
# game-coordinator (``agent-game-coordinator``, its own session registry) and the
# agent is pointed at it via GAME_COORDINATOR_ADDR. The suite's coordinator_idle
# fixture dials the SHARED ``game-coordinator`` by service name, so it never
# touches this one — the cohort loop gets exclusive, uninterrupted coordinator
# time and the soak terminates deterministically within CONCLUDE_TIMEOUT. So this
# is now a HARD assertion (no xfail): a regression that breaks declare -> verdict
# -> teardown fails CI.
def test_cohort_loop_runs_to_verdict_and_terminates(agent_experiment_stack):
    """The FULL cohort loop reaches a terminal verdict and TEARS DOWN:
    declared -> paired games conclude -> verdict -> experiment.concluded ->
    targeting torn down + capacity freed. HARD gate (#1163): the agent runs against
    a DEDICATED coordinator (docker-compose.agent-coordinator.yml) so it owns its
    registry and is not starved by the shared-suite contention — see the comment
    above."""
    assert _poll_log_contains(LOG_DECLARED, DECLARE_TIMEOUT), (
        "experiment was never declared"
    )
    assert _poll_log_contains(LOG_GAME_CONCLUDED, CONCLUDE_TIMEOUT), (
        "no shadow game ever concluded"
    )
    # The experiment reaches a verdict (always logged, every outcome).
    assert _poll_log_contains(LOG_CONCLUDED, CONCLUDE_TIMEOUT), (
        "experiment never reached a verdict (no experiment.concluded); log tail:\n"
        + _agent_logs()[-4000:]
    )

    # ...and TERMINATES (targeting stripped, capacity freed). The terminal marker
    # depends on the verdict outcome (see the LOG_PROMOTED note): a discard/
    # inconclusive/abort logs experiment.torn_down; a promote routes through the
    # promoter (status DONE) and surfaces the promotion log line instead. Accept
    # EITHER so the gate is deterministic regardless of which the synthetic mock
    # games produce — both prove the loop reached a TERMINAL state and freed the
    # experiment, which is the regression this gate guards.
    def _terminated() -> bool:
        log = _agent_logs()
        return (
            LOG_TORN_DOWN in log
            or LOG_PROMOTED in log
            or LOG_PROMOTE_FAILED in log
        )

    assert poll_until(_terminated, timeout=CONCLUDE_TIMEOUT, interval=1.0), (
        "experiment never reached a terminal teardown (no experiment.torn_down / "
        "promotion terminal); log tail:\n" + _agent_logs()[-4000:]
    )
    assert "panic:" not in _agent_logs(), "agent panicked during the cohort loop"
