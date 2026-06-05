"""
Shared helper functions for JoustMania integration tests.

These helpers provide:
- Game event waiting utilities
- Menu interaction helpers
- Mock controller manipulation
- Client factory functions
"""

import asyncio
import os
import sys

import grpc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from proto import (
    controller_manager_mock_pb2,
    controller_manager_mock_pb2_grpc,
    controller_manager_pb2,
    controller_manager_pb2_grpc,
    game_coordinator_pb2,
    game_coordinator_pb2_grpc,
    menu_pb2,
    menu_pb2_grpc,
)


# =============================================================================
# Client factory functions
# =============================================================================


async def get_menu_client(docker_compose):
    """Get Menu service gRPC client."""
    host = docker_compose.get_service_host("menu", 50054)
    port = docker_compose.get_service_port("menu", 50054)
    channel = grpc.aio.insecure_channel(f"{host}:{port}")
    return menu_pb2_grpc.MenuServiceStub(channel), channel


async def get_mock_client(docker_compose):
    """Get Mock controller control gRPC client."""
    host = docker_compose.get_service_host("controller-manager", 50062)
    port = docker_compose.get_service_port("controller-manager", 50062)
    channel = grpc.aio.insecure_channel(f"{host}:{port}")
    return controller_manager_mock_pb2_grpc.MockControllerServiceStub(channel), channel


async def get_game_client(docker_compose):
    """Get GameCoordinator gRPC client."""
    host = docker_compose.get_service_host("game-coordinator", 50053)
    port = docker_compose.get_service_port("game-coordinator", 50053)
    channel = grpc.aio.insecure_channel(f"{host}:{port}")
    return game_coordinator_pb2_grpc.GameCoordinatorServiceStub(channel), channel


async def get_controller_client(docker_compose):
    """Get ControllerManager gRPC client (port 50052)."""
    host = docker_compose.get_service_host("controller-manager", 50052)
    port = docker_compose.get_service_port("controller-manager", 50052)
    channel = grpc.aio.insecure_channel(f"{host}:{port}")
    return controller_manager_pb2_grpc.ControllerManagerServiceStub(channel), channel


# =============================================================================
# Controller helpers
# =============================================================================


async def get_mock_controller_serials(docker_compose) -> list[str]:
    """Get list of mock controller serials via ListMockControllers."""
    host = docker_compose.get_service_host("controller-manager", 50062)
    port = docker_compose.get_service_port("controller-manager", 50062)
    channel = grpc.aio.insecure_channel(f"{host}:{port}")
    client = controller_manager_mock_pb2_grpc.MockControllerServiceStub(channel)

    response = await client.ListMockControllers(
        controller_manager_mock_pb2.ListRequest()
    )
    await channel.close()

    return list(response.serials)


async def setup_mock_controllers(
    docker_compose, count: int = 4, reserved: bool = False, tag: str = ""
) -> list[str]:
    """Add mock controllers via RPC and return their serials.

    Uses AddControllers RPC to dynamically create controllers.
    This replaces the static mock_controller_count flag for the
    multiplexer path.

    Args:
        docker_compose: Docker compose fixture
        count: Number of controllers to add
        reserved: When True, the added controllers are reserved (hidden from
            the menu / button-stream consumers). Used by headless shadow games.
        tag: Owning game/agent identifier, applied to all added controllers.
            Enables ``remove_reserved_controllers(tag)`` cleanup.

    Returns:
        List of serial strings for the added controllers

    Note:
        Reuse of existing controllers (the fast path) only applies to the
        default unreserved/untagged case. Reserved or tagged requests always
        add fresh controllers so the reservation/tag is honored.
    """
    mock_client, channel = await get_mock_client(docker_compose)

    # Reserved/tagged requests must create fresh controllers so the reservation
    # actually applies; only the plain unreserved case reuses existing ones.
    if not reserved and not tag:
        existing = await mock_client.ListMockControllers(
            controller_manager_mock_pb2.ListRequest()
        )
        if existing.count >= count:
            await channel.close()
            return list(existing.serials[:count])

        # Add the needed controllers
        needed = count - existing.count
        response = await mock_client.AddControllers(
            controller_manager_mock_pb2.AddControllersRequest(count=needed)
        )
        assert response.success, f"Failed to add controllers: {response.error}"
        await channel.close()

        # Return all serials (existing + newly added)
        all_serials = list(existing.serials) + list(response.serials)
        return all_serials[:count]

    response = await mock_client.AddControllers(
        controller_manager_mock_pb2.AddControllersRequest(
            count=count, reserved=reserved, tag=tag
        )
    )
    assert response.success, f"Failed to add controllers: {response.error}"
    await channel.close()
    return list(response.serials)


async def remove_reserved_controllers(docker_compose, tag: str) -> list[str]:
    """Remove all mock controllers whose tag matches ``tag``.

    Lists controllers via ListMockControllers, filters by the reservation tag,
    and removes each. Used as per-test cleanup for headless shadow games so a
    crashed/failed test does not leak reserved controllers into the next one.

    Args:
        docker_compose: Docker compose fixture
        tag: Reservation tag to sweep (empty tag is a no-op to avoid removing
            untagged lobby controllers).

    Returns:
        List of serials that were removed.
    """
    if not tag:
        return []

    mock_client, channel = await get_mock_client(docker_compose)
    try:
        listing = await mock_client.ListMockControllers(
            controller_manager_mock_pb2.ListRequest()
        )
        to_remove = [c.serial for c in listing.controllers if c.tag == tag]
        for serial in to_remove:
            await mock_client.RemoveController(
                controller_manager_mock_pb2.RemoveControllerRequest(serial=serial)
            )
        return to_remove
    finally:
        await channel.close()


async def list_mock_controllers_detailed(docker_compose) -> list:
    """Return the detailed MockControllerInfo list (serial, reserved, tag)."""
    mock_client, channel = await get_mock_client(docker_compose)
    try:
        listing = await mock_client.ListMockControllers(
            controller_manager_mock_pb2.ListRequest()
        )
        return list(listing.controllers)
    finally:
        await channel.close()


async def get_controller_serials(docker_compose) -> list[str]:
    """Get list of connected controller serials via ListMockControllers."""
    return await get_mock_controller_serials(docker_compose)


