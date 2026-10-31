"""UC-046 BR-018 (#2096): the simulated trigger policy of pattern controllers.

Validation parity with the agent (shared fixture), the pure ``trigger(slot, k)``,
the per-delivered-frame press on the owning stream only, the signal hash fold and
the reconnect continuity of k.
"""

import json
from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest

from proto import controller_manager_pb2
from services.controller_manager.discovery_loop import DiscoveryLoop
from services.controller_manager.multiplexer import motion_patterns as mp
from services.controller_manager.multiplexer.mock_adapter import MockAdapter
from services.controller_manager.tests.backend_doubles import AdapterAsBackend, stub_method

REPO_ROOT = Path(__file__).resolve().parents[3]
CASES = REPO_ROOT / "services/agent/patterngame/testdata/trigger_policy_cases.json"

# 30 Hz, slot 0 bursts at frames 60-65, 120-125, ...; slot 1 (d_phase 0.5) at 30-35, 90-95, ...
BASE = {
    "players": 2,
    "stream_rate_hz": 30,
    "base": 1.0,
    "drift": 0.0,
    "amp": 0.0,
    "period_s": 2.0,
    "phase": 0.0,
    "burst_h": 3.0,
    "burst_period_s": 2.0,
    "d_base": 0.0,
    "d_drift": 0.0,
    "d_amp": 0.0,
    "d_phase": 0.5,
}
BURST = {"kind": "burst"}
THRESHOLD = {"kind": "threshold", "level": 3.5, "reaction_frames": 2, "hold_frames": 3}


def _archetype(policy=None, **overrides):
    params = {**BASE, **overrides}
    if policy is not None:
        params["trigger_policy"] = policy
    arch, errors = mp.validate_archetype("unit_policy", params)
    assert errors == []
    return arch


class TestUC046TriggerPolicyValidation:
    @pytest.mark.uc("UC-046", flow="main", steps=[2, 4], br=["BR-018", "BR-003"])
    def test_shared_cases_match_the_agent(self):
        """BR-018: the controller manager accepts and rejects exactly the cases the agent's validator does."""
        doc = json.loads(CASES.read_text())
        for case in doc["cases"]:
            params = {**doc["base"], **case.get("params", {})}
            if "policy" in case:
                params["trigger_policy"] = case["policy"]
            arch, errors = mp.validate_archetype("policy_case", params)
            assert (errors == []) == case["valid"], (case["name"], errors)
            if case["valid"]:
                assert arch.has_trigger_policy == case["has_policy"], case["name"]

    @pytest.mark.uc("UC-046", flow="main", steps=[2], br=["BR-018", "BR-003"])
    def test_shipped_policy_archetype_is_valid_and_not_selected_by_default(self):
        """BR-018: burst_holder ships with a policy and stays out of the default selection `all`."""
        for path in (REPO_ROOT / "services/flagd/agent.json", REPO_ROOT / "services/flagd/ci/agent.json"):
            flags = json.loads(path.read_text())["flags"]
            arch, errors = mp.validate_archetype(
                "burst_holder", flags["experiment.mock_archetype"]["variants"]["burst_holder"]
            )
            assert errors == [] and arch.trigger_policy == mp.TriggerPolicy("burst")
            assert "burst_holder" not in flags["experiment.mock_archetypes"]["variants"]["all"].split(",")


