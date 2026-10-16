"""UC-046 motion-pattern library: functions, set files, validation V1..V9, signal hash."""

import copy
import json
import math
import struct
import time
from pathlib import Path

import pytest

from services.controller_manager.multiplexer import motion_patterns as mp

REPO_ROOT = Path(__file__).resolve().parents[3]


def _seg_constant(from_s, level):
    return {"from_s": from_s, "function": "constant", "level": level}


def _seg_ramp(from_s, start, slope):
    return {"from_s": from_s, "function": "ramp", "start": start, "slope": slope}


def _seg_sine(from_s, mean, amplitude, period_s, phase=0.0):
    return {
        "from_s": from_s,
        "function": "sine",
        "mean": mean,
        "amplitude": amplitude,
        "period_s": period_s,
        "phase": phase,
    }


def _dying(start):
    return [_seg_constant(0, 1.0), _seg_ramp(start, 1.2, 2.0), _seg_constant(start + 1.5, 5.0)]


def _valid_doc():
    """A minimal valid set: two terminating slots and one survivor at 30 Hz, T = 20 s."""
    return {
        "name": "unit_test",
        "version": 1,
        "description": "unit test set",
        "stream_rate_hz": 30,
        "max_game_seconds": 20,
        "slots": [_dying(6.0), _dying(10.0), [_seg_constant(0, 1.0)]],
    }


def _rules(errors):
    return {e.split(":", 1)[0] for e in errors}


class TestUC046Functions:
    """BR-001 / BR-002: game time and the function family, as golden vectors."""

    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-002"])
    def test_constant_golden(self):
        """UC-046 BR-002: constant(level) is the level at every local time."""
        seg = mp.Segment(from_s=2.0, function="constant", params=(1.25,))
        assert [seg.value(t) for t in (2.0, 3.5, 100.0)] == [1.25, 1.25, 1.25]

    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-002"])
    def test_ramp_golden(self):
        """UC-046 BR-002: ramp(start, slope) = start + slope * tau with tau = t - from_s."""
        seg = mp.Segment(from_s=8.0, function="ramp", params=(1.2, 2.0))
        assert seg.value(8.0) == pytest.approx(1.2)
        assert seg.value(8.75) == pytest.approx(2.7)
        assert seg.value(9.5) == pytest.approx(4.2)

    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-002"])
    def test_sine_golden(self):
        """UC-046 BR-002: sine = mean + amplitude * sin(2 pi (tau / period + phase)), phase in cycles."""
        seg = mp.Segment(from_s=4.0, function="sine", params=(1.3, 0.3, 1.5, 0.0))
        assert seg.value(4.0) == pytest.approx(1.3)
        assert seg.value(4.375) == pytest.approx(1.6)  # quarter period: peak
        assert seg.value(5.125) == pytest.approx(1.0)  # three quarters: trough
        shifted = mp.Segment(from_s=4.0, function="sine", params=(1.3, 0.3, 1.5, 0.25))
        assert shifted.value(4.0) == pytest.approx(1.6)  # phase 0.25 cycles starts at the peak

    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-001", "BR-002"])
    def test_segments_switch_at_from_s_and_last_lasts_forever(self):
        """UC-046 BR-002: a segment lasts until the next one starts; the last one lasts forever."""
        slot = mp.SlotPattern(
            segments=(
                mp.Segment(0.0, "constant", (1.0,)),
                mp.Segment(4.0, "ramp", (1.0, 1.0)),
                mp.Segment(6.0, "constant", (5.0,)),
            )
        )
        assert slot.value(3.999) == 1.0
        assert slot.value(4.0) == pytest.approx(1.0)
        assert slot.value(5.0) == pytest.approx(2.0)
        assert slot.value(6.0) == 5.0
        assert slot.value(10_000.0) == 5.0

    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-001"])
    def test_game_time_is_frame_index_over_rate(self):
        """UC-046 BR-001: t = k / r, so frame k of a 30 Hz set always carries f(k / 30)."""
        ps, errors = mp.validate_pattern_set(_valid_doc(), file_stem="unit_test_v1")
        assert errors == []
        assert ps.game_time(0) == 0.0
        assert ps.game_time(210) == pytest.approx(7.0)
        assert ps.value(0, 210) == pytest.approx(1.2 + 2.0 * 1.0)
        assert ps.horizon_frames == 600