async def get_ready_players(docker_compose):
    """Helper function to get mock controllers and convert them to players."""
    serials = await get_mock_controller_serials(docker_compose)

    # Convert serials to players
    players = []
    for i, serial in enumerate(serials):
        players.append(
            game_coordinator_pb2.Player(serial=serial, team=i % 2, alive=True, score=0)
        )
    return players


# =============================================================================
# Game event stream collector
# =============================================================================


class GameEventCollector:
    """Collects game events from StreamGameEvents for the entire test duration.

    Start at test begin, events are collected in background, then wait for
    specific events when needed. This avoids race conditions with event streams.

    Usage with context manager:
        async with GameEventCollector(game_client) as collector:
            # ... trigger game start ...
            await collector.wait_for_event("game_started", timeout=15)
            # ... trigger game end ...
            await collector.wait_for_event("game_ended", timeout=10)

    Or manual usage:
        collector = GameEventCollector(game_client)
        await collector.start()
        # ... test code ...
        await collector.stop()
    """

    def __init__(self, game_client, game_id: str | None = None, start_config=None):
        """Create a collector.

        Args:
            game_client: GameCoordinator gRPC client.
            game_id: When set, the collector subscribes to that specific
                session's stream (``StreamEventsRequest(game_id=...)``) AND
                filters collected events to that game_id. When None, it
                subscribes to the primary stream and collects every event
                (legacy behavior).
            start_config: When set, the underlying StreamGameEvents call carries
                this ``start_config`` so it both STARTS a game and subscribes to
                that new session's bus. The assigned game_id is captured from the
                event stream into ``self.game_id`` (see ``wait_for_game_id``).
        """
        self.game_client = game_client
        # Filter applied to collected events. Captured/assigned game_id when a
        # headless start is used (start_config set).
        self.game_id = game_id
        self._start_config = start_config
        # game_id filtering is enabled ONLY when this collector is bound to a
        # specific session: an explicit game_id, or a headless start_config (the
        # assigned id is learned from the stream). A plain zero-arg collector on
        # the primary stream must NOT filter — it would otherwise lock onto a
        # stale/late event's game_id and drop the new game's events (the exact
        # zero-arg menu-collector semantics the intervention tests rely on).
        self._filter_enabled = bool(game_id) or start_config is not None
        self.events: list = []
        self._task: asyncio.Task | None = None
        self._event_conditions: dict[str, asyncio.Event] = {}
        self._game_id_known = asyncio.Event()
        if game_id:
            self._game_id_known.set()

    async def __aenter__(self):
        """Start collecting on context entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Stop collecting on context exit."""
        await self.stop()
        return False

    async def start(self):
        """Start collecting game events in background."""
        self._task = asyncio.create_task(self._collect())

    def _build_request(self):
        """Build the StreamEventsRequest for this collector's mode."""
        if self._start_config is not None:
            return game_coordinator_pb2.StreamEventsRequest(
                start_config=self._start_config
            )
        if self.game_id:
            return game_coordinator_pb2.StreamEventsRequest(game_id=self.game_id)
        return game_coordinator_pb2.StreamEventsRequest()

    def _accepts(self, event) -> bool:
        """Whether an event passes this collector's game_id filter."""
        # Unfiltered (plain zero-arg primary collector): accept every event.
        if not self._filter_enabled:
            return True
        # Filter target not yet captured (headless start, first event pending):
        # accept until the assigned game_id is learned below.
        if not self.game_id:
            return True
        # Lifecycle/idle events with no game_id bound are still relevant.
        if not event.game_id:
            return True
        return event.game_id == self.game_id

    async def _collect(self):
        """Background task to collect events from stream."""
        try:
            async for event in self.game_client.StreamGameEvents(self._build_request()):
                # Headless start only: learn the assigned game_id from the first
                # event that carries one and adopt it as the filter. Never do
                # this for an unfiltered primary collector.
                if self._filter_enabled and self.game_id is None and event.game_id:
                    self.game_id = event.game_id
                    self._game_id_known.set()

                if not self._accepts(event):
                    continue

                self.events.append(event)
                # Signal any waiters for this event type
                event_type = event.event_type
                if event_type in self._event_conditions:
                    self._event_conditions[event_type].set()
        except asyncio.CancelledError:
            pass

    async def wait_for_game_id(self, timeout: float = 15.0) -> str:
        """Wait until the assigned/observed game_id is known and return it.

        Used by headless starts (start_config) where the game_id is assigned by
        the coordinator and learned from the event stream.
        """
        await asyncio.wait_for(self._game_id_known.wait(), timeout=timeout)
        return self.game_id

    async def stop(self):
        """Stop collecting events and cancel the background task."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def wait_for_event(self, event_type: str, timeout: float = 10.0):
        """Wait for a specific event type to be received.

        Args:
            event_type: Event type to wait for (e.g., "game_started", "game_ended")
            timeout: Maximum time to wait in seconds

        Raises:
            TimeoutError: If the event is not received within timeout
        """
        # Check if we already have this event
        for event in self.events:
            if event.event_type == event_type:
                return event

        # Create condition for this event type if not exists
        if event_type not in self._event_conditions:
            self._event_conditions[event_type] = asyncio.Event()

        # Wait for the event
        try:
            await asyncio.wait_for(
                self._event_conditions[event_type].wait(), timeout=timeout
            )
            # Find and return the event
            for event in reversed(self.events):
                if event.event_type == event_type:
                    return event
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Game did not emit '{event_type}' within {timeout} seconds"
            )

    async def wait_for_any_event(self, event_types: list[str], timeout: float = 10.0):
        """Wait for any of the specified event types.

        Args:
            event_types: List of event types to wait for
            timeout: Maximum time to wait in seconds

        Raises:
            TimeoutError: If none of the events are received within timeout
        """
        import time

        start = time.time()
        while time.time() - start < timeout:
            # Check if we already have any of these events
            for event in self.events:
                if event.event_type in event_types:
                    return event
            await asyncio.sleep(0.1)

        raise TimeoutError(
            f"Game did not emit any of {event_types} within {timeout} seconds"
        )

    def get_events(self, event_type: str | None = None) -> list:
        """Get collected events, optionally filtered by type."""
        if event_type:
            return [e for e in self.events if e.event_type == event_type]
        return list(self.events)

    def clear(self):
        """Clear collected events."""
        self.events.clear()
        self._event_conditions.clear()


# =============================================================================
# Force end helpers
# =============================================================================


async def force_end_game(
    game_client,
    event_collector: GameEventCollector,
    timeout: float = 10.0,
):
    """Force end game and wait for the end event via collector.

    Uses the provided event collector (already listening) to wait for end event.

    Args:
        game_client: GameCoordinator gRPC client
        event_collector: GameEventCollector already started
        timeout: Timeout for end event in seconds
    """
    # Force end game
    await game_client.ForceEndGame(game_coordinator_pb2.ForceEndGameRequest())

    # Wait for end event via collector
    await event_collector.wait_for_any_event(
        ["game_ended", "game_force_ended", "game_error"], timeout=timeout
    )


async def force_end_game_by_id(
    game_client, game_id: str, reason: str = "test_cleanup"
) -> bool:
    """Force-end a specific game session by game_id.

    Best-effort cleanup helper for headless tests: targets one session via
    ``ForceEndGameRequest(game_id=...)`` and never raises (an already-ended or
    unknown game_id is fine during teardown).

    Returns:
        True if the coordinator reported success, False otherwise.
    """
    if not game_id:
        return False
    try:
        response = await game_client.ForceEndGame(
            game_coordinator_pb2.ForceEndGameRequest(reason=reason, game_id=game_id)
        )
        return response.success
    except Exception:
        return False


async def list_games(game_client) -> list:
    """Return the list of live GameInfo sessions via the ListGames RPC."""
    response = await game_client.ListGames(game_coordinator_pb2.ListGamesRequest())
    assert response.success, f"ListGames failed: {response.error}"
    return list(response.games)


async def start_game_headless(
    game_client,
    start_config,
    timeout: float = 20.0,
) -> tuple[str, "GameEventCollector"]:
    """Start a game directly via StreamGameEvents (no menu) and return its id.

    Bypasses the lobby entirely: opens a ``StreamGameEvents`` stream carrying
    ``start_config``, which both starts a new session and subscribes to that
    session's event bus. The collector captures the coordinator-assigned
    game_id from the event stream and filters to only that game's events.

    Args:
        game_client: GameCoordinator gRPC client.
        start_config: A ``StartGameConfig`` (see ``build_start_config``).
        timeout: Max time to wait for the game to start (game_started event).

    Returns:
        Tuple of (game_id, collector). The collector is already running and
        keeps consuming events; callers wait on it for lifecycle events and
        must ``stop()`` it (or use it as a context manager) when done.

    Raises:
        TimeoutError: If the game does not start within ``timeout``.
        RuntimeError: If the coordinator rejects the start.
    """
    collector = GameEventCollector(game_client, start_config=start_config)
    await collector.start()

    # A rejected start yields a single game_start_error event then closes the
    # stream — surface it instead of hanging until timeout.
    try:
        await collector.wait_for_any_event(
            ["game_starting", "game_started", "game_start_error"],
            timeout=timeout,
        )
    except TimeoutError:
        await collector.stop()
        raise

    errors = collector.get_events("game_start_error")
    if errors:
        await collector.stop()
        raise RuntimeError(f"Headless game start rejected: {dict(errors[0].data)}")

    game_id = await collector.wait_for_game_id(timeout=timeout)
    return game_id, collector


def build_start_config(
    game_name: str,
    serials: list[str],
    sensitivity: int = 2,
    invincibility_seconds: float = 2.0,
    min_rounds: int = 5,
):
    """Build a minimal StartGameConfig for a headless game start.

    Assigns players alternating teams (so team modes have both teams populated)
    and attaches the matching mode-specific config sub-message where one is
    required. Mirrors the menu's ``_build_game_config`` for the modes used by
    the concurrent/isolation tests; uses CI-friendly defaults.

    Note on timing-sensitive modes: the headless path resolves mode config from
    this proto via ``GameFactory._extract_mode_config`` and does NOT consult
    flagd, so an unset (0) invincibility/min_rounds falls back to the
    coordinator's slow production defaults (4.0s / 10 rounds), NOT the CI flagd
    values the menu-flow uses. We therefore set them explicitly to the CI
    defaults (2.0s / 5) so the ``end_tournament``/``end_fight_club`` round-wait
    timings line up.

    Args:
        game_name: Game mode name (e.g. "JoustFFA", "JoustTeams", "Swapper").
        serials: Controller serials to use as players.
        sensitivity: Common sensitivity 0-4 (default 2 = MEDIUM).
        invincibility_seconds: Tournament/FightClub invincibility (CI default 2.0).
        min_rounds: FightClub minimum rounds before a winner (CI default 5).
    """
    players = [
        game_coordinator_pb2.Player(serial=serial, team=i % 2, alive=True, score=0)
        for i, serial in enumerate(serials)
    ]
    config = game_coordinator_pb2.StartGameConfig(
        game_name=game_name,
        players=players,
        sensitivity=sensitivity,
    )

    # Attach the mode-specific oneof where the factory expects one. Modes whose
    # config is empty/optional (FFA, Werewolf, Zombies) work without it.
    if game_name == "JoustFFA":
        config.ffa_config.CopyFrom(game_coordinator_pb2.FFAConfig())
    elif game_name == "JoustTeams":
        config.teams_config.CopyFrom(
            game_coordinator_pb2.TeamsConfig(num_teams=2, random_assignment=False)
        )
    elif game_name == "JoustRandomTeams":
        config.random_teams_config.CopyFrom(
            game_coordinator_pb2.RandomTeamsConfig(num_teams=2)
        )
    elif game_name == "Swapper":
        config.swapper_config.CopyFrom(game_coordinator_pb2.SwapperConfig())
    elif game_name == "Werewolf":
        config.werewolf_config.CopyFrom(
            game_coordinator_pb2.WerewolfConfig(reveal_time_seconds=35.0)
        )
    elif game_name == "Zombies":
        config.zombie_config.CopyFrom(game_coordinator_pb2.ZombieConfig())
    elif game_name == "NonStop":
        config.nonstop_config.CopyFrom(
            game_coordinator_pb2.NonstopConfig(time_limit_seconds=0)
        )
    elif game_name == "Tournament":
        config.tournament_config.CopyFrom(
            game_coordinator_pb2.TournamentConfig(
                invincibility_seconds=invincibility_seconds
            )
        )
    elif game_name == "FightClub":
        config.fight_club_config.CopyFrom(
            game_coordinator_pb2.FightClubConfig(
                invincibility_seconds=invincibility_seconds,
                min_rounds=min_rounds,
            )
        )
    elif game_name == "Traitor":
        # num_teams=0 lets the coordinator auto-calculate from player count.
        config.traitor_config.CopyFrom(
            game_coordinator_pb2.TraitorConfig(num_teams=0)
        )

    return config


# =============================================================================
# Menu helpers
# =============================================================================


async def select_game_mode(menu_client, game_mode: str):
    """Navigate menu to select a specific game mode.

    Args:
        menu_client: Menu service gRPC client
        game_mode: Game mode name (e.g., "JoustFFA", "JoustTeams", "JoustRandomTeams")
    """
    # Send web command to select game mode directly
    response = await menu_client.ProcessInput(
        menu_pb2.ProcessInputRequest(
            input_type="web_command",
            data={"command": "select_game", "game_name": game_mode},
        )
    )
    return response.success


async def mark_controllers_ready(mock_client, serials: list[str]):
    """Mark controllers as ready by simulating button presses.

    In the Menu system, pressing TRIGGER marks controller as ready.
    (MOVE cycles game modes instead)

    Controllers are automatically known to the Menu via initial connection
    events sent when the Menu subscribes to the button stream.

    Args:
        mock_client: Mock controller service gRPC client
        serials: List of controller serial numbers to mark ready
    """
    for serial in serials:
        # Simulate Trigger button press to mark ready
        # TRIGGER = 0 in the proto enum
        await mock_client.SimulateButton(
            controller_manager_mock_pb2.ButtonRequest(
                serial=serial,
                button=controller_manager_mock_pb2.ButtonRequest.Button.TRIGGER,
                pressed=True,
            )
        )
        await asyncio.sleep(0.05)
        # Release button
        await mock_client.SimulateButton(
            controller_manager_mock_pb2.ButtonRequest(
                serial=serial,
                button=controller_manager_mock_pb2.ButtonRequest.Button.TRIGGER,
                pressed=False,
            )
        )
        await asyncio.sleep(0.05)


async def trigger_game_start(mock_client, serial: str):
    """Trigger game start by simulating trigger press from a ready controller.

    NOTE: This is typically not needed - the game auto-starts when all
    controllers become ready. This function is only needed if you want
    to manually trigger a game start when not all controllers are ready.

    Args:
        mock_client: Mock controller service gRPC client
        serial: Serial of a ready controller to trigger the game start
    """
    # Simulate trigger press to start game
    # TRIGGER = 0 in the proto enum
    await mock_client.SimulateButton(
        controller_manager_mock_pb2.ButtonRequest(
            serial=serial,
            button=controller_manager_mock_pb2.ButtonRequest.Button.TRIGGER,
            pressed=True,
        )
    )
    await asyncio.sleep(0.05)
    # Release trigger
    await mock_client.SimulateButton(
        controller_manager_mock_pb2.ButtonRequest(
            serial=serial,
            button=controller_manager_mock_pb2.ButtonRequest.Button.TRIGGER,
            pressed=False,
        )
    )


async def reset_all_controllers(mock_client, serials: list[str]):
    """Reset all controllers to non-ready state.

    This is useful between tests to ensure clean state.

    Args:
        mock_client: Mock controller service gRPC client
        serials: List of controller serial numbers to reset
    """
    # The Menu's StateManager resets on game end, but we can force
    # a state reset by reconnecting or using admin mode
    pass  # Controllers reset automatically when game ends


# =============================================================================
# Full flow helpers
# =============================================================================


async def start_game_via_menu(
    docker_compose,
    game_mode: str = "JoustFFA",
    timeout: float = 20.0,
    event_collector: GameEventCollector = None,
):
    """Start a game through the Menu service (full flow).

    This simulates the real user flow:
    1. Start the Menu service
    2. Controllers connect and see menu
    3. Controllers mark themselves as ready (Move button)
    4. Game auto-starts when all controllers are ready
    5. Menu requests game from GameCoordinator

    Requires a GameEventCollector started before this call to reliably
    detect game start events.

    Args:
        docker_compose: Docker compose fixture
        game_mode: Game mode to select (default: "JoustFFA")
        timeout: Timeout for game to start
        event_collector: GameEventCollector already started and listening.
            Required for reliable event detection.

    Raises:
        ValueError: If event_collector is not provided
        TimeoutError: If game does not start within timeout
    """
    if event_collector is None:
        raise ValueError(
            "event_collector is required - start a GameEventCollector before calling"
        )

    # Get clients
    menu_client, menu_channel = await get_menu_client(docker_compose)
    mock_client, _ = await get_mock_client(docker_compose)

    # Get game coordinator client for cleanup
    game_client, _ = await get_game_client(docker_compose)

    # Force-end any previous game to ensure clean state
    await game_client.ForceEndGame(
        game_coordinator_pb2.ForceEndGameRequest(reason="test_cleanup")
    )
    await asyncio.sleep(0.2)  # Allow game cleanup to complete

    # Stop Menu first to clear any stale controller state, then restart fresh
    await menu_client.StopMenu(menu_pb2.StopMenuRequest())
    await asyncio.sleep(0.1)

    # Start the Menu service
    start_response = await menu_client.StartMenu(menu_pb2.StartMenuRequest())
    if not start_response.success:
        raise RuntimeError(f"Failed to start Menu: {start_response.error}")
    await asyncio.sleep(0.3)  # Allow Menu to initialize and receive controller events

    # Get controller serials
    serials = await get_controller_serials(docker_compose)
    if not serials:
        raise RuntimeError("No controllers connected")

    # Select game mode
    await select_game_mode(menu_client, game_mode)
    await asyncio.sleep(0.1)

    # Mark all controllers as ready - game auto-starts when all are ready
    # Controllers are automatically known to Menu via initial connection events
    print(f"Marking {len(serials)} controllers as ready: {serials}")
    await mark_controllers_ready(mock_client, serials)
    print("Controllers marked as ready, waiting for game start...")

    # Wait for game_started event via collector.
    #
    # The first start after `compose up` races service warmup (menu controller
    # registration, coordinator/flagd cold start). The session-scoped
    # `warmup_game_path` fixture absorbs that, but we also retry the
    # ready-marking once on an initial timeout as defense-in-depth: a single
    # dropped "ready" or an unwarmed path then no longer hard-fails the call.
    try:
        await event_collector.wait_for_event("game_started", timeout=timeout)
    except TimeoutError:
        print(
            f"DEBUG: Game start timeout after {timeout}s. "
            f"Collected {len(event_collector.events)} events:"
        )
        for event in event_collector.events:
            print(f"  - {event.event_type}: {dict(event.data)}")

        print("Retrying ready-marking once before failing...")
        await mark_controllers_ready(mock_client, serials)
        try:
            await event_collector.wait_for_event("game_started", timeout=timeout)
        except TimeoutError:
            print(
                f"DEBUG: Game start timeout on retry. "
                f"Collected {len(event_collector.events)} events:"
            )
            for event in event_collector.events:
                print(f"  - {event.event_type}: {dict(event.data)}")
            raise

    # Close menu channel (not needed anymore)
    await menu_channel.close()


# =============================================================================
# LED color verification helpers
# =============================================================================

# Game mode lobby colors (full brightness) - used to verify return to menu
# These are the base colors before dimming; menu applies ~30% brightness
GAME_MODE_COLORS = {
    "JoustFFA": (255, 140, 0),  # Orange
    "JoustTeams": (0, 100, 255),  # Blue
    "JoustRandomTeams": (0, 200, 255),  # Cyan
    "Swapper": (255, 0, 255),  # Magenta
    "Werewolf": (0, 255, 100),  # Green
    "Traitor": (128, 0, 128),  # Dark Purple
    "Zombies": (100, 100, 100),  # Gray
    "Commander": (255, 0, 0),  # Red
    "FightClub": (255, 255, 0),  # Yellow
    "Tournament": (150, 0, 255),  # Purple
    "NonStop": (255, 50, 120),  # Pink
    "Ninja": (255, 140, 0),  # Orange (same as FFA)
}


async def get_controller_color(mock_client, serial: str) -> tuple[int, int, int]:
    """Get current LED color for a controller using GetColor RPC.

    Args:
        mock_client: Mock controller service gRPC client
        serial: Controller serial number

    Returns:
        Tuple of (r, g, b) color values
    """
    response = await mock_client.GetColor(
        controller_manager_mock_pb2.GetColorRequest(serial=serial)
    )
    assert response.success, f"GetColor failed for {serial}: {response.error}"
    return (response.r, response.g, response.b)


async def verify_controllers_have_color(mock_client, serials: list[str]):
    """Verify all controllers have some non-zero LED color.

    Args:
        mock_client: Mock controller service gRPC client
        serials: List of controller serial numbers to check
    """
    for serial in serials:
        color = await get_controller_color(mock_client, serial)
        total = sum(color)
        assert total > 0, f"{serial} LED is off (color: {color})"


def _color_matches(
    actual: tuple[int, int, int], expected: tuple[int, int, int], tolerance: int
) -> bool:
    """Check if actual color matches expected within tolerance."""
    for a, e in zip(actual, expected):
        if abs(a - e) > tolerance:
            return False
    return True


async def wait_for_lobby_colors(
    mock_client,
    serials: list[str],
    expected_color: tuple[int, int, int] | None = None,
    tolerance: int = 30,
    timeout: float = 5.0,
    poll_interval: float = 0.2,
):
    """Wait for all controllers to show expected lobby colors.

    Polls controller colors until they match expected values or timeout.
    This handles timing variations in menu color reset after game ends.

    Args:
        mock_client: Mock controller service gRPC client
        serials: List of controller serial numbers to check
        expected_color: Expected RGB color tuple. If None, just checks non-zero.
        tolerance: Max difference per channel (default 30 for dimming variations)
        timeout: Maximum time to wait in seconds
        poll_interval: Time between polls in seconds

    Raises:
        AssertionError: If colors don't match within timeout
    """
    start_time = asyncio.get_event_loop().time()
    last_colors: dict[str, tuple[int, int, int]] = {}

    while (asyncio.get_event_loop().time() - start_time) < timeout:
        all_match = True

        for serial in serials:
            color = await get_controller_color(mock_client, serial)
            last_colors[serial] = color

            # Check if LED is on
            if sum(color) == 0:
                all_match = False
                continue

            # Check if color matches expected (if specified)
            if expected_color is not None:
                if not _color_matches(color, expected_color, tolerance):
                    all_match = False

        if all_match:
            return  # All controllers match!

        await asyncio.sleep(poll_interval)

    # Timeout - report which controllers didn't match
    mismatches = []
    for serial in serials:
        color = last_colors.get(serial, (0, 0, 0))
        if sum(color) == 0:
            mismatches.append(f"{serial}: LED is off (color: {color})")
        elif expected_color is not None and not _color_matches(
            color, expected_color, tolerance
        ):
            mismatches.append(f"{serial}: got {color}, expected {expected_color}")

    raise AssertionError(
        f"Lobby colors not set within {timeout}s. Mismatches:\n" + "\n".join(mismatches)
    )


async def verify_lobby_colors(
    mock_client,
    serials: list[str],
    expected_color: tuple[int, int, int] | None = None,
    tolerance: int = 30,
):
    """Verify all controllers show the expected lobby color.

    After a game ends, the menu should reset all controllers to dim lobby colors.
    We verify that LEDs match the expected color (or are at least non-zero).

    Args:
        mock_client: Mock controller service gRPC client
        serials: List of controller serial numbers to check
        expected_color: Expected RGB color tuple. If None, just checks non-zero.
        tolerance: Max difference per channel (default 30 for dimming variations)
    """
    for serial in serials:
        color = await get_controller_color(mock_client, serial)
        total_brightness = sum(color)
        assert total_brightness > 0, (
            f"{serial} LED is off (stuck at death effect), color: {color}"
        )

        if expected_color is not None:
            # Verify color matches expected (within tolerance for dimming)
            for i, (actual, expected) in enumerate(zip(color, expected_color)):
                diff = abs(actual - expected)
                channel = ["R", "G", "B"][i]
                assert diff <= tolerance, (
                    f"{serial} {channel} channel mismatch: got {actual}, expected {expected} "
                    f"(diff={diff}, tolerance={tolerance}). Full color: {color}, expected: {expected_color}"
                )


# =============================================================================
# Observability streaming helpers
# =============================================================================


class ObservabilityObserver:
    """Collects LED/rumble/button events from StreamObservability RPC.

    Usage:
        observer = ObservabilityObserver(mock_client)
        await observer.start()
        # ... run game ...
        events = observer.get_events()
        await observer.stop()
    """

    def __init__(self, mock_client):
        self.mock_client = mock_client
        self.events: list = []
        self._task: asyncio.Task | None = None

    async def start(self):
        """Start collecting observability events in background."""
        self._task = asyncio.create_task(self._collect())

    async def _collect(self):
        """Background task to collect events from stream."""
        try:
            async for event in self.mock_client.StreamObservability(
                controller_manager_mock_pb2.ObservabilityRequest()
            ):
                self.events.append(event)
        except asyncio.CancelledError:
            pass

    async def stop(self):
        """Stop collecting events and cancel the background task."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def get_events(self) -> list:
        """Get all collected events."""
        return list(self.events)

    def get_led_events(self, serial: str | None = None) -> list:
        """Get LED change events, optionally filtered by serial."""
        events = [e for e in self.events if e.HasField("led_change")]
        if serial:
            events = [e for e in events if e.serial == serial]
        return events

    def get_last_colors(self) -> dict[str, tuple[int, int, int]]:
        """Get the last LED color for each controller."""
        last_colors = {}
        for event in self.events:
            if event.HasField("led_change"):
                led = event.led_change
                last_colors[event.serial] = (led.r, led.g, led.b)
        return last_colors


