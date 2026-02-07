"""
Pytest fixtures for audio service tests.
"""

from __future__ import annotations

# Disable OpenTelemetry for tests - must be done before importing service modules
from lib.otel_metrics import disable_metrics_for_tests
from lib.telemetry import disable_telemetry_for_tests

disable_telemetry_for_tests()
disable_metrics_for_tests()

import os
from unittest.mock import patch

import pytest


class MockGrpcContext:
    """Mock gRPC context for testing."""

    def __init__(self):
        self.cancelled_flag = False

    def cancelled(self):
        return self.cancelled_flag

    def is_active(self):
        return not self.cancelled_flag


@pytest.fixture
def mock_grpc_context():
    return MockGrpcContext()


@pytest.fixture
def mock_audio_servicer():
    """Create AudioServiceServicer in mock mode."""
    with patch.dict(os.environ, {"MOCK_MODE": "true"}):
        from services.audio.servicer import AudioServiceServicer

        servicer = AudioServiceServicer()
        servicer._settings_loaded = True
        return servicer


@pytest.fixture
def mock_audio_manager():
    """Create AudioManager in mock mode."""
    with patch.dict(os.environ, {"MOCK_MODE": "true"}):
        from services.audio.servicer import AudioManager

        return AudioManager()
