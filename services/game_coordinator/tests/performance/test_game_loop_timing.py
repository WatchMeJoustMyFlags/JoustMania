"""
Game loop iteration timing tests.

Verifies that individual game loop iterations (motion processing
+ death detection + win condition checks) complete within timing
budgets. Tests the full per-frame pipeline as experienced by the
game coordinator during an actual game.

The pipeline per iteration:
  For each controller:
    1. Calculate acceleration magnitude (sqrt)
    2. Apply EMA smoothing filter
    3. Compute LERP thresholds based on music speed
    4. Check grace period, death, and warning conditions
  Then:
    5. Check win condition
"""

import sys
import time
from pathlib import Path

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
tests_dir = test_dir.parent
service_dir = tests_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from helpers import (
    TimingResult,
    create_ffa_game,
    generate_mock_accel,
    measure_time,
)

# Maximum allowed single iteration time (3 frame budgets at 60fps)
MAX_ITERATION_MS = 50.0
# Target mean iteration time (should be well within frame budget)
MEAN_ITERATION_BUDGET_MS = 16.67
# Allowed p99 - p50 frame-time spread: a quarter frame at 60 Hz
JITTER_BUDGET_MS = MEAN_ITERATION_BUDGET_MS / 4


class TestGameLoopTiming:
    """Test per-iteration timing of the game loop."""

    @pytest.mark.asyncio
    async def test_iteration_timing_4_players(self):
        """Measure per-iteration timing with 4 players (typical game size).

        Asserts:
            - Mean iteration time within 16.67ms budget
            - No single iteration exceeds 50ms (3 frame budget)
        """
        num_controllers = 4
        num_iterations = 500
        game, serials = create_ffa_game(num_controllers)
        result = TimingResult()

        # Warmup: first iterations trigger lazy initialization (OpenFeature, imports)
        for _ in range(20):
            warmup_data = generate_mock_accel(num_controllers, movement="idle")
            for cd in warmup_data:
                await game._process_controller_state(cd)

        for _ in range(num_iterations):
            mock_data = generate_mock_accel(num_controllers, movement="random")

            with measure_time() as timer:
                # Process all controller states (the core of each game loop iteration)
                for controller_data in mock_data:
                    await game._process_controller_state(controller_data)

                # Check win condition (runs after every iteration in real game loop)
                await game._check_win_condition()

            result.frame_times_ms.append(timer.elapsed_ms)

        result.frame_count = num_iterations

        print(f"\n--- Game loop timing: {num_controllers} players, {num_iterations} iterations ---")
        print(f"  Mean:   {result.mean_ms:.3f}ms")
        print(f"  P95:    {result.p95_ms:.3f}ms")
        print(f"  P99:    {result.p99_ms:.3f}ms")
        print(f"  Max:    {result.max_ms:.3f}ms")
        print(f"  Min:    {result.min_ms:.3f}ms")

        assert result.mean_ms < MEAN_ITERATION_BUDGET_MS, (
            f"Mean iteration time ({result.mean_ms:.3f}ms) exceeds budget ({MEAN_ITERATION_BUDGET_MS}ms)"
        )
        assert result.max_ms < MAX_ITERATION_MS, (
            f"Max iteration time ({result.max_ms:.3f}ms) exceeds 3-frame limit ({MAX_ITERATION_MS}ms)"
        )

    @pytest.mark.asyncio
    async def test_iteration_timing_18_players(self):
        """Measure per-iteration timing with 18 players (common party size)."""
        num_controllers = 18
        num_iterations = 500
        game, serials = create_ffa_game(num_controllers)
        result = TimingResult()

        for _ in range(20):
            warmup_data = generate_mock_accel(num_controllers, movement="idle")
            for cd in warmup_data:
                await game._process_controller_state(cd)

        for _ in range(num_iterations):
            mock_data = generate_mock_accel(num_controllers, movement="random")
            with measure_time() as timer:
                for controller_data in mock_data:
                    await game._process_controller_state(controller_data)
                await game._check_win_condition()
            result.frame_times_ms.append(timer.elapsed_ms)

        result.frame_count = num_iterations

        print(f"\n--- Game loop timing: {num_controllers} players, {num_iterations} iterations ---")
        print(f"  Mean:   {result.mean_ms:.3f}ms")
        print(f"  P95:    {result.p95_ms:.3f}ms")
        print(f"  P99:    {result.p99_ms:.3f}ms")
        print(f"  Max:    {result.max_ms:.3f}ms")

        assert result.mean_ms < MEAN_ITERATION_BUDGET_MS, (
            f"Mean iteration time ({result.mean_ms:.3f}ms) exceeds budget ({MEAN_ITERATION_BUDGET_MS}ms)"
        )
        assert result.max_ms < MAX_ITERATION_MS, (
            f"Max iteration time ({result.max_ms:.3f}ms) exceeds 3-frame limit ({MAX_ITERATION_MS}ms)"
        )

    @pytest.mark.asyncio
    async def test_iteration_timing_32_players(self):
        """Measure per-iteration timing with 32 players (large event)."""
        num_controllers = 32
        num_iterations = 500
        game, serials = create_ffa_game(num_controllers)
        result = TimingResult()

        for _ in range(20):
            warmup_data = generate_mock_accel(num_controllers, movement="idle")
            for cd in warmup_data:
                await game._process_controller_state(cd)

        for _ in range(num_iterations):
            mock_data = generate_mock_accel(num_controllers, movement="random")
            with measure_time() as timer:
                for controller_data in mock_data:
                    await game._process_controller_state(controller_data)
                await game._check_win_condition()
            result.frame_times_ms.append(timer.elapsed_ms)

        result.frame_count = num_iterations

        print(f"\n--- Game loop timing: {num_controllers} players, {num_iterations} iterations ---")
        print(f"  Mean:   {result.mean_ms:.3f}ms")
        print(f"  P95:    {result.p95_ms:.3f}ms")
        print(f"  P99:    {result.p99_ms:.3f}ms")
        print(f"  Max:    {result.max_ms:.3f}ms")

        assert result.mean_ms < MEAN_ITERATION_BUDGET_MS, (
            f"Mean iteration time ({result.mean_ms:.3f}ms) exceeds budget ({MEAN_ITERATION_BUDGET_MS}ms)"
        )
        assert result.max_ms < MAX_ITERATION_MS, (
            f"Max iteration time ({result.max_ms:.3f}ms) exceeds 3-frame limit ({MAX_ITERATION_MS}ms)"
        )

    @pytest.mark.asyncio
    async def test_iteration_timing_10_players(self):
        """Measure per-iteration timing with 10 players (large game).

        Asserts:
            - Mean iteration time within 16.67ms budget
            - No single iteration exceeds 50ms (3 frame budget)
        """
        num_controllers = 10
        num_iterations = 500
        game, serials = create_ffa_game(num_controllers)
        result = TimingResult()

        # Warmup: first iterations trigger lazy initialization (OpenFeature, imports)
        for _ in range(20):
            warmup_data = generate_mock_accel(num_controllers, movement="idle")
            for cd in warmup_data:
                await game._process_controller_state(cd)

        for _ in range(num_iterations):
            mock_data = generate_mock_accel(num_controllers, movement="random")

            with measure_time() as timer:
                for controller_data in mock_data:
                    await game._process_controller_state(controller_data)

                await game._check_win_condition()

            result.frame_times_ms.append(timer.elapsed_ms)

        result.frame_count = num_iterations

        print(f"\n--- Game loop timing: {num_controllers} players, {num_iterations} iterations ---")
        print(f"  Mean:   {result.mean_ms:.3f}ms")
        print(f"  P95:    {result.p95_ms:.3f}ms")
        print(f"  P99:    {result.p99_ms:.3f}ms")
        print(f"  Max:    {result.max_ms:.3f}ms")

        assert result.mean_ms < MEAN_ITERATION_BUDGET_MS, (
            f"Mean iteration time ({result.mean_ms:.3f}ms) exceeds budget ({MEAN_ITERATION_BUDGET_MS}ms)"
        )
        assert result.max_ms < MAX_ITERATION_MS, (
            f"Max iteration time ({result.max_ms:.3f}ms) exceeds 3-frame limit ({MAX_ITERATION_MS}ms)"
        )

    @pytest.mark.asyncio
    async def test_iteration_consistency(self):
        """Verify that frame-time jitter stays small against the frame budget.

        Consistent frame timing is important for fair gameplay: a slow frame
        delays death detection for every controller in it. What matters is the
        jitter in absolute time relative to the 16.67 ms frame, not relative to
        the (sub-0.1 ms) mean.

        This used to assert a coefficient of variation (stdev / mean) < 1.0.
        At a ~0.05 ms mean a single scheduler preemption dominates the CV, so
        it failed on shared runners and WSL with no real regression (#1283:
        CV 1.196 at mean 0.033 ms). The assertion is now on robust absolute
        statistics: the p99 frame stays well inside the frame budget and the
        p99 - p50 spread stays under a quarter frame. Frames are timed with
        thread CPU time (time.thread_time), so preemption by other processes is
        excluded; wall-clock frame cost is covered by the iteration timing tests.
        """
        import statistics

        num_controllers = 6
        num_iterations = 1000
        game, serials = create_ffa_game(num_controllers)
        frame_times = []

        # Warmup to stabilize lazy initialization
        for _ in range(20):
            warmup_data = generate_mock_accel(num_controllers, movement="idle")
            for cd in warmup_data:
                await game._process_controller_state(cd)

        for _ in range(num_iterations):
            mock_data = generate_mock_accel(num_controllers, movement="gentle")

            # Thread CPU time, not wall time: time spent preempted by other
            # processes on a shared runner is not jitter of the game loop's own
            # work, and under load it alone can approach the jitter budget.
            started = time.thread_time()
            for controller_data in mock_data:
                await game._process_controller_state(controller_data)
            frame_times.append((time.thread_time() - started) * 1000.0)

        ordered = sorted(frame_times)
        p50 = statistics.median(ordered)
        p99 = ordered[int(len(ordered) * 0.99) - 1]
        spread = p99 - p50
        mean_time = statistics.mean(frame_times)
        stdev_time = statistics.stdev(frame_times)
        cv = stdev_time / mean_time if mean_time > 0 else float("inf")

        print(f"\n--- Iteration consistency: {num_controllers} players ---")
        print(f"  P50:    {p50:.3f}ms")
        print(f"  P99:    {p99:.3f}ms (spread p99-p50 {spread:.3f}ms)")
        print(f"  Mean:   {mean_time:.3f}ms StdDev: {stdev_time:.3f}ms CV: {cv:.3f} (informational only)")

        assert p99 < MEAN_ITERATION_BUDGET_MS / 2, (
            f"P99 frame time ({p99:.3f}ms) uses more than half the {MEAN_ITERATION_BUDGET_MS}ms frame budget"
        )
        assert spread < JITTER_BUDGET_MS, (
            f"Frame-time jitter p99-p50 ({spread:.3f}ms) exceeds {JITTER_BUDGET_MS}ms. P50={p50:.3f}ms, P99={p99:.3f}ms"
        )

    @pytest.mark.asyncio
    @pytest.mark.nfr("NFR-004")
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    async def test_iteration_timing_8_players_toggling_the_shield_hold(self):
        """8 players in a hold game toggle the trigger every other frame (#2096, UC-030 BR-008).

        The stream write and the sound RPC each take 20 ms here: a hold transition
        that awaited either would blow the frame budget, so the frame path must
        not await anything for a hold transition (feedback goes to an outbound
        task, UC-040 BR-026).
        """
        import asyncio
        from unittest.mock import patch

        from services.game_coordinator.games.base import ShieldHoldParams

        num_controllers = 8
        num_iterations = 300
        game, serials = create_ffa_game(num_controllers)
        game._shield_hold = ShieldHoldParams(cap=10.0, budget=30.0, initial=10.0, recharge=0.5, min_burst=0.1)
        game._shield_hold_latched = True
        for serial in serials:
            game.players[serial].hold_energy = 10.0
            game.players[serial].hold_energy_at = time.time()

        async def slow(*_args, **_kwargs):
            await asyncio.sleep(0.02)

        game.gameplay_stream.write = slow
        result = TimingResult()
        with patch.object(game, "_play_sound", side_effect=slow):
            for iteration in range(num_iterations):
                pressed = iteration % 2 == 1
                mock_data = generate_mock_accel(num_controllers, movement="gentle")
                for controller_data in mock_data:
                    controller_data.trigger_pressed = pressed
                with measure_time() as timer:
                    for controller_data in mock_data:
                        await game._process_controller_state(controller_data)
                result.frame_times_ms.append(timer.elapsed_ms)

        print(f"\n--- Hold toggling: {num_controllers} players, p99 {result.p99_ms:.3f}ms ---")
        assert any(game.players[s].hold_budget_used > 0 or game.players[s].hold_started_at for s in serials)
        assert result.p99_ms < MEAN_ITERATION_BUDGET_MS / 2, (
            f"P99 frame time ({result.p99_ms:.3f}ms) with hold toggling uses more than half the frame budget"
        )

    @pytest.mark.asyncio
    @pytest.mark.nfr("NFR-004")  # the cost of a death frame, on top of TestDeathDetectionLatency's frame count
    async def test_death_processing_overhead(self):
        """Measure overhead of death processing vs normal frame processing.

        Deaths involve additional work (event publishing, span updates, etc.).
        Verify that death processing does not cause excessive spikes.
        """
        num_controllers = 6
        game, serials = create_ffa_game(num_controllers)

        # Warm up the EMA filter with a few idle frames
        for _ in range(10):
            mock_data = generate_mock_accel(num_controllers, movement="idle")
            for cd in mock_data:
                await game._process_controller_state(cd)

        # Measure normal frame (idle movement)
        normal_times = []
        for _ in range(100):
            mock_data = generate_mock_accel(num_controllers, movement="idle")
            with measure_time() as timer:
                for cd in mock_data:
                    await game._process_controller_state(cd)
            normal_times.append(timer.elapsed_ms)

        # Measure frame with violent movement (may trigger deaths)
        violent_times = []
        for _ in range(100):
            # Re-create game for each violent frame to have alive players
            game_v, _ = create_ffa_game(num_controllers)
            # Warm up EMA
            for _ in range(5):
                idle_data = generate_mock_accel(num_controllers, movement="idle")
                for cd in idle_data:
                    await game_v._process_controller_state(cd)

            mock_data = generate_mock_accel(num_controllers, movement="violent")
            with measure_time() as timer:
                for cd in mock_data:
                    await game_v._process_controller_state(cd)
            violent_times.append(timer.elapsed_ms)

        normal_mean = sum(normal_times) / len(normal_times)
        violent_mean = sum(violent_times) / len(violent_times)
        overhead_factor = violent_mean / normal_mean if normal_mean > 0 else float("inf")

        print("\n--- Death processing overhead ---")
        print(f"  Normal mean:  {normal_mean:.3f}ms")
        print(f"  Violent mean: {violent_mean:.3f}ms")
        print(f"  Overhead:     {overhead_factor:.2f}x")

        # Assert the death cost in absolute time, not as a ratio: at a ~0.04 ms
        # idle frame a 5x ratio is 0.2 ms and a single preemption flips it
        # (seen at 5.05x locally). A death must cost less than a quarter frame.
        death_cost_ms = violent_mean - normal_mean
        assert death_cost_ms < MEAN_ITERATION_BUDGET_MS / 4, (
            f"Death processing adds {death_cost_ms:.3f}ms per frame ({overhead_factor:.2f}x). "
            f"Normal={normal_mean:.3f}ms, Violent={violent_mean:.3f}ms"
        )
        # Violent frames should still be within frame budget
        violent_p95 = sorted(violent_times)[int(len(violent_times) * 0.95)]
        assert violent_p95 < MEAN_ITERATION_BUDGET_MS, f"Violent frame P95 ({violent_p95:.3f}ms) exceeds frame budget"