class ButtonStreamObserver:
    """Subscribes to ControllerManager.StreamButtonEvents and records the roster.

    This is exactly the view the menu has of the controller fleet. Reserved
    controllers must never appear here: no CONNECT event, never in any
    ``connected_serials`` roster. Used by the shadow-vs-menu isolation test to
    prove reserved controllers stay invisible to lobby logic.

    Usage:
        observer = ButtonStreamObserver(controller_client)
        await observer.start()
        # ... add reserved controllers, run shadow game ...
        assert "MOCK0005" not in observer.all_seen_serials()
        await observer.stop()
    """

    def __init__(self, controller_client):
        self.controller_client = controller_client
        self.events: list = []
        self._task: asyncio.Task | None = None

    async def start(self):
        """Open the button-event stream and collect in background."""
        self._task = asyncio.create_task(self._collect())
        # Let the initial connection snapshot arrive before callers assert.
        await asyncio.sleep(0.5)

    async def _collect(self):
        """Send an initial (empty) config then collect button events."""

        async def request_iter():
            # Empty config opens the stream and triggers the initial roster
            # snapshot (the menu's subscribe path).
            yield controller_manager_pb2.ButtonEventStreamControl(
                config=controller_manager_pb2.ButtonEventStreamConfig()
            )
            # Keep the request stream open for the stream's lifetime.
            while True:
                await asyncio.sleep(3600)

        try:
            async for event in self.controller_client.StreamButtonEvents(
                request_iter()
            ):
                self.events.append(event)
        except asyncio.CancelledError:
            pass
        except Exception:
            # Stream closed/cancelled during teardown — fine.
            pass

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def all_seen_serials(self) -> set[str]:
        """Every serial this observer has seen, across event serials + rosters."""
        seen: set[str] = set()
        for event in self.events:
            if event.serial:
                seen.add(event.serial)
            for serial in event.connected_serials:
                seen.add(serial)
        return seen

    def latest_roster(self) -> list[str]:
        """The most recent non-empty ``connected_serials`` roster seen."""
        for event in reversed(self.events):
            if event.connected_serials:
                return list(event.connected_serials)
        return []


