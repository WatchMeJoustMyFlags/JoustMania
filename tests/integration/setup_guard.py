"""Fail-fast setup for the expensive e2e fixtures (#1649).

CI runs the integration suite with ``--reruns 1``. pytest-rerunfailures clears
the cached result of a FAILED session- or module-scoped fixture before each
rerun, so a broken stack bring-up ran again for every test of the shard, twice
per test, each time a full ``docker compose up --build --wait`` with no time
bound. One broken setup ate the whole 30 min e2e-agent budget (merge_group run
39317658385), and the error text never reached the log: testcontainers logs the
compose stderr through ``logging`` and the run was cancelled before pytest
printed its summary.

``SetupGuard`` runs a fixture's setup at most once per session: after a failure
every later attempt fails at once with the recorded cause. The failure is also
written to stderr with output capture suspended, so it shows in the live CI log even if the
job is cancelled later. ``run_bounded`` runs a setup command with a time bound
and turns a non-zero exit or a timeout into a readable failure.

Pure Python + pytest only (no docker, no project imports): the contract tier
tests it without a stack (tests/contract/test_e2e_setup_fail_fast.py).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, nullcontext

import pytest

# Characters of command output kept in a failure message: enough for the
# failing build step or the unhealthy container, small enough to read.
OUTPUT_TAIL_CHARS = 4000


def _tail(text: str | bytes | None, limit: int = OUTPUT_TAIL_CHARS) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    return text[-limit:]


def emit(message: str, config: pytest.Config | None = None) -> None:
    """Write to stderr with pytest's output capture suspended, so the text
    reaches the live log now instead of a report printed at the end."""
    capman = config.pluginmanager.getplugin("capturemanager") if config else None
    suspended = capman.global_and_fixture_disabled() if capman else nullcontext()
    with suspended:
        sys.stderr.write(message if message.endswith("\n") else message + "\n")
        sys.stderr.flush()


class SetupGuard:
    """Run one expensive fixture setup at most once per session."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.failure: str | None = None

    @contextmanager
    def attempt(
        self,
        config: pytest.Config | None = None,
        diagnose: Callable[[], str] | None = None,
    ) -> Iterator[None]:
        """Wrap the setup part of a fixture (not its ``yield``).

        ``config`` (``request.config``) lets the failure bypass output capture.
        ``diagnose`` is called once on failure; its text (container states,
        log tails) is added to the recorded cause. It must not raise.
        """
        if self.failure is not None:
            pytest.fail(
                f"{self.name}: setup already failed earlier in this session, "
                f"not retried (#1649). First failure:\n{self.failure}",
                pytrace=False,
            )
        try:
            yield
        except (Exception, pytest.fail.Exception) as exc:
            cause = f"{type(exc).__name__}: {exc}"
            if diagnose is not None:
                cause += "\n" + diagnose()
            self.failure = _tail(cause, 2 * OUTPUT_TAIL_CHARS)
            emit(
                f"\n===== E2E SETUP FAILED: {self.name} (#1649) =====\n"
                f"{self.failure}\n"
                f"===== end {self.name} =====\n",
                config,
            )
            raise


def run_bounded(
    cmd: Sequence[str],
    *,
    what: str,
    timeout: float,
    cwd: str | None = None,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run a setup command; fail with its output tail on error or timeout."""
    try:
        result = subprocess.run(
            list(cmd),
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"{what}: timed out after {timeout:.0f}s\n"
            f"CMD: {' '.join(cmd)}\n"
            f"STDOUT (tail):\n{_tail(exc.stdout)}\n"
            f"STDERR (tail):\n{_tail(exc.stderr)}",
            pytrace=False,
        )
    if result.returncode != 0:
        pytest.fail(
            f"{what}: exit code {result.returncode}\n"
            f"CMD: {' '.join(cmd)}\n"
            f"STDOUT (tail):\n{_tail(result.stdout)}\n"
            f"STDERR (tail):\n{_tail(result.stderr)}",
            pytrace=False,
        )
    return result


def compose_diagnostics(
    compose_cmd: Sequence[str], *, cwd: str | None = None, env: dict | None = None
) -> str:
    """Container states and recent logs of a compose project; never raises."""
    parts = []
    for args, label in (
        (["ps", "-a"], "docker compose ps -a"),
        (["logs", "--no-color", "--tail=40"], "docker compose logs --tail=40"),
    ):
        try:
            out = subprocess.run(
                [*compose_cmd, *args],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            parts.append(f"--- {label}:\n{_tail(out.stdout + out.stderr)}")
        except Exception as exc:  # noqa: BLE001 - diagnostics are best-effort
            parts.append(f"--- {label}: unavailable ({exc!r})")
    return "\n".join(parts)