class TestUC046Validation:
    """BR-003: one valid and one invalid case per rule."""

    @pytest.mark.uc("UC-046", flow="main", steps=[4], br=["BR-003"])
    def test_valid_set_passes_every_rule(self):
        """UC-046 BR-003: a well-formed set passes V1..V9."""
        ps, errors = mp.validate_pattern_set(_valid_doc(), file_stem="unit_test_v1")
        assert errors == []
        assert ps.set_id == "unit_test_v1"
        assert ps.slot_count == 3

    @pytest.mark.uc("UC-046", flow="A2", br=["BR-003"])
    @pytest.mark.parametrize(
        ("mutate", "stem"),
        [
            (lambda d: d.update(name="Bad-Name"), "unit_test_v1"),
            (lambda d: d.update(version=0), "unit_test_v1"),
            (lambda d: d.update(description=""), "unit_test_v1"),
            (lambda d: d.update(description="x" * 501), "unit_test_v1"),
            (lambda _d: None, "other_name_v1"),
            (lambda d: d.update(extra=1), "unit_test_v1"),
        ],
        ids=["name", "version", "empty-description", "long-description", "file-name", "unknown-key"],
    )
    def test_v1_identity(self, mutate, stem):
        """UC-046 A2 / BR-003 V1: name, version, description and file name are checked."""
        doc = _valid_doc()
        mutate(doc)
        _, errors = mp.validate_pattern_set(doc, file_stem=stem)
        assert "V1" in _rules(errors)

    @pytest.mark.uc("UC-046", flow="A2", br=["BR-003"])
    @pytest.mark.parametrize("slots", [[_dying(6.0)], [[_seg_constant(0, 1.0)]] * 9, [_dying(6.0), []]])
    def test_v2_slot_count_and_segments(self, slots):
        """UC-046 A2 / BR-003 V2: 2 <= N <= 8 and every slot has a segment."""
        doc = _valid_doc()
        doc["slots"] = slots
        _, errors = mp.validate_pattern_set(doc, file_stem="unit_test_v1")
        assert "V2" in _rules(errors)

    @pytest.mark.uc("UC-046", flow="A2", br=["BR-003"])
    @pytest.mark.parametrize("rate", [10, 25, 100, True, "30"])
    def test_v3_rate(self, rate):
        """UC-046 A2 / BR-003 V3: r must be 15, 30 or 60."""
        doc = _valid_doc()
        doc["stream_rate_hz"] = rate
        _, errors = mp.validate_pattern_set(doc, file_stem="unit_test_v1")
        assert "V3" in _rules(errors)

    @pytest.mark.uc("UC-046", flow="A2", br=["BR-003"])
    @pytest.mark.parametrize("horizon", [9, 61, 20.5])
    def test_v4_duration(self, horizon):
        """UC-046 A2 / BR-003 V4: 10 <= T <= 60 seconds."""
        doc = _valid_doc()
        doc["max_game_seconds"] = horizon
        _, errors = mp.validate_pattern_set(doc, file_stem="unit_test_v1")
        assert "V4" in _rules(errors)

    @pytest.mark.uc("UC-046", flow="A2", br=["BR-003"])
    @pytest.mark.parametrize(
        "segment",
        [
            {"from_s": 0, "function": "constant", "level": float("nan")},
            {"from_s": 0, "function": "constant", "level": float("inf")},
            {"from_s": 0, "function": "sine", "mean": 1.0, "amplitude": 0.0, "period_s": 0.0, "phase": 0.0},
            {"from_s": 0, "function": "sine", "mean": 1.0, "amplitude": 0.0, "period_s": 1.0, "phase": 1.0},
            {"from_s": 0, "function": "square", "level": 1.0},
            {"from_s": 0, "function": "constant", "level": 1.0, "slope": 1.0},
            {"from_s": 1, "function": "constant", "level": 1.0},
        ],
        ids=["nan", "inf", "zero-period", "phase-1", "unknown-function", "foreign-param", "first-not-0"],
    )
    def test_v5_finite_parameters(self, segment):
        """UC-046 A2 / BR-003 V5: finite numbers, period > 0, starts from 0 and strictly increase."""
        doc = _valid_doc()
        doc["slots"][2] = [segment]
        _, errors = mp.validate_pattern_set(doc, file_stem="unit_test_v1")
        assert "V5" in _rules(errors)

    @pytest.mark.uc("UC-046", flow="A2", br=["BR-003"])
    def test_v5_starts_strictly_increase(self):
        """UC-046 A2 / BR-003 V5: two segments with the same start are rejected."""
        doc = _valid_doc()
        doc["slots"][2] = [_seg_constant(0, 1.0), _seg_constant(5.0, 1.1), _seg_constant(5.0, 1.2)]
        _, errors = mp.validate_pattern_set(doc, file_stem="unit_test_v1")
        assert "V5" in _rules(errors)

    @pytest.mark.uc("UC-046", flow="A2", br=["BR-003"])
    def test_v6_bounds_checked_at_every_frame(self):
        """UC-046 A2 / BR-003 V6: a ramp that leaves 0..10 g at some frame is rejected."""
        doc = _valid_doc()
        doc["slots"][1] = [_seg_constant(0, 1.0), _seg_ramp(6.0, 1.2, 2.0)]  # reaches 29 g by T
        _, errors = mp.validate_pattern_set(doc, file_stem="unit_test_v1")
        assert "V6" in _rules(errors)

    @pytest.mark.uc("UC-046", flow="A2", br=["BR-003"])
    def test_v7_idle_prelude(self):
        """UC-046 A2 / BR-003 V7: f must be exactly 1.0 g before t = 4 s."""
        doc = _valid_doc()
        doc["slots"][2] = [_seg_constant(0, 1.0), _seg_constant(3.9, 1.1)]
        _, errors = mp.validate_pattern_set(doc, file_stem="unit_test_v1")
        assert "V7" in _rules(errors)
        doc["slots"][2] = [_seg_constant(0, 1.0), _seg_constant(4.0, 1.1)]
        _, errors = mp.validate_pattern_set(doc, file_stem="unit_test_v1")
        assert "V7" not in _rules(errors)

    @pytest.mark.uc("UC-046", flow="A3", br=["BR-003"])
    def test_v8_never_ending_set_rejected(self):
        """UC-046 A3 / BR-003 V8: fewer than N-1 terminating slots is a never-ending set."""
        doc = _valid_doc()
        doc["slots"][1] = [_seg_constant(0, 1.0), _seg_constant(10.0, 3.9)]  # below the 4.0 g floor
        _, errors = mp.validate_pattern_set(doc, file_stem="unit_test_v1")
        assert "V8" in _rules(errors)

    @pytest.mark.uc("UC-046", flow="A3", br=["BR-003"])
    def test_v8_terminal_level_must_hold_from_one_second_before_t(self):
        """UC-046 A3 / BR-003 V8: reaching 4.0 g later than T - 1 s does not terminate."""
        doc = _valid_doc()
        doc["slots"][1] = [_seg_constant(0, 1.0), _seg_constant(19.5, 5.0)]
        _, errors = mp.validate_pattern_set(doc, file_stem="unit_test_v1")
        assert "V8" in _rules(errors)
        doc["slots"][1] = [_seg_constant(0, 1.0), _seg_constant(15.0, 5.0)]
        _, errors = mp.validate_pattern_set(doc, file_stem="unit_test_v1")
        assert "V8" not in _rules(errors)

    @pytest.mark.uc("UC-046", flow="A2", br=["BR-003"])
    def test_v9_death_windows_must_be_separated(self):
        """UC-046 A2 / BR-003 V9: windows closer than 0.5 s are rejected; 1 s apart passes."""
        doc = _valid_doc()
        doc["slots"][1] = _dying(6.3)
        _, errors = mp.validate_pattern_set(doc, file_stem="unit_test_v1")
        assert "V9" in _rules(errors)
        doc["slots"][1] = _dying(7.5)
        _, errors = mp.validate_pattern_set(doc, file_stem="unit_test_v1")
        assert "V9" not in _rules(errors)

    @pytest.mark.uc("UC-046", flow="main", br=["BR-003"])
    def test_death_window_uses_coordinator_ema(self):
        """UC-046 BR-003 V9: the window is [EMA > 1.8 g, EMA > 2.8 g] with weight 4, primed at f(0)."""
        values = [1.0] * 10 + [5.0] * 20
        window = mp.death_window(values)
        ema, crossings = 1.0, {}
        for k, v in enumerate(values):
            if k:
                ema = (ema * 4 + v) / 5
            for g in (1.8, 2.8):
                if ema > g and g not in crossings:
                    crossings[g] = k
        assert window == (crossings[1.8], crossings[2.8])
        assert mp.death_window([1.0] * 30) is None

    @pytest.mark.uc("UC-046", flow="A2", br=["BR-003"])
    def test_load_unknown_and_malformed_sets(self, tmp_path):
        """UC-046 A2: an unknown id or a broken file reports the failed rule, never raises."""
        missing = mp.load_pattern_set("nope_v1", directory=tmp_path)
        assert not missing.found
        assert not missing.valid
        bad_id = mp.load_pattern_set("../etc/passwd", directory=tmp_path)
        assert not bad_id.found
        assert bad_id.errors[0].startswith("V1")
        (tmp_path / "broken_v1.json").write_text("{not json")
        broken = mp.load_pattern_set("broken_v1", directory=tmp_path)
        assert broken.found
        assert not broken.valid
        (tmp_path / "unit_test_v1.json").write_text(json.dumps(_valid_doc()))
        ok = mp.load_pattern_set("unit_test_v1", directory=tmp_path)
        assert ok.valid
        assert ok.pattern_set.set_id == "unit_test_v1"


