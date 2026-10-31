"""UC-046: pattern-driven mock controllers in the adapter, the gameplay stream and the mock RPCs."""

import json
from unittest.mock import create_autospec, patch

import grpc
import pytest

from proto import controller_manager_mock_pb2, controller_manager_pb2
from services.controller_manager.discovery_loop import DiscoveryLoop
from services.controller_manager.mock_control_service import MockControllerService
from services.controller_manager.multiplexer import motion_patterns as mp
from services.controller_manager.multiplexer.mock_adapter import MockAdapter
from services.controller_manager.tests.backend_doubles import AdapterAsBackend, stub_method

ARCHETYPE = "unit_cascade"
PARAMS = {
    "players": 4,
    "stream_rate_hz": 30,
    "base": 1.0,
    "drift": 0.01,
    "amp": 0.1,
    "period_s": 3.0,
    "phase": 0.0,
    "burst_h": 0.5,
    "burst_period_s": 4.0,
    "d_base": 0.0,
    "d_drift": 0.01,
    "d_amp": 0.0,
    "d_phase": 0.25,
}


class _Ctx:
    """Minimal grpc context recording the status the servicer sets."""

    def __init__(self):
        self.code = None
        self.details = None

    def set_code(self, code):
        self.code = code

    def set_details(self, details):
        self.details = details


@pytest.fixture
def adapter():
    return MockAdapter()


@pytest.fixture
def service(adapter):
    return MockControllerService(adapter)


@pytest.fixture
def archetype():
    archetype, errors = mp.validate_archetype(ARCHETYPE, PARAMS)
    assert errors == []
    return archetype


def _add_pattern(service, params=None, count=4):
    return service.AddControllers(
        controller_manager_mock_pb2.AddControllersRequest(
            count=count,
            reserved=True,
            tag="agent:t",
            archetype=ARCHETYPE,
            archetype_params=json.dumps(PARAMS if params is None else params),
        ),
        None,
    )


class TestUC046Adapter:
    @pytest.mark.uc("UC-046", flow="main", steps=[4, 5], br=["BR-001", "BR-006"])
    def test_frame_index_counts_delivered_frames_not_polls(self, adapter, archetype):
        """UC-046 BR-001: k advances once per delivered frame of the owning stream; polls never move it."""
        serials = [adapter.add_controller(reserved=True) for _ in range(4)]
        adapter.install_pattern(serials, archetype)
        for _ in range(50):
            adapter.poll(serials[0])
        delivered = [adapter.pattern_frame(serials[0], "game", explicit=True)[0]["z"] for _ in range(300)]
        expected = [archetype.value(0, k) for k in range(300)]
        assert delivered == expected
        assert adapter.pattern_info(serials[0]).frame_index == 300

    @pytest.mark.uc("UC-046", flow="main", steps=[4], br=["BR-006"])
    def test_slot_i_is_the_ith_reserved_serial(self, adapter, archetype):
        """UC-046 BR-006: slot i drives the i-th serial of the reservation, never a serial-derived index."""
        serials = [adapter.add_controller(reserved=True) for _ in range(4)]
        adapter.install_pattern(serials, archetype)
        assert [adapter.pattern_info(s).slot for s in serials] == [0, 1, 2, 3]
        with pytest.raises(ValueError):
            adapter.install_pattern(serials[:3], archetype)

    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-001"])
    def test_only_the_owning_stream_advances(self, adapter, archetype):
        """UC-046 BR-001: a stream without an explicit filter (dashboard) never advances k."""
        serial = adapter.add_controller(reserved=True)
        others = [adapter.add_controller(reserved=True) for _ in range(3)]
        adapter.install_pattern([serial, *others], archetype)
        adapter.pattern_frame(serial, "dashboard", explicit=False)
        assert adapter.pattern_info(serial).owner is None
        adapter.pattern_frame(serial, "game", explicit=True)
        adapter.pattern_frame(serial, "game2", explicit=True)
        adapter.pattern_frame(serial, "dashboard", explicit=False)
        assert adapter.pattern_info(serial).frame_index == 1
        # The owner's stream ends (reconnect): the next explicit stream continues k.
        adapter.release_pattern_stream("game")
        adapter.pattern_frame(serial, "game2", explicit=True)
        assert adapter.pattern_info(serial).frame_index == 2

    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-012"])
    def test_precedence_latch_then_pattern_and_hold_never_applies(self, adapter, archetype):
        """UC-046 BR-012: an active death latch is delivered first, then the pattern; a hold never applies."""
        serials = [adapter.add_controller(reserved=True) for _ in range(4)]
        adapter.install_pattern(serials, archetype)
        serial = serials[0]
        adapter.hold_accel(serial, 3.0, 3.0, 3.0)
        assert adapter.pattern_frame(serial, "game", explicit=True)[0]["z"] == archetype.value(0, 0)
        adapter.poll(serial)
        assert adapter.controllers[serial]["accel"]["x"] == 0.0  # polled state mirrors the pattern, not the hold
        controller = adapter.controllers[serial]
        controller["death_accel"] = {"x": 7.0, "y": 5.0, "z": 5.0}
        controller["death_frames_remaining"] = 2
        latched = adapter.pattern_frame(serial, "game", explicit=True)
        assert latched == ({"x": 7.0, "y": 5.0, "z": 5.0}, False)
        info = adapter.pattern_info(serial)
        assert info.frame_index == 2  # k advances on a latched frame too
        assert info.last_value == pytest.approx((49 + 25 + 25) ** 0.5)
        adapter.consume_death_frame(serial)
        adapter.consume_death_frame(serial)
        assert adapter.pattern_frame(serial, "game", explicit=True)[0]["z"] == archetype.value(0, 2)

    @pytest.mark.uc("UC-046", flow="A10", br=["BR-008"])
    def test_overrun_counted_per_owned_controller_signal_unchanged(self, adapter, archetype):
        """UC-046 A10: an overrun is counted for the stream's controllers; the delivered signal stays frame-indexed."""
        serials = [adapter.add_controller(reserved=True) for _ in range(4)]
        adapter.install_pattern(serials, archetype)
        for s in serials:
            adapter.pattern_frame(s, "game", explicit=True)
        assert adapter.record_pattern_overrun("game") == 4
        assert adapter.record_pattern_overrun("other") == 0
        assert adapter.pattern_frame(serials[1], "game", explicit=True)[0]["z"] == archetype.value(1, 1)
        assert adapter.pattern_info(serials[1]).record()["frame_overruns"] == 1


