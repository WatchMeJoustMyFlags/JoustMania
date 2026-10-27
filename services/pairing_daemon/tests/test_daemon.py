"""Tests for psmove_pairing.daemon module."""

import time
from unittest.mock import AsyncMock, create_autospec, patch

import pytest

from psmove_pairing.daemon import _HEALTH_STALENESS_THRESHOLD, PairingDaemon

from .conftest import MockCommandRunner


@pytest.fixture
def daemon(mock_tracer):
    """Provide PairingDaemon instance for tests."""
    return PairingDaemon(mock_tracer)


class TestValidatePrerequisites:
    """Tests for validate_prerequisites()."""

    @pytest.mark.asyncio
    async def test_all_prerequisites_pass(self, daemon):
        """Test when all prerequisites are available."""
        runner = MockCommandRunner()
        runner.add_response(["bluetoothctl", "show"], (0, "Controller ..."))

        with patch("psmove_pairing.daemon.run_command", runner):
            with patch("psmove_pairing.daemon.shutil.which", return_value="/usr/bin/bluetoothctl"):
                result = await daemon.validate_prerequisites()
                assert result is True

    @pytest.mark.asyncio
    async def test_bluetoothctl_not_found(self, daemon):
        """Test when bluetoothctl is not found."""
        runner = MockCommandRunner()

        def mock_which(cmd):
            if cmd == "bluetoothctl":
                return None
            return f"/usr/bin/{cmd}"

        with patch("psmove_pairing.daemon.run_command", runner):
            with patch("psmove_pairing.daemon.shutil.which", side_effect=mock_which):
                result = await daemon.validate_prerequisites()
                assert result is False

    @pytest.mark.asyncio
    async def test_hciconfig_not_found(self, daemon):
        """Test when hciconfig is not found."""
        runner = MockCommandRunner()
        runner.add_response(["bluetoothctl", "show"], (0, "Controller ..."))

        def mock_which(cmd):
            if cmd == "hciconfig":
                return None
            return f"/usr/bin/{cmd}"

        with patch("psmove_pairing.daemon.run_command", runner):
            with patch("psmove_pairing.daemon.shutil.which", side_effect=mock_which):
                result = await daemon.validate_prerequisites()
                assert result is False


class TestIsHealthy:
    """Tests for is_healthy()."""

    def test_healthy_during_startup(self, daemon):
        """Test that daemon is healthy during startup grace period."""
        assert daemon.is_healthy() is True

    def test_healthy_after_polls(self, daemon):
        """Test daemon is healthy after both loops have run."""
        daemon.update_usb_poll_timestamp()
        daemon.update_bt_monitor_timestamp()
        daemon._startup_time = time.time() - _HEALTH_STALENESS_THRESHOLD - 10
        assert daemon.is_healthy() is True

    def test_unhealthy_when_usb_stale(self, daemon):
        """Test daemon is unhealthy when USB poll is stale."""
        daemon._startup_time = time.time() - _HEALTH_STALENESS_THRESHOLD - 10
        daemon.update_bt_monitor_timestamp()
        daemon._last_usb_poll = time.time() - _HEALTH_STALENESS_THRESHOLD - 10
        assert daemon.is_healthy() is False

    def test_unhealthy_when_bt_stale(self, daemon):
        """Test daemon is unhealthy when Bluetooth monitor is stale."""
        daemon._startup_time = time.time() - _HEALTH_STALENESS_THRESHOLD - 10
        daemon.update_usb_poll_timestamp()
        daemon._last_bt_monitor = time.time() - _HEALTH_STALENESS_THRESHOLD - 10
        assert daemon.is_healthy() is False


class TestGetHealthStatus:
    """Tests for get_health_status()."""

    def test_status_includes_all_fields(self, daemon):
        """Test health status includes all expected fields."""
        status = daemon.get_health_status()
        assert "healthy" in status
        assert "uptime_seconds" in status
        assert "last_usb_poll_seconds_ago" in status
        assert "last_bt_monitor_seconds_ago" in status
        assert "usb_poll_count" in status
        assert "bt_monitor_count" in status

    def test_status_reflects_health(self, daemon):
        """Test health status reflects actual health."""
        daemon.update_usb_poll_timestamp()
        daemon.update_bt_monitor_timestamp()
        status = daemon.get_health_status()
        assert status["healthy"] is True

    def test_status_tracks_poll_counts(self, daemon):
        """Test health status tracks poll counts."""
        initial_status = daemon.get_health_status()
        assert initial_status["usb_poll_count"] == 0
        assert initial_status["bt_monitor_count"] == 0


