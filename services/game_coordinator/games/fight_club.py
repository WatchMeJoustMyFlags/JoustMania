"""
Fight Club Game Mode - gRPC-based implementation

1v1 arena tournament with queue system. Players take turns fighting
the current defender. Winner stays, loser goes to back of queue.
Highest score at end wins.

Original JoustMania behavior preserved.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from lib.colors import Colors
from lib.feature_flags import read_float_flag
from lib.telemetry import inject_trace_context
from lib.types import GameEvent, Sound
from proto import controller_manager_pb2
from services.game_coordinator import metrics
from services.game_coordinator.games.base import (
    GAME_MODE_ATTR,
    BaseGameMode,
    Phase,
    Player,
    resolve_non_negative_duration,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# Game constants (defaults, can be overridden via settings)
ROUND_DURATION = 22.0  # Seconds per round
DEFAULT_INVINCIBILITY_DURATION = 4.0  # Seconds of invincibility at round start
DEFAULT_MIN_ROUNDS = 10  # Minimum rounds before game can end
COUNTDOWN_BEEPS = 3  # Beeps in last 3 seconds of round
FC_INTRO_DURATION_S = 2.0  # Intro length; the stream is drained meanwhile (UC-025 A2 step 1)
ROUND_PAUSE_SECONDS = 1.0  # Pause between a round's kill and the next round

# Colors
DEFENDER_COLOR = Colors.Red.value  # Red for defender
FIGHTER_COLOR = Colors.Green.value  # Green for challenger
WAITING_COLOR = Colors.White20.value  # Dim gray for waiting


class FightState(Enum):
    """State of a player in Fight Club."""

    IN_LINE = "in_line"  # Waiting in queue
    DEFENDER = "defender"  # Current champion
    FIGHTER = "fighter"  # Challenging the defender
    FACE_OFF = "face_off"  # Tiebreaker (no time limit)


@dataclass
class FightClubPlayer(Player):
    """Extended Player class for Fight Club."""

    state: FightState = FightState.IN_LINE
    score: int = 0
    invincible_until: float = 0.0


class FightClubGame(BaseGameMode):
    """
    Fight Club game mode using gRPC communication.

    Players form a queue and fight 1v1 matches:
    - Each round has a 22 second time limit
    - First 4 seconds are invincibility period
    - Defender wins: stays, earns point, challenger goes to back of queue
    - Fighter wins: becomes defender, earns point
    - Time expires: both "die" and go to back of queue
    - Highest score after minimum rounds wins

    Special: Face-off mode for tied scores (no time limit).
    """

    def __init__(
        self,
        controller_manager_client,
        event_publisher,
        audio_client=None,
        game_id: str = "",
        initial_players: list | None = None,
        sensitivity: int = 2,
        invincibility_seconds: float = DEFAULT_INVINCIBILITY_DURATION,
        min_rounds: int = DEFAULT_MIN_ROUNDS,
        rng_seed: int = 0,
    ):
        """
        Initialize Fight Club game.

        Args:
            controller_manager_client: gRPC stub for ControllerManager service
            event_publisher: Callback function to publish game events
            audio_client: gRPC stub for Audio service
            game_id: Unique identifier for this game instance
            initial_players: Optional list of Player protobuf messages
            sensitivity: Sensitivity level 0-4 (passed from StartGameConfig)
            invincibility_seconds: Seconds of invincibility at round start
            min_rounds: Minimum rounds before game can end
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

        self.queue: list[str] = []  # Queue of player serials
        self.current_defender: str | None = None
        self.current_fighter: str | None = None
        self.round_number: int = 0
        self.round_end_time: float = 0.0
        self.round_task: asyncio.Task | None = None
        self.game_over: bool = False
        # True while the current round is a face-off (UC-025 A2 step 3).
        self.face_off_mode: bool = False
        # The tied leaders the next round pairs (set by _should_end_game, #1345).
        self._face_off_pair: tuple[str, str] | None = None
        # The winner, latched when the game decides to end (UC-025 BR-005, #1343).
        self._win_latched: bool = False
        self._latched_winner: str | None = None
        self.game_span: trace.Span | None = None
        # Configurable timing - now passed via config
        self._invincibility_duration: float = invincibility_seconds
        self._min_rounds: int = min_rounds

        # #766 F2: round duration promoted to ``game.fight_club.round_seconds``.
        # Read ONCE here (init-frozen); a non-positive/malformed value falls back
        # to the ROUND_DURATION module constant (a 0s round would break play).
        round_seconds = resolve_non_negative_duration(
            read_float_flag("game", "fight_club.round_seconds", ROUND_DURATION, game_id=self.game_id),
            ROUND_DURATION,
        )
        self._round_duration: float = round_seconds if round_seconds > 0 else ROUND_DURATION

    def get_game_name(self) -> str:
        """Return game mode identifier."""
        return "Fight Club"

    async def _initialize_players_impl(self, controllers: list):
        """
        Initialize players and create queue.

        Args:
            controllers: List of controller protobuf messages
        """
        # Initialize all players
        for controller in controllers:
            player = FightClubPlayer(
                serial=controller.serial,
                team=0,
                alive=True,
                color=WAITING_COLOR,
                state=FightState.IN_LINE,
                score=0,
            )
            self.players[controller.serial] = player
            self.queue.append(controller.serial)
            logger.debug(f"Added player: {controller.serial} to queue")

        logger.info(f"Initialized {len(self.players)} players in Fight Club")

        await self.event_publisher(
            "players_initialized",
            {
                "player_count": len(self.players),
                "serials": list(self.players.keys()),
            },
        )

    def _create_player_spans(self, game_context):  # noqa: ARG002
        """Create flat player lifecycle spans, parented to game span."""
        if not self.game_span:
            logger.warning("No game_span available, creating orphan player spans")
            for serial, player in self.players.items():
                player.span = tracer.start_span(
                    "player_lifecycle",
                    attributes={
                        "player.serial": serial,
                        GAME_MODE_ATTR: self.get_game_name(),
                    },
                )
            return

        # Create player spans as children of game span
        with trace.use_span(self.game_span, end_on_exit=False):
            for serial, player in self.players.items():
                player.span = tracer.start_span(
                    "player_lifecycle",
                    attributes={
                        "player.serial": serial,
                        GAME_MODE_ATTR: self.get_game_name(),
                    },
                )
                logger.debug(f"Created span for player {serial}")

    def _get_additional_phases(self) -> list:
        """Return Fight Club intro phase."""
        return [Phase(name="fight_club_intro", execute=self._intro_phase)]

    async def _intro_phase(self):
        """
        Fight Club introduction phase.

        Show all players as waiting, play intro sound.
        """
        logger.info("Starting Fight Club intro phase...")

        # A write error (the early stream already broke) is logged, not raised:
        # the drain stop at the end of the intro reopens the stream (UC-025 A2 step 1).
        try:
            # Set all controllers to waiting color
            for serial in self.players:
                if self.gameplay_stream:
                    color_cmd = controller_manager_pb2.GameplayStreamControl(
                        base_color=controller_manager_pb2.ControllerColorConfig(
                            serial=serial,
                            color=controller_manager_pb2.RGB(
                                r=WAITING_COLOR[0], g=WAITING_COLOR[1], b=WAITING_COLOR[2]
                            ),
                        )
                    )
                    await self.gameplay_stream.write(color_cmd)
        except Exception as e:
            logger.error(f"Failed to set the Fight Club intro colours: {e}", exc_info=True)

        # Play intro sound (use start sound - no dedicated fight club intro exists)
        await self._play_sound(Sound.SFX_START3, priority=2)

        # Wait for intro (interruptible, checked every 0.1 s)
        for _ in range(round(FC_INTRO_DURATION_S / 0.1)):
            if not self.running:
                return
            await asyncio.sleep(0.1)

        logger.info("Fight Club intro complete")

    async def _start_round(self):
        """
        Start a new round with defender and challenger.
        """
        self.round_number += 1

        pair = self._face_off_pair
        self._face_off_pair = None
        self.face_off_mode = pair is not None
        if pair is not None:
            self._seat_face_off_pair(pair)
        else:
            # If no defender, take first from queue
            if self.current_defender is None and self.queue:
                self.current_defender = self.queue.pop(0)

            # Never pair a player with itself (#1345)
            while self.queue and self.queue[0] == self.current_defender:
                self.queue.pop(0)

            # Get challenger from queue
            if self.queue:
                self.current_fighter = self.queue.pop(0)
            else:
                # No more challengers - game should end
                logger.info("No challengers left, ending game")
                self.game_over = True
                return

        defender = self.players[self.current_defender]
        fighter = self.players[self.current_fighter]

        # Set states
        defender.state = FightState.DEFENDER
        defender.color = DEFENDER_COLOR
        defender.alive = True
        defender.invincible_until = time.time() + self._invincibility_duration

        fighter.state = FightState.FIGHTER
        fighter.color = FIGHTER_COLOR
        fighter.alive = True
        fighter.invincible_until = time.time() + self._invincibility_duration

        # Set round end time
        if not self.face_off_mode:
            self.round_end_time = time.time() + self._round_duration
        else:
            self.round_end_time = float("inf")  # No time limit in face-off

        logger.info(
            f"Round {self.round_number}: {self.current_defender} (defender, score={defender.score}) "
            f"vs {self.current_fighter} (fighter, score={fighter.score})"
            f"{' [FACE-OFF]' if self.face_off_mode else ''}"
        )

        # Update colors
        await self._update_player_colors()

        # Play round start sound
        await self._play_sound(Sound.SFX_BEEP, priority=2)

        await self.event_publisher(
            "round_start",
            {
                "round": self.round_number,
                "defender": self.current_defender,
                "fighter": self.current_fighter,
                "defender_score": defender.score,
                "fighter_score": fighter.score,
                "face_off": self.face_off_mode,
            },
        )

        # Start round timer task (unless face-off)
        if not self.face_off_mode:
            self.round_task = self._track_task(asyncio.create_task(self._round_timer()))

    def _seat_face_off_pair(self, pair: tuple[str, str]) -> None:
        """Seat the tied leaders as this round's defender and fighter (UC-025 A2 step 3).

        A defender who is one of the pair keeps the defender role. A combatant who
        is not in the pair goes to the front of the queue (still in the rotation),
        and both pair members leave the queue, so nobody fights itself (#1345).
        """
        defender_serial, fighter_serial = pair
        if self.current_defender == fighter_serial:
            defender_serial, fighter_serial = fighter_serial, defender_serial

        displaced = [s for s in (self.current_defender, self.current_fighter) if s is not None and s not in pair]
        for serial in pair:
            while serial in self.queue:
                self.queue.remove(serial)
        for serial in reversed(displaced):
            player = self.players[serial]
            player.state = FightState.IN_LINE
            player.color = WAITING_COLOR
            self.queue.insert(0, serial)

        self.current_defender = defender_serial
        self.current_fighter = fighter_serial
        logger.info(f"Starting face-off: {defender_serial} vs {fighter_serial}")

    async def _round_timer(self):
        """
        Track round time and play countdown beeps.
        """
        while self.running:
            remaining = self.round_end_time - time.time()

            if remaining <= 0:
                # Time's up - both players lose
                logger.info("Round time expired - both players lose")
                await self._handle_timeout()
                break

            # Play beeps in last 3 seconds
            if remaining <= COUNTDOWN_BEEPS and remaining > 0:
                await self._play_sound(Sound.SFX_BEEP_LOUD, priority=2)

            await asyncio.sleep(1.0)

    async def _handle_timeout(self):
        """
        Handle round timeout - both players go to back of queue.
        """
        if self.current_defender:
            defender = self.players[self.current_defender]
            defender.state = FightState.IN_LINE
            defender.color = WAITING_COLOR
            self.queue.append(self.current_defender)

        if self.current_fighter:
            fighter = self.players[self.current_fighter]
            fighter.state = FightState.IN_LINE
            fighter.color = WAITING_COLOR
            self.queue.append(self.current_fighter)

        self.current_defender = None
        self.current_fighter = None

        await self.event_publisher(
            "round_timeout",
            {"round": self.round_number},
        )

        # Update colors and start next round
        await self._update_player_colors()

        if self._decide_game_end():
            return
        # A force end during the awaits above starts no next round (UC-025 BR-005).
        if not self.running or self.game_over:
            return
        await self._start_round()

    async def _update_player_colors(self):
        """Update all player colors based on their state."""
        for serial, player in self.players.items():
            fc_player = player
            color = WAITING_COLOR

            if fc_player.state == FightState.DEFENDER:
                color = DEFENDER_COLOR
            elif fc_player.state == FightState.FIGHTER:
                color = FIGHTER_COLOR

            if self.gameplay_stream:
                color_cmd = controller_manager_pb2.GameplayStreamControl(
                    base_color=controller_manager_pb2.ControllerColorConfig(
                        serial=serial,
                        color=controller_manager_pb2.RGB(r=color[0], g=color[1], b=color[2]),
                    )
                )
                await self.gameplay_stream.write(color_cmd)

    async def _check_win_condition(self) -> bool:
        """Check if game should end."""
        return self.game_over

    def _should_end_game(self) -> bool:
        """
        Check if game should end after this round.

        Game ends after minimum rounds if there's a clear winner.
        """
        if self.round_number < self._min_rounds:
            return False

        # Check if anyone has a clear lead
        scores = [(s, p.score) for s, p in self.players.items()]
        scores.sort(key=lambda x: x[1], reverse=True)

        if len(scores) >= 2 and scores[0][1] == scores[1][1]:
            # Top 2 are tied: the next round is a face-off between them. Only the
            # pair is stored; _start_round seats it, so the current round's
            # combatants stay in the rotation (#1345).
            if self._face_off_pair is None:
                self._face_off_pair = (scores[0][0], scores[1][0])
                logger.info(f"Face-off next: {scores[0][0]} vs {scores[1][0]}")
            return False

        return True

    def _leader(self) -> str | None:
        """The one top scorer; None on a tie at the top or without players."""
        scores = sorted((p.score for p in self.players.values()), reverse=True)
        if not scores or (len(scores) >= 2 and scores[0] == scores[1]):
            return None
        return max(self.players, key=lambda serial: self.players[serial].score)

    def _decide_game_end(self) -> bool:
        """End the game after this round when ``_should_end_game`` says so (UC-025 BR-005).

        Synchronous, no await: the winner and the root-span outcome are latched in
        the same step as the decision, so a force end that lands afterwards (same
        tick, the 1 s hold, the teardown) keeps the winner, and one that landed
        before it (``running`` already False) latches none (BR-003).
        """
        if not self._should_end_game():
            return False
        self.game_over = True
        if self.running and not self._win_latched:
            self._win_latched = True
            self._latched_winner = self._leader()
            self._latch_game_outcome()
        return True

    async def _kill_player_impl(self, serial: str, accel_mag: float):
        """
        Handle player death in a fight.

        Args:
            serial: Controller serial number
            accel_mag: Acceleration magnitude that caused death
        """
        player = self.players[serial]
        fc_player = player

        # Check immunity via the unified effective-grace rule (#817):
        # max(agent shield grace_until, admin invincible_until).
        if self.is_in_grace(fc_player):
            logger.debug(f"Player {serial} is invincible, ignoring death")
            return

        # Only process if player is in active fight
        if fc_player.state not in (FightState.DEFENDER, FightState.FIGHTER):
            return

        # Between rounds (the 1 s pause, or after the game decided) no round is
        # fought: a kill of the round winner has no opponent to score (#1343).
        if self.current_defender is None or self.current_fighter is None:
            logger.debug(f"Player {serial} killed between rounds, ignoring")
            return

        fc_player.alive = False

        # Cancel round timer
        if self.round_task and not self.round_task.done():
            self.round_task.cancel()

        # Determine winner
        if serial == self.current_defender:
            # Fighter wins!
            winner_serial = self.current_fighter
            loser_serial = self.current_defender
            logger.info(f"Fighter {winner_serial} defeats defender {loser_serial}!")
        else:
            # Defender wins!
            winner_serial = self.current_defender
            loser_serial = self.current_fighter
            logger.info(f"Defender {winner_serial} defeats fighter {loser_serial}!")

        winner = self.players[winner_serial]
        loser = self.players[loser_serial]

        # Update scores
        winner.score += 1

        # Winner becomes/stays defender
        winner.state = FightState.DEFENDER
        winner.color = DEFENDER_COLOR
        winner.alive = True
        self.current_defender = winner_serial

        # Loser goes to back of queue
        loser.state = FightState.IN_LINE
        loser.color = WAITING_COLOR
        loser.alive = True
        self.queue.append(loser_serial)
        self.current_fighter = None

        # The face-off round (if this was one) is over (#1345).
        self.face_off_mode = False

        logger.info(f"Score update: {winner_serial}={winner.score}, {loser_serial}={loser.score}")

        # The loser stays in the game: its death effect goes out now, before the
        # pause, so the next round's colours land after it (D-G32); the base kill
        # path skips its own trailing death effect (_sends_death_effect).
        await self._write_loser_death_effect(loser_serial)
        # The base kill path zeroed the gauge; the re-queued loser is still playing.
        # Skipped after a force end, which cleared the player analytics.
        if self.running:
            metrics.player_alive.labels(serial=loser_serial, game_id=self.game_id).set(1)

        # Play victory sound for round
        await self._play_sound(Sound.SFX_BEEP, priority=1)

        if player.span:
            player.span.add_event(
                "round_lost",
                attributes={
                    "accel_magnitude": accel_mag,
                    "round": self.round_number,
                    "winner": winner_serial,
                },
            )

        await self.event_publisher(
            "round_end",
            {
                "round": self.round_number,
                "winner": winner_serial,
                "loser": loser_serial,
                "winner_score": winner.score,
                "loser_score": loser.score,
            },
        )

        # Check if game should end
        if self._decide_game_end():
            return
        await self._pause_between_rounds()
        # A force end during the pause starts no next round (UC-025 BR-005).
        if not self.running or self.game_over:
            return
        await self._start_round()

    async def _pause_between_rounds(self) -> None:
        """Brief pause between a round's kill and the next round.

        A seam: tests replace it on the instance instead of patching ``asyncio.sleep``.
        """
        await asyncio.sleep(ROUND_PAUSE_SECONDS)

    def _sends_death_effect(self, serial: str) -> bool:  # noqa: ARG002
        """No base death effect: a Fight Club loser stays in the game (D-G32, #1342 class).

        ``_kill_player_impl`` writes the loser's death effect itself, before the pause
        and the next round's colours. A declined kill (invincible or waiting player)
        gets none.
        """
        return False

    async def _write_loser_death_effect(self, serial: str) -> None:
        """Send ``GAME_EFFECT_PLAYER_DEATH`` to the round loser, under its player span.

        Runs before the next round starts, so a call that ended under it (UC-030
        BR-006) skips the effect with a warning instead of aborting the round
        transition (#1345, the #1985 guard).
        """
        if not self.gameplay_stream:
            return
        player = self.players[serial]
        trace_parent, trace_state = inject_trace_context(player.span)
        await self._write_effect_guarded(
            controller_manager_pb2.GameplayStreamControl(
                game_effect=controller_manager_pb2.GameEffectCommand(
                    serial=serial,
                    effect=controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH,
                    trace_parent=trace_parent,
                    trace_state=trace_state,
                )
            ),
            "Round loser death effect",
        )

    async def _end_game_impl(self):
        """Handle game ending."""
        logger.info("Ending Fight Club game...")
        self.state = self.state.__class__.ENDING

        # Cancel round task
        if self.round_task and not self.round_task.done():
            self.round_task.cancel()

        # Only the latched winner counts (UC-025 BR-003, BR-005): a force end, a
        # stream end before the decision and a tie at the top declare none.
        force_ended = self.game_outcome() == "force_ended"
        winner_serial = None if force_ended else self._latched_winner
        winner_score = self.players[winner_serial].score if winner_serial else 0
        scores = [(s, p.score) for s, p in self.players.items()]
        scores.sort(key=lambda x: x[1], reverse=True)

        logger.info(f"Winner: {winner_serial or 'none'} with score {winner_score}")

        if winner_serial:
            if self.gameplay_stream:
                effect_cmd = controller_manager_pb2.GameplayStreamControl(
                    game_effect=controller_manager_pb2.GameEffectCommand(
                        serial=winner_serial,
                        effect=controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW,
                    )
                )
                await self._write_end_effect(effect_cmd)

            # Play victory sound (audio service handles voice selection)
            await self._play_sound(Sound.VOX_CONGRATULATIONS, priority=2)

            # Wait for the rainbow (interruptible by a force end)
            await self._wait_for_rainbow_effect()

        # End all player spans
        for serial, player in self.players.items():
            if player.span:
                fc_player = player
                is_winner = serial == winner_serial
                player.span.add_event(
                    "game_ended",
                    attributes={
                        "winner": is_winner,
                        "score": fc_player.score,
                        "final_rank": [s for s, _ in scores].index(serial) + 1,
                    },
                )
                player.span.set_status(Status(StatusCode.OK))
                player.span.end()

        self.state = self.state.__class__.ENDED
        ended_payload = {
            "game_id": self.game_id,
            "duration": time.time() - self.start_time if self.start_time else 0,
            "winner": winner_serial or "none",
            "winner_score": winner_score,
            "total_rounds": self.round_number,
            "final_scores": {s: p.score for s, p in self.players.items()},
            "outcome": self.game_outcome(),
        }
        if force_ended:
            ended_payload["reason"] = self.force_end_reason
        await self.event_publisher("game_ended", ended_payload)

        logger.info("Fight Club game ended")

    async def cleanup(self) -> None:
        """Cancel round timer and any tracked tasks."""
        if self.round_task and not self.round_task.done():
            self.round_task.cancel()
        await super().cleanup()

    async def on_force_end(self) -> None:
        """Handle force-end by marking game as over."""
        self.game_over = True
        await super().on_force_end()

    async def run(self):
        """
        Run the Fight Club game.

        Override to manage round-based gameplay.
        """
        from services.game_coordinator.games.base import GameState

        # The session game root span carries the outcome (#1328).
        self._capture_game_root_span()
        with tracer.start_as_current_span("fight_club_game") as game_span:
            self.game_span = game_span
            game_span.set_attribute("game.id", self.game_id)
            game_span.set_attribute(GAME_MODE_ATTR, self.get_game_name())

            try:
                self.running = True
                await self.event_publisher(GameEvent.GAME_STARTING, {"game_id": self.game_id})

                # Initialization phase
                with tracer.start_as_current_span("initialization_phase"):
                    # invincibility and min_rounds are now set in __init__ from StartGameConfig
                    logger.info(
                        f"Fight Club config: invincibility={self._invincibility_duration}s, "
                        f"min_rounds={self._min_rounds}"
                    )
                    await self._initialize_players()
                    if len(self.players) < 2:
                        raise ValueError(f"Need at least 2 players, got {len(self.players)}")
                    # Before any task is created: tasks copy the context (#1273).
                    self._set_flag_eval_context()
                    self._create_player_spans(None)

                # The intro colours go over the stream, so it opens first; a drain
                # discards the intro's frames and keeps disconnects (UC-025 A2 step 1).
                await self._start_gameplay_stream()
                self._start_early_stream_drain()
                phases_completed = False
                try:
                    for phase in self._get_additional_phases():
                        if not self.running:
                            break
                        with tracer.start_as_current_span(phase.name):
                            await phase.execute()
                    phases_completed = True
                finally:
                    await self._stop_early_stream_drain(reopen=phases_completed)

                if not self.running:
                    return

                # Countdown
                with tracer.start_as_current_span("countdown_phase"):
                    await self._countdown()

                if not self.running:
                    return

                # Start gameplay
                self.state = GameState.RUNNING
                self.start_time = time.time()

                # Fight Club publishes game_started after the countdown (UC-025 A2 step 1).
                await self.event_publisher(
                    GameEvent.GAME_STARTED, {"game_id": self.game_id, "player_count": len(self.players)}
                )

                # Start music (no tempo loop by design, UC-025 BR-002)
                await self._start_game_music()

                # Prime the EMA filters before death detection (UC-030 BR-003)
                await self._warmup_ema()

                if not self.running:
                    return

                # The first round (and its timer) starts after the warm-up
                await self._start_round()

                # Game loop (gameplay stream already started before intro)
                with tracer.start_as_current_span("gameplay_phase"):
                    await self._game_loop()
                self._latch_game_outcome()

                # End game
                with tracer.start_as_current_span("teardown_phase") as teardown_span:
                    # Shield LEDs end with the game, before the winner effects (#1301).
                    await self._end_shield_feedback()
                    await self._end_game_impl()
                    self._record_game_outcome(teardown_span)
                    # Winner effects are sent: close the stream (as base run(), UC-030 BR-006).
                    self._release_gameplay_stream()

                game_span.set_status(Status(StatusCode.OK))

            except Exception as e:
                logger.error(f"Game error: {e}", exc_info=True)
                game_span.record_exception(e)
                game_span.set_status(Status(StatusCode.ERROR, str(e)))
                raise
            finally:
                self._record_force_ended_outcome_if_unrecorded()
                force_ended = not self.running
                self.running = False
                await self._stop_game_music()
                if force_ended:
                    await self.on_force_end()
                await self._cleanup_after_run()

    async def _process_controller_state(self, state):
        """
        Detect hits for the two fighters only (UC-025 BR-002, #1347).

        The defender and the fighter go through the base path (UC-030 steps 4-7
        and A1): the ``perception.ema_weight`` EMA, the EMA reset during grace and
        invincibility (``effective_grace_until``), the ``warning_until`` gate,
        analytics, health, battery and name capture. Waiting players are not
        checked, but their name, battery and health are still captured.
        """
        player = self.players.get(state.serial)
        if player is None or not player.alive:
            return

        if player.state not in (FightState.DEFENDER, FightState.FIGHTER):
            self._capture_controller_info(player, state.serial, state)
            return

        await super()._process_controller_state(state)
