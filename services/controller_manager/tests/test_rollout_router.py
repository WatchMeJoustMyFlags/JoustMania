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


def _current(**kw):
    router = RolloutRouter()
    with patch("lib.feature_flags.get_flag_client", return_value=_client(**kw)):
        return router.current_target()


class TestCurrentTarget:
    """Window-level rollout summary used by the bluetooth-health span (#732 M3)."""

    def test_off_reports_empty(self):
        assert _current(strategy="off", count=99) == ("", 0)

    def test_immediate_reports_target_and_count(self):
        assert _current(strategy="immediate", target="unstable", count=0) == ("unstable", 0)

    def test_progressive_reports_target_and_count(self):
        assert _current(strategy="progressive", target="unstable", count=3) == ("unstable", 3)

    def test_empty_target_reports_off(self):
        assert _current(strategy="immediate", target="", count=5) == ("", 0)

    def test_negative_count_clamped_to_zero(self):
        assert _current(strategy="progressive", target="unstable", count=-4) == ("unstable", 0)

    def test_flag_client_error_reports_off(self):
        router = RolloutRouter()
        with patch("lib.feature_flags.get_flag_client", side_effect=RuntimeError("flagd down")):
            assert router.current_target() == ("", 0)


def _strategy(**kw):
    router = RolloutRouter()
    with patch("lib.feature_flags.get_flag_client", return_value=_client(**kw)):
        return router.current_strategy()


class TestCurrentStrategy:
    """Strategy string surfaced on the bluetooth-health span (#829)."""

    def test_off(self):
        assert _strategy(strategy="off") == "off"

    def test_immediate(self):
        assert _strategy(strategy="immediate") == "immediate"

    def test_progressive(self):
        assert _strategy(strategy="progressive") == "progressive"

    def test_flag_client_error_reports_empty(self):
        router = RolloutRouter()
        with patch("lib.feature_flags.get_flag_client", side_effect=RuntimeError("flagd down")):
            assert router.current_strategy() == ""
