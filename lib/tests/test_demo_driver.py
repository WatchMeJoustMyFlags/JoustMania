"""Tests for the talk demo driver (FR-092, tools/demo/demo_driver.py, UC-070).

The real module runs against fake gRPC servicers (autospec'd from the generated
servicer classes) and a fake clock, so the scripted scenarios are checked
without a stack and without sleeping. Lives in lib/tests because tools/ has no
test setup of its own (#1670); the lib CI job and `make pr-gates` (py:lib) run
it when tools/demo/** changes.
"""

import importlib.util
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import grpc
import pytest
from proto import (
    controller_manager_mock_pb2 as mockpb,
)
from proto import (
    controller_manager_mock_pb2_grpc as mockpb_grpc,
)
from proto import (
    game_coordinator_pb2 as gcpb,
)
from proto import (
    game_coordinator_pb2_grpc as gcpb_grpc,
)
from proto import (
    menu_pb2,
    menu_pb2_grpc,
)

# lib/tests/ -> lib/ -> repo root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SPEC = importlib.util.spec_from_file_location("demo_driver", REPO_ROOT / "tools" / "demo" / "demo_driver.py")
dd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dd)


class FakeClock:
    """Replaces the driver's `time` module: sleep() advances time, nothing blocks."""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeRpcError(grpc.RpcError):
    pass


def _magnitude(call):
    req = call.args[0]
    return math.sqrt(req.accel_x**2 + req.accel_y**2 + req.accel_z**2)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(dd, "time", fake)
    return fake


def _fake_stub(servicer_cls):
    """Autospec'd client stub: one `(request, timeout=None)` method per RPC of the servicer.

    The generated stubs set their methods in __init__, so they cannot be autospec'd
    directly; the servicer class lists the same RPC names, so a renamed RPC breaks here.
    """
    rpc_names = [n for n in vars(servicer_cls) if not n.startswith("_")]

    def rpc(self, request, timeout=None):  # noqa: ARG001
        return None

    stub_cls = type("Stub", (), dict.fromkeys(rpc_names, rpc))
    return create_autospec(stub_cls, instance=True, spec_set=True)


@pytest.fixture
def stack():
    return SimpleNamespace(
        mock=_fake_stub(mockpb_grpc.MockControllerServiceServicer),
        menu=_fake_stub(menu_pb2_grpc.MenuServiceServicer),
        coord=_fake_stub(gcpb_grpc.GameCoordinatorServiceServicer),
    )


def _moves(stack):
    """(serial, magnitude) of every SimulateMovement call, in order."""
    return [(c.args[0].serial, _magnitude(c)) for c in stack.mock.SimulateMovement.call_args_list]


def _approx(value):
    return pytest.approx(value, abs=1e-5)


@pytest.mark.uc("UC-070", flow="main", steps=[1])
def test_registry_names_the_scenarios_fr092_promises():
    assert {"skill_gap", "statue", "death_cascade"} <= set(dd.SCENARIOS)
    for desc, rule, observable, runner in dd.SCENARIOS.values():
        assert desc and rule and observable
        assert callable(runner)


@pytest.mark.uc("UC-070", flow="main", steps=[1])
def test_no_zone_vector_can_kill_at_the_default_threshold():
    for vec in (dd.ZONE_DIP, dd.ZONE_STILL, dd.ZONE_ACTIVE, dd.ZONE_ACTIVE_HIGH, dd.ZONE_WARNING):
        assert math.sqrt(sum(c * c for c in vec)) <= dd.SAFE_MAX_G + 1e-9
    assert dd.SAFE_MAX_G <= 1.6  # default death threshold is 1.8g, keep 0.2g margin


@pytest.mark.uc("UC-070", flow="main", steps=[1])
@pytest.mark.parametrize("name", sorted(dd.SCENARIOS))
def test_every_scenario_emits_only_safe_vectors(stack, name):
    dd.SCENARIOS[name][3](stack, ["A", "B", "C", "D"], 12.0)
    mags = [m for _, m in _moves(stack)]
    assert mags
    assert max(mags) <= dd.SAFE_MAX_G + 1e-5


