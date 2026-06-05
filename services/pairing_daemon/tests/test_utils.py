"""Tests for psmove_pairing.utils module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from psmove_pairing.utils import run_command


class TestRunCommand:
    """Tests for run_command()."""

    @pytest.mark.asyncio
    async def test_successful_command(self):
        """Test successful command execution."""
        exit_code, output = await run_command(["echo", "hello"])
        assert exit_code == 0
        assert output == "hello"

    @pytest.mark.asyncio
    async def test_failed_command(self):
        """Test command that returns non-zero exit code."""
        exit_code, output = await run_command(["false"])
        assert exit_code == 1

    @pytest.mark.asyncio
    async def test_command_with_stderr(self):
        """Test that stderr is captured by default."""
        exit_code, output = await run_command(["sh", "-c", "echo error >&2"])
        assert exit_code == 0
        assert "error" in output

    @pytest.mark.asyncio
    async def test_command_without_stderr(self):
        """Test that stderr can be suppressed."""
        exit_code, output = await run_command(["sh", "-c", "echo error >&2"], capture_stderr=False)
        assert exit_code == 0
        assert output == ""

    @pytest.mark.asyncio
    async def test_timeout_handling(self):
        """Test that commands time out properly and the subprocess is killed."""
        # Fake process: communicate() is never awaited (wait_for is patched to raise),
        # kill() is sync, wait() is awaited by the timeout cleanup path.
        fake_proc = MagicMock()
        fake_proc.communicate = MagicMock()
        fake_proc.kill = MagicMock()
        fake_proc.wait = AsyncMock()

        with (
            patch("psmove_pairing.utils.asyncio.create_subprocess_exec", return_value=fake_proc),
            patch("psmove_pairing.utils.asyncio.wait_for", side_effect=TimeoutError),
        ):
            exit_code, output = await run_command(["sleep", "100"])
            assert exit_code == -1
            assert output == "timeout"
            fake_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_exception_handling(self):
        """Test that exceptions are handled gracefully."""
        with patch("psmove_pairing.utils.asyncio.create_subprocess_exec", side_effect=OSError("mock error")):
            exit_code, output = await run_command(["nonexistent-command"])
            assert exit_code == -1
            assert "mock error" in output

    @pytest.mark.asyncio
    async def test_multiline_output(self):
        """Test command with multiline output."""
        exit_code, output = await run_command(["printf", "line1\nline2\nline3"])
        assert exit_code == 0
        assert "line1" in output
        assert "line2" in output
        assert "line3" in output

    @pytest.mark.asyncio
    async def test_with_env_vars(self):
        """Test command with additional environment variables."""
        exit_code, output = await run_command(["sh", "-c", "echo $TEST_VAR"], env={"TEST_VAR": "hello"})
        assert exit_code == 0
        assert output == "hello"
