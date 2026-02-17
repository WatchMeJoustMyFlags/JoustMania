"""Tests for lib/system_metrics.py"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add lib to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from lib.system_metrics import (
    collect_system_metrics,
    start_system_metrics_collector,
    start_system_metrics_collector_thread,
)


def _make_mock_metrics():
    """Create mock counter and gauge objects."""
    cpu_counter = MagicMock()
    memory_gauge = MagicMock()
    threads_gauge = MagicMock()
    return cpu_counter, memory_gauge, threads_gauge


def _make_mock_cpu_times(user=1.0, system=0.5):
    """Create a mock cpu_times result."""
    mock = MagicMock()
    mock.user = user
    mock.system = system
    return mock


def _make_mock_mem_info(rss=1024 * 1024):
    """Create a mock memory_info result."""
    mock = MagicMock()
    mock.rss = rss
    return mock


class TestCollectSystemMetrics:
    """Tests for collect_system_metrics async function."""

    @patch("lib.system_metrics.psutil.Process")
    async def test_collects_cpu_delta(self, mock_process_cls):
        cpu_counter, memory_gauge, threads_gauge = _make_mock_metrics()

        process = MagicMock()
        mock_process_cls.return_value = process

        # Initial cpu_times and then one iteration
        initial_cpu = _make_mock_cpu_times(user=1.0, system=0.5)
        updated_cpu = _make_mock_cpu_times(user=2.0, system=1.0)
        process.cpu_times.side_effect = [initial_cpu, updated_cpu]
        process.memory_info.return_value = _make_mock_mem_info()
        process.num_threads.return_value = 5

        task = asyncio.create_task(collect_system_metrics(cpu_counter, memory_gauge, threads_gauge, interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task

        # Delta = (2.0 + 1.0) - (1.0 + 0.5) = 1.5
        cpu_counter.inc.assert_called_with(1.5)

    @patch("lib.system_metrics.psutil.Process")
    async def test_sets_memory_gauge(self, mock_process_cls):
        cpu_counter, memory_gauge, threads_gauge = _make_mock_metrics()

        process = MagicMock()
        mock_process_cls.return_value = process

        initial_cpu = _make_mock_cpu_times()
        process.cpu_times.side_effect = [initial_cpu, _make_mock_cpu_times()]
        process.memory_info.return_value = _make_mock_mem_info(rss=2048)
        process.num_threads.return_value = 3

        task = asyncio.create_task(collect_system_metrics(cpu_counter, memory_gauge, threads_gauge, interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task

        memory_gauge.set.assert_called_with(2048)

    @patch("lib.system_metrics.psutil.Process")
    async def test_sets_thread_gauge(self, mock_process_cls):
        cpu_counter, memory_gauge, threads_gauge = _make_mock_metrics()

        process = MagicMock()
        mock_process_cls.return_value = process

        initial_cpu = _make_mock_cpu_times()
        process.cpu_times.side_effect = [initial_cpu, _make_mock_cpu_times()]
        process.memory_info.return_value = _make_mock_mem_info()
        process.num_threads.return_value = 7

        task = asyncio.create_task(collect_system_metrics(cpu_counter, memory_gauge, threads_gauge, interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task

        threads_gauge.set.assert_called_with(7)

    @patch("lib.system_metrics.psutil.Process")
    async def test_handles_error(self, mock_process_cls):
        cpu_counter, memory_gauge, threads_gauge = _make_mock_metrics()

        process = MagicMock()
        mock_process_cls.return_value = process

        initial_cpu = _make_mock_cpu_times()
        # First call for init, then error on iteration, then success
        process.cpu_times.side_effect = [initial_cpu, RuntimeError("psutil error"), _make_mock_cpu_times()]
        process.memory_info.return_value = _make_mock_mem_info()
        process.num_threads.return_value = 1

        task = asyncio.create_task(collect_system_metrics(cpu_counter, memory_gauge, threads_gauge, interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task

        # Should not crash — loop continues after error

    @patch("lib.system_metrics.psutil.Process")
    async def test_zero_delta_skipped(self, mock_process_cls):
        cpu_counter, memory_gauge, threads_gauge = _make_mock_metrics()

        process = MagicMock()
        mock_process_cls.return_value = process

        # Same CPU times = zero delta
        same_cpu = _make_mock_cpu_times(user=1.0, system=0.5)
        process.cpu_times.side_effect = [same_cpu, same_cpu]
        process.memory_info.return_value = _make_mock_mem_info()
        process.num_threads.return_value = 1

        task = asyncio.create_task(collect_system_metrics(cpu_counter, memory_gauge, threads_gauge, interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task

        cpu_counter.inc.assert_not_called()


class TestStartCollector:
    """Tests for start_system_metrics_collector."""

    @patch("lib.system_metrics.psutil.Process")
    async def test_returns_task(self, mock_process_cls):
        cpu_counter, memory_gauge, threads_gauge = _make_mock_metrics()

        process = MagicMock()
        mock_process_cls.return_value = process
        process.cpu_times.return_value = _make_mock_cpu_times()
        process.memory_info.return_value = _make_mock_mem_info()
        process.num_threads.return_value = 1

        task = start_system_metrics_collector(cpu_counter, memory_gauge, threads_gauge, interval=0.01)

        assert isinstance(task, asyncio.Task)
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task


class TestStartCollectorThread:
    """Tests for start_system_metrics_collector_thread."""

    @patch("lib.system_metrics.psutil.Process")
    def test_returns_thread(self, mock_process_cls):
        import threading

        process = MagicMock()
        mock_process_cls.return_value = process
        process.cpu_times.return_value = _make_mock_cpu_times()
        process.memory_info.return_value = _make_mock_mem_info()
        process.num_threads.return_value = 1

        cpu_counter, memory_gauge, threads_gauge = _make_mock_metrics()
        thread = start_system_metrics_collector_thread(cpu_counter, memory_gauge, threads_gauge, interval=60)

        assert isinstance(thread, threading.Thread)

    @patch("lib.system_metrics.psutil.Process")
    def test_thread_is_daemon(self, mock_process_cls):
        process = MagicMock()
        mock_process_cls.return_value = process
        process.cpu_times.return_value = _make_mock_cpu_times()
        process.memory_info.return_value = _make_mock_mem_info()
        process.num_threads.return_value = 1

        cpu_counter, memory_gauge, threads_gauge = _make_mock_metrics()
        thread = start_system_metrics_collector_thread(cpu_counter, memory_gauge, threads_gauge, interval=60)

        assert thread.daemon is True