@pytest.mark.uc("UC-070", flow="main", steps=[1])
def test_idle_dominator_alternates_the_dominator_and_holds_idlers_still(stack):
    dd.scenario_idle_dominator(stack, ["D", "I1", "I2"], 4.0)
    moves = _moves(stack)
    dominator = [m for s, m in moves if s == "D"]
    assert dominator[:4] == [_approx(1.3), _approx(1.5), _approx(1.3), _approx(1.5)]
    assert all(m == _approx(1.0) for s, m in moves if s != "D")
    assert {s for s, _ in moves} == {"D", "I1", "I2"}


@pytest.mark.uc("UC-070", flow="main", steps=[1])
def test_low_variance_holds_the_whole_field_still(stack):
    dd.scenario_low_variance(stack, ["A", "B", "C"], 3.0)
    moves = _moves(stack)
    assert {s for s, _ in moves} == {"A", "B", "C"}
    assert all(m == _approx(1.0) for _, m in moves)


@pytest.mark.uc("UC-070", flow="main", steps=[1])
def test_blocked_battery_plays_the_skill_gap_pattern(stack, clock):
    dd.scenario_blocked_battery(stack, ["A", "B", "C", "D"], 2.0)
    blocked = _moves(stack)
    clock.now = 1000.0
    other = SimpleNamespace(mock=_fake_stub(mockpb_grpc.MockControllerServiceServicer))
    dd.scenario_skill_gap(other, ["A", "B", "C", "D"], 2.0)
    assert blocked
    assert blocked == _moves(other)


@pytest.mark.uc("UC-070", flow="main", steps=[1, 2])
def test_skill_gap_splits_steady_pros_from_jerky_novices(stack):
    serials = ["A", "B", "C", "D"]
    dd.scenario_skill_gap(stack, serials, 2.0)
    moves = _moves(stack)
    pro_mags = [m for s, m in moves if s in ("A", "B")]
    novice_mags = [m for s, m in moves if s == "C"]
    assert pro_mags and all(m == _approx(1.3) for m in pro_mags)
    assert len([1 for s, _ in moves if s == "A"]) == 1  # held vector set once
    # novices alternate dip (0.5g) / warning (1.6g), starting with the dip
    assert novice_mags[:4] == [_approx(0.5), _approx(1.6), _approx(0.5), _approx(1.6)]
    assert max(m for _, m in moves) <= dd.SAFE_MAX_G + 1e-5
    assert {s for s, _ in moves} == set(serials)


@pytest.mark.uc("UC-070", flow="main", steps=[1])
def test_skill_gap_needs_two_controllers(stack):
    with pytest.raises(RuntimeError, match="needs >= 2"):
        dd.scenario_skill_gap(stack, ["A"], 1.0)
    stack.mock.SimulateMovement.assert_not_called()


@pytest.mark.uc("UC-070", flow="main", steps=[1])
def test_statue_holds_one_controller_still_while_the_others_jostle(stack):
    dd.scenario_statue(stack, ["S", "M1", "M2"], 2.0)
    moves = _moves(stack)
    statue_moves = [m for s, m in moves if s == "S"]
    assert statue_moves == [_approx(1.0)]  # one constant STILL vector, never re-sent
    mover_mags = [m for s, m in moves if s == "M1"]
    assert mover_mags[:4] == [_approx(0.5), _approx(1.5), _approx(0.5), _approx(1.5)]
    assert {s for s, _ in moves} == {"S", "M1", "M2"}


@pytest.mark.uc("UC-070", flow="main", steps=[1, 2])
def test_death_cascade_eliminates_all_but_one_every_three_seconds(stack, clock):
    kill_times = []

    def record(request):
        kill_times.append((request.serial, clock.now))

    stack.mock.SimulateDeath.side_effect = record
    start = clock.now
    dd.scenario_death_cascade(stack, ["A", "B", "C", "D"], 12.0)
    assert [s for s, _ in kill_times] == ["A", "B", "C"]  # survivor D is never killed
    gaps = [b - a for (_, a), (_, b) in zip(kill_times, kill_times[1:], strict=False)]
    assert all(g >= 3.0 for g in gaps)
    assert kill_times[0][1] - start < 1.0  # the cascade starts immediately (session young)
    assert max(m for _, m in _moves(stack)) <= dd.SAFE_MAX_G + 1e-5


