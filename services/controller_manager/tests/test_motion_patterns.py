"""UC-046 parametric motion functions: terms, slot modifiers, validation, archetype flags, signal hash."""

import json
import math
import struct
import time
from pathlib import Path

import pytest

from services.controller_manager.multiplexer import motion_patterns as mp

REPO_ROOT = Path(__file__).resolve().parents[3]
FLAG_FILES = (REPO_ROOT / "services/flagd/agent.json", REPO_ROOT / "services/flagd/ci/agent.json")


def _params(**overrides):
    """A neutral 4-player archetype at 30 Hz; overrides set individual fields."""
    p = {
        "players": 4,
        "stream_rate_hz": 30,
        "base": 1.0,
        "drift": 0.0,
        "amp": 0.0,
        "period_s": 2.0,
        "phase": 0.0,
        "burst_h": 0.0,
        "burst_period_s": 0.0,
        "d_base": 0.0,
        "d_drift": 0.0,
        "d_amp": 0.0,
        "d_phase": 0.0,
    }
    p.update(overrides)
    return p


def _archetype(**overrides):
    arch, errors = mp.validate_archetype("unit_test", _params(**overrides))
    assert errors == []
    return arch


def _death_time(arch, slot, threshold, horizon_s):
    """First game time at which the coordinator's EMA (default weight) exceeds threshold, or None."""
    from services.game_coordinator.games.base import BaseGameMode

    class _Player:
        smoothed_accel = 0.0
        last_accel_mag = 0.0

    player = _Player()
    r = arch.stream_rate_hz
    for k in range(int(horizon_s * r)):
        BaseGameMode._update_ema(player, arch.value(slot, k), 4.0)
        if player.smoothed_accel > threshold:
            return k / r
    return None


class TestUC046Terms:
    """BR-002: each term of intensity_i(t) as a golden vector."""

    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-002"])
    def test_base_and_drift(self):
        """UC-046 BR-002: base + drift * t."""
        s = _archetype(base=1.2, drift=0.05).slots[0]
        assert s.value(0.0) == pytest.approx(1.2)
        assert s.value(10.0) == pytest.approx(1.7)

    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-002"])
    def test_sine(self):
        """UC-046 BR-002: amp * sin(2 pi (t / period + phase)), phase in cycles."""
        s = _archetype(amp=0.3, period_s=2.0).slots[0]
        assert s.value(0.0) == pytest.approx(1.0)
        assert s.value(0.5) == pytest.approx(1.3)
        assert s.value(1.5) == pytest.approx(0.7)
        shifted = _archetype(amp=0.3, period_s=2.0, phase=0.25).slots[0]
        assert shifted.value(0.0) == pytest.approx(1.3)

    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-002"])
    def test_burst_is_a_rectangle_of_fixed_width(self):
        """UC-046 BR-002: a 0.2 s pulse of burst_h every burst_period_s, first at (1 - phase) * period."""
        s = _archetype(burst_h=0.8, burst_period_s=5.0).slots[0]
        assert s.burst(0.0) == 0.0  # never at t = 0
        assert s.burst(4.99) == 0.0
        assert s.burst(5.0) == 0.8
        assert s.burst(5.19) == 0.8
        assert s.burst(5.21) == 0.0
        assert s.burst(10.1) == 0.8
        late = _archetype(burst_h=0.8, burst_period_s=5.0, phase=0.5).slots[0]
        assert late.burst(2.4) == 0.0
        assert late.burst(2.5) == 0.8  # (1 - 0.5) * 5 s
        assert _archetype(burst_h=0.0).slots[0].burst(5.0) == 0.0

    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-002"])
    def test_burst_frames_at_each_rate(self):
        """UC-046 BR-002: the 0.2 s width is 3, 6 and 12 frames at 15, 30 and 60 Hz."""
        for rate, frames in ((15, 3), (30, 6), (60, 12)):
            arch = _archetype(stream_rate_hz=rate, burst_h=1.0, burst_period_s=4.0)
            on = [k for k in range(10 * rate) if arch.slots[0].burst(arch.game_time(k)) > 0]
            assert len(on) == 2 * frames, rate  # pulses at 4 s and 8 s

    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-002"])
    def test_output_clamped_to_0_and_10_g(self):
        """UC-046 BR-002: intensity is clamped to 0..10 g whatever the drift."""
        rising = _archetype(drift=1.0).slots[0]
        assert rising.value(100.0) == 10.0
        falling = _archetype(drift=-1.0).slots[0]
        assert falling.value(100.0) == 0.0

    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-001"])
    def test_game_time_is_frame_index_over_rate(self):
        """UC-046 BR-001: frame k carries intensity(k / r)."""
        arch = _archetype(drift=0.1)
        assert arch.game_time(90) == pytest.approx(3.0)
        assert arch.value(0, 90) == pytest.approx(1.3)


