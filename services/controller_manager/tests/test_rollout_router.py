"""
Unit tests for RolloutRouter — consumes the rollout flagd domain to drive
progressive backend rollout.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

from services.controller_manager.multiplexer.rollout_router import RolloutRouter

ALL = ["C", "A", "F", "B", "E", "D"]  # unsorted on purpose
SORTED = ["A", "B", "C", "D", "E", "F"]


def _client(strategy="off", target="unstable", count=0):
    client = MagicMock()

    def get_string(key, default, _ctx):
        if key == "strategy":
            return strategy
        if key == "target_backend":
            return target
        return default

    def get_int(key, default, _ctx):
        if key == "current_controller_count":
            return count
        return default

    client.get_string_value = MagicMock(side_effect=get_string)
    client.get_integer_value = MagicMock(side_effect=get_int)
    return client


def _run(serial, all_serials, **kw):
    router = RolloutRouter()
    with patch("lib.feature_flags.get_flag_client", return_value=_client(**kw)):
        return router.target_for(serial, all_serials)


class TestStrategyOff:
    def test_off_returns_none(self):
        assert _run("A", ALL, strategy="off", count=99) is None


class TestStrategyImmediate:
    def test_immediate_targets_every_serial(self):
        assert _run("A", ALL, strategy="immediate", target="unstable") == "unstable"
        assert _run("F", ALL, strategy="immediate", target="unstable") == "unstable"

    def test_immediate_empty_target_returns_none(self):
        assert _run("A", ALL, strategy="immediate", target="") is None


class TestStrategyProgressive:
    def test_count_zero_returns_none(self):
        assert _run("A", ALL, strategy="progressive", count=0) is None

    def test_count_one_first_serial_only(self):
        # sorted -> A is first
        assert _run("A", ALL, strategy="progressive", count=1) == "unstable"
        assert _run("B", ALL, strategy="progressive", count=1) is None

    def test_count_three_boundary(self):
        # sorted[:3] = A, B, C
        assert _run("C", ALL, strategy="progressive", count=3) == "unstable"
        assert _run("D", ALL, strategy="progressive", count=3) is None

    def test_count_six_includes_all_present(self):
        for s in SORTED:
            assert _run(s, ALL, strategy="progressive", count=6) == "unstable"

    def test_count_all_99_includes_everyone(self):
        for s in SORTED:
            assert _run(s, ALL, strategy="progressive", count=99) == "unstable"

    def test_uses_sorted_order_not_input_order(self):
        # Input order has C first, but sorted-first is A.
        assert _run("C", ALL, strategy="progressive", count=1) is None
        assert _run("A", ALL, strategy="progressive", count=1) == "unstable"

    def test_serial_not_in_all_serials_returns_none(self):
        assert _run("Z", ALL, strategy="progressive", count=99) is None


class TestDefensive:
    def test_flag_client_error_returns_none(self):
        router = RolloutRouter()
        with patch("lib.feature_flags.get_flag_client", side_effect=RuntimeError("flagd down")):
            assert router.target_for("A", ALL) is None

    def test_unknown_strategy_returns_none(self):
        assert _run("A", ALL, strategy="bogus", count=99) is None

    def test_string_eval_error_returns_none(self):
        router = RolloutRouter()
        client = MagicMock()
        client.get_string_value = MagicMock(side_effect=ValueError("boom"))
        with patch("lib.feature_flags.get_flag_client", return_value=client):
            assert router.target_for("A", ALL) is None