# ---------------------------------------------------------------------------
# NFR-004: death detection latency (game-side share)
# ---------------------------------------------------------------------------

# Game-loop / gameplay-stream period at the default game_loop.update_frequency_hz (60 Hz, NFR-003)
FRAME_PERIOD_MS = 1000.0 / 60.0
# perception.ema_weight variants: responsive / default / smooth
EMA_WEIGHTS = (2.0, 4.0, 6.0)
STEP_MAGNITUDES_G = (3.0, 4.0)
KILL_REPEATS = 30


def _frames_to_kill_oracle(rest_g: float, step_g: float, weight: float, threshold: float) -> int | None:
    """Closed-form EMA step response, independent of the production loop.

    After priming at ``rest_g`` and n frames at ``step_g`` the smoothed value is
    ``step - (step - rest) * (w / (w + 1)) ** n``; the kill happens on the first
    frame where it exceeds ``threshold``. Returns None when it never does.
    """
    if step_g <= threshold:
        return None
    decay = weight / (weight + 1.0)
    n = 1
    while step_g - (step_g - rest_g) * decay**n <= threshold:
        n += 1
    return n


def _update(serial_accels: dict[str, tuple[float, float, float]]):
    from proto import controller_manager_pb2

    return controller_manager_pb2.GameplayDataUpdate(
        controllers=[
            controller_manager_pb2.GameplayData(
                serial=serial,
                battery=80,
                accel=controller_manager_pb2.Vector3(x=x, y=y, z=z),
                gyro=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=0.0),
            )
            for serial, (x, y, z) in serial_accels.items()
        ]
    )