@pytest.mark.uc("UC-070", flow="main", steps=[1])
def test_scenarios_are_reproducible(clock):
    """Guards against random or wall-clock inputs: under the fake clock a run is a pure function of its arguments."""

    def play(fn):
        clock.now = 1000.0
        s = SimpleNamespace(mock=_fake_stub(mockpb_grpc.MockControllerServiceServicer))
        fn(s, ["A", "B", "C", "D"], 5.0)
        return [repr(c) for c in s.mock.method_calls]

    for name in sorted(dd.SCENARIOS):
        fn = dd.SCENARIOS[name][3]
        assert play(fn) == play(fn), name


@pytest.mark.uc("UC-070", flow="main", steps=[1])
def test_add_controllers_requests_menu_visible_tagged_mocks(stack):
    stack.mock.AddControllers.return_value = mockpb.AddControllersResponse(success=True, serials=["m1", "m2"])
    assert dd.add_controllers(stack, 2) == ["m1", "m2"]
    req = stack.mock.AddControllers.call_args.args[0]
    assert (req.count, req.reserved, req.tag) == (2, False, "demo_driver")


@pytest.mark.uc("UC-070", flow="main", steps=[1])
def test_add_controllers_raises_on_rejection(stack):
    stack.mock.AddControllers.return_value = mockpb.AddControllersResponse(success=False, error="full")
    with pytest.raises(RuntimeError, match="full"):
        dd.add_controllers(stack, 2)


@pytest.mark.uc("UC-070", flow="main", steps=[1])
def test_remove_demo_controllers_removes_only_tagged(stack):
    stack.mock.ListMockControllers.return_value = mockpb.ListResponse(
        controllers=[
            mockpb.MockControllerInfo(serial="mine", tag="demo_driver"),
            mockpb.MockControllerInfo(serial="other", tag="experiment"),
        ]
    )
    dd.remove_demo_controllers(stack)
    removed = [c.args[0].serial for c in stack.mock.RemoveController.call_args_list]
    assert removed == ["mine"]


@pytest.mark.uc("UC-070", flow="main", steps=[1])
def test_start_real_game_readies_every_controller_and_returns_game_id(stack):
    stack.menu.StartMenu.return_value = menu_pb2.StartMenuResponse(success=True)
    stack.coord.StreamGameEvents.return_value = iter([gcpb.GameEvent(event_type="game_started", game_id="g-42")])
    assert dd.start_real_game(stack, ["A", "B"], "JoustFFA", 5.0) == "g-42"
    pressed = [(c.args[0].serial, c.args[0].pressed) for c in stack.mock.SimulateButton.call_args_list]
    assert pressed == [("A", True), ("A", False), ("B", True), ("B", False)]
    select = stack.menu.ProcessInput.call_args.args[0]
    assert dict(select.data) == {"command": "select_game", "game_name": "JoustFFA"}
    assert stack.coord.ForceEndGame.call_args.args[0].reason == "demo_driver_cleanup"


@pytest.mark.uc("UC-070", flow="main", steps=[1])
def test_start_real_game_times_out_without_a_game_id(stack, clock):
    stack.menu.StartMenu.return_value = menu_pb2.StartMenuResponse(success=True)

    def silent_events():
        while True:
            clock.now += 10.0
            yield gcpb.GameEvent(event_type="tick", game_id="")

    stack.coord.StreamGameEvents.return_value = silent_events()
    with pytest.raises(TimeoutError):
        dd.start_real_game(stack, ["A"], "JoustFFA", 5.0)


@pytest.mark.uc("UC-070", flow="main", steps=[1])
def test_start_real_game_fails_when_menu_will_not_start(stack):
    stack.menu.StartMenu.return_value = menu_pb2.StartMenuResponse(success=False, error="busy")
    with pytest.raises(RuntimeError, match="busy"):
        dd.start_real_game(stack, ["A"], "JoustFFA", 5.0)
    stack.mock.SimulateButton.assert_not_called()