class TestUC046TriggerFunction:
    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-018"])
    def test_burst_presses_in_the_burst_windows_only(self):
        """BR-018 burst: pressed exactly while burst_i(k / r) > 0; never at k = 0."""
        arch = _archetype(BURST)
        pressed = [k for k in range(200) if arch.trigger(0, k)]
        assert pressed == [k for s in (60, 120, 180) for k in range(s, s + 6)]
        assert [k for k in range(100) if arch.trigger(1, k)] == [*range(30, 36), *range(90, 96)]
        assert not arch.trigger(0, 0)

    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-018"])
    def test_threshold_lookback(self):
        """BR-018 threshold: pressed at k when some j in [k-reaction-hold+1, k-reaction] reached the level."""
        arch = _archetype(THRESHOLD)
        assert [k for k in range(140) if arch.trigger(0, k)] == [*range(62, 70), *range(122, 130)]
        # Level above every value: never pressed.
        high = _archetype({**THRESHOLD, "level": 4.5})
        assert not any(high.trigger(0, k) for k in range(300))

    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-018"])
    def test_trigger_is_pure_and_none_never_presses(self):
        """BR-018: trigger(slot, k) depends on the archetype and k only, in any evaluation order."""
        arch = _archetype(THRESHOLD)
        forward = [arch.trigger(0, k) for k in range(200)]
        backward = [arch.trigger(0, k) for k in reversed(range(200))][::-1]
        assert forward == backward
        assert not any(_archetype().trigger(0, k) for k in range(200))

    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-018"])
    @pytest.mark.parametrize(
        "policy",
        [BURST, THRESHOLD, {"kind": "threshold", "level": 1.0, "reaction_frames": 0, "hold_frames": 2}],
    )
    def test_runtime_lookback_matches_the_pure_function(self, policy):
        """BR-018: the adapter's incremental evaluation equals trigger(slot, k) on every frame."""
        adapter = MockAdapter()
        serials = [adapter.add_controller(reserved=True) for _ in range(2)]
        arch = _archetype(policy, drift=0.004, amp=0.6)
        adapter.install_pattern(serials, arch)
        for slot, serial in enumerate(serials):
            got = [adapter.pattern_frame(serial, "game", explicit=True)[1] for _ in range(400)]
            assert got == [arch.trigger(slot, k) for k in range(400)]


class TestUC046TriggerDelivery:
    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-018", "BR-001"])
    def test_observer_never_presses_nor_advances(self):
        """BR-018 / BR-001: only the owning stream gets a press; a dashboard stream sees False and leaves k alone."""
        adapter = MockAdapter()
        serial = adapter.add_controller(reserved=True)
        other = adapter.add_controller(reserved=True)
        adapter.install_pattern([serial, other], _archetype(BURST))
        for _ in range(60):
            adapter.pattern_frame(serial, "game", explicit=True)
        assert adapter.pattern_frame(serial, "game", explicit=True)[1] is True  # k = 60
        assert adapter.pattern_frame(serial, "dashboard", explicit=False) == ({"x": 0.0, "y": 0.0, "z": 4.0}, False)
        assert adapter.pattern_info(serial).frame_index == 61

    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-018", "BR-012"])
    def test_death_latch_does_not_change_the_trigger(self):
        """BR-018: the trigger follows intensity(k) from the archetype, not the delivered (latched) value."""
        adapter = MockAdapter()
        serials = [adapter.add_controller(reserved=True) for _ in range(2)]
        arch = _archetype(THRESHOLD)
        adapter.install_pattern(serials, arch)
        controller = adapter.controllers[serials[0]]
        controller["death_accel"] = {"x": 9.0, "y": 0.0, "z": 0.0}
        controller["death_frames_remaining"] = 10
        got = [adapter.pattern_frame(serials[0], "game", explicit=True)[1] for _ in range(10)]
        assert got == [False] * 10  # 9 g latched frames are not intensity >= level

    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-018", "BR-001"])
    def test_reconnect_continues_k_without_repeated_or_skipped_press(self):
        """BR-001 / BR-018: after the owner stream ends the next explicit stream continues k and the press sequence."""
        adapter = MockAdapter()
        serials = [adapter.add_controller(reserved=True) for _ in range(2)]
        arch = _archetype(THRESHOLD)
        adapter.install_pattern(serials, arch)
        first = [adapter.pattern_frame(serials[0], "game", explicit=True)[1] for _ in range(64)]
        adapter.release_pattern_stream("game")
        second = [adapter.pattern_frame(serials[0], "game2", explicit=True)[1] for _ in range(76)]
        assert first + second == [arch.trigger(0, k) for k in range(140)]

    @pytest.mark.uc("UC-046", flow="main", steps=[8], br=["BR-018", "BR-005"])
    def test_hash_folds_the_trigger_only_with_a_policy(self):
        """BR-018 / telemetry §9: a policy folds one trigger byte per frame; without one the hash is main's golden."""

        def run(policy):
            adapter = MockAdapter()
            serials = [adapter.add_controller(reserved=True) for _ in range(2)]
            adapter.install_pattern(serials, _archetype(policy))
            presses = [adapter.pattern_frame(serials[0], "game", explicit=True)[1] for _ in range(90)]
            return presses, adapter.pattern_info(serials[0]).hasher

        presses_a, hash_a = run(BURST)
        presses_b, hash_b = run(BURST)
        assert presses_a == presses_b and any(presses_a)
        assert hash_a.checkpoints == hash_b.checkpoints
        _, plain = run(None)
        assert plain.value != hash_a.value
        arch = _archetype(BURST)
        expected = mp.SignalHasher(30)
        for k in range(90):
            expected.update(arch.value(0, k), arch.trigger(0, k))
        assert hash_a.value == expected.value

    @pytest.mark.uc("UC-046", flow="main", steps=[8], br=["BR-005"])
    def test_hash_of_an_archetype_without_policy_is_unchanged(self):
        """BR-018: the signal hash of an archetype without a policy equals the pre-#2096 golden."""
        arch, errors = mp.validate_archetype(
            "unit_cascade",
            {
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
            },
        )
        assert errors == []
        adapter = MockAdapter()
        serials = [adapter.add_controller(reserved=True) for _ in range(4)]
        adapter.install_pattern(serials, arch)
        for _ in range(90):
            assert adapter.pattern_frame(serials[0], "game", explicit=True)[1] is False
        hasher = adapter.pattern_info(serials[0]).hasher
        assert hasher.hexdigest() == "e66b7b9b63e60a0c"
        assert hasher.checkpoints == ["9e85af39408d1cb1", "59b968e71e14140b", "e66b7b9b63e60a0c"]


