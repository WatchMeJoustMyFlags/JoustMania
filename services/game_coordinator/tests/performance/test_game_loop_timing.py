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
        """Verify low jitter in iteration times.

        Consistent frame timing is important for fair gameplay.
        High jitter means some frames take much longer than others,
        which can cause uneven death detection.
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

            with measure_time() as timer:
                for controller_data in mock_data:
                    await game._process_controller_state(controller_data)

            frame_times.append(timer.elapsed_ms)

        mean_time = statistics.mean(frame_times)
        stdev_time = statistics.stdev(frame_times)
        # Coefficient of variation: stdev/mean (lower = more consistent)
        cv = stdev_time / mean_time if mean_time > 0 else float("inf")

        print(f"\n--- Iteration consistency: {num_controllers} players ---")
        print(f"  Mean:   {mean_time:.3f}ms")
        print(f"  StdDev: {stdev_time:.3f}ms")
        print(f"  CV:     {cv:.3f} (lower = more consistent)")

        # CV should be below 1.0 (stdev < mean), indicating reasonable consistency
        assert cv < 1.0, (
            f"Frame time coefficient of variation ({cv:.3f}) is too high. "
            f"Mean={mean_time:.3f}ms, StdDev={stdev_time:.3f}ms"
        )

    @pytest.mark.asyncio
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

        # Death processing should not exceed 5x normal frame time
        assert overhead_factor < 5.0, (
            f"Death processing overhead ({overhead_factor:.2f}x) is too high. "
            f"Normal={normal_mean:.3f}ms, Violent={violent_mean:.3f}ms"
        )
        # Violent frames should still be within frame budget
        violent_p95 = sorted(violent_times)[int(len(violent_times) * 0.95)]
        assert violent_p95 < MEAN_ITERATION_BUDGET_MS, f"Violent frame P95 ({violent_p95:.3f}ms) exceeds frame budget"
