"""
Motion processing throughput tests.

Verifies that the motion data processing pipeline can sustain
60Hz updates for varying controller counts within the frame budget.

The pipeline under test:
  Controller data -> _process_controller_state() -> EMA filter -> threshold check -> death/warning

These tests exercise the Python game logic directly (no gRPC overhead).
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

# Frame budget at 60fps = 16.67ms
FRAME_BUDGET_MS = 16.67
# Allow occasional frame skip (2 frames = 33.33ms)
DOUBLE_FRAME_BUDGET_MS = 33.33
# Test duration in seconds of simulated game time
TEST_DURATION_SECONDS = 5
# Target frame rate
TARGET_FPS = 60


class TestMotionThroughput:
    """Test motion data processing throughput at 60Hz."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("num_controllers", [4, 8, 10, 18, 32])
    async def test_frame_processing_within_budget(self, num_controllers: int):
        """Process 60 frames/second for N controllers and verify frame times.

        Asserts:
            - P95 frame processing time < 16.67ms (60fps budget)
            - P99 frame processing time < 33ms (allows occasional frame skip)
        """
        game, serials = create_ffa_game(num_controllers)
        result = TimingResult()

        # Warmup: first iterations trigger lazy initialization (OpenFeature, imports)
        for _ in range(20):
            warmup_data = generate_mock_accel(num_controllers, movement="idle")
            for cd in warmup_data:
                await game._process_controller_state(cd)

        total_frames = TARGET_FPS * TEST_DURATION_SECONDS  # 300 frames for 5s at 60fps

        for _ in range(total_frames):
            # Generate controller data with mixed movement patterns
            # Use "random" movement to exercise all code paths
            mock_data = generate_mock_accel(num_controllers, movement="random")

            # Measure time to process all controllers in one frame
            with measure_time() as timer:
                for controller_data in mock_data:
                    await game._process_controller_state(controller_data)

            result.frame_times_ms.append(timer.elapsed_ms)

        result.frame_count = total_frames
        result.total_time_s = sum(result.frame_times_ms) / 1000.0

        # Report results
        print(f"\n--- Throughput: {num_controllers} controllers, {total_frames} frames ---")
        print(f"  Mean frame time:  {result.mean_ms:.3f}ms")
        print(f"  P95 frame time:   {result.p95_ms:.3f}ms")
        print(f"  P99 frame time:   {result.p99_ms:.3f}ms")
        print(f"  Max frame time:   {result.max_ms:.3f}ms")
        print(f"  Total wall time:  {result.total_time_s:.3f}s")

        # Assertions
        assert result.p95_ms < FRAME_BUDGET_MS, (
            f"P95 frame time ({result.p95_ms:.3f}ms) exceeds 60fps budget "
            f"({FRAME_BUDGET_MS}ms) for {num_controllers} controllers"
        )
        assert result.p99_ms < DOUBLE_FRAME_BUDGET_MS, (
            f"P99 frame time ({result.p99_ms:.3f}ms) exceeds double frame budget "
            f"({DOUBLE_FRAME_BUDGET_MS}ms) for {num_controllers} controllers"
        )

    @pytest.mark.asyncio
    async def test_sustained_throughput_10_controllers(self):
        """Verify sustained throughput over a longer period with 10 controllers.

        Runs for 5 seconds of simulated game time at 60Hz (300 frames)
        and checks that the processing can keep up with the stream rate.
        """
        num_controllers = 10
        game, serials = create_ffa_game(num_controllers)
        result = TimingResult()

        # Warmup to avoid cold-start outliers
        for _ in range(20):
            warmup_data = generate_mock_accel(num_controllers, movement="idle")
            for cd in warmup_data:
                await game._process_controller_state(cd)

        total_frames = TARGET_FPS * TEST_DURATION_SECONDS
        frames_over_budget = 0

        for _ in range(total_frames):
            mock_data = generate_mock_accel(num_controllers, movement="random")

            with measure_time() as timer:
                for controller_data in mock_data:
                    await game._process_controller_state(controller_data)

            elapsed_ms = timer.elapsed_ms
            result.frame_times_ms.append(elapsed_ms)

            if elapsed_ms > FRAME_BUDGET_MS:
                frames_over_budget += 1

        result.frame_count = total_frames
        result.total_time_s = sum(result.frame_times_ms) / 1000.0

        over_budget_percent = (frames_over_budget / total_frames) * 100

        print(f"\n--- Sustained throughput: {num_controllers} controllers ---")
        print(f"  Frames over budget: {frames_over_budget}/{total_frames} ({over_budget_percent:.1f}%)")
        print(f"  Mean frame time:    {result.mean_ms:.3f}ms")
        print(f"  P95 frame time:     {result.p95_ms:.3f}ms")

        # Allow up to 5% of frames to exceed the budget
        assert over_budget_percent < 5.0, f"{over_budget_percent:.1f}% of frames exceeded budget (max allowed 5%)"

    @pytest.mark.asyncio
    async def test_idle_controllers_minimal_overhead(self):
        """Verify that idle controllers (no movement) process quickly.

        Idle controllers should be the cheapest to process since they
        will not trigger any warning/death code paths.
        """
        num_controllers = 10
        game, serials = create_ffa_game(num_controllers)
        result = TimingResult()

        # Warmup to avoid cold-start outliers
        for _ in range(20):
            warmup_data = generate_mock_accel(num_controllers, movement="idle")
            for cd in warmup_data:
                await game._process_controller_state(cd)

        total_frames = TARGET_FPS * TEST_DURATION_SECONDS

        for _ in range(total_frames):
            mock_data = generate_mock_accel(num_controllers, movement="idle")

            with measure_time() as timer:
                for controller_data in mock_data:
                    await game._process_controller_state(controller_data)

            result.frame_times_ms.append(timer.elapsed_ms)

        result.frame_count = total_frames

        print(f"\n--- Idle throughput: {num_controllers} controllers ---")
        print(f"  Mean frame time: {result.mean_ms:.3f}ms")
        print(f"  Max frame time:  {result.max_ms:.3f}ms")

        # Idle processing should be well under budget
        assert result.p95_ms < FRAME_BUDGET_MS / 2, (
            f"Idle P95 ({result.p95_ms:.3f}ms) should be under half the frame budget ({FRAME_BUDGET_MS / 2:.2f}ms)"
        )

    @pytest.mark.asyncio
    async def test_throughput_scales_linearly(self):
        """Verify that processing time scales roughly linearly with controller count.

        Doubling controllers should roughly double the processing time,
        not exhibit worse-than-linear scaling.
        """
        # Load-robust (#1958, #2048): the two sizes are timed in interleaved rounds
        # (load hits both alike) and each size keeps its fastest round (min time
        # discards load spikes). A quadratic hot path still gives ~4x for 2x the
        # controllers, so the 3x bound below keeps its teeth.
        rounds = 25
        frames_per_round = 20
        sizes = (4, 8)
        games = {n: create_ffa_game(n)[0] for n in sizes}
        batches = {n: [generate_mock_accel(n, movement="random") for _ in range(frames_per_round)] for n in sizes}
        for n in sizes:  # warmup
            for cd in generate_mock_accel(n, movement="idle"):
                await games[n]._process_controller_state(cd)

        best_ms = {n: float("inf") for n in sizes}
        for _ in range(rounds):
            for n in sizes:
                start = time.perf_counter()
                for frame in batches[n]:
                    for cd in frame:
                        await games[n]._process_controller_state(cd)
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                best_ms[n] = min(best_ms[n], elapsed_ms)

        best_4 = best_ms[4] / frames_per_round
        best_8 = best_ms[8] / frames_per_round

        # Doubling controllers should increase time by at most 3x
        # (allows for overhead; perfect scaling would be 2x)
        scaling_factor = best_8 / best_4 if best_4 > 0 else float("inf")

        print("\n--- Scaling test ---")
        print(f"  4 controllers best round: {best_4:.3f}ms")
        print(f"  8 controllers best round: {best_8:.3f}ms")
        print(f"  Scaling factor:     {scaling_factor:.2f}x (ideal=2.0x)")

        assert scaling_factor < 3.0, (
            f"Processing scales worse than linearly: {scaling_factor:.2f}x for 2x controllers (expected < 3.0x)"
        )