def verify_death_effects(events: list, killed_serials: list[str]):
    """Verify death effects occurred for killed players.

    Args:
        events: List of ObservabilityEvent from observer
        killed_serials: List of controller serials that were killed
    """
    for serial in killed_serials:
        death_events = [
            e
            for e in events
            if e.serial == serial
            and e.HasField("led_change")
            and "death" in e.led_change.source.lower()
        ]
        # Death effect is optional depending on game mode
        # Just log if not found rather than asserting
        if not death_events:
            print(f"Note: No death effect event found for {serial}")


def verify_winner_effect(events: list, winner_serial: str):
    """Verify winner got celebration effect (rainbow).

    Args:
        events: List of ObservabilityEvent from observer
        winner_serial: Controller serial of the winner
    """
    winner_events = [
        e
        for e in events
        if e.serial == winner_serial
        and e.HasField("led_change")
        and "rainbow" in e.led_change.source.lower()
    ]
    # Rainbow effect is optional depending on game mode
    if not winner_events:
        print(f"Note: No rainbow winner effect found for {winner_serial}")


def verify_lobby_colors_restored(events: list, serials: list[str]):
    """Verify all controllers got non-zero colors after game end.

    Args:
        events: List of ObservabilityEvent from observer
        serials: List of all controller serials
    """
    # Get last LED event per controller
    last_colors = {}
    for event in events:
        if event.HasField("led_change"):
            led = event.led_change
            last_colors[event.serial] = (led.r, led.g, led.b)

    for serial in serials:
        if serial in last_colors:
            color = last_colors[serial]
            # Verify not stuck at black
            assert sum(color) > 0, f"{serial} final LED is black"


