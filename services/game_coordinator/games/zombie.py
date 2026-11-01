"""
Zombie Game Mode - gRPC-based implementation

Humans vs Zombies asymmetric gameplay. When zombies "kill" humans,
they become zombies. Humans win if they survive the time limit,
zombies win if all humans are converted.

Note: Weapon system (trigger button to shoot zombies) requires button
event support in gameplay stream - not yet implemented.

Original JoustMania behavior preserved (minus weapon system).
"""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from lib.colors import Colors
from lib.feature_flags import read_float_flag, read_int_flag, read_object_flag
from lib.types import Sound
from proto import controller_manager_pb2
from services.game_coordinator import game_clock, metrics
from services.game_coordinator.games.base import (
    GAME_MODE_ATTR,
    Phase,
    Player,
    Sensitivity,
    resolve_mode_thresholds,
    resolve_non_negative_duration,
)
from services.game_coordinator.games.streamed_intro import StreamedIntroGameMode
from services.game_coordinator.stream_writes import serialized_write

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# Game constants
INITIAL_ZOMBIES = 2  # Number of starting zombies
ZOMBIE_RESPAWN_MIN = 2.0  # Minimum respawn delay for zombies
ZOMBIE_RESPAWN_MAX = 10.0  # Maximum respawn delay for zombies
ZOMBIE_INTRO_DURATION_S = 3.0  # Intro length; the stream is drained meanwhile (UC-023 step 1)
CONVERSION_COLOUR_DELAY = 0.5  # Death flash before a converted human turns grey (UC-023 step 3)

# Colors
HUMAN_COLOR = Colors.White.value  # White for humans
ZOMBIE_COLOR = Colors.ZombieGray.value  # Blue-gray for zombies

# Zombie thresholds - zombies are harder to kill
ZOMBIE_THRESHOLDS = {
    Sensitivity.ULTRA_SLOW: (1.4, 1.6),
    Sensitivity.SLOW: (1.7, 1.9),
    Sensitivity.MEDIUM: (2.1, 2.6),
    Sensitivity.FAST: (2.9, 3.9),
    Sensitivity.ULTRA_FAST: (3.5, 4.5),
}


def calculate_game_duration(num_players: int) -> float:
    """
    Calculate game duration based on player count.

    Linear in the player count: 11.25 s per player (22.5 s for 2, 45 s for 4,
    90 s for 8, 135 s for 12, 180 s for 16).

    Args:
        num_players: Total number of players

    Returns:
        Game duration in seconds
    """
    return ((num_players * 3) / 16) * 60


@dataclass
class ZombiePlayer(Player):
    """Extended Player class with zombie information."""

    is_zombie: bool = False
    respawn_until: float = 0.0  # Time when zombie can respawn (0 = not respawning)


@dataclass(frozen=True)
class ZombieInitFlags:
    """The zombie flags latched at game init, validated and immutable (#2359)."""

    thresholds: Mapping[Sensitivity, tuple[float, float]]
    initial_count: int
    respawn_min_seconds: float
    respawn_max_seconds: float

    @classmethod
    def from_raw(
        cls, *, thresholds: Any, initial_count: Any, respawn_min_seconds: Any, respawn_max_seconds: Any
    ) -> "ZombieInitFlags":
        """Validate the raw flag values; a malformed one gives the module default (fail closed).

        ``initial_count`` must be a positive int. The respawn bounds are non-negative and the
        min must not exceed the max, else both fall back together to keep a sane window.
        """
        respawn_min = resolve_non_negative_duration(respawn_min_seconds, ZOMBIE_RESPAWN_MIN)
        respawn_max = resolve_non_negative_duration(respawn_max_seconds, ZOMBIE_RESPAWN_MAX)
        if respawn_min > respawn_max:
            respawn_min, respawn_max = ZOMBIE_RESPAWN_MIN, ZOMBIE_RESPAWN_MAX
        count_ok = isinstance(initial_count, int) and initial_count >= 1
        return cls(
            thresholds=MappingProxyType(dict(resolve_mode_thresholds(thresholds, ZOMBIE_THRESHOLDS))),
            initial_count=initial_count if count_ok else INITIAL_ZOMBIES,
            respawn_min_seconds=respawn_min,
            respawn_max_seconds=respawn_max,
        )