class TestUC046GameplayStream:
    @pytest.fixture
    def servicer(self, adapter):
        with (
            patch("services.controller_manager.servicer.create_backend", return_value=AdapterAsBackend(adapter)),
            patch("services.controller_manager.servicer.DiscoveryLoop") as loop_cls,
        ):
            loop = create_autospec(DiscoveryLoop, instance=True)
            loop.drain_health_counters.return_value = (0, 0, 0)
            loop.drain_disconnects.return_value = []
            loop.connection_quality.return_value = None  # UC-035 BR-005: no window yet
            loop_cls.return_value = loop
            from services.controller_manager.servicer import ControllerManagerServicer

            yield ControllerManagerServicer()

    @staticmethod
    def _state(serial, _info):
        state = controller_manager_pb2.ControllerState(serial=serial)
        state.accel.CopyFrom(controller_manager_pb2.Vector3(x=0.1, y=0.1, z=1.0))
        return state

    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-001"])
    def test_delivered_frames_carry_the_pattern(self, servicer, adapter, archetype):
        """UC-046 step 5: each delivered frame of a pattern controller carries (0, 0, intensity(k / r))."""
        serials = [adapter.add_controller(reserved=True) for _ in range(4)]
        adapter.install_pattern(serials, archetype)
        stub_method(servicer.state_builder, "build_state", side_effect=self._state)
        for s in serials:
            servicer.tracked_controllers[s] = {}
        servicer.tracked_controllers["REAL01"] = {}
        frames = [servicer._build_gameplay_update(set(serials), "game") for _ in range(int(9 * 30))]
        last = {gd.serial: gd.accel for gd in frames[-1].controllers}
        assert last[serials[0]].z == pytest.approx(archetype.value(0, 269), abs=1e-6)
        assert last[serials[0]].x == 0.0
        noise = servicer._build_gameplay_update(None, "dashboard")
        assert {gd.serial: gd.accel.z for gd in noise.controllers}["REAL01"] == pytest.approx(1.0)
        assert adapter.pattern_info(serials[0]).frame_index == 270

    @pytest.mark.uc("UC-046", flow="main", steps=[4], br=["BR-001"])
    def test_stream_rate_pinned_to_the_archetype(self, servicer, adapter, archetype):
        """UC-046 step 4 / FR-103: a stream naming pattern controllers runs at the archetype's rate."""
        serials = [adapter.add_controller(reserved=True) for _ in range(4)]
        adapter.install_pattern(serials, archetype)
        other = adapter.add_controller()
        assert servicer._pattern_stream_rate(set(serials)) == archetype.stream_rate_hz
        assert servicer._pattern_stream_rate({other}) is None
        assert servicer._pattern_stream_rate(None) is None


