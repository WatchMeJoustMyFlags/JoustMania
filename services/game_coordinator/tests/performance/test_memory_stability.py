"""
Memory stability tests for extended game sessions.

Verifies that the motion processing pipeline does not leak memory
over extended periods. Uses tracemalloc to measure Python heap
allocations before and after sustained processing.

Key areas checked for leaks:
- Player state (EMA filter accumulation)
- Controller data processing (protobuf message handling)
- Per-frame threshold calculations
"""

import gc
import sys
import tracemalloc
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
    create_ffa_game,
    generate_mock_accel,
)

# Maximum allowed memory growth in bytes (1MB)
MAX_MEMORY_GROWTH_BYTES = 1 * 1024 * 1024


class TestMemoryStability:
    """Test for memory leaks over extended game sessions."""

    @pytest.mark.asyncio
    async def test_no_memory_leak_extended_session(self):
        """Run 1000+ iterations and verify memory growth < 1MB.

        Simulates a long game session to check for gradual memory leaks
        in the motion processing pipeline.
        """
        num_controllers = 6
        num_iterations = 1500
        game, serials = create_ffa_game(num_controllers)

        # Warm up: run 50 iterations to stabilize allocations
        for _ in range(50):
            mock_data = generate_mock_accel(num_controllers, movement="random")
            for cd in mock_data:
                await game._process_controller_state(cd)

        # Force garbage collection before measurement
        gc.collect()

        # Start tracking memory
        tracemalloc.start()
        snapshot_before = tracemalloc.take_snapshot()

        # Run the extended session
        for _ in range(num_iterations):
            mock_data = generate_mock_accel(num_controllers, movement="random")
            for cd in mock_data:
                await game._process_controller_state(cd)

        # Force garbage collection before final measurement
        gc.collect()

        snapshot_after = tracemalloc.take_snapshot()
        tracemalloc.stop()

        # Calculate memory difference
        stats = snapshot_after.compare_to(snapshot_before, "lineno")
        total_growth = sum(stat.size_diff for stat in stats if stat.size_diff > 0)

        print(f"\n--- Memory stability: {num_controllers} controllers, {num_iterations} iterations ---")
        print(f"  Total memory growth: {total_growth / 1024:.1f}KB")
        print(f"  Budget:              {MAX_MEMORY_GROWTH_BYTES / 1024:.1f}KB")
        print("  Top allocators:")
        for stat in stats[:5]:
            if stat.size_diff > 0:
                print(f"    {stat}")

        assert total_growth < MAX_MEMORY_GROWTH_BYTES, (
            f"Memory grew by {total_growth / 1024:.1f}KB over {num_iterations} iterations, "
            f"exceeding {MAX_MEMORY_GROWTH_BYTES / 1024:.1f}KB budget"
        )

    @pytest.mark.asyncio
    async def test_memory_stable_across_game_restarts(self):
        """Verify memory is reclaimed when creating new game instances.

        Simulates the pattern of game ending and a new game starting
        (as happens in the lobby -> game -> lobby cycle).
        """
        num_controllers = 4
        iterations_per_game = 200
        num_games = 5

        # Warm up
        game, _ = create_ffa_game(num_controllers)
        for _ in range(50):
            mock_data = generate_mock_accel(num_controllers, movement="random")
            for cd in mock_data:
                await game._process_controller_state(cd)
        del game
        gc.collect()

        # Start tracking
        tracemalloc.start()
        snapshot_before = tracemalloc.take_snapshot()

        for _ in range(num_games):
            game, serials = create_ffa_game(num_controllers)

            for _ in range(iterations_per_game):
                mock_data = generate_mock_accel(num_controllers, movement="random")
                for cd in mock_data:
                    await game._process_controller_state(cd)

            # Simulate game cleanup (drop references)
            game.players.clear()
            game.running = False
            del game

        gc.collect()

        snapshot_after = tracemalloc.take_snapshot()
        tracemalloc.stop()

        stats = snapshot_after.compare_to(snapshot_before, "lineno")
        total_growth = sum(stat.size_diff for stat in stats if stat.size_diff > 0)

        print(f"\n--- Memory across {num_games} game restarts ---")
        print(f"  Total memory growth: {total_growth / 1024:.1f}KB")
        print(f"  Budget:              {MAX_MEMORY_GROWTH_BYTES / 1024:.1f}KB")

        assert total_growth < MAX_MEMORY_GROWTH_BYTES, (
            f"Memory grew by {total_growth / 1024:.1f}KB across {num_games} game restarts, "
            f"exceeding {MAX_MEMORY_GROWTH_BYTES / 1024:.1f}KB budget"
        )

    @pytest.mark.asyncio
    async def test_player_state_does_not_accumulate(self):
        """Verify that per-player state (EMA, accel) does not grow unboundedly.

        The EMA filter should be a constant-size computation. This test
        verifies that player objects do not accumulate history.
        """
        num_controllers = 4
        game, serials = create_ffa_game(num_controllers)

        # Get initial size of player objects
        initial_sizes = {}
        for serial, player in game.players.items():
            initial_sizes[serial] = sys.getsizeof(player)

        # Run 1000 iterations
        for _ in range(1000):
            mock_data = generate_mock_accel(num_controllers, movement="random")
            for cd in mock_data:
                await game._process_controller_state(cd)

        # Check sizes after processing
        for serial, player in game.players.items():
            final_size = sys.getsizeof(player)
            growth = final_size - initial_sizes[serial]

            # Player objects should not grow (EMA is in-place)
            assert growth == 0, (
                f"Player {serial} grew by {growth} bytes after 1000 iterations. "
                f"Initial={initial_sizes[serial]}, Final={final_size}"
            )

    @pytest.mark.asyncio
    async def test_high_controller_count_memory(self):
        """Verify memory usage with a high controller count (stress test).

        Tests with 32 controllers to check for any per-controller memory leaks
        that might be masked at lower counts.
        """
        num_controllers = 32
        num_iterations = 500
        game, serials = create_ffa_game(num_controllers)

        # Warm up
        for _ in range(20):
            mock_data = generate_mock_accel(num_controllers, movement="idle")
            for cd in mock_data:
                await game._process_controller_state(cd)

        gc.collect()
        tracemalloc.start()
        snapshot_before = tracemalloc.take_snapshot()

        for _ in range(num_iterations):
            mock_data = generate_mock_accel(num_controllers, movement="random")
            for cd in mock_data:
                await game._process_controller_state(cd)

        gc.collect()
        snapshot_after = tracemalloc.take_snapshot()
        tracemalloc.stop()

        stats = snapshot_after.compare_to(snapshot_before, "lineno")
        total_growth = sum(stat.size_diff for stat in stats if stat.size_diff > 0)

        print(f"\n--- High controller count: {num_controllers} controllers, {num_iterations} iterations ---")
        print(f"  Total memory growth: {total_growth / 1024:.1f}KB")
        print(f"  Per-controller growth: {total_growth / num_controllers / 1024:.2f}KB")

        assert total_growth < MAX_MEMORY_GROWTH_BYTES, (
            f"Memory grew by {total_growth / 1024:.1f}KB with {num_controllers} controllers, "
            f"exceeding {MAX_MEMORY_GROWTH_BYTES / 1024:.1f}KB budget"
        )
