"""
Pytest fixtures for audio service tests.
"""

# Disable OpenTelemetry for tests - must be done before importing service modules
from lib.otel_logging import disable_logging_for_tests
from lib.otel_metrics import disable_metrics_for_tests
from lib.telemetry import disable_telemetry_for_tests

disable_telemetry_for_tests()
disable_metrics_for_tests()
disable_logging_for_tests()

import os
from pathlib import Path
from unittest.mock import patch

import pytest

PACKAGED_ASSETS = Path(__file__).parent.parent / "assets"


class MockGrpcContext:
    """Mock gRPC context for testing."""

    def __init__(self):
        self.cancelled_flag = False

    def cancelled(self):
        return self.cancelled_flag

    def is_active(self):
        return not self.cancelled_flag


@pytest.fixture(autouse=True)
def _no_real_flagd_provider(monkeypatch):
    """Keep a real FlagdProvider out of the unit tests (#2226).

    ``AudioServiceServicer()`` reads ``menu_voice`` through ``init_flag_domain("user")``.
    Unpatched, that installed an in-process FlagdProvider whose gRPC sync worker kept
    retrying ``flagd:8015`` for the rest of the session, next to the ``grpc.aio`` servers
    of other tests; CI died with a segfault in a gRPC channel thread. Without a provider,
    flag reads return their defaults, as they did when flagd was unreachable.
    """
    monkeypatch.setattr("lib.feature_flags.init_flag_domain", lambda _domain: None)


@pytest.fixture
def mock_grpc_context():
    return MockGrpcContext()


@pytest.fixture
def mock_audio_servicer():
    """Create AudioServiceServicer with silent AudioManager (no hardware).

    The AudioManager is initialized in silent mode to avoid needing audio hardware,
    but audio_enabled is set to True so RPC methods execute their full logic.
    Tests that need audio_enabled=False set it explicitly.
    """
    from services.audio.servicer import AudioServiceServicer

    # Bypass flagd lookup at init: return False so AudioManager uses silent=True (no hardware).
    with (
        patch.object(AudioServiceServicer, "_load_play_audio_flag", return_value=False),
        patch.dict(os.environ, {"AUDIO_ASSETS_DIR": str(PACKAGED_ASSETS)}),
    ):
        servicer = AudioServiceServicer()
    # Enable audio at the servicer level so RPCs execute their full logic.
    servicer.audio_enabled = True
    return servicer


@pytest.fixture
def mock_audio_manager():
    """Create AudioManager in silent mode (no hardware)."""
    from services.audio.servicer import AudioManager

    return AudioManager(silent=True)