def _args(**overrides):
    base = {
        "scenario": "statue",
        "players": 3,
        "duration": 1.0,
        "game_mode": "JoustFFA",
        "start_timeout": 5.0,
        "keep_game": False,
        "keep_controllers": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _wire_run(stack):
    stack.mock.AddControllers.return_value = mockpb.AddControllersResponse(success=True, serials=["a", "b", "c"])
    stack.mock.ListMockControllers.return_value = mockpb.ListResponse(
        controllers=[mockpb.MockControllerInfo(serial=s, tag="demo_driver") for s in "abc"]
    )
    stack.menu.StartMenu.return_value = menu_pb2.StartMenuResponse(success=True)
    stack.coord.StreamGameEvents.side_effect = lambda *_: iter(
        [gcpb.GameEvent(event_type="game_started", game_id="g1")]
    )


@pytest.mark.uc("UC-070", flow="main", steps=[1])
def test_run_once_plays_the_scenario_then_ends_the_game_and_removes_controllers(stack):
    _wire_run(stack)
    dd.run_once(stack, _args())
    assert stack.coord.ForceEndGame.call_args.args[0].reason == "demo_driver_scenario_done"
    assert [c.args[0].serial for c in stack.mock.RemoveController.call_args_list] == ["a", "b", "c"]
    assert ("a", _approx(1.0)) in _moves(stack)  # statue = first serial held still


@pytest.mark.uc("UC-070", flow="main", steps=[1])
def test_run_once_keep_flags_leave_game_and_controllers_but_reset_motion(stack):
    _wire_run(stack)
    dd.run_once(stack, _args(keep_game=True, keep_controllers=True))
    reasons = [c.args[0].reason for c in stack.coord.ForceEndGame.call_args_list]
    assert "demo_driver_scenario_done" not in reasons
    stack.mock.RemoveController.assert_not_called()
    assert [c.args[0].serial for c in stack.mock.ResetController.call_args_list] == ["a", "b", "c"]


@pytest.mark.uc("UC-070", flow="main", steps=[1])
def test_run_once_cleans_up_when_the_scenario_fails(stack):
    _wire_run(stack)
    stack.mock.SimulateMovement.side_effect = FakeRpcError()
    with pytest.raises(FakeRpcError):
        dd.run_once(stack, _args())
    assert len(stack.mock.RemoveController.call_args_list) == 3


@pytest.mark.uc("UC-070", flow="main", steps=[1])
def test_list_and_unknown_scenario_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["demo_driver.py", "--list"])
    assert dd.main() == 0
    assert "death_cascade" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["demo_driver.py", "--scenario", "nope"])
    assert dd.main() == 2
    assert "unknown scenario" in capsys.readouterr().err


def _countdown_stream(clock, seen):
    """game_started, then the countdown, then countdown_end; `seen` records when each is read."""
    for event_type in ("game_started", "countdown_start", "countdown_end"):
        seen[event_type] = clock.now
        yield gcpb.GameEvent(event_type=event_type, game_id="g1")
        clock.now += 0.75  # one countdown phase passes between events


@pytest.mark.uc("UC-070", flow="main", steps=[1, 2])
def test_start_real_game_returns_only_after_countdown_warmup_and_grace(stack, clock):
    """#1710: the coordinator ignores deaths until countdown_end + EMA warmup (0.1s) + startup grace (0.3s)."""
    seen = {}
    stack.coord.StreamGameEvents.side_effect = lambda *_: _countdown_stream(clock, seen)
    assert dd.start_real_game(stack, ["A"], "JoustFFA", 30.0) == "g1"
    live_at = seen["countdown_end"] + 0.1 + 0.3
    assert clock.now >= live_at
    assert dd.LIVE_PLAY_SETTLE_SECONDS >= dd.EMA_WARMUP_SECONDS + dd.STARTUP_GRACE_SECONDS


@pytest.mark.uc("UC-070", flow="main", steps=[1, 2])
def test_death_cascade_first_kill_waits_for_live_play(stack, clock):
    seen = {}
    _wire_run(stack)
    stack.coord.StreamGameEvents.side_effect = lambda *_: _countdown_stream(clock, seen)
    kills = []
    stack.mock.SimulateDeath.side_effect = lambda _request: kills.append(clock.now)
    dd.run_once(stack, _args(scenario="death_cascade", duration=8.0))
    assert kills
    assert kills[0] >= seen["countdown_end"] + 0.1 + 0.3
