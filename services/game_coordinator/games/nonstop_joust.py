"""
Nonstop Joust Game Mode - Endless respawn with scoring

Players respawn after death, compete for highest score.
Features:
- 3-second respawn countdown
- 2-second spawn protection
- Kill/death/streak tracking
- Time-limited or manual stop

Phase 36b: Refactored to extend BaseGameMode, eliminating ~450 lines of duplicate code.
"""

import logging
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace

from lib.feature_flags import read_float_flag, read_object_flag
from lib.types import GameEvent, Sound
from services.game_coordinator import game_clock, metrics
from services.game_coordinator.games.base import (
    BaseGameMode,
    GameState,
    Phase,
    Player,
    resolve_non_negative_duration,
)
from services.game_coordinator.stream_writes import serialized_write

tracer = trace.get_tracer(__name__)
logger = logging.getLogger(__name__)

# Game constants
# UPDATE_FREQUENCY is now read from runtime_config (Phase 43)
RESPAWN_DURATION = 3.0  # seconds to respawn
SPAWN_PROTECTION_DURATION = 2.0  # seconds of invulnerability after spawn

# Final-score formula: score = max(0, SCORE_BASE - deaths * SCORE_DEATH_PENALTY)
SCORE_BASE = 100  # starting score with zero deaths
SCORE_DEATH_PENALTY = 10  # points lost per death

# The winner is shown for this long after a time-limit end (interruptible by force_end)
WINNER_HOLD_SECONDS = 2.0
WINNER_HOLD_TICK_SECONDS = 0.1