class ZombieGame(StreamedIntroGameMode):
    """
    Zombie game mode using gRPC communication.

    Asymmetric gameplay:
    - 2 players start as zombies (blue-gray)
    - Rest are humans (white)
    - When a human "dies" (exceeds threshold), they become a zombie
    - Zombies have higher thresholds (harder to kill)
    - Zombies respawn after a delay when killed
    - Humans win if time expires with at least one human alive
    - Zombies win if all humans are converted

    Note: Weapon system not implemented (requires button events in gameplay stream).
    """

    RUN_SPAN_NAME = "zombie_game"

    def __init__(
        self,
        controller_manager_client,
        event_publisher,
        audio_client=None,
        game_id: str = "",
        initial_players: list | None = None,
        sensitivity: int = 2,
        rng_seed: int = 0,
    ):
        """
        Initialize Zombie game.

        Args:
            controller_manager_client: gRPC stub for ControllerManager service
            event_publisher: Callback function to publish game events
            audio_client: gRPC stub for Audio service
            game_id: Unique identifier for this game instance
            initial_players: Optional list of Player protobuf messages
            sensitivity: Sensitivity level 0-4 (passed from StartGameConfig)
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

        # The side that won, latched once in _check_win_condition (UC-023 BR-003).
        # None = no winner (force-ended before a win, or the stream ended without one).
        self._winner_side: str | None = None

        # Pending zombie respawn timers by serial: a revive cancels the
        # timer, a new death replaces it. Also tracked via _track_task for cleanup.
        self._respawn_tasks: dict[str, asyncio.Task] = {}

        # Pending zombie-colour writes after a conversion's death flash, by serial:
        # the delay runs off the game loop. Also tracked via _track_task.
        self._conversion_colour_tasks: dict[str, asyncio.Task] = {}

        # zombie threshold overrides promoted to ``game.zombie.thresholds``.
        # Read once at init (init-frozen); malformed values fall back to ZOMBIE_THRESHOLDS.
        self.mode_flags = ZombieInitFlags.from_raw(
            thresholds=read_object_flag("game", "zombie.thresholds", {}, game_id=self.game_id),
            initial_count=read_int_flag("game", "zombie.initial_count", INITIAL_ZOMBIES, game_id=self.game_id),
            respawn_min_seconds=read_float_flag(
                "game", "zombie.respawn_min_seconds", ZOMBIE_RESPAWN_MIN, game_id=self.game_id
            ),
            respawn_max_seconds=read_float_flag(
                "game", "zombie.respawn_max_seconds", ZOMBIE_RESPAWN_MAX, game_id=self.game_id
            ),
        )
        self.zombie_thresholds = dict(self.mode_flags.thresholds)

        # initial zombie count and respawn delay window promoted to game
        # flags. Read ONCE here (init-frozen); malformed values fall back to the
        # module constants. ``initial_count`` must be a positive int; respawn
        # bounds are non-negative and the min must not exceed the max (else both
        # fall back together to preserve a sane window).
        self.initial_zombies = self.mode_flags.initial_count
        self.zombie_respawn_min = self.mode_flags.respawn_min_seconds
        self.zombie_respawn_max = self.mode_flags.respawn_max_seconds

        self.zombie_serials: list[str] = []
        self.human_serials: list[str] = []
        self.game_duration: float = 180.0  # Will be calculated based on player count
        self.time_remaining: float = 0.0
        self.timer_task: asyncio.Task | None = None
        self.game_span: trace.Span | None = None

    def get_game_name(self) -> str:
        """Return game mode identifier."""
        return "Zombie"

    def get_instruction_clip(self) -> Sound | None:
        """Spoken instructions before the countdown (UC-023 A2, UC-020 A5)."""
        return Sound.MENU_VOX_ZOMBIE_INSTRUCTIONS

    async def _initialize_players_impl(self, controllers: list):
        """
        Initialize players and assign initial zombies.

        Args:
            controllers: List of controller protobuf messages
        """
        num_players = len(controllers)
        self.game_duration = calculate_game_duration(num_players)
        self.time_remaining = self.game_duration

        # Randomly select initial zombies. Sort serials before the RNG consumes
        # them (#1003 CRN alignment) so a seed-matched pair samples the same
        # players from the same pre-shuffle ordering.
        serials = sorted(c.serial for c in controllers)
        num_zombies = min(self.initial_zombies, num_players - 1)  # At least 1 human
        zombie_serials = set(self._rng.sample(serials, num_zombies))

        for controller in controllers:
            is_zombie = controller.serial in zombie_serials
            color = ZOMBIE_COLOR if is_zombie else HUMAN_COLOR

            player = ZombiePlayer(
                serial=controller.serial,
                team=1 if is_zombie else 0,  # Team 0 = humans, Team 1 = zombies
                alive=True,
                color=color,
                is_zombie=is_zombie,
            )
            self.players[controller.serial] = player

            if is_zombie:
                self.zombie_serials.append(controller.serial)
            else:
                self.human_serials.append(controller.serial)

            logger.debug(f"Added player: {controller.serial} ({'zombie' if is_zombie else 'human'})")

        logger.info(
            f"Initialized {num_players} players: {len(self.human_serials)} humans, "
            f"{len(self.zombie_serials)} zombies. Game duration: {self.game_duration:.0f}s"
        )

        await self.event_publisher(
            "players_initialized",
            {
                "player_count": num_players,
                "zombie_count": len(self.zombie_serials),
                "human_count": len(self.human_serials),
                "game_duration": self.game_duration,
                "serials": list(self.players.keys()),
            },
        )

    def _create_player_spans(self, game_context):  # noqa: ARG002
        """Create flat player lifecycle spans, parented to game span."""
        if not self.game_span:
            logger.warning("No game_span available, creating orphan player spans")
            for serial, player in self.players.items():
                zombie_player = player
                player.span = tracer.start_span(
                    "player_lifecycle",
                    attributes={
                        "player.serial": serial,
                        "player.is_zombie": zombie_player.is_zombie,
                        "player.team": "zombie" if zombie_player.is_zombie else "human",
                        GAME_MODE_ATTR: self.get_game_name(),
                    },
                )
            return

        # Create player spans as children of game span
        with trace.use_span(self.game_span, end_on_exit=False):
            for serial, player in self.players.items():
                zombie_player = player
                player.span = tracer.start_span(
                    "player_lifecycle",
                    attributes={
                        "player.serial": serial,
                        "player.is_zombie": zombie_player.is_zombie,
                        "player.team": "zombie" if zombie_player.is_zombie else "human",
                        GAME_MODE_ATTR: self.get_game_name(),
                    },
                )
                logger.debug(f"Created span for player {serial}")

    def _get_additional_phases(self) -> list:
        """Return zombie intro phase."""
        return [Phase(name="zombie_intro", execute=self._zombie_intro_phase)]

    async def _zombie_intro_phase(self):
        """
        Zombie introduction phase.

        Show players their roles with appropriate colors.
        """
        logger.info("Starting zombie intro phase...")

        await self.event_publisher(
            "zombie_intro_start",
            {
                "zombie_count": len(self.zombie_serials),
                "human_count": len(self.human_serials),
            },
        )

        # A write error (the early stream already broke) is logged, not raised:
        # the drain stop at the end of the intro reopens the stream (UC-023 step 1).
        try:
            # Set initial colors
            for serial, player in self.players.items():
                zombie_player = player
                color = ZOMBIE_COLOR if zombie_player.is_zombie else HUMAN_COLOR

                base_color_cmd = controller_manager_pb2.GameplayStreamControl(
                    base_color=controller_manager_pb2.ControllerColorConfig(
                        serial=serial,
                        color=controller_manager_pb2.RGB(r=color[0], g=color[1], b=color[2]),
                    )
                )
                await serialized_write(self.gameplay_stream, base_color_cmd)
        except Exception as e:
            logger.error(f"Failed to set the zombie intro colours: {e}", exc_info=True)

        # Play intro sound (use start sound - no dedicated zombie intro exists)
        await self._play_sound(Sound.SFX_START3, priority=2)

        # Wait for intro (interruptible, checked every 0.1 s)
        for _ in range(round(ZOMBIE_INTRO_DURATION_S / 0.1)):
            if not self.running:
                return
            await game_clock.sleep(0.1)

        await self.event_publisher("zombie_intro_end", {})
        logger.info("Zombie intro phase complete")

    def _compute_effective_thresholds(self, player: Player) -> tuple[float, float]:
        """
        Compute effective thresholds for a player.

        Zombies have higher thresholds (harder to kill).
        Humans use standard base class thresholds.
        """
        zombie_player = player
        if zombie_player.is_zombie:
            return self.zombie_thresholds.get(self.sensitivity, (2.1, 2.6))
        return super()._compute_effective_thresholds(player)

    async def _game_timer(self):
        """
        Track game time and announce remaining time.

        Runs as a background task during gameplay.
        """
        announcements = {
            60: Sound.VOX_ZOMBIE_ONE_MINUTE,
            30: Sound.VOX_ZOMBIE_THIRTY_SECONDS,
            10: Sound.VOX_ZOMBIE_TEN_SECONDS,
        }

        while self.running and self.time_remaining > 0:
            await game_clock.sleep(1.0)
            previous_remaining = self.time_remaining
            self.time_remaining -= 1.0

            # Once the win is latched (the 1 s hold) the timer announces nothing.
            if self._winner_side is not None:
                continue

            # A mark fires when the timer crosses it, so durations with a .5
            # fraction (22.5 s, 67.5 s, ...) still announce 60, 30 and 10 s once.
            for seconds, sound in announcements.items():
                if previous_remaining > seconds >= self.time_remaining:
                    logger.info(f"Time announcement: {seconds} seconds remaining")
                    await self.event_publisher("time_announcement", {"seconds_remaining": seconds})
                    await self._play_sound(sound, priority=2)

        # Time's up - humans win if any survive
        if self.running and self.time_remaining <= 0:
            logger.info("Time's up! Checking for human survivors...")

    async def _check_win_condition(self) -> bool:
        """
        Check if the game should end.

        Zombies win if all humans are converted.
        Humans win if time expires with at least one human alive.
        """
        # The win is latched once: later calls (the 1 s hold) do not republish it.
        if self._winner_side is not None:
            return True
        if self.force_ended:
            # A force-end declares no winner (BR-003): nothing is latched or published.
            return False

        # Count alive humans (not counting converting players)
        alive_humans = [s for s in self.human_serials if self.players[s].alive and not self.players[s].is_zombie]

        # Check if all humans converted
        if len(alive_humans) == 0:
            logger.info("All humans converted! Zombies win!")
            if not self._latch_win("zombies"):
                return False
            await self.event_publisher(
                "zombie_winner",
                {
                    "winner": "zombies",
                    "zombie_count": len(self.zombie_serials),
                },
            )
            return True

        # Check if time expired
        if self.time_remaining <= 0:
            logger.info(f"Time's up! Humans win with {len(alive_humans)} survivors!")
            if not self._latch_win("humans"):
                return False
            await self.event_publisher(
                "zombie_winner",
                {
                    "winner": "humans",
                    "survivors": alive_humans,
                    "survivor_count": len(alive_humans),
                },
            )
            return True

        return False

    def _latch_win(self, side: str) -> bool:
        """Latch the outcome, then the winning side, before the publish awaits (BR-003, #1988).

        The outcome latches first: a force-end that won the race declares no winner
        (False, nothing latched), one landing during the publish keeps this winner.
        """
        self._latch_game_outcome()
        if self.game_outcome() == "force_ended":
            return False
        self._winner_side = side
        return True

    async def _kill_player_impl(self, serial: str, accel_mag: float):
        """
        Handle player death/conversion.

        - Humans become zombies when killed
        - Zombies respawn after a delay

        Args:
            serial: Controller serial number
            accel_mag: Acceleration magnitude that caused death
        """
        player = self.players[serial]
        zombie_player = player

        if zombie_player.is_zombie:
            # Zombie killed - set respawn timer
            zombie_player.alive = False
            respawn_delay = self._rng.uniform(self.zombie_respawn_min, self.zombie_respawn_max)
            zombie_player.respawn_until = game_clock.now() + respawn_delay

            logger.info(f"Zombie {serial} killed, respawning in {respawn_delay:.1f}s")

            # A zombie-colour write still pending from its conversion is dropped: the
            # death effect keeps the LED off, and the respawn sets the colour itself
            # (UC-023 steps 3 and 4).
            pending_colour = self._conversion_colour_tasks.pop(serial, None)
            if pending_colour is not None:
                pending_colour.cancel()

            # Start respawn task (tracked for automatic cleanup)
            self._respawn_tasks[serial] = self._track_task(
                asyncio.create_task(self._respawn_zombie(serial, respawn_delay))
            )

            if player.span:
                player.span.add_event(
                    "zombie_killed",
                    attributes={
                        "accel_magnitude": accel_mag,
                        "respawn_delay": respawn_delay,
                    },
                )
        else:
            # Human killed - convert to zombie!
            zombie_player.alive = True  # Stays alive as zombie
            zombie_player.is_zombie = True
            zombie_player.color = ZOMBIE_COLOR
            zombie_player.team = 1
            # Grace period so the death spike that converted the human can't
            # instantly re-kill the fresh zombie. Sourced from the
            # ``game.death_grace_period_seconds`` flag, frozen per
            # game at init in BaseGameMode; matches the respawn grace below.
            zombie_player.grace_until = game_clock.now() + self.death_grace_period

            # Update lists
            if serial in self.human_serials:
                self.human_serials.remove(serial)
            if serial not in self.zombie_serials:
                self.zombie_serials.append(serial)

            logger.info(f"Human {serial} converted to zombie! {len(self.human_serials)} humans remain")

            # Update color to zombie - death flash then zombie color
            flash_cmd = controller_manager_pb2.GameplayStreamControl(
                game_effect=controller_manager_pb2.GameEffectCommand(
                    serial=serial,
                    effect=controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH,
                )
            )
            await serialized_write(self.gameplay_stream, flash_cmd)

            # The zombie colour follows the flash after CONVERSION_COLOUR_DELAY, in a
            # tracked task: the kill handler runs inline in the game loop, which must
            # not stall on the delay (UC-023 step 3).
            self._conversion_colour_tasks[serial] = self._track_task(
                asyncio.create_task(self._show_zombie_colour_after_flash(serial))
            )

            # The base kill path set the per-player gauge to 0; the converted
            # zombie is still in the game. Skipped after a force end, which
            # cleared the player analytics (no gauge series recreated).
            if self.running:
                self._mark_alive_in_metrics(serial)

            # Play conversion sound (use zombie death sound for conversion)
            await self._play_sound(Sound.SFX_ZOMBIE_DEATH, priority=2)

            if player.span:
                player.span.add_event(
                    "human_converted",
                    attributes={
                        "accel_magnitude": accel_mag,
                        "remaining_humans": len(self.human_serials),
                    },
                )

            await self.event_publisher(
                "human_converted",
                {
                    "serial": serial,
                    "remaining_humans": len(self.human_serials),
                    "total_zombies": len(self.zombie_serials),
                },
            )

    async def _show_zombie_colour_after_flash(self, serial: str) -> None:
        """Set a converted human's zombie colour once the death flash has played (#2023).

        Skipped when the game stopped or the zombie died meanwhile (its death effect
        keeps the LED off; the zombie kill also cancels this task, so a respawn within
        the delay gets no second colour write). A guarded write: on a dead gameplay
        call it is skipped, and the reopened stream restores the zombie colour
        (UC-030 BR-006). CONVERSION_COLOUR_DELAY stays below the 1 s post-win hold
        (UC-020 BR-003), so a winning conversion's grey lands before the rainbow.
        """
        try:
            await game_clock.sleep(CONVERSION_COLOUR_DELAY)
            player = self.players.get(serial)
            if not self.running or player is None or not player.alive or not player.is_zombie:
                return
            await self._write_effect_guarded(
                controller_manager_pb2.GameplayStreamControl(
                    base_color=controller_manager_pb2.ControllerColorConfig(
                        serial=serial,
                        color=controller_manager_pb2.RGB(r=ZOMBIE_COLOR[0], g=ZOMBIE_COLOR[1], b=ZOMBIE_COLOR[2]),
                    )
                ),
                "Zombie conversion colour",
            )
        finally:
            if self._conversion_colour_tasks.get(serial) is asyncio.current_task():
                del self._conversion_colour_tasks[serial]

    def _sends_death_effect(self, serial: str) -> bool:
        """No base death effect for a converted human, which stays in the game (#1342).

        A converted human is still alive after ``_kill_player_impl`` and already got
        its own death flash (the zombie colour follows from a task, #2023); a killed
        zombie is dead and gets the base death effect (LED off until it respawns).
        """
        return not self.players[serial].alive

    def _mark_alive_in_metrics(self, serial: str) -> None:
        metrics.player_alive.labels(serial=serial, game_id=self.game_id).set(1)
        self._set_players_alive_aggregate(len([p for p in self.players.values() if p.alive]))

    async def _respawn_zombie(self, serial: str, delay: float):
        """
        Respawn a zombie after the delay.

        Args:
            serial: Controller serial number
            delay: Respawn delay in seconds
        """
        await game_clock.sleep(delay)
        await self._respawn_player(serial, cause="timer")

    async def _respawn_player(self, serial: str, cause: str = "revive") -> bool:
        """
        Respawn a dead zombie now: the timer path and the native revive path (#1342).

        Refused (returns False) once the winner is latched or the game stopped
        running (UC-023 BR-003/BR-004), for an unknown serial, a human, or a zombie
        that is already alive. All state changes happen BEFORE the first await, so
        a racing revive or timer sees ``alive`` and refuses (exactly one respawn).

        Args:
            serial: Controller serial number
            cause: ``timer`` (respawn delay) or ``revive`` (revive_player)

        Returns:
            True if the zombie respawned.
        """
        if not self.running or self._winner_side is not None:
            return False
        player = self.players.get(serial)
        if player is None or not player.is_zombie or player.alive:
            return False

        # Cancel a pending timer, unless this IS the timer task.
        pending = self._respawn_tasks.pop(serial, None)
        if pending is not None and pending is not asyncio.current_task():
            pending.cancel()

        player.alive = True
        player.respawn_until = 0.0
        player.warning_until = 0.0
        player.smoothed_accel = 0.0
        # Grace period after respawn, sourced from ``game.death_grace_period_seconds``,
        # frozen per game at init in BaseGameMode.
        player.grace_until = game_clock.now() + self.death_grace_period
        self._mark_alive_in_metrics(serial)

        logger.info(f"Zombie {serial} respawned ({cause})")

        if player.span:
            player.span.add_event("zombie_respawned", {"respawn.cause": cause})

        # Zombie colour FIRST, then the flash: the flash restores to the current
        # base colour, which the death effect left black. Guarded writes: the
        # respawn timer is a background task, and a write on a dead call is skipped
        # (the reopened stream restores the zombie colour, UC-030 BR-006).
        if self.gameplay_stream:
            await self._write_effect_guarded(
                controller_manager_pb2.GameplayStreamControl(
                    base_color=controller_manager_pb2.ControllerColorConfig(
                        serial=serial,
                        color=controller_manager_pb2.RGB(r=ZOMBIE_COLOR[0], g=ZOMBIE_COLOR[1], b=ZOMBIE_COLOR[2]),
                    )
                ),
                "Zombie respawn colour",
            )
            await self._write_effect_guarded(
                controller_manager_pb2.GameplayStreamControl(
                    game_effect=controller_manager_pb2.GameEffectCommand(
                        serial=serial,
                        effect=controller_manager_pb2.GAME_EFFECT_FLASH,
                    )
                ),
                "Zombie respawn flash",
            )
        return True

    async def _end_game_impl(self):
        """Handle game ending."""
        logger.info("Ending Zombie game...")
        self.state = self.state.__class__.ENDING

        # Cancel timer task
        if self.timer_task and not self.timer_task.done():
            self.timer_task.cancel()

        # The winner was latched when the win condition fired. A force-end declares
        # no winner (UC-023 BR-003); a stream end without a win has none either.
        force_ended = self.game_outcome() == "force_ended"
        winner_side = None if force_ended else self._winner_side
        winner = winner_side or "none"
        if winner_side == "humans":
            winner_serials = [s for s in self.human_serials if self.players[s].alive]
        elif winner_side == "zombies":
            winner_serials = self.zombie_serials
        else:
            winner_serials = []

        # Show rainbow effect on winners
        if self.gameplay_stream:
            for serial in winner_serials:
                player = self.players.get(serial)
                if player and player.alive:
                    effect_cmd = controller_manager_pb2.GameplayStreamControl(
                        game_effect=controller_manager_pb2.GameEffectCommand(
                            serial=serial,
                            effect=controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW,
                        )
                    )
                    await self._write_end_effect(effect_cmd)

        # Play victory sound (Zombie/vox/ directory)
        if winner_side == "humans":
            await self._play_sound(Sound.VOX_HUMAN_VICTORY, priority=2)
        elif winner_side == "zombies":
            await self._play_sound(Sound.VOX_ZOMBIE_VICTORY, priority=2)

        # Wait for rainbow effect to complete
        if winner_side is not None:
            await self._wait_for_rainbow_effect()

        # End all player spans
        for serial, player in self.players.items():
            if player.span:
                zombie_player = player
                is_winner = serial in winner_serials
                player.span.add_event(
                    "game_ended",
                    attributes={
                        "winner": is_winner,
                        "is_zombie": zombie_player.is_zombie,
                        "winning_team": winner,
                        **self._analytics_span_attributes(player),
                    },
                )
                player.span.set_status(Status(StatusCode.OK))
                player.span.end()

        # Per-player analytics summaries for the agent (UC-041 step 4, #2358)
        await self._publish_analytics_summaries(set(winner_serials))

        self.state = self.state.__class__.ENDED
        ended_payload = {
            "game_id": self.game_id,
            "duration": game_clock.now() - self.start_time if self.start_time else 0,
            "winner": winner,
            "time_remaining": self.time_remaining,
            "outcome": self.game_outcome(),
        }
        if force_ended:
            ended_payload["reason"] = self.force_end_reason
        await self.event_publisher("game_ended", ended_payload)

        logger.info("Zombie game ended")

    async def cleanup(self) -> None:
        """Cancel timer task, respawn tasks, and any tracked tasks."""
        if self.timer_task and not self.timer_task.done():
            self.timer_task.cancel()
        await super().cleanup()
        # A task cancelled before its first step never runs its own removal.
        self._conversion_colour_tasks.clear()

    async def _run_streamed_gameplay(self) -> bool:
        # The game timer counts from gameplay start, after the warm-up
        self.timer_task = self._track_task(asyncio.create_task(self._game_timer()))
        return await super()._run_streamed_gameplay()

    async def _teardown_before_end(self, teardown_span, progress) -> None:
        """Zombies ends no shield feedback before its winner effects (the finally skips it after them)."""

    def _teardown_after_outcome(self) -> None:
        """Zombies leaves the stream to the run-end cleanup (UC-030 BR-006)."""
