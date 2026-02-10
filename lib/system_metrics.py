"""
System Metrics Collector - Collects process-level metrics.

Provides async background collection of CPU, memory, and thread metrics
using psutil. Designed to be reusable across all services.

Metric names follow the Prometheus/Go standard convention:
  - process_cpu_seconds_total (counter) - cumulative CPU seconds
  - process_resident_memory_bytes (gauge) - RSS in bytes
  - process_threads (gauge) - thread count

Usage:
    from lib.system_metrics import start_system_metrics_collector

    # In your async serve() function:
    async def serve():
        metrics_task = start_system_metrics_collector(
            cpu_counter=my_cpu_counter,
            memory_gauge=my_memory_gauge,
            threads_gauge=my_threads_gauge,
            interval=10.0,
        )
"""

import asyncio
import logging

import psutil

logger = logging.getLogger(__name__)


async def collect_system_metrics(
    cpu_counter,
    memory_gauge,
    threads_gauge,
    interval: float = 10.0,
):
    """
    Background task to collect system metrics periodically.

    Runs psutil calls in thread pool to avoid blocking the event loop.

    Args:
        cpu_counter: Counter for cumulative CPU seconds (process_cpu_seconds_total)
        memory_gauge: Gauge for RSS memory in bytes (process_resident_memory_bytes)
        threads_gauge: Gauge for thread count (process_threads)
        interval: Collection interval in seconds (default: 10.0)
    """
    process = psutil.Process()
    loop = asyncio.get_event_loop()
    prev_cpu_times = process.cpu_times()

    while True:
        try:
            cpu_times = await loop.run_in_executor(None, process.cpu_times)
            mem_info = await loop.run_in_executor(None, process.memory_info)
            thread_count = await loop.run_in_executor(None, process.num_threads)

            # Increment counter by delta of CPU seconds since last collection
            delta = (cpu_times.user + cpu_times.system) - (prev_cpu_times.user + prev_cpu_times.system)
            if delta > 0:
                cpu_counter.inc(delta)
            prev_cpu_times = cpu_times

            memory_gauge.set(mem_info.rss)
            threads_gauge.set(thread_count)

        except Exception as e:
            logger.error(f"Error collecting system metrics: {e}")

        await asyncio.sleep(interval)


def start_system_metrics_collector(
    cpu_counter,
    memory_gauge,
    threads_gauge,
    interval: float = 10.0,
) -> asyncio.Task:
    """
    Start the system metrics collector as a background task.

    Args:
        cpu_counter: Counter for cumulative CPU seconds (process_cpu_seconds_total)
        memory_gauge: Gauge for RSS memory in bytes (process_resident_memory_bytes)
        threads_gauge: Gauge for thread count (process_threads)
        interval: Collection interval in seconds (default: 10.0)

    Returns:
        The asyncio Task (can be cancelled if needed)
    """
    return asyncio.create_task(collect_system_metrics(cpu_counter, memory_gauge, threads_gauge, interval))


def collect_system_metrics_sync(
    cpu_counter,
    memory_gauge,
    threads_gauge,
    interval: float = 10.0,
):
    """
    Synchronous version of system metrics collection for threaded services.

    Runs in a loop, collecting metrics at the specified interval.
    Designed to run in a daemon thread.

    Args:
        cpu_counter: Counter for cumulative CPU seconds (process_cpu_seconds_total)
        memory_gauge: Gauge for RSS memory in bytes (process_resident_memory_bytes)
        threads_gauge: Gauge for thread count (process_threads)
        interval: Collection interval in seconds (default: 10.0)
    """
    import time

    process = psutil.Process()
    prev_cpu_times = process.cpu_times()

    while True:
        try:
            cpu_times = process.cpu_times()
            delta = (cpu_times.user + cpu_times.system) - (prev_cpu_times.user + prev_cpu_times.system)
            if delta > 0:
                cpu_counter.inc(delta)
            prev_cpu_times = cpu_times

            memory_gauge.set(process.memory_info().rss)
            threads_gauge.set(process.num_threads())
        except Exception as e:
            logger.error(f"Error collecting system metrics: {e}")

        time.sleep(interval)


def start_system_metrics_collector_thread(
    cpu_counter,
    memory_gauge,
    threads_gauge,
    interval: float = 10.0,
):
    """
    Start the system metrics collector as a background daemon thread.

    For use in synchronous/non-async services.

    Args:
        cpu_counter: Counter for cumulative CPU seconds (process_cpu_seconds_total)
        memory_gauge: Gauge for RSS memory in bytes (process_resident_memory_bytes)
        threads_gauge: Gauge for thread count (process_threads)
        interval: Collection interval in seconds (default: 10.0)

    Returns:
        The started Thread object
    """
    import threading

    thread = threading.Thread(
        target=collect_system_metrics_sync,
        args=(cpu_counter, memory_gauge, threads_gauge, interval),
        daemon=True,
    )
    thread.start()
    return thread