class TestUC046ShippedSets:
    """The repo's parameter sets (FR-101, FR-102)."""

    @pytest.mark.uc("UC-046", flow="main", steps=[2, 4], br=["BR-003", "BR-009"])
    def test_every_shipped_set_is_valid(self):
        """UC-046 BR-003: CI validates every set file under services/controller_manager/patterns/."""
        ids = mp.list_pattern_set_ids()
        assert len(ids) >= 5, "an experiment needs verdict_min_pairs (5) distinct sets"
        for set_id in ids:
            result = mp.load_pattern_set(set_id)
            assert result.valid, (set_id, result.errors)
            assert result.pattern_set.set_id == set_id

    @pytest.mark.uc("UC-046", flow="main", steps=[6], br=["BR-003", "BR-005"])
    def test_shipped_sets_end_with_one_survivor_at_default_calibration(self):
        """UC-046 BR-005: at the default calibration every shipped set kills exactly N-1 slots, in window order."""
        for set_id in mp.list_pattern_set_ids():
            ps = mp.load_pattern_set(set_id).pattern_set
            windows = [mp.death_window(mp.slot_values(ps, i)) for i in range(ps.slot_count)]
            assert sum(w is not None for w in windows) == ps.slot_count - 1, set_id

    @pytest.mark.uc("UC-046", flow="A3", br=["BR-003"])
    def test_terminal_floor_above_every_thresholds_variant(self):
        """UC-046 BR-003 V8: 4.0 g is above the highest death threshold of every `thresholds` variant."""
        from services.game_coordinator.games.base import resolve_base_thresholds

        for path in (REPO_ROOT / "services/flagd/game.json", REPO_ROOT / "services/flagd/ci/game.json"):
            flag = json.loads(path.read_text())["flags"]["thresholds"]
            for name, variant in flag["variants"].items():
                tables = resolve_base_thresholds(variant)
                highest = max(max(tables["slow_max"]), max(tables["fast_max"]))
                assert highest < mp.TERMINAL_LEVEL_G, (path.name, name, highest)


