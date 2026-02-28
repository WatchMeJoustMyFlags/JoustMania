"""
Unit tests for multiplexer backend combination validation.

Tests that only safe backend combinations are allowed:
- Single backends: mock, hidapi
- Mock + hidapi
- Rejects: duplicates, unknown backends
"""

import sys
from pathlib import Path

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

from services.controller_manager.multiplexer.validation import validate_backend_combination


class TestValidSingleBackends:
    """Single backends should always be valid."""

    @pytest.mark.parametrize("name", ["mock", "hidapi"])
    def test_single_backend_passes(self, name):
        validate_backend_combination([name])


class TestValidDualBackends:
    """Mock + real backend combinations should be valid."""

    def test_mock_hidapi(self):
        validate_backend_combination(["mock", "hidapi"])

    def test_order_does_not_matter(self):
        validate_backend_combination(["hidapi", "mock"])


class TestInvalidCombinations:
    """Conflicting backend combinations should raise ValueError."""

    def test_rejects_duplicate_hidapi(self):
        with pytest.raises(ValueError, match="Unsupported backend combination"):
            validate_backend_combination(["hidapi", "hidapi"])

    def test_rejects_unknown_backend(self):
        with pytest.raises(ValueError, match="Unsupported backend combination"):
            validate_backend_combination(["mock", "windows"])

    def test_rejects_bluetooth(self):
        with pytest.raises(ValueError, match="Unsupported backend combination"):
            validate_backend_combination(["bluetooth"])