class TestUC046MockRpcs:
    @pytest.mark.uc("UC-046", flow="A2", br=["BR-003"])
    def test_invalid_archetype_adds_nothing(self, service, adapter):
        """UC-046 A2: invalid parameters are rejected with the failed rules and reserve no controller."""
        rejected = _add_pattern(service, params={**PARAMS, "period_s": 0.0})
        assert not rejected.success
        assert "period_s" in rejected.error
        broken = service.AddControllers(
            controller_manager_mock_pb2.AddControllersRequest(count=4, archetype=ARCHETYPE, archetype_params="{"),
            None,
        )
        assert not broken.success
        assert adapter.controllers == {}

    @pytest.mark.uc("UC-046", flow="main", steps=[3, 4], br=["BR-006"], cond=["Pre-2"])
    def test_add_controllers_installs_the_set(self, service, adapter):
        """UC-046 steps 3-4: AddControllers(archetype) reserves one controller per player and echoes the archetype."""
        resp = _add_pattern(service)
        assert resp.success
        assert (resp.archetype, resp.stream_rate_hz, resp.players) == (ARCHETYPE, 30, 4)
        assert [adapter.pattern_info(s).slot for s in resp.serials] == [0, 1, 2, 3]
        assert all(adapter.controllers[s]["reserved"] for s in resp.serials)
        wrong = _add_pattern(service, count=3)
        assert not wrong.success
        assert "players" in wrong.error

    @pytest.mark.uc("UC-046", flow="main", steps=[6], br=["BR-004"])
    def test_scripted_motion_refused_for_pattern_controllers(self, service, adapter):
        """UC-046 BR-004: SimulateMovement and SimulateDeath are refused for pattern controllers only."""
        serial = _add_pattern(service).serials[0]
        ctx = _Ctx()
        move = service.SimulateMovement(
            controller_manager_mock_pb2.MovementRequest(serial=serial, accel_x=1, accel_y=1, accel_z=1), ctx
        )
        assert not move.success
        assert ctx.code == grpc.StatusCode.FAILED_PRECONDITION
        ctx = _Ctx()
        death = service.SimulateDeath(controller_manager_mock_pb2.DeathRequest(serial=serial), ctx)
        assert not death.success
        assert ctx.code == grpc.StatusCode.FAILED_PRECONDITION
        assert adapter.controllers[serial]["death_accel"] is None
        plain = adapter.add_controller()
        assert service.SimulateDeath(controller_manager_mock_pb2.DeathRequest(serial=plain), _Ctx()).success

    @pytest.mark.uc("UC-046", flow="main", steps=[8], br=["BR-005"])
    @pytest.mark.uc("UC-003", flow="A4", steps=[2])
    def test_remove_controller_returns_replay_record(self, service, adapter, archetype):
        """UC-046 step 8: removing a pattern controller returns frames, signal hash, checkpoints and overruns."""
        serials = list(_add_pattern(service).serials)
        for _ in range(65):
            adapter.pattern_frame(serials[2], "game", explicit=True)
        adapter.record_pattern_overrun("game")
        expected = mp.SignalHasher(archetype.stream_rate_hz)
        for k in range(65):
            expected.update(archetype.value(2, k))
        resp = service.RemoveController(controller_manager_mock_pb2.RemoveControllerRequest(serial=serials[2]), None)
        assert resp.success
        assert (resp.archetype, resp.pattern_slot, resp.frames_delivered, resp.frame_overruns) == (
            ARCHETYPE,
            2,
            65,
            1,
        )
        assert resp.signal_hash == expected.value
        assert list(resp.signal_checkpoints) == expected.checkpoints
        assert len(resp.signal_checkpoints) == 2
        plain = adapter.add_controller()
        plain_resp = service.RemoveController(controller_manager_mock_pb2.RemoveControllerRequest(serial=plain), None)
        assert plain_resp.success and plain_resp.archetype == ""


class TestUC046PatternControllersHidden:
    @pytest.mark.uc("UC-046", cond=["Post-F-1"], br=["BR-004"])
    def test_pattern_controllers_are_always_reserved(self, service, adapter):
        """UC-046 Post-F-1: pattern controllers are always reserved (hidden from the lobby)."""
        resp = service.AddControllers(
            controller_manager_mock_pb2.AddControllersRequest(
                count=4, reserved=False, tag="t", archetype=ARCHETYPE, archetype_params=json.dumps(PARAMS)
            ),
            None,
        )
        assert resp.success
        assert all(adapter.get_metadata(s)["reserved"] for s in resp.serials)
        listed = service.ListMockControllers(controller_manager_mock_pb2.ListRequest(), None)
        assert {c.serial for c in listed.controllers if c.reserved} >= set(resp.serials)
        from services.controller_manager.discovery_loop import visible_serials

        tracked = {s: {"reserved": adapter.get_metadata(s)["reserved"]} for s in resp.serials}
        tracked["LOBBY01"] = {"reserved": False}
        assert visible_serials(tracked) == ["LOBBY01"]

    @pytest.mark.uc("UC-046", flow="A2", br=["BR-003"])
    def test_install_error_is_reported_not_raised(self, service, adapter, monkeypatch):
        """UC-046 A2: an unexpected install error answers success=false like the noise path."""

        def boom(*_a, **_k):
            raise RuntimeError("adapter exploded")

        monkeypatch.setattr(adapter, "install_pattern", boom)
        resp = _add_pattern(service)
        assert not resp.success
        assert "adapter exploded" in resp.error