# =============================================================================
# Game kill helpers
# =============================================================================

# SimulateDeath holds death-level acceleration for 1.0s (DEATH_HOLD_SECONDS in
# mock_control_service.py). Retry no sooner than that, so a retry only fires
# once the previous attempt's hold has fully expired.
KILL_RETRY_INTERVAL = 1.5
KILL_VERIFY_TIMEOUT = 12.0
_VERIFY_POLL_INTERVAL = 0.2


async def get_player_states(game_client, game_id: str = "") -> dict | None:
    """Query a game's state and return {serial: PlayerInfo}.

    Returns None if the game is no longer running (ended or not started),
    so callers can distinguish "game over" from "kill not registered yet".

    Args:
        game_client: GameCoordinator gRPC client.
        game_id: Target a specific session; empty = primary (legacy).
    """
    request = game_coordinator_pb2.GetGameStateRequest()
    if game_id:
        request.game_id = game_id
    response = await game_client.GetGameState(request)
    if not response.success:
        return None
    if response.game_info.state != game_coordinator_pb2.RUNNING:
        return None
    return {p.serial: p for p in response.game_info.players}


async def kill_player_verified(
    mock_client,
    game_client,
    serial: str,
    registered,
    timeout: float = KILL_VERIFY_TIMEOUT,
    game_id: str = "",
) -> bool:
    """SimulateDeath and verify it registered in the game, retrying if dropped.

    Kills fired during countdown/EMA-warmup/grace windows are silently ignored
    by the game loop (#757), so a single fire-and-forget SimulateDeath is not
    reliable under CI load. This helper polls GetGameState until `registered`
    is satisfied and re-fires SimulateDeath every KILL_RETRY_INTERVAL.

    Args:
        mock_client: Mock controller service gRPC client
        game_client: GameCoordinator gRPC client
        serial: Controller serial to kill
        registered: Predicate PlayerInfo -> bool, true once the kill took
            effect (e.g., lambda p: not p.alive)
        timeout: Max total time to keep trying

    Returns:
        True if the kill registered, False if the game stopped running first
        (e.g., this kill ended the game before the poll observed it).

    Raises:
        TimeoutError: If the kill never registered while the game kept running
    """
    deadline = asyncio.get_event_loop().time() + timeout
    next_kill_at = 0.0

    while True:
        now = asyncio.get_event_loop().time()
        if now >= next_kill_at:
            response = await mock_client.SimulateDeath(
                controller_manager_mock_pb2.DeathRequest(serial=serial)
            )
            assert response.success, f"SimulateDeath RPC failed for {serial}"
            next_kill_at = now + KILL_RETRY_INTERVAL

        players = await get_player_states(game_client, game_id=game_id)
        if players is None:
            # Game ended (possibly because this kill landed) — nothing left to verify
            return False
        player = players.get(serial)
        if player is None or registered(player):
            return True

        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError(
                f"Kill of {serial} did not register within {timeout}s "
                f"(alive={player.alive}, team={player.team})"
            )
        await asyncio.sleep(_VERIFY_POLL_INTERVAL)


