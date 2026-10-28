"""The e2e setup guard fails fast under ``--reruns 1`` (#1649).

pytest-rerunfailures clears the cached error of a failed module/session fixture
before each rerun, so without the guard a broken stack bring-up re-ran for every
test (twice per test) until the 30 min e2e job timeout. These tests run a tiny
suite in a subprocess, with a module fixture whose setup always fails, and count
how often the setup body ran. No docker.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "integration"

FAILING_SUITE = textwrap.dedent(
    """
    import time
    from pathlib import Path

    import pytest

    from setup_guard import SetupGuard, run_bounded

    COUNTER = Path(__file__).with_name("setup_runs.txt")
    GUARD = SetupGuard("fake stack")


    @pytest.fixture(scope="module")
    def stack(request):
        with GUARD.attempt(request.config, diagnose=lambda: {diag!r}):
            COUNTER.write_text(str(int(COUNTER.read_text() or 0) + 1))
            run_bounded(
                [{python!r}, "-c", "import sys; sys.stderr.write('pull denied'); sys.exit(3)"],
                what="fake compose up",
                timeout=30,
            )
        yield


    def test_one(stack):
        pass


    def test_two(stack):
        pass


    def test_three(stack):
        pass
    """
)


def _run_suite(
    tmp_path: Path, *extra: str, diag: str = "container fake: unhealthy"
) -> tuple[subprocess.CompletedProcess, int]:
    (tmp_path / "setup_runs.txt").write_text("")
    (tmp_path / "test_fake_stack.py").write_text(
        FAILING_SUITE.format(python=sys.executable, diag=diag)
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(tmp_path),
            *extra,
            str(tmp_path / "test_fake_stack.py"),
        ],
        cwd=tmp_path,
        env={"PYTHONPATH": str(INTEGRATION_DIR), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return result, int((tmp_path / "setup_runs.txt").read_text() or 0)


def test_failed_setup_runs_once_under_reruns(tmp_path):
    """A failing module setup runs ONCE, not once per test and rerun."""
    result, runs = _run_suite(tmp_path, "--reruns", "1")
    output = result.stdout + result.stderr
    assert runs == 1, f"setup ran {runs} times, expected 1:\n{output[-3000:]}"
    assert "3 error" in output, output[-3000:]
    assert "setup already failed earlier in this session" in output, output[-3000:]


def test_failed_setup_cause_reaches_real_stderr(tmp_path):
    """The cause (command output + diagnostics) is written to stderr past
    pytest's output capture at once, so a CI job cancelled later still shows it."""
    result, _ = _run_suite(tmp_path)
    assert "E2E SETUP FAILED: fake stack" in result.stderr, result.stderr[-3000:]
    assert "fake compose up: exit code 3" in result.stderr
    assert "pull denied" in result.stderr
    assert "container fake: unhealthy" in result.stderr


def test_setup_command_timeout_is_bounded(tmp_path):
    """``run_bounded`` turns a hanging command into a failure after its bound."""
    script = tmp_path / "test_hang.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import time

            import pytest

            from setup_guard import run_bounded


            def test_hang():
                started = time.monotonic()
                with pytest.raises(pytest.fail.Exception, match="timed out after 1s"):
                    run_bounded(
                        [{sys.executable!r}, "-c", "import time; time.sleep(60)"],
                        what="hung compose up",
                        timeout=1,
                    )
                assert time.monotonic() - started < 15
            """
        )
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(script)],
        cwd=tmp_path,
        env={"PYTHONPATH": str(INTEGRATION_DIR), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]


def test_long_diagnostics_never_cut_the_real_error(tmp_path):
    """Container dumps on a real stack are long; the command's own error (exit
    code, stderr) must survive them in the recorded and printed cause."""
    result, _ = _run_suite(tmp_path, diag="x" * 50_000 + "\nLAST DIAGNOSTIC LINE")
    assert "fake compose up: exit code 3" in result.stderr, result.stderr[:3000]
    assert "pull denied" in result.stderr
    assert "LAST DIAGNOSTIC LINE" in result.stderr