class TestUC046SlotModifiers:
    @pytest.mark.uc("UC-046", flow="main", steps=[4], br=["BR-003", "BR-006"])
    def test_modifiers_apply_per_slot(self):
        """UC-046 BR-003: base_i = base + i d_base, drift_i, amp_i likewise, phase_i = frac(phase + i d_phase)."""
        arch = _archetype(base=1.0, d_base=0.1, drift=0.01, d_drift=0.02, amp=0.2, d_amp=0.05, phase=0.5, d_phase=0.25)
        assert [round(s.base, 6) for s in arch.slots] == [1.0, 1.1, 1.2, 1.3]
        assert [round(s.drift, 6) for s in arch.slots] == [0.01, 0.03, 0.05, 0.07]
        assert [round(s.amp, 6) for s in arch.slots] == [0.2, 0.25, 0.3, 0.35]
        assert [round(s.phase, 6) for s in arch.slots] == [0.5, 0.75, 0.0, 0.25]
        assert arch.players == 4

    @pytest.mark.uc("UC-046", flow="main", steps=[6], br=["BR-003", "BR-005"])
    def test_long_game_never_crosses_the_threshold(self):
        """UC-046 BR-003: drift 0 with peaks under 1.8 g never kills: the game is as long as the cap allows."""
        arch = _archetype(amp=0.15, period_s=4.0, d_phase=0.25)
        for slot in range(arch.players):
            assert _death_time(arch, slot, threshold=1.8, horizon_s=3600) is None

    @pytest.mark.uc("UC-046", flow="main", steps=[6], br=["BR-003", "BR-005"])
    def test_cascade_crosses_in_drift_order(self):
        """UC-046 BR-005: positive drifts cross the default 1.8 g threshold in order of drift."""
        arch = _archetype(drift=0.01, d_drift=0.01, amp=0.1, period_s=3.0, d_phase=0.25)
        times = [_death_time(arch, slot, threshold=1.8, horizon_s=300) for slot in range(arch.players)]
        assert all(t is not None for t in times)
        assert times[3] < times[2] < times[1] < times[0]


class TestUC046Validation:
    @pytest.mark.uc("UC-046", flow="main", steps=[4], br=["BR-003"])
    def test_valid_archetype(self):
        """UC-046 BR-003: a well-formed archetype passes; JSON text is accepted too."""
        arch, errors = mp.validate_archetype("unit_test", json.dumps(_params()))
        assert errors == []
        assert arch.canonical_json() == mp.canonical_json(_params())

    @pytest.mark.uc("UC-046", flow="A2", br=["BR-003"])
    @pytest.mark.parametrize(
        ("overrides", "fragment"),
        [
            ({"players": 1}, "players"),
            ({"players": 9}, "players"),
            ({"players": 3.5}, "integer"),
            ({"stream_rate_hz": 25}, "stream_rate_hz"),
            ({"period_s": 0.0}, "period_s"),
            ({"burst_h": 0.5, "burst_period_s": 0.0}, "burst_period_s"),
            ({"phase": 1.0}, "phase"),
            ({"d_phase": -0.1}, "d_phase"),
            ({"base": float("nan")}, "finite"),
            ({"amp": float("inf")}, "finite"),
            ({"drift": 1.5}, "drift"),
            ({"base": 9.8, "d_base": 0.2}, "slot 3 base"),
            ({"amp": True}, "finite"),
            ({"extra": 1}, "unknown"),
        ],
    )
    def test_invalid_archetypes(self, overrides, fragment):
        """UC-046 A2 / BR-003: each bound is enforced with a readable rule."""
        _, errors = mp.validate_archetype("unit_test", _params(**overrides))
        assert errors
        assert any(fragment in e for e in errors), errors

    @pytest.mark.uc("UC-046", flow="A2", br=["BR-003"])
    def test_missing_field_bad_name_and_bad_json(self):
        """UC-046 A2: missing fields, a bad name, non-JSON and non-object parameters are rejected."""
        params = _params()
        del params["d_amp"]
        assert "d_amp is missing" in mp.validate_archetype("unit_test", params)[1]
        assert mp.validate_archetype("Bad-Name", _params())[1]
        assert mp.validate_archetype("unit_test", "{not json")[1]
        assert mp.validate_archetype("unit_test", [1, 2])[1]