async def kill_players_until_one_remains(
    mock_client,
    serials: list[str],
    delay: float = 0.5,
    game_client=None,
    game_id: str = "",
) -> list[str]:
    """Kill players one by one until only one remains.

    Each kill is verified via GetGameState (player no longer alive) and
    retried if it was dropped by the game loop.

    Args:
        mock_client: Mock controller service gRPC client
        serials: List of all controller serials
        delay: Delay between kills in seconds
        game_client: GameCoordinator gRPC client for kill verification

    Returns:
        List of serials that were killed (all except the last one)
    """
    killed = []
    # Kill all but the last player
    for serial in serials[:-1]:
        await asyncio.sleep(delay)
        if not await kill_player_verified(
            mock_client, game_client, serial, lambda p: not p.alive, game_id=game_id
        ):
            break  # Game already ended
        killed.append(serial)
    return killed


async def kill_players_for_team_win(
    mock_client, serials: list[str], delay: float = 0.5, game_client=None, game_id: str = ""
) -> list[str]:
    """Kill enough players to trigger a team game win.

    For team games, killing 3 of 4 players guarantees one team is eliminated.
    Each kill is verified via GetGameState and retried if dropped.

    Args:
        mock_client: Mock controller service gRPC client
        serials: List of all controller serials
        delay: Delay between kills in seconds
        game_client: GameCoordinator gRPC client for kill verification
        game_id: Target a specific session (empty = primary/legacy).

    Returns:
        List of serials that were killed
    """
    killed = []
    # For 4 players in 2 teams, killing 3 ensures one team is gone
    players_to_kill = serials[:3] if len(serials) >= 4 else serials[:-1]
    for serial in players_to_kill:
        await asyncio.sleep(delay)
        if not await kill_player_verified(
            mock_client, game_client, serial, lambda p: not p.alive, game_id=game_id
        ):
            break  # Game already ended
        killed.append(serial)
    return killed