class TestUC046CoordinatorMirror:
    """The V9 calibration mirrors the game coordinator; these fail on drift."""

    def test_threshold_tables_match_coordinator(self):
        from services.game_coordinator.games import base

        assert tuple(base.SLOW_MAX) == mp.COORDINATOR_SLOW_MAX
        assert tuple(base.FAST_MAX) == mp.COORDINATOR_FAST_MAX

    def test_ema_update_matches_coordinator(self):
        from services.game_coordinator.games.base import BaseGameMode

        class _P:
            smoothed_accel = 0.0
            last_accel_mag = 0.0

        player = _P()
        values = [1.0, 1.0, 3.0, 5.0, 5.0, 2.0]
        mine = []
        ema = values[0]
        for k, v in enumerate(values):
            BaseGameMode._update_ema(player, v, mp.DEFAULT_EMA_WEIGHT)
            if k:
                ema = (ema * mp.DEFAULT_EMA_WEIGHT + v) / (mp.DEFAULT_EMA_WEIGHT + 1)
            mine.append(ema)
            assert player.smoothed_accel == pytest.approx(ema)
        assert mine[-1] == pytest.approx(player.smoothed_accel)


def _fnv1a64(data: bytes) -> int:
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


class TestUC046SignalHash:
    """telemetry.md §9 signal hash."""

    def test_reference_fnv_vector(self):
        assert _fnv1a64(b"") == 0xCBF29CE484222325
        assert _fnv1a64(b"a") == 0xAF63DC4C8601EC8C

    @pytest.mark.uc("UC-046", flow="main", steps=[8], br=["BR-005"])
    def test_signal_hash_golden(self):
        """UC-046 step 8: FNV-1a/64 over int32 LE milli-g frames, with one checkpoint per r frames."""
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
    def test_same_set_same_hash(self):
        """UC-046 step 10: replaying a set's frames gives identical hashes and checkpoints (NFR-040)."""
        ps = mp.load_pattern_set("ffa_cascade_v1").pattern_set
        runs = []
        for _ in range(2):
            hashers = [mp.SignalHasher(ps.stream_rate_hz) for _ in range(ps.slot_count)]
            for k in range(ps.horizon_frames + 1):
                for slot, h in enumerate(hashers):
                    h.update(ps.value(slot, k))
            runs.append([(h.hexdigest(), tuple(h.checkpoints)) for h in hashers])
        assert runs[0] == runs[1]
        assert len({r[0] for r in runs[0]}) > 1  # slots differ from each other


class TestUC046Performance:
    @pytest.mark.uc("UC-046", flow="main", steps=[5], br=["BR-002"])
    def test_evaluation_cost_within_budget(self):
        """UC-046 step 5 / NFR-044: one f evaluation plus hash update stays well under 50 us here."""
        ps = mp.load_pattern_set("ffa_cascade_v1").pattern_set
        hasher = mp.SignalHasher(ps.stream_rate_hz)
        n = 20_000
        start = time.perf_counter()
        for k in range(n):
            hasher.update(ps.value(1, k % (ps.horizon_frames + 1)))
        per_frame_us = (time.perf_counter() - start) / n * 1e6
        assert per_frame_us < 50.0


def test_parse_does_not_mutate_input():
    doc = _valid_doc()
    before = copy.deepcopy(doc)
    mp.validate_pattern_set(doc, file_stem="unit_test_v1")
    assert doc == before
    assert math.isfinite(mp.TERMINAL_LEVEL_G)