def resolve_scoring(flag_value, default_base: int, default_penalty: int) -> tuple[int, int]:
    """
    Validate the ``nonstop.scoring`` object flag -> ``(base, death_penalty)``.

    Both entries must be non-negative integers; on any malformed value the
    hardcoded module constants are used, keeping the promotion behavior-neutral
    (#766 F2).
    """
    if not isinstance(flag_value, dict):
        return default_base, default_penalty

    def _non_neg_int(value, default: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            return default
        return value if value >= 0 else default

    base = _non_neg_int(flag_value.get("base"), default_base)
    penalty = _non_neg_int(flag_value.get("death_penalty"), default_penalty)
    return base, penalty


@dataclass(frozen=True)
class NonstopInitFlags:
    """The nonstop flags latched at game init, validated and immutable (#2359)."""

    respawn_seconds: float
    spawn_protection_seconds: float
    score_base: int
    score_death_penalty: int

    @classmethod
    def from_raw(cls, *, respawn_seconds: Any, spawn_protection_seconds: Any, scoring: Any) -> "NonstopInitFlags":
        """Validate the raw flag values; a malformed one gives the module constant (fail closed)."""
        base, penalty = resolve_scoring(scoring, SCORE_BASE, SCORE_DEATH_PENALTY)
        return cls(
            respawn_seconds=resolve_non_negative_duration(respawn_seconds, RESPAWN_DURATION),
            spawn_protection_seconds=resolve_non_negative_duration(spawn_protection_seconds, SPAWN_PROTECTION_DURATION),
            score_base=base,
            score_death_penalty=penalty,
        )


@dataclass
class NonstopPlayer(Player):
    """Represents a player in Nonstop Joust with scoring and respawn state."""

    # Scoring (Phase 22)
    kills: int = 0
    deaths: int = 0
    current_streak: int = 0
    best_streak: int = 0
    score: int = 0

    # Respawn state (Phase 22)
    respawn_timer: float = 0.0  # Seconds left until respawn (refreshed each frame from respawn_deadline)
    respawn_deadline: float | None = None  # Game clock time of the respawn; None = no respawn pending
    spawn_protected: bool = False
    spawn_protection_end: float = 0.0
    # The controller disconnected: the death counts, no respawn, no revive (UC-026 A2)
    disconnected: bool = False


class NonstopJoustGame(BaseGameMode):
    """
    Nonstop Joust game mode - endless respawn with scoring.

    Players compete for highest score in time-limited or endless matches.
    Features respawn mechanics, spawn protection, and kill tracking.

    Phase 36b: Extends BaseGameMode to inherit:
    - Span orchestration (run() template method)
    - Common game operations (_load_settings, _countdown, _process_controller_state, etc.)
    - Consistent OpenTelemetry span hierarchy

    Implements Nonstop-specific behavior:
    - NonstopPlayer with scoring fields (kills, deaths, score, streaks)
    - Time-based win condition (time_limit setting)
    - Respawn mechanics (players don't stay dead)
    - Spawn protection (2 seconds after respawn)
    - Player spans stay open (death events only, no span end)
    """

    def __init__(
        self,
        controller_manager_client,
        event_publisher,
        audio_client=None,
        game_id: str = "",
        initial_players: list | None = None,
        sensitivity: int = 2,
        time_limit_seconds: int = 0,
        rng_seed: int = 0,
    ):
        """
        Initialize Nonstop Joust game.

        Args:
            controller_manager_client: gRPC stub for ControllerManager service
            event_publisher: Callback function to publish game events
            audio_client: gRPC stub for Audio service (Phase 29)
            game_id: Unique identifier for this game instance
            initial_players: List of Player protobuf messages from StartGame RPC
            sensitivity: Sensitivity level 0-4 (passed from StartGameConfig)
            time_limit_seconds: Game duration in seconds (0 = unlimited)
        """
        super().__init__(
            controller_manager_client=controller_manager_client,
            event_publisher=event_publisher,
            audio_client=audio_client,
            game_id=game_id,
            initial_players=initial_players,
            sensitivity=sensitivity,
            rng_seed=rng_seed,
        )

        # Nonstop-specific settings - now passed via config
        self.time_limit = time_limit_seconds  # 0 = unlimited, otherwise seconds
        self.players: dict[str, NonstopPlayer] = {}  # Override type hint
        # Set when the game loop detects the time limit (UC-026 BR-003): the only
        # end that declares a winner.
        self._time_limit_reached = False
        # Monotonic clock for the respawn deadline (UC-026 step 2); tests inject a fake.
        self._clock = game_clock.now

        # respawn/spawn-protection durations and scoring weights promoted
        # to game flags. Read ONCE here (init-frozen); malformed values fall back
        # to the module constants (the source of truth).
        self.mode_flags = NonstopInitFlags.from_raw(
            respawn_seconds=read_float_flag("game", "nonstop.respawn_seconds", RESPAWN_DURATION, game_id=self.game_id),
            spawn_protection_seconds=read_float_flag(
                "game", "nonstop.spawn_protection_seconds", SPAWN_PROTECTION_DURATION, game_id=self.game_id
            ),
            scoring=read_object_flag("game", "nonstop.scoring", {}, game_id=self.game_id),
        )
        self.respawn_duration = self.mode_flags.respawn_seconds
        self.spawn_protection_duration = self.mode_flags.spawn_protection_seconds
        self.score_base = self.mode_flags.score_base
        self.score_death_penalty = self.mode_flags.score_death_penalty

    def get_game_name(self) -> str:
        """Return game mode identifier."""
        return "Nonstop Joust"

    async def _initialize_players_impl(self, controllers: list):
        """
        Initialize players using NonstopPlayer with scoring fields.

        Args:
            controllers: List of controller protobuf messages from GetReadyControllers
        """

        for controller in controllers:
            player = NonstopPlayer(
                serial=controller.serial,
                team=0,
                alive=True,
                color=(255, 255, 255),
            )
            self.players[controller.serial] = player
            logger.debug(f"Added player: {controller.serial}")

    def _create_player_spans(self, game_context):
        """
        Create flat player lifecycle spans (stay open for entire game).

        Nonstop Joust spans don't end on death - players respawn.

        Args:
            game_context: Parent span context for proper hierarchy
        """
        for serial, player in self.players.items():
            player.span = self._create_player_lifecycle_span(serial, game_context)
            logger.debug(f"Started lifecycle span for player {serial} (stays open)")

    async def _check_win_condition(self) -> bool:
        """
        Check if time limit has been reached.

        Returns:
            True if game should end (time limit reached), False otherwise
        """
        if self.time_limit == 0:
            return False  # Unlimited mode

        if not self.start_time:
            return False

        elapsed = game_clock.now() - self.start_time
        if elapsed >= self.time_limit:
            logger.info(f"Time limit reached: {elapsed:.1f}s / {self.time_limit}s")
            return True

        return False

    async def _process_controller_state(self, controller_state):
        """
        Process controller state with spawn protection check.

        Override parent to skip death checks during spawn protection.

        Args:
            controller_state: ControllerState protobuf message
        """
        serial = controller_state.serial

        if serial not in self.players:
            return  # Unknown controller

        player = self.players[serial]

        if not player.alive:
            return  # Dead player, ignore

        # Skip death checks during spawn protection (Nonstop-specific)
        if player.spawn_protected:
            return

        # Call parent for standard acceleration processing
        await super()._process_controller_state(controller_state)

    def _player_death_event_attributes(self, serial: str) -> dict:
        """NonStop attributes on the base kill path's player_death event (#1327).

        Recorded before ``_kill_player_impl`` counts the death, so ``deaths``
        includes this one (the same value the event-bus ``player_death`` carries).
        """
        player = self.players[serial]
        return {
            "kills": player.kills,
            "deaths": player.deaths + 1,
            "score": player.score,
            # A respawn death keeps the player in the game; a disconnect takes it out (BR-004).
            "death.eliminated": player.disconnected,
        }

    def _kill_records_elimination_order(self, serial: str) -> bool:
        """Only a disconnect death is an elimination; a respawn death is not (UC-026 BR-004, #2524)."""
        return self.players[serial].disconnected

    async def _kill_player_impl(self, serial: str, accel_mag: float):
        """
        Kill a player and start respawn timer (DON'T end span).

        Nonstop Joust players respawn, so their lifecycle spans stay open.

        Args:
            serial: Controller serial number
            accel_mag: Acceleration magnitude that caused death
        """
        player = self.players[serial]
        player.alive = False
        player.deaths += 1
        player.current_streak = 0
        if player.disconnected:
            # A disconnect is a death without respawn (UC-026 A2)
            player.respawn_timer = 0.0
            player.respawn_deadline = None
        else:
            player.respawn_timer = self.respawn_duration
            # Wall-clock deadline, not a frame count (UC-026 step 2); 0 respawns on the next frame
            player.respawn_deadline = self._clock() + self.respawn_duration

        logger.info(f"Player died: {serial} (kills: {player.kills}, deaths: {player.deaths}, score: {player.score})")

        # The span stays open (respawn). The player_death span event (with kills /
        # deaths / score, via _player_death_event_attributes) was recorded by the
        # base kill path; the event-bus publish below is a separate channel.

        # Publish death event (unique to Nonstop)
        await self.event_publisher(
            "player_death",
            {
                "serial": serial,
                "accel_magnitude": accel_mag,
                "kills": player.kills,
                "deaths": player.deaths,
                "score": player.score,
            },
        )

    async def _handle_disconnect(self, serial: str) -> None:
        """A disconnect is one death and the player does not respawn (UC-026 A2, #1591, #1971).

        The flag is set before the base kill path runs, so ``_kill_player_impl``
        starts no respawn. A player already waiting to respawn gets no second
        death: the pending respawn is cancelled.
        """
        player = self.players.get(serial)
        if player is None or player.disconnected:
            return
        player.disconnected = True
        # No spawn-protection expiry (white LED write) for a player who is gone
        player.spawn_protected = False
        if player.alive:
            await super()._handle_disconnect(serial)
            return

        logger.info(f"Player disconnected while waiting to respawn: {serial} (respawn cancelled)")
        player.respawn_deadline = None
        player.respawn_timer = 0.0
        # Out for good without a second death: the disconnect is the elimination (BR-004).
        # Not once the outcome is latched or the game was force-ended, as for a kill.
        if self._kill_gate_open(serial):
            self._record_elimination_order(serial)
        if player.span:
            player.span.add_event("player_disconnected", attributes={"reason": "controller_disconnect"})

    def _build_initial_stream_control(self, update_frequency_hz: int):
        """Build the initial GameplayStreamControl for the dynamic-filter stream.

        Mirrors the base class: the initial colors list carries the player set
        (serials are derived from it server-side); the alive-set filter is sent
        separately via FilterUpdate (Phase 45 dynamic filtering).

        Regression guard: this construction used the proto field ``serials``,
        which controller_manager.proto removed and reserved ("use colors
        instead") — every NonStop game crashed at loop startup with 'Protocol
        message GameplayStreamConfig has no "serials" field'. The crash hid for
        a long time because unit tests injected a mock stream past this block,
        the parallel lifecycle batch accepted ``game_error`` as a terminal
        event, and the intervention manager evaluated against the dead game.
        Extracted as a method so the real message construction is unit-tested.
        """
        from proto import controller_manager_pb2

        player_colors = [
            controller_manager_pb2.ControllerColorConfig(
                serial=serial,
                color=controller_manager_pb2.RGB(r=player.color[0], g=player.color[1], b=player.color[2]),
            )
            for serial, player in self.players.items()
        ]
        return controller_manager_pb2.GameplayStreamControl(
            config=controller_manager_pb2.GameplayStreamConfig(
                update_frequency_hz=update_frequency_hz,
                colors=player_colors,
            )
        )

    async def _game_loop(self):
        """Override game loop to add respawn timer updates and dynamic filtering."""
        logger.info("Starting Nonstop game loop with respawn mechanics and dynamic filtering...")

        try:
            from proto import controller_manager_pb2

            # Player spans were created by the base _run_gameplay_phase (under "players").
            # Get runtime config (Phase 43: Dynamic Hz adjustment like base class)
            from services.game_coordinator.runtime_config import get_config_manager

            config = get_config_manager().get_config()
            update_frequency_hz = config.update_frequency_hz

            logger.info(f"Starting Nonstop game loop at {update_frequency_hz}Hz")

            # Create bidirectional stream (Phase 45 - dynamic filtering, Phase 46 - feedback commands)
            countdown_stream = self.gameplay_stream
            self.gameplay_stream = self.controller_client.StreamGameplayData()

            try:
                # Send initial configuration (see _build_initial_stream_control —
                # extracted so the message construction is unit-testable after the
                # ``serials`` proto-field regression crashed every NonStop game).
                await serialized_write(self.gameplay_stream, self._build_initial_stream_control(update_frequency_hz))
            finally:
                # This stream replaces the one run() opened for the countdown and
                # warm-up: close that one, after the new one claimed the players
                # (UC-030 BR-006).
                cancel = getattr(countdown_stream, "cancel", None)
                if callable(cancel):
                    cancel()

            # Startup grace: residual spikes in the first frames cannot kill (UC-030 BR-003)
            # It never shortens a longer grace (an agent shield) granted before the loop.
            grace_until = game_clock.now() + 0.3
            for player in self.players.values():
                if player.alive:
                    player.grace_until = max(player.grace_until, grace_until)

            # Track current alive set for detecting changes (Phase 45)
            last_alive_serials = {p.serial for p in self.players.values() if p.alive}
            logger.info(f"Initial alive players: {len(last_alive_serials)}")

            # Disconnects seen during the countdown or warm-up are deaths now, before the
            # first frame (UC-026 A2). The time limit has not started, so no win check.
            if self.running:
                await self._handle_warmup_disconnects()
                last_alive_serials = await self._send_alive_filter_update(last_alive_serials)

            # Frame metrics as in the base loop: game_loop_frames_dropped_total,
            # actual Hz, consistency, jitter
            loop_start_time = game_clock.now()
            last_iteration_time = loop_start_time
            loop_iterations = 0
            target_frame_time_ms = 1000.0 / update_frequency_hz
            recent_frame_times: list[float] = []
            frames_on_target = 0

            # The controller manager paces the stream: each frame is processed as it
            # arrives, with no sleep in between (a sleep made the loop fall behind and
            # act on stale frames, NFR-004)
            async for gameplay_update in self.gameplay_stream:
                # The time limit runs from the first frame after the warm-up (UC-026 step 3)
                if self.start_time is None:
                    self.start_time = game_clock.now()

                if not self.running:
                    break

                # Disconnects first: they were detected before this frame, so a player
                # whose respawn deadline also passed does not respawn and die twice
                # (UC-026 A2)
                for disconnect in gameplay_update.disconnects:
                    await self._handle_disconnect(disconnect.serial)

                # Respawns whose deadline passed, before this frame's motion: a death in
                # this frame respawns on the next one at the earliest (UC-026 step 2)
                await self._update_respawn_timers()

                # Motion; the time limit is checked once per frame below. The
                # runtime config is read once for the frame.
                with self._frame_config_scope():
                    for gameplay_data in gameplay_update.controllers:
                        await self._process_controller_state(gameplay_data)

                # Check if alive players changed (Phase 45 - dynamic filtering)
                # Note: Nonstop has frequent changes due to respawns
                current_alive_serials = {p.serial for p in self.players.values() if p.alive}

                if current_alive_serials != last_alive_serials:
                    # Send filter update to server
                    filter_msg = controller_manager_pb2.GameplayStreamControl(
                        filter_update=controller_manager_pb2.FilterUpdate(serials=list(current_alive_serials))
                    )
                    await serialized_write(self.gameplay_stream, filter_msg)

                    logger.info(
                        f"Nonstop filter updated: {len(last_alive_serials)} → "
                        f"{len(current_alive_serials)} alive players"
                    )

                    # Emit filter metrics (Phase 45)
                    from services.game_coordinator import metrics

                    metrics.filter_updates_total.labels(game_mode=self.get_game_name()).inc()
                    metrics.active_controllers.set(len(current_alive_serials))
                    metrics.filtered_controllers.set(len(self.players) - len(current_alive_serials))

                    last_alive_serials = current_alive_serials

                loop_iterations += 1
                iteration_end = game_clock.now()
                frames_on_target = self._update_frame_metrics(
                    (iteration_end - last_iteration_time) * 1000,
                    target_frame_time_ms,
                    recent_frame_times,
                    frames_on_target,
                    loop_iterations,
                    loop_start_time,
                )
                last_iteration_time = iteration_end

                # Time limit: latch the outcome now, so a force-end after this point
                # keeps the winner (UC-026 BR-003)
                if await self._check_win_condition():
                    self._time_limit_reached = True
                    self._latch_game_outcome()
                    break

        except Exception as e:
            logger.error(f"Game loop error: {e}", exc_info=True)
            raise
        # The stream stays open for the end-game step; run()'s teardown (natural end)
        # or cleanup() (force-end, error) closes it (UC-030 BR-006).

    async def _resend_dead_player_state(self) -> None:
        """A reopened stream's config sends the countdown players off: show the colour again (#2484).

        Without the reset, ``_last_respawn_color`` blocks the re-send until the colour
        changes, up to 1 s of a dark LED.
        """
        for player in self.players.values():
            player._last_respawn_color = None
        await super()._resend_dead_player_state()

    async def _update_respawn_timers(self):
        """Update respawn timers and respawn players (Nonstop-specific)."""
        from proto import controller_manager_pb2

        current_time = game_clock.now()
        now = self._clock()

        for serial, player in self.players.items():
            # Respawn on the clock, not on the frame count (UC-026 step 2)
            if not player.alive and player.respawn_deadline is not None:
                player.respawn_timer = max(0.0, player.respawn_deadline - now)
                if player.respawn_timer > 0:
                    # Countdown colours from the time left
                    await self._show_respawn_countdown(serial, player.respawn_timer)
                else:
                    await self._respawn_player(serial)

            # Check spawn protection expiration
            if player.spawn_protected and current_time >= player.spawn_protection_end:
                player.spawn_protected = False
                # Return to normal white color via stream
                base_color_cmd = controller_manager_pb2.GameplayStreamControl(
                    base_color=controller_manager_pb2.ControllerColorConfig(
                        serial=serial,
                        color=controller_manager_pb2.RGB(r=255, g=255, b=255),  # White
                    )
                )
                await serialized_write(self.gameplay_stream, base_color_cmd)

    async def _show_respawn_countdown(self, serial: str, time_remaining: float):
        """
        Show respawn countdown colors (Nonstop-specific).

        Args:
            serial: Controller serial number
            time_remaining: Seconds until respawn
        """
        from proto import controller_manager_pb2

        # Countdown colors: Gray -> Yellow -> Green
        if time_remaining > 2.0:
            color = (128, 128, 128)  # Gray (3s)
        elif time_remaining > 1.0:
            color = (255, 255, 0)  # Yellow (2s)
        else:
            color = (0, 255, 0)  # Green (1s)

        # Only update if color changed (avoid spamming)
        player = self.players[serial]
        if not hasattr(player, "_last_respawn_color") or player._last_respawn_color != color:
            player._last_respawn_color = color

            # Play respawn countdown beep (Phase 29)
            await self._play_sound(Sound.SFX_BEEP_LOUD, priority=1)

            # Update color via stream
            base_color_cmd = controller_manager_pb2.GameplayStreamControl(
                base_color=controller_manager_pb2.ControllerColorConfig(
                    serial=serial,
                    color=controller_manager_pb2.RGB(r=color[0], g=color[1], b=color[2]),
                )
            )
            await serialized_write(self.gameplay_stream, base_color_cmd)

    async def _respawn_player(self, serial: str) -> bool:
        """
        Respawn a dead player (Nonstop-specific).

        Refused once the game is no longer RUNNING, the outcome is latched or the
        stream is closed (UC-026 BR-003, #1954), and for a disconnected player
        (UC-026 A2, #1971): base ``revive_player`` propagates
        the False, so a late agent revive changes nothing and writes nothing.

        Args:
            serial: Controller serial number

        Returns:
            True if the player respawned, False if refused.
        """
        from proto import controller_manager_pb2

        if self.state != GameState.RUNNING or self._latched_outcome is not None or self.gameplay_stream is None:
            logger.info(f"Respawn of {serial} refused: the game is over")
            return False

        player = self.players[serial]
        if player.disconnected:
            # A disconnected controller never comes back as a live player (UC-026 A2)
            logger.info(f"Respawn of {serial} refused: the controller disconnected")
            return False
        player.alive = True
        metrics.player_alive.labels(serial=serial, game_id=self.game_id).set(1)
        self._set_players_alive_aggregate(len([p for p in self.players.values() if p.alive]))
        player.respawn_deadline = None  # an agent revive also ends a pending countdown
        player.respawn_timer = 0.0
        player._last_respawn_color = None  # the next death shows its countdown colour again
        player.spawn_protected = True
        player.spawn_protection_end = game_clock.now() + self.spawn_protection_duration
        # Reset warning state for clean respawn
        player.warning_until = 0.0
        player.smoothed_accel = 0.0

        # Add respawn event to player's lifecycle span
        if player.span:
            player.span.add_event(
                "player_respawned",
                {"kills": player.kills, "deaths": player.deaths, "score": player.score},
            )

        logger.info(f"Player respawned: {serial} (spawn protection active)")

        # Play respawn sound (Phase 29)
        await self._play_sound(Sound.SFX_JOIN, priority=2)

        # White color during spawn protection via stream
        base_color_cmd = controller_manager_pb2.GameplayStreamControl(
            base_color=controller_manager_pb2.ControllerColorConfig(
                serial=serial,
                color=controller_manager_pb2.RGB(r=255, g=255, b=255),  # White
            )
        )
        await serialized_write(self.gameplay_stream, base_color_cmd)

        # Publish respawn event
        await self.event_publisher(
            "player_respawned",
            {
                "serial": serial,
                "kills": player.kills,
                "deaths": player.deaths,
                "score": player.score,
            },
        )
        return True

    async def _set_unique_colors(self):
        """
        Set unique colors for each player in Nonstop Joust (Phase 39 - Task 3).

        Each player gets a distinct color so they can be identified during gameplay.
        Uses HSV color generation for maximum distinction.
        """
        from lib.colors import generate_colors

        logger.info("Assigning unique colors...")

        try:
            # Generate unique colors for each player
            unique_colors = generate_colors(len(self.players))

            # Assign colors to players (LEDs set when gameplay stream starts)
            for idx, (_serial, player) in enumerate(self.players.items()):
                color = unique_colors[idx]
                player.color = color  # Update player's color attribute

            logger.info(f"Assigned {len(self.players)} unique colors (will display at game start)")

        except Exception as e:
            logger.error(f"Failed to assign colors: {e}", exc_info=True)

    def _get_additional_phases(self) -> list:
        """
        Return phases to execute before countdown.

        Nonstop assigns colors silently - players see them at game start (after countdown),
        matching original JoustMania behavior.
        """
        return [Phase(name="color_assignment", execute=self._set_unique_colors)]

    def _final_score(self, player: NonstopPlayer) -> int:
        """Score = score_base - deaths * score_death_penalty, minimum 0 (UC-026 BR-001, #766 F2)."""
        return max(0, self.score_base - (player.deaths * self.score_death_penalty))

    def _close_all_player_spans(self):
        """Add the ``final_score`` event to each player span, then close them (base).

        The base closes the spans at the end of ``gameplay_phase``, before the
        end-game step, so the score is computed here from the same formula.
        """
        for player in self.players.values():
            if player.span:
                final_attrs = {
                    "score": self._final_score(player),
                    "kills": player.kills,
                    "deaths": player.deaths,
                    "game_duration": game_clock.now() - self.start_time if self.start_time else 0,
                }
                final_attrs.update(self._analytics_span_attributes(player))
                player.span.add_event("final_score", final_attrs)
        super()._close_all_player_spans()

    def _player_analytics_extras(self, player: NonstopPlayer) -> dict:
        """NonStop summaries carry the score, kills and deaths instead of the survival time."""
        return {"score": player.score, "kills": player.kills, "deaths": player.deaths}

    async def _end_game_impl(self):
        """Handle game ending with scoring calculation."""
        from proto import controller_manager_pb2

        logger.info("Ending game...")
        self.state = GameState.ENDING

        # Calculate final scores (inverse of deaths - fewer deaths = higher score)
        for player in self.players.values():
            player.score = self._final_score(player)

        # A winner only when the time limit ended the game and the latched outcome is
        # not force_ended; a force-end, A1 or an early stream end has none (UC-026 BR-003).
        force_ended = self.game_outcome() == "force_ended"
        winner = None
        if self._time_limit_reached and not force_ended:
            # Highest score, tie-break by fewest deaths
            winner = max(self.players.values(), key=lambda p: (p.score, -p.deaths), default=None)
        winner_serial = winner.serial if winner else None

        if winner:
            logger.info(f"Winner: {winner.serial} with score {winner.score} (K:{winner.kills} D:{winner.deaths})")

            # Show rainbow effect on winner's controller (the stream is open until teardown).
            # Skipped when the server already ended it: the winner stays, the game is not an error.
            if self.gameplay_stream:
                effect_cmd = controller_manager_pb2.GameplayStreamControl(
                    game_effect=controller_manager_pb2.GameEffectCommand(
                        serial=winner.serial,
                        effect=controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW,
                    )
                )
                await self._write_end_effect(effect_cmd)

            # Play victory sound (Phase 29)
            await self._play_sound(Sound.VOX_CONGRATULATIONS, priority=2)

            await self.event_publisher(
                GameEvent.GAME_WINNER,
                {
                    "serial": winner.serial,
                    "score": winner.score,
                    "kills": winner.kills,
                    "deaths": winner.deaths,
                },
            )

            # Show the winner for a bit (interruptible by force_end)
            for _ in range(round(WINNER_HOLD_SECONDS / WINNER_HOLD_TICK_SECONDS)):
                if not self.running:
                    logger.info("End game interrupted by force_end")
                    break
                await game_clock.sleep(WINNER_HOLD_TICK_SECONDS)

        # The player spans (with final_score) were closed at the end of gameplay_phase.

        # Publish analytics summaries for all players
        await self._publish_analytics_summaries({winner_serial} if winner_serial else set())

        self.state = GameState.ENDED
        ended_payload = {
            "game_id": self.game_id,
            "duration": game_clock.now() - self.start_time if self.start_time else 0,
            "outcome": self.game_outcome(),
            "winner": winner_serial or "none",
        }
        if force_ended:
            ended_payload["reason"] = self.force_end_reason
        await self.event_publisher(GameEvent.GAME_ENDED, ended_payload)

        logger.info("Game ended")