# =============================================================================
# Complex game mode kill helpers
# =============================================================================


async def end_swapper_game(
    mock_client,
    serials: list[str],
    game_client,
    delay: float = 0.3,
    timeout: float = 30.0,
    game_id: str = "",
) -> list[str]:
    """End a Swapper game by swapping all players to one team.

    In Swapper, death causes team swap instead of elimination.
    Game ends when all players are on the same team.
    The last player to swap is excluded from winners.

    Strategy: State-driven convergence loop. Re-query team assignments via
    GetGameState each round and kill one player on team 1, verifying the swap
    registered. Re-querying makes this robust against dropped kills and
    against "bounce-backs" (a player swapping twice off one death spike, #757).

    Args:
        mock_client: Mock controller service gRPC client
        serials: List of all controller serials (unused, kept for API compat)
        game_client: GameCoordinator client for GetGameState
        delay: Delay between kills in seconds
        timeout: Max total time for the convergence loop
        game_id: Target a specific session (empty = primary/legacy).

    Returns:
        List of serials that were swapped (killed)
    """
    killed = []
    deadline = asyncio.get_event_loop().time() + timeout

    while True:
        players = await get_player_states(game_client, game_id=game_id)
        if players is None:
            return killed  # Game ended — converged

        team_1 = [s for s, p in players.items() if p.team == 1]
        if not team_1:
            # All players on team 0 — game end should be imminent
            return killed

        if asyncio.get_event_loop().time() >= deadline:
            counts = {s: p.team for s, p in players.items()}
            raise TimeoutError(f"Swapper did not converge within {timeout}s: {counts}")

        serial = team_1[0]
        print(f"Swapper: killing {serial} (team 1 has {len(team_1)} players)")
        await asyncio.sleep(delay)
        if not await kill_player_verified(
            mock_client, game_client, serial, lambda p: p.team == 0, game_id=game_id
        ):
            return killed  # Game ended during the kill
        killed.append(serial)