class TestTimestampUpdates:
    """Tests for timestamp update methods."""

    def test_update_usb_poll_timestamp(self, daemon):
        """Test USB poll timestamp is updated."""
        assert daemon._last_usb_poll == 0.0
        daemon.update_usb_poll_timestamp()
        assert daemon._last_usb_poll > 0

    def test_update_bt_monitor_timestamp(self, daemon):
        """Test Bluetooth monitor timestamp is updated."""
        assert daemon._last_bt_monitor == 0.0
        daemon.update_bt_monitor_timestamp()
        assert daemon._last_bt_monitor > 0


class TestRun:
    """Tests for run()."""

    @pytest.mark.asyncio
    async def test_run_starts_both_loops(self, daemon):
        """Test that run starts both USB and BT loops."""
        import asyncio

        usb_loop_started = False
        bt_loop_started = False

        async def mock_usb_loop():
            nonlocal usb_loop_started
            usb_loop_started = True
            raise asyncio.CancelledError()

        async def mock_bt_loop():
            nonlocal bt_loop_started
            bt_loop_started = True
            raise asyncio.CancelledError()

        daemon._usb_poll_loop = mock_usb_loop
        daemon._bt_monitor_loop = mock_bt_loop

        runner = MockCommandRunner()
        runner.add_response(["bluetoothctl", "show"], (0, "ok"))

        with patch("psmove_pairing.daemon.run_command", runner):
            with patch("psmove_pairing.daemon.shutil.which", return_value="/usr/bin/tool"):
                try:
                    await daemon.run()
                except asyncio.CancelledError:
                    pass

        assert usb_loop_started
        assert bt_loop_started

    @pytest.mark.uc("UC-001", flow="main", steps=[7], br=["BR-010"])
    async def test_shutdown_ends_the_led_cues(self, daemon):
        """#2091: cancelling the daemon cancels the LED cue tasks on the controllers on USB."""
        import asyncio

        from psmove_pairing.usb_led import UsbLedCue

        async def run_forever():
            await asyncio.Event().wait()

        daemon._usb_poll_loop = run_forever
        daemon._bt_monitor_loop = run_forever
        daemon.usb_pairing.led_cue = create_autospec(UsbLedCue, instance=True)

        with (
            patch.object(daemon, "validate_prerequisites", AsyncMock(return_value=True)),
            patch("psmove_pairing.daemon.register_pairing_agent", AsyncMock()),
        ):
            task = asyncio.create_task(daemon.run())
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        daemon.usb_pairing.led_cue.close.assert_awaited_once()


@pytest.mark.uc("UC-001", br=["BR-009"])
def test_pairing_agent_answers_for_the_daemons_usb_pairings(daemon):
    from psmove_pairing.bluez_agent import agent

    assert agent.is_known == daemon.usb_pairing.paired_this_session


@pytest.mark.uc("UC-001", br=["BR-009"])
async def test_monitor_cycle_makes_sure_the_agent_is_registered(daemon):
    """Every Bluetooth monitor cycle re-registers the agent if BlueZ has not confirmed it, also after an error."""
    import asyncio
    from unittest.mock import AsyncMock

    daemon.bt_monitor.monitor = AsyncMock(side_effect=RuntimeError("monitor failed"))
    ensure = AsyncMock(return_value=False)
    with (
        patch("psmove_pairing.daemon.ensure_pairing_agent", ensure),
        patch("psmove_pairing.daemon.asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])),
        pytest.raises(asyncio.CancelledError),
    ):
        await daemon._bt_monitor_loop()

    assert ensure.await_count == 2


class TestStartupAndStaleness:
    """#1770: bounded startup commands, sane staleness before the first poll."""

    @pytest.mark.asyncio
    async def test_bluetoothctl_check_has_short_timeout(self, daemon):
        """bluetoothctl show must not use run_command's 60 s default."""
        seen = {}

        async def fake_run(cmd, **kwargs):
            seen["timeout"] = kwargs.get("timeout_seconds", 60)
            return -1, "timeout"

        with patch("psmove_pairing.daemon.run_command", fake_run):
            with patch("psmove_pairing.daemon.shutil.which", return_value="/usr/bin/x"):
                assert await daemon.validate_prerequisites() is False
        assert seen["timeout"] <= 10

    @pytest.mark.asyncio
    async def test_run_command_honours_timeout(self):
        """A hanging command is killed after the given timeout, not 60 s."""
        from psmove_pairing.utils import run_command

        start = time.monotonic()
        code, output = await run_command(["sleep", "30"], timeout_seconds=0.2)
        assert (code, output) == (-1, "timeout")
        assert time.monotonic() - start < 5

    def test_stale_log_never_reports_epoch(self, daemon, caplog):
        """Before the first poll, staleness counts from daemon start."""
        daemon._startup_time = time.time() - _HEALTH_STALENESS_THRESHOLD - 10
        with caplog.at_level("WARNING", logger="psmove-pairing"):
            assert daemon.is_healthy() is False
        assert "never polled" in caplog.text
