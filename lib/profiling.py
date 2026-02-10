"""
Continuous profiling with Grafana Pyroscope for JoustMania services.

Provides CPU flame graphs for identifying hotspots in the 100Hz controller
polling loop, game loop processing, and gRPC handlers. Especially valuable
on resource-constrained Raspberry Pi hardware.

Usage:
    from lib.profiling import init_profiling

    # In service startup, after init_metrics():
    init_profiling()

    # In tests (conftest.py):
    from lib.profiling import disable_profiling_for_tests
    disable_profiling_for_tests()
"""

import logging
import os
import threading

logger = logging.getLogger(__name__)

_initialized = False
_init_lock = threading.Lock()
_test_mode = False


def disable_profiling_for_tests() -> None:
    """
    Disable Pyroscope profiling for unit tests.

    Call this once in conftest.py to prevent Pyroscope connection attempts
    during tests. Follows the same pattern as disable_telemetry_for_tests().
    """
    global _initialized, _test_mode
    _test_mode = True
    _initialized = True


def init_profiling(application_name: str | None = None) -> None:
    """
    Initialize Pyroscope continuous profiling.

    Configures CPU profiling at 100Hz with GIL-only sampling. The application
    name defaults to OTEL_SERVICE_NAME for consistency with tracing/metrics.

    Args:
        application_name: Override for the Pyroscope application name.
            Defaults to OTEL_SERVICE_NAME env var.
    """
    global _initialized
    if _initialized:
        return

    with _init_lock:
        if _initialized:
            return

        try:
            import pyroscope
        except ImportError:
            logger.warning("pyroscope-io not installed, skipping profiling initialization")
            _initialized = True
            return

        server_address = os.getenv("PYROSCOPE_SERVER_ADDRESS", "http://pyroscope:4040")
        app_name = application_name or os.getenv("OTEL_SERVICE_NAME", "unknown")

        pyroscope.configure(
            application_name=app_name,
            server_address=server_address,
            sample_rate=100,
            detect_subprocesses=False,
            oncpu=True,
            gil_only=True,
            tags={"service": app_name},
        )

        _initialized = True
        logger.info(f"Pyroscope profiling initialized: {app_name} -> {server_address}")