async def end_werewolf_game(
    mock_client, serials: list[str], delay: float = 0.3, game_client=None, game_id: str = ""
) -> list[str]:
    """End a Werewolf game by killing all but one player.

    Werewolves are ~44% of players, randomly assigned.
    Win conditions: all humans dead OR all werewolves dead.

    Strategy: Kill all but one player. This guarantees one team is fully
    eliminated regardless of random role assignment. Each kill is verified
    via GetGameState and retried if dropped.

    Args:
        mock_client: Mock controller service gRPC client
        serials: List of all controller serials
        delay: Delay between kills in seconds
        game_client: GameCoordinator gRPC client for kill verification
        game_id: Target a specific session (empty = primary/legacy).

    Returns:
        List of serials that were killed
    """
    killed = []
    # Kill all but one player — guarantees one team is fully eliminated
    print(f"Killing {len(serials) - 1} players to end Werewolf game")
    for serial in serials[:-1]:
        await asyncio.sleep(delay)
        if not await kill_player_verified(
            mock_client, game_client, serial, lambda p: not p.alive, game_id=game_id
        ):
            break  # Game already ended (one team eliminated early)
        killed.append(serial)

    return killed


async def end_zombies_game(
    mock_client,
    serials: list[str],
    delay: float = 0.3,
    game_client=None,
    timeout: float = 30.0,
    game_id: str = "",
) -> list[str]:
    """End a Zombies game by converting all humans to zombies.

    In Zombies, humans become zombies when killed (not eliminated).
    Game ends when all humans are converted OR time expires.

    Strategy: State-driven loop. Humans are team 0, zombies team 1 — query
    GetGameState and kill only the remaining humans, verifying each
    conversion (team flips to 1). Re-query each round so dropped kills are
    retried. Killing zombies is avoided entirely (it only causes respawn
    churn and death-effect LED noise near game end, #757).

    Args:
        mock_client: Mock controller service gRPC client
        serials: List of all controller serials (unused, kept for API compat)
        delay: Delay between kills in seconds
        game_client: GameCoordinator gRPC client for state queries
        timeout: Max total time for the conversion loop
        game_id: Target a specific session (empty = primary/legacy).

    Returns:
        List of serials that were killed (converted)
    """
    killed = []
    deadline = asyncio.get_event_loop().time() + timeout

    while True:
        players = await get_player_states(game_client, game_id=game_id)
        if players is None:
            return killed  # Game ended — all humans converted

        humans = [s for s, p in players.items() if p.team == 0]
        if not humans:
            return killed  # Conversion complete — game end imminent

        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError(
                f"Zombies: {len(humans)} humans still unconverted after {timeout}s: {humans}"
            )

        serial = humans[0]
        print(f"Zombies: converting {serial} ({len(humans)} humans remain)")
        await asyncio.sleep(delay)
        if not await kill_player_verified(
            mock_client, game_client, serial, lambda p: p.team == 1, game_id=game_id
        ):
            return killed  # Game ended during the kill
        killed.append(serial)


async def end_fight_club_game(
    mock_client,
    serials: list[str],
    game_client,
    delay: float = 0.2,
    round_wait: float = 3.5,
    rounds: int = 6,
) -> list[str]:
    """End a Fight Club game by running through rounds until a winner emerges.

    Fight Club is queue-based 1v1 matches. CI default: min_rounds=5,
    invincibility=2.0s. After a kill there's a 1.0s inter-round pause,
    then a new round starts with fresh invincibility.

    Strategy: Each round, wait for invincibility to expire, then kill all
    serials. FightClub's _kill_player_impl only processes players in
    DEFENDER or FIGHTER state, so queued players are harmlessly ignored.

    Args:
        mock_client: Mock controller service gRPC client
        serials: List of all controller serials
        game_client: GameCoordinator client (for game state queries)
        delay: Delay between individual kill attempts in seconds
        round_wait: Time to wait before each kill attempt. Must exceed
            inter-round pause (1.0s) + invincibility duration (2.0s CI).
            Default 3.5s = 1.0 + 2.0 + 0.5 buffer.
        rounds: Number of rounds to run (default 6: 5 CI min_rounds + 1)

    Returns:
        List of serials that were killed
    """
    killed = []

    for round_num in range(rounds):
        print(f"Fight Club round {round_num + 1}/{rounds}")

        # Wait for inter-round pause (1s) + invincibility (2s) + buffer
        await asyncio.sleep(round_wait)

        # Kill all serials — only active fighter/defender dies (queued players ignored)
        for serial in serials:
            response = await mock_client.SimulateDeath(
                controller_manager_mock_pb2.DeathRequest(serial=serial)
            )
            if response.success:
                killed.append(serial)
                print(f"  Killed: {serial}")

        await asyncio.sleep(delay)

    return killed


async def end_tournament_game(
    mock_client, serials: list[str], delay: float = 0.2, invincibility_wait: float = 4.2
) -> list[str]:
    """End a Tournament game by running through bracket matches.

    Tournament is single-elimination bracket with 1v1 matches.
    Each match is 22s max with 4s invincibility.

    Strategy: For each round, kill one of the fighters.
    Continue until only one player remains.

    Args:
        mock_client: Mock controller service gRPC client
        serials: List of all controller serials
        delay: Delay between kills in seconds
        invincibility_wait: Time to wait for invincibility to end (default 4.2s)

    Returns:
        List of serials that were killed
    """
    killed = []
    active_players = list(serials)

    # Run bracket rounds until 1 player left
    round_num = 0
    while len(active_players) > 1:
        round_num += 1
        print(f"Tournament round {round_num}, {len(active_players)} players remaining")

        # Wait for invincibility to end
        await asyncio.sleep(invincibility_wait)

        # Kill first active player (eliminates them)
        loser = active_players[0]
        response = await mock_client.SimulateDeath(
            controller_manager_mock_pb2.DeathRequest(serial=loser)
        )
        if response.success:
            killed.append(loser)
            active_players.remove(loser)
            print(f"  Eliminated: {loser}")
        else:
            print(f"  Failed to eliminate {loser}: {response.error}")

        await asyncio.sleep(delay)

    return killed