class TestUC046TriggerOnTheGameplayStream:
    @pytest.fixture
    def servicer(self):
        adapter = MockAdapter()
        with (
            patch("services.controller_manager.servicer.create_backend", return_value=AdapterAsBackend(adapter)),
            patch("services.controller_manager.servicer.DiscoveryLoop") as loop_cls,
        ):
            loop = create_autospec(DiscoveryLoop, instance=True)
            loop.drain_health_counters.return_value = (0, 0, 0)
            loop.drain_disconnects.return_value = []
            loop.connection_quality.return_value = None
            loop_cls.return_value = loop
            from services.controller_manager.servicer import ControllerManagerServicer

            yield ControllerManagerServicer(), adapter

    @staticmethod
    def _state(serial, _info):
        state = controller_manager_pb2.ControllerState(serial=serial)
        state.accel.CopyFrom(controller_manager_pb2.Vector3(x=0.1, y=0.1, z=1.0))
        return state

    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-018", "BR-001"])
    def test_game_stream_frames_carry_the_policy_press(self, servicer):
        """BR-018: the game stream's frame k sets trigger_pressed = trigger(slot, k); the dashboard never does."""
        servicer, adapter = servicer
        serials = [adapter.add_controller(reserved=True) for _ in range(2)]
        arch = _archetype(BURST)
        adapter.install_pattern(serials, arch)
        stub_method(servicer.state_builder, "build_state", side_effect=self._state)
        for s in serials:
            servicer.tracked_controllers[s] = {}
        game, dashboard = [], []
        for _ in range(130):
            frame = servicer._build_gameplay_update(set(serials), "game")
            game.append({gd.serial: gd.trigger_pressed for gd in frame.controllers})
            frame = servicer._build_gameplay_update(None, "dashboard")
            dashboard.append({gd.serial: gd.trigger_pressed for gd in frame.controllers})
        for slot, serial in enumerate(serials):
            assert [f[serial] for f in game] == [arch.trigger(slot, k) for k in range(130)]
            assert not any(f[serial] for f in dashboard)