async def _measure_kill(weight: float, music_speed: float, step_g: float) -> tuple[int, float, float]:
    """Drive one fresh 4-player FFA game through the real per-update entry point.

    One player rests at 1 g (primes the EMA), then receives a constant
    ``step_g`` sample every frame while the others stay at rest. Returns
    (frames until the kill, the death threshold in effect, wall time of the
    kill frame in ms).
    """
    from lib.types import Sensitivity

    game, serials = create_ffa_game(4)
    game.sensitivity = Sensitivity.MEDIUM
    game.music_speed = music_speed
    game._ema_weight = weight
    target = serials[0]
    rest = dict.fromkeys(serials, (0.0, 0.0, 1.0))

    await game._process_gameplay_update(_update(rest))  # prime the EMA at 1 g
    threshold = game._compute_effective_thresholds(game.players[target])[1]

    step = dict(rest)
    step[target] = (0.0, 0.0, step_g)
    for frame in range(1, 600):
        with measure_time() as timer:
            await game._process_gameplay_update(_update(step))
        if not game.players[target].alive:
            return frame, threshold, timer.elapsed_ms
    raise AssertionError(f"no kill after 600 frames (weight={weight}, speed={music_speed}, step={step_g}g)")


@pytest.mark.nfr("NFR-004")
@pytest.mark.uc("UC-030", br=["BR-004"])
class TestDeathDetectionLatency:
    """NFR-004: from the first over-threshold sample to the kill, game side.

    The game coordinator processes each gameplay update as it arrives
    (``_process_gameplay_update``), so its share of the latency is
    ``(frames_to_kill - 1) * frame period + processing time of the kill frame``.
    frames_to_kill is set by the EMA weight and the threshold, and is
    deterministic; the processing time is measured. Not covered here: HID
    sampling (NFR-001), the controller-manager stream tick (up to one frame of
    queueing) and gRPC transport.
    """

    @pytest.mark.asyncio
    async def test_frames_to_kill_match_ema_step_response(self):
        """The production loop kills on exactly the frame the closed-form EMA predicts."""
        from services.game_coordinator.games.base import FAST_MUSIC_SPEED, SLOW_MUSIC_SPEED

        rows = []
        for weight in EMA_WEIGHTS:
            for speed_name, speed in (("slow", SLOW_MUSIC_SPEED), ("fast", FAST_MUSIC_SPEED)):
                for step_g in STEP_MAGNITUDES_G:
                    frames, threshold, _ = await _measure_kill(weight, speed, step_g)
                    expected = _frames_to_kill_oracle(1.0, step_g, weight, threshold)
                    assert frames == expected, (
                        f"weight={weight} {speed_name} step={step_g}g threshold={threshold:.2f}g: "
                        f"killed on frame {frames}, EMA step response predicts {expected}"
                    )
                    rows.append((weight, speed_name, threshold, step_g, frames))

        print("\n--- NFR-004 frames to kill (medium sensitivity, 60 Hz) ---")
        print("  ema_weight  music  threshold  step   frames  game-side latency")
        for weight, speed_name, threshold, step_g, frames in rows:
            latency_ms = (frames - 1) * FRAME_PERIOD_MS
            print(
                f"  {weight:>10.0f}  {speed_name:<5}  {threshold:>7.2f} g  {step_g:.1f} g"
                f"  {frames:>6}  {latency_ms:7.1f} ms"
            )

        # Default configuration documented in NFR-004: weight 4, medium, slow
        # music, 3 g step -> kill on the 3rd frame, 2 frame periods after the
        # first over-threshold sample.
        default = next(r for r in rows if r[0] == 4.0 and r[1] == "slow" and r[3] == 3.0)
        assert default[4] == 3

    @pytest.mark.asyncio
    async def test_kill_frame_processing_time(self):
        """The kill frame itself (EMA, thresholds, _kill_player, win check) costs well under a frame."""
        from services.game_coordinator.games.base import SLOW_MUSIC_SPEED

        kill_frame_ms = []
        for _ in range(KILL_REPEATS):
            for weight in EMA_WEIGHTS:
                _, _, elapsed_ms = await _measure_kill(weight, SLOW_MUSIC_SPEED, 3.0)
                kill_frame_ms.append(elapsed_ms)

        ordered = sorted(kill_frame_ms)
        p50 = ordered[len(ordered) // 2]
        p95 = ordered[int(len(ordered) * 0.95) - 1]
        print(f"\n--- NFR-004 kill-frame processing, 4 players, n={len(ordered)} ---")
        print(f"  P50: {p50:.3f}ms  P95: {p95:.3f}ms  Max: {ordered[-1]:.3f}ms")

        assert p95 < FRAME_PERIOD_MS / 4, f"kill frame P95 {p95:.3f}ms exceeds a quarter frame"