class TestUC046ArchetypeFlags:
    """The archetypes shipped as object variants of experiment.mock_archetype (FR-101)."""

    @staticmethod
    def _flag(path, key):
        return json.loads(path.read_text())["flags"][key]

    @pytest.mark.uc("UC-046", flow="main", steps=[1, 2], br=["BR-003", "BR-009"])
    def test_every_shipped_archetype_is_valid(self):
        """UC-046 BR-003: every variant except `off` validates; enough distinct archetypes for verdict_min_pairs."""
        for path in FLAG_FILES:
            flag = self._flag(path, "experiment.mock_archetype")
            assert flag["variants"]["off"] == {}
            assert flag["defaultVariant"] == "off"
            names = [n for n in flag["variants"] if n != "off"]
            assert len(names) >= 5
            for name in names:
                _, errors = mp.validate_archetype(name, flag["variants"][name])
                assert errors == [], (path.name, name, errors)

    @pytest.mark.uc("UC-046", flow="main", steps=[2], br=["BR-003"])
    def test_flag_files_agree_and_select_by_context(self):
        """UC-046 step 2: both flag files carry the same archetypes, selected by the `archetype` context var."""
        prod, ci = (self._flag(p, "experiment.mock_archetype") for p in FLAG_FILES)
        assert prod == ci
        assert prod["targeting"] == {"if": [{"var": "archetype"}, {"var": "archetype"}, "off"]}
        selection = self._flag(FLAG_FILES[0], "experiment.mock_archetypes")
        # Experiments are archetype-only: the default selects every shipped archetype without a
        # trigger policy; a policy archetype (UC-046 BR-018) is selected by name only (#2096).
        assert selection["defaultVariant"] == "all"
        assert "none" not in selection["variants"]
        plain = [n for n, v in prod["variants"].items() if n != "off" and "trigger_policy" not in v]
        assert sorted(selection["variants"]["all"].split(",")) == sorted(plain)
        cap = self._flag(FLAG_FILES[0], "experiment.max_game_seconds")
        assert cap["variants"][cap["defaultVariant"]] == 300

    @pytest.mark.uc("UC-046", flow="main", steps=[6], br=["BR-005"])
    def test_shipped_cascades_kill_organically(self):
        """UC-046 BR-005: slow_cascade kills N-1 players at the default calibration; calm_long kills nobody."""
        variants = self._flag(FLAG_FILES[0], "experiment.mock_archetype")["variants"]
        cascade, _ = mp.validate_archetype("slow_cascade", variants["slow_cascade"])
        times = [_death_time(cascade, s, 1.8, 300) for s in range(cascade.players)]
        assert sum(t is not None for t in times) >= cascade.players - 1
        calm, _ = mp.validate_archetype("calm_long", variants["calm_long"])
        assert all(_death_time(calm, s, 1.8, 600) is None for s in range(calm.players))


def _fnv1a64(data: bytes) -> int:
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


class TestUC046SignalHash:
    def test_reference_fnv_vector(self):
        assert _fnv1a64(b"") == 0xCBF29CE484222325
        assert _fnv1a64(b"a") == 0xAF63DC4C8601EC8C

    @pytest.mark.uc("UC-046", flow="main", steps=[8], br=["BR-005"])
    def test_signal_hash_golden(self):
        """UC-046 step 8: FNV-1a/64 over int32 LE milli-g frames, one checkpoint per r frames."""
        values = [1.0, 1.0005, 2.5, 0.0, 9.9995]
        hasher = mp.SignalHasher(rate=2)
        for v in values:
            hasher.update(v)
        packed = b"".join(struct.pack("<i", mp.milli_g(v)) for v in values)
        assert hasher.value == _fnv1a64(packed)
        assert hasher.hexdigest() == "78aa7bed38c99e4a"
        assert hasher.frames == 5
        assert hasher.checkpoints == [f"{_fnv1a64(packed[:8]):016x}", f"{_fnv1a64(packed[:16]):016x}"]
        assert [mp.milli_g(v) for v in values] == [1000, 1001, 2500, 0, 10000]

    @pytest.mark.uc("UC-046", flow="main", steps=[10], br=["BR-005"])
    def test_same_archetype_same_checkpoints(self):
        """UC-046 step 10 / NFR-040: the same archetype yields identical checkpoints, and a prefix of them."""
        arch = _archetype(drift=0.01, d_drift=0.01, amp=0.2, burst_h=0.5, burst_period_s=3.0, d_phase=0.25)

        def run(frames):
            hashers = [mp.SignalHasher(arch.stream_rate_hz) for _ in range(arch.players)]
            for k in range(frames):
                for slot, h in enumerate(hashers):
                    h.update(arch.value(slot, k))
            return [h.checkpoints for h in hashers]

        long_run, short_run = run(20 * 30), run(12 * 30 + 7)
        assert long_run == run(20 * 30)
        for slot in range(arch.players):
            assert long_run[slot][: len(short_run[slot])] == short_run[slot]
        assert len({tuple(c) for c in long_run}) == arch.players  # slots differ


class TestUC046Performance:
    @pytest.mark.nfr("NFR-044")
    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-002"])
    def test_evaluation_cost_within_budget(self):
        """UC-046 step 5 / NFR-044: one evaluation plus hash update stays well under 50 us here."""
        arch = _archetype(drift=0.01, amp=0.2, burst_h=0.5, burst_period_s=3.0)
        hasher = mp.SignalHasher(arch.stream_rate_hz)
        n = 20_000
        start = time.perf_counter()
        for k in range(n):
            hasher.update(arch.value(1, k))
        per_frame_us = (time.perf_counter() - start) / n * 1e6
        assert per_frame_us < 50.0
        assert math.isfinite(per_frame_us)
