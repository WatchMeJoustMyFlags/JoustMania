"""
Tournament Game Mode - gRPC-based implementation

Single elimination bracket tournament with 1v1 matches.
Winner advances, loser is eliminated. Handles odd player counts
with byes (automatically advance to next round).

Original JoustMania behavior preserved.
"""

import asyncio
import logging
import math
from dataclasses import dataclass
from enum import Enum

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from lib.feature_flags import read_float_flag
from lib.types import GameEvent, Sound
from proto import controller_manager_pb2
from services.game_coordinator import game_clock
from services.game_coordinator.games.base import (
    GAME_MODE_ATTR,
    BaseGameMode,
    GameState,
    Phase,
    Player,
    resolve_non_negative_duration,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


# Colors for tournament states
WAITING_COLOR = (40, 40, 40)  # Gray while waiting
MATCH_COLORS = [
    (255, 0, 0),  # Red - Player 1
    (0, 0, 255),  # Blue - Player 2
]
WINNER_COLOR = (0, 255, 0)  # Green for match winner
ELIMINATED_COLOR = (0, 0, 0)  # Off when eliminated

# Match timing (defaults, can be overridden via settings)
MATCH_DURATION = 22.0  # Seconds per match
DEFAULT_INVINCIBILITY_DURATION = 4.0  # Seconds at start of match
TIME_BETWEEN_MATCHES = 5.0  # Pause between matches
ROUND_PAUSE_S = 2.0  # Pause after round_start, before the round's first match
TOURNAMENT_INTRO_DURATION_S = 3.0  # Intro length; the stream is drained meanwhile (UC-025 step 1)


class TournamentState(Enum):
    """Player state in tournament."""

    WAITING = "waiting"  # In bracket, waiting for match
    FIGHTING = "fighting"  # Currently in a match
    CHAMPION = "champion"  # Won the tournament
    ELIMINATED = "eliminated"  # Lost a match


@dataclass
class TournamentPlayer(Player):
    """Extended Player class with tournament state."""

    tournament_state: TournamentState = TournamentState.WAITING
    bracket_position: int = 0  # Position in bracket
    round_number: int = 1  # Current round (advances with wins)
    invincible_until: float = 0.0  # Timestamp when invincibility ends
    wins: int = 0  # Total wins in tournament


@dataclass
class Match:
    """Represents a single match in the tournament."""

    match_id: int
    round_number: int
    player1_serial: str | None = None  # None = waiting for winner from previous match
    player2_serial: str | None = None
    winner_serial: str | None = None
    is_complete: bool = False
    is_bye: bool = False  # True if only one player (automatic advance)


class TournamentGame(BaseGameMode):
    """
    Tournament game mode using gRPC communication.

    Single elimination bracket where players compete in 1v1 matches.
    Winners advance, losers are eliminated. Last player standing wins.

    Handles odd player counts with byes (some players automatically
    advance to next round).
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
        rng_seed: int = 0,
    ):
        """
        Initialize Tournament game.

        Args:
            controller_manager_client: gRPC stub for ControllerManager service
            event_publisher: Callback function to publish game events
            audio_client: gRPC stub for Audio service
            game_id: Unique identifier for this game instance
            initial_players: Optional list of Player protobuf messages
            sensitivity: Sensitivity level 0-4 (passed from StartGameConfig)
            invincibility_seconds: Seconds of invincibility at match start
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

        self.bracket: list[Match] = []
        self.current_match: Match | None = None
        self.current_round = 1
        self.total_rounds = 0
        self.match_span: trace.Span | None = None
        # The bracket is a state machine stepped on every gameplay frame of the base
        # game loop (UC-025 steps 2-4, D-G33): deadlines are on the game clock (UC-030 BR-010).
        self._match_deadline: float = 0.0  # the match time limit (A1)
        self._next_action_at: float = 0.0  # the end of the round pause or the pause between matches
        self._bracket_finished: bool = False  # no match left to play: the win condition
        self._bracket_error: Exception | None = None  # a bracket step failed: run() raises it
        self._stall_logged: bool = False
        self._disconnected: set[str] = set()  # serials whose controller disconnected (A4)
        # The result, latched in the step that decides the final (UC-025 step 5, BR-005).
        self._result_latched: bool = False
        self._champion: str | None = None
        self._champion_announced: bool = False
        self._no_winner_latched: bool = False
        self._no_winner_announced: bool = False
        # Configurable timing - now passed via config
        self._invincibility_duration: float = invincibility_seconds

        # #766 F2: match duration and inter-match pause promoted to game flags.
        # Read ONCE here (init-frozen); malformed values fall back to module
        # constants. A 0s match would break play, so it must be strictly positive;
        # a 0s inter-match pause is acceptable.
        match_seconds = resolve_non_negative_duration(
            read_float_flag("game", "tournament.match_seconds", MATCH_DURATION, game_id=self.game_id),
            MATCH_DURATION,
        )
        self._match_duration: float = match_seconds if match_seconds > 0 else MATCH_DURATION
        self._time_between_matches: float = resolve_non_negative_duration(
            read_float_flag(
                "game", "tournament.time_between_matches_seconds", TIME_BETWEEN_MATCHES, game_id=self.game_id
            ),
            TIME_BETWEEN_MATCHES,
        )

    def get_game_name(self) -> str:
        """Return game mode identifier."""
        return "Tournament"

    def get_instruction_clip(self) -> Sound | None:
        """Spoken instructions before the countdown (UC-025 A5, UC-020 A5)."""
        return Sound.MENU_VOX_TOURNAMENT_INSTRUCTIONS

    @staticmethod
    def _bye_match_indices(first_round_matches: int, num_byes: int) -> set[int]:
        """First-round match indices that are byes, spread so byes rarely meet in round 2 (#1994).

        Round-2 match j takes first-round matches 2j and 2j+1, so byes packed at the front
        pair up bye-vs-bye. Taking indices in bit-reversed order gives every round-2 match
        one bye before any gets a second (and spreads them over later rounds too).
        """
        bits = max(first_round_matches.bit_length() - 1, 0)
        order = sorted(range(first_round_matches), key=lambda i: int(format(i, f"0{bits}b")[::-1] or "0", 2))
        return set(order[:num_byes])

    def _generate_bracket(self, player_count: int) -> list[Match]:
        """
        Generate tournament bracket for given player count.

        Creates a balanced bracket with byes for non-power-of-2 counts.

        Args:
            player_count: Number of players

        Returns:
            List of Match objects representing the bracket
        """
        if player_count < 2:
            return []

        # Find next power of 2 >= player_count
        total_slots = 1
        while total_slots < player_count:
            total_slots *= 2

        self.total_rounds = int(math.log2(total_slots))
        num_byes = total_slots - player_count

        # Shuffle players for random seeding. Sort serials deterministically
        # BEFORE the RNG consumes them (#1003 CRN alignment): a shared seed only
        # makes the shuffle produce the same PERMUTATION, so the two games in a
        # pair must feed it the same pre-shuffle ordering — otherwise the
        # permutation maps to different players and CRN does not bite.
        serials = sorted(self.players.keys())
        self._rng.shuffle(serials)

        # Assign bracket positions
        for i, serial in enumerate(serials):
            player = self.players[serial]
            player.bracket_position = i

        matches = []
        match_id = 0

        # First round matches
        first_round_matches = total_slots // 2
        bye_matches = self._bye_match_indices(first_round_matches, num_byes)
        player_idx = 0

        for i in range(first_round_matches):
            match = Match(
                match_id=match_id,
                round_number=1,
            )
            match_id += 1

            # Assign players or byes
            if player_idx < len(serials):
                match.player1_serial = serials[player_idx]
                player_idx += 1

            if i in bye_matches:
                # This match is a bye - player1 auto-advances
                match.is_bye = True
                match.winner_serial = match.player1_serial
                match.is_complete = True
            elif player_idx < len(serials):
                match.player2_serial = serials[player_idx]
                player_idx += 1

            matches.append(match)

        # Generate placeholder matches for subsequent rounds
        matches_in_round = first_round_matches // 2
        current_round = 2

        while matches_in_round >= 1:
            for _ in range(matches_in_round):
                match = Match(
                    match_id=match_id,
                    round_number=current_round,
                )
                matches.append(match)
                match_id += 1

            matches_in_round //= 2
            current_round += 1

        # Bye winners enter round 2 now: bye matches are born complete, so
        # _get_next_match never returns them (#1990). _advance_winner reads self.bracket, so publish it first.
        self.bracket = matches
        for match in matches:
            if match.is_bye and match.winner_serial:
                self._advance_winner(match.winner_serial, match)

        logger.info(f"Generated bracket: {len(matches)} matches, {self.total_rounds} rounds, {num_byes} byes")
        return matches

    def _get_next_match(self) -> Match | None:
        """
        Get the next incomplete match that's ready to play.

        A match is ready if both players are assigned (bye matches are born
        complete and their winners are advanced at build time).

        Returns:
            Next match to play, or None if tournament is complete
        """
        for match in self.bracket:
            if match.is_complete:
                continue
            if match.player1_serial and match.player2_serial:
                return match
        return None

    def _advance_winner(self, winner_serial: str, completed_match: Match, carried_out: bool = False):
        """
        Advance winner to next round's match.

        Args:
            winner_serial: Serial of winning player
            completed_match: The match that was just completed
            carried_out: The serial is an out player carried into the next slot by a
                match with no winner (both players out): its round is not bumped.
        """
        if completed_match.round_number >= self.total_rounds:
            # Tournament winner!
            return

        # Find next round match for this winner
        next_round = completed_match.round_number + 1
        next_round_matches = [m for m in self.bracket if m.round_number == next_round]

        # Determine which match and slot
        matches_in_prev_round = len([m for m in self.bracket if m.round_number == completed_match.round_number])
        match_index = completed_match.match_id % matches_in_prev_round
        next_match_index = match_index // 2

        if next_match_index < len(next_round_matches):
            next_match = next_round_matches[next_match_index]

            # Assign to first or second slot based on match position
            if match_index % 2 == 0:
                next_match.player1_serial = winner_serial
            else:
                next_match.player2_serial = winner_serial

            logger.info(f"Advanced {winner_serial} to round {next_round}, match {next_match.match_id}")

            if carried_out:
                return
            # Update player state; a winner that disconnected stays out (A4), and
            # its next opponent gets a walkover (_resolve_walkovers).
            player = self.players[winner_serial]
            player.round_number = next_round
            if player.tournament_state != TournamentState.ELIMINATED:
                player.tournament_state = TournamentState.WAITING

    async def _initialize_players_impl(self, controllers: list):
        """
        Initialize players for tournament.

        Args:
            controllers: List of controller protobuf messages
        """
        for controller in controllers:
            player = TournamentPlayer(
                serial=controller.serial,
                team=-1,  # No teams in tournament
                alive=True,
                color=WAITING_COLOR,
                tournament_state=TournamentState.WAITING,
            )
            self.players[controller.serial] = player
            logger.debug(f"Added player: {controller.serial}")

        # Generate bracket
        self.bracket = self._generate_bracket(len(controllers))

        logger.info(f"Initialized {len(controllers)} players for tournament")

        await self.event_publisher(
            "players_initialized",
            {
                "player_count": len(controllers),
                "total_rounds": self.total_rounds,
                "bracket_size": len(self.bracket),
                "serials": list(self.players.keys()),
            },
        )

    def _create_player_spans(self, game_context):
        """Create flat player spans (no team hierarchy in tournament)."""
        # Get current context if not provided
        if game_context is None:
            from opentelemetry import context as otel_context

            game_context = otel_context.get_current()

        for serial, player in self.players.items():
            player.span = tracer.start_span(
                "player_lifecycle",
                context=game_context,
                attributes={
                    "player.serial": serial,
                    "player.bracket_position": player.bracket_position,
                    "game.mode": "Tournament",
                },
            )
            logger.debug(f"Created span for player {serial}")

    def _get_additional_phases(self) -> list:
        """Return tournament-specific phases."""
        return [Phase(name="tournament_intro", execute=self._tournament_intro_phase)]

    async def _tournament_intro_phase(self):
        """Show bracket and prepare for first matches."""
        logger.info("Starting tournament intro phase...")

        await self.event_publisher(
            "tournament_intro",
            {
                "total_players": len(self.players),
                "total_rounds": self.total_rounds,
                "byes": len([m for m in self.bracket if m.is_bye]),
            },
        )

        # Set all to waiting color. A write on a broken early stream is skipped:
        # the drain stop at the end of the intro reopens the stream (UC-025 step 1).
        for serial in self.players:
            await self._set_base_color(serial, WAITING_COLOR)

        # Play intro sound (use start sound - no dedicated tournament intro exists)
        await self._play_sound(Sound.SFX_START3, priority=2)

        # Wait for intro (interruptible, checked every 0.1 s)
        for _ in range(round(TOURNAMENT_INTRO_DURATION_S / 0.1)):
            if not self.running:
                return
            await asyncio.sleep(0.1)

        logger.info("Tournament intro phase complete")

    async def _set_base_color(self, serial: str, color: tuple[int, int, int]) -> None:
        """Set a player's LED colour and keep ``player.color`` in sync.

        A reopened stream sends ``player.color`` for every player in its config, so
        a mid-game reopen restores the current colours (UC-025 step 2, UC-030 BR-006).
        """
        self.players[serial].color = color
        # Guarded: a write during the reconnect backoff (the old call is finished but
        # still ``self.gameplay_stream``) must not end the bracket (UC-030 BR-006).
        await self._write_effect_guarded(
            controller_manager_pb2.GameplayStreamControl(
                base_color=controller_manager_pb2.ControllerColorConfig(
                    serial=serial,
                    color=controller_manager_pb2.RGB(r=color[0], g=color[1], b=color[2]),
                )
            ),
            "Tournament colour",
        )

    async def _start_match(self, match: Match) -> None:
        """Start a match: both fighters FIGHTING, red and blue, invincible (UC-025 step 2)."""
        p1 = self.players[match.player1_serial]
        p2 = self.players[match.player2_serial]

        # Set match states
        p1.tournament_state = TournamentState.FIGHTING
        p2.tournament_state = TournamentState.FIGHTING
        p1.alive = True
        p2.alive = True
        self.current_match = match
        # Invincible from the seating on, before the first await below: a kill or an
        # eliminate_player the loop runs during match_start or the colour writes is
        # declined (UC-025 step 2, BR-006; #2025 S3). Duration: invincibility_seconds.
        invincible_until = game_clock.now() + self._invincibility_duration
        p1.invincible_until = invincible_until
        p2.invincible_until = invincible_until

        # Create match span (inherits current active context: gameplay_phase)
        self.match_span = tracer.start_span(
            "tournament_match",
            attributes={
                "match.id": match.match_id,
                "match.round": match.round_number,
                "player1.serial": match.player1_serial,
                "player2.serial": match.player2_serial,
            },
        )

        logger.info(f"Match {match.match_id} starting: {match.player1_serial} vs {match.player2_serial}")

        await self.event_publisher(
            "match_start",
            {
                "match_id": match.match_id,
                "round": match.round_number,
                "player1": match.player1_serial,
                "player2": match.player2_serial,
            },
        )

        # Set colors
        await self._set_base_color(match.player1_serial, MATCH_COLORS[0])
        await self._set_base_color(match.player2_serial, MATCH_COLORS[1])

        # Play match start sound (use beep - no dedicated fight sound exists)
        await self._play_sound(Sound.SFX_BEEP_LOUD, priority=2)

        # The match time limit (A1) runs from here
        self._match_deadline = game_clock.now() + self._match_duration

    async def _finalize_match(self, match: Match) -> None:
        """End a decided match: winner effect, loser off, ``match_end``, advance (UC-025 steps 3-4).

        Runs once per match, for a kill (or a fighter's disconnect, A4), where
        ``_kill_player_impl`` decided the match, and for the timeout (A1), where
        the bracket step picked the winner.
        """
        match.is_complete = True
        self.current_match = None
        self._next_action_at = game_clock.now() + self._time_between_matches

        # Determine loser
        loser_serial = match.player2_serial if match.winner_serial == match.player1_serial else match.player1_serial

        winner = self.players[match.winner_serial]
        loser = self.players[loser_serial]

        winner.wins += 1
        if winner.tournament_state == TournamentState.FIGHTING:
            winner.tournament_state = TournamentState.WAITING
        loser.tournament_state = TournamentState.ELIMINATED
        loser.alive = False

        # Show winner effect. GAME_EFFECT_WIN_FLASH never existed in the
        # current GameEffect enum (third proto-drift crash in this mode's
        # match path, after the serials field and the __anext__ call) —
        # use WINNER_RAINBOW, the celebration effect the champion path
        # (#699) already uses; the runtime AttributeError killed the game
        # at first match completion. The final gets none: the champion's
        # rainbow comes with the end of the game (UC-025 step 5, #1986).
        if not self._is_final(match):
            await self._write_effect_guarded(
                controller_manager_pb2.GameplayStreamControl(
                    game_effect=controller_manager_pb2.GameEffectCommand(
                        serial=match.winner_serial,
                        effect=controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW,
                    )
                ),
                "Tournament match winner effect",
            )

        # Turn off loser
        await self._set_base_color(loser_serial, ELIMINATED_COLOR)

        # End loser's span
        if loser.span:
            loser.span.add_event(
                "eliminated",
                attributes={
                    "match_id": match.match_id,
                    "round": match.round_number,
                    "eliminated_by": match.winner_serial,
                },
            )
            loser.span.set_status(Status(StatusCode.OK))
            loser.span.end()
            loser.span = None  # Mark as closed to prevent double-ending

        # A force-end during the writes above publishes no match_end (UC-025 BR-003)
        if self.running:
            await self.event_publisher(
                "match_end",
                {
                    "match_id": match.match_id,
                    "round": match.round_number,
                    "winner": match.winner_serial,
                    "loser": loser_serial,
                },
            )

        # End match span
        if self.match_span:
            self.match_span.add_event(
                "match_complete",
                attributes={
                    "winner": match.winner_serial,
                    "loser": loser_serial,
                },
            )
            self.match_span.set_status(Status(StatusCode.OK))
            self.match_span.end()
            self.match_span = None

        # Advance winner
        self._advance_winner(match.winner_serial, match)

    def _resolve_walkovers(self) -> None:
        """Advance the opponent of a player who left the bracket by a disconnect (UC-025 A4).

        A match not yet played whose slot holds an eliminated player goes to the
        other player without a match. A slot still waiting for its feeder waits:
        the walkover happens once the feeder's winner is advanced. A match left
        with two eliminated players ends with no winner and carries one of them
        (still eliminated) into the next round, so that match's other player
        gets the walkover instead of waiting on an empty slot.
        """
        changed = True
        while changed:
            changed = False
            for match in self.bracket:
                if match.is_complete or match is self.current_match:
                    continue
                slots = [s for s in (match.player1_serial, match.player2_serial) if s is not None]
                out = [s for s in slots if self.players[s].tournament_state == TournamentState.ELIMINATED]
                if not out:
                    continue
                remaining = [s for s in slots if s not in out]
                if remaining:
                    match.winner_serial = remaining[0]
                    match.is_complete = True
                    logger.info(f"Match {match.match_id}: walkover for {remaining[0]} ({out[0]} is out)")
                    self._advance_winner(remaining[0], match)
                    changed = True
                elif len(out) == 2:
                    match.is_complete = True
                    logger.warning(f"Match {match.match_id}: both players are out, no winner")
                    self._advance_winner(out[0], match, carried_out=True)
                    changed = True

    def _eliminate_on_timeout(self, match: Match) -> None:
        """Count the loser of a timed-out match out (UC-025 A1).

        The same elimination gauges and players-alive aggregate as a death
        (``dead_count``, ``game_player_alive`` 0, the elimination order), but no
        death: no explosion, no death effect, no ``game_player_deaths_total``
        and no ``player_death`` span event. ``_finalize_match`` turns its LED off.
        """
        loser_serial = match.player2_serial if match.winner_serial == match.player1_serial else match.player1_serial
        loser = self.players[loser_serial]
        loser.tournament_state = TournamentState.ELIMINATED
        loser.alive = False
        self._count_elimination(loser_serial)
        self._set_players_alive_aggregate(len([p for p in self.players.values() if p.alive]))

    async def _bracket_tick(self) -> None:
        """Step the bracket once per gameplay frame (UC-025 steps 2-4, A1, D-G33).

        The base game loop reads the stream for the whole game; this runs after
        each frame's detection. A decided match is finalised, a match past its
        time limit gets a random winner, and after the round or between-match
        pause the next match starts. ``_bracket_finished`` is set once no match is
        left and at most one player remains.
        """
        if self._bracket_finished or not self.running:
            return
        match = self.current_match
        if match is not None:
            if not match.is_complete:
                if game_clock.now() < self._match_deadline:
                    return
                # Time's up - both survive, random winner (A1)
                match.winner_serial = self._rng.choice([match.player1_serial, match.player2_serial])
                logger.info(f"Match {match.match_id} timeout - random winner: {match.winner_serial}")
                self._eliminate_on_timeout(match)
                if self._is_final(match):
                    self._latch_champion(match.winner_serial)
            await self._finalize_match(match)
            await self._announce_champion()
            return

        if game_clock.now() < self._next_action_at:
            return

        self._resolve_walkovers()
        next_match = self._get_next_match()
        if next_match is None:
            left = [s for s, p in self.players.items() if p.tournament_state != TournamentState.ELIMINATED]
            if len(left) <= 1:
                logger.info("Tournament complete!")
                self._bracket_finished = True
                # A final won by a walkover (A4) crowns the player left.
                if len(left) == 1:
                    self._latch_champion(left[0])
                    await self._announce_champion()
                elif self._latch_no_winner():
                    # Nobody is left (A3): no champion, no rainbow, no voice.
                    await self._announce_no_winner()
            elif not self._stall_logged:
                # No playable match yet more than one player is left: the bracket is
                # stuck (a bye that never advanced was #1990; a match with two
                # disconnected players used to feed an empty slot, #2000).
                self._stall_logged = True
                logger.error(f"Tournament bracket stalled with {len(left)} players left and no playable match")
            return

        # Check if round changed
        if next_match.round_number > self.current_round:
            self.current_round = next_match.round_number
            logger.info(f"Starting round {self.current_round}")
            await self.event_publisher(
                "round_start",
                {"round": self.current_round, "total_rounds": self.total_rounds},
            )
            # Play round start sound (use start - no dedicated round sound exists)
            await self._play_sound(Sound.SFX_START, priority=2)
            self._next_action_at = game_clock.now() + ROUND_PAUSE_S
            return

        await self._start_match(next_match)

    async def _process_gameplay_update(self, gameplay_update) -> bool:
        """Base frame processing (disconnects, then detection), then one bracket step.

        A failing bracket step before the champion is latched ends the game loop
        with the error kept for run(), which raises it (``game_error``) and latches
        no outcome; it is kept here rather than raised to the base loop so that a
        failure after a force-end is still reported (UC-030 BR-006). After the
        latch an error, a gRPC error of the gameplay call included (no reconnect),
        is logged and the result stands: the game ends through the teardown with
        the champion and ``completed`` (UC-025 BR-005, D-G34).
        """
        try:
            game_over = await super()._process_gameplay_update(gameplay_update)
        except self._STREAM_CALL_ERRORS:
            if not self._result_latched:
                raise
            # The deciding frame's write (the final loser's death effect) failed:
            # no reconnect, the teardown skips its effects on a failed call.
            logger.warning(
                "Gameplay stream failed in the frame that decided the final; the result stands", exc_info=True
            )
            return True
        except Exception:
            if not self._result_latched:
                raise
            logger.error("Tournament frame failed after the champion was latched; the result stands", exc_info=True)
            return True
        try:
            if game_over:
                # The final was decided in this frame: finish it now (UC-025 step 5).
                await self._finish_decided_final()
                return True
            await self._bracket_tick()
        except Exception as e:
            if self._result_latched:
                logger.error(
                    f"Tournament bracket step failed after the champion was latched; the result stands: {e}",
                    exc_info=True,
                )
                return True
            logger.error(f"Tournament bracket step failed: {e}", exc_info=True)
            self._bracket_error = e
            return True
        return await self._check_win_condition()

    def _latch_game_outcome(self) -> None:
        """A bracket failure ends the game loop but is no natural end: nothing is latched."""
        if self._bracket_error is not None:
            return
        super()._latch_game_outcome()

    async def _process_controller_state(self, controller_state):
        """Hit detection only for the two fighters of the match (UC-025 step 3).

        A waiting player is not checked (#1737), but its controller name, health
        and battery are still read, so the battery guard and the span name stay
        current (UC-030 step 3).
        """
        serial = controller_state.serial
        player = self.players.get(serial)
        if player is not None and player.alive and player.tournament_state != TournamentState.FIGHTING:
            self._capture_controller_info(player, serial, controller_state)
            return
        await super()._process_controller_state(controller_state)

    async def _handle_disconnect(self, serial: str) -> None:
        """A disconnect takes the player out of the bracket (UC-025 A4).

        Marked before the base death path, so the kill handler knows: a fighter
        forfeits its match even during invincibility, and a waiting player is out
        (its next opponent gets a walkover in the next bracket step).
        """
        player = self.players.get(serial)
        if player is None or not player.alive:
            return
        self._disconnected.add(serial)
        await super()._handle_disconnect(serial)

    async def _check_win_condition(self) -> bool:
        """The game ends once the final is decided, the bracket has no match left, or a bracket step failed.

        Called by the base game loop after every disconnect and controller sample;
        it publishes nothing (the champion is announced by ``_announce_champion``).
        """
        return self._result_latched or self._bracket_finished or self._bracket_error is not None

    def _is_final(self, match: Match) -> bool:
        """Whether the match is the final (the last round has one match)."""
        return match.round_number >= self.total_rounds

    def _latch_champion(self, serial: str) -> bool:
        """Latch the champion and the root-span outcome when the final is decided (UC-025 BR-005).

        Synchronous, no await: called in the step that decides the final (the
        kill, the time limit, a walkover), so a force-end that lands afterwards
        keeps the champion, and one that landed before it (``running`` already
        False) latches none (BR-003). The outcome is latched first and checked
        again (defensive): a force-end could land between the running
        check and the latch, and then no champion may be set or announced.
        Returns whether it latched.
        """
        if self._result_latched or not self.running:
            return False
        self._latch_game_outcome()
        if self.game_outcome() == "force_ended":
            return False
        self._result_latched = True
        self._champion = serial
        self.players[serial].tournament_state = TournamentState.CHAMPION
        logger.info(f"Tournament champion: {serial} with {self.players[serial].wins} wins!")
        return True

    async def _announce_champion(self) -> None:
        """Publish ``tournament_champion`` once, only for a latched champion (UC-025 step 5)."""
        if self._champion is None or self._champion_announced:
            return
        self._champion_announced = True
        champion = self.players[self._champion]
        await self.event_publisher(
            "tournament_champion",
            {
                "champion": self._champion,
                "wins": champion.wins,
                "rounds_played": max(self.current_round, champion.round_number),
            },
        )

    async def _finish_decided_final(self) -> None:
        """Finalise the decided final (loser off, ``match_end``; no match rainbow) and announce the champion.

        Idempotent: the frame that decided the final runs it, and the end of the
        game runs it again for a final decided outside a frame (an intervention).
        """
        if self._champion is None:
            return
        match = self.current_match
        if match is not None and match.is_complete:
            try:
                await self._finalize_match(match)
            except Exception as e:
                # The champion is latched: the result stands (UC-025 BR-005, D-G34).
                logger.error(f"Finishing the decided final failed; the champion stands: {e}", exc_info=True)
        await self._announce_champion()

    def _latch_no_winner(self) -> bool:
        """Latch the outcome of a bracket that finished with nobody left (UC-025 A3, BR-003).

        Synchronous, no await, like ``_latch_champion``: a force-end that landed
        before it (``running`` already False, or between the check and the latch)
        keeps its ``force_ended`` outcome and nothing is announced. Returns
        whether it latched.
        """
        if self._result_latched or not self.running:
            return False
        self._latch_game_outcome()
        if self.game_outcome() == "force_ended":
            return False
        self._result_latched = True
        self._no_winner_latched = True
        logger.info("Tournament ended with no winner")
        return True

    async def _announce_no_winner(self) -> None:
        """Publish ``tournament_no_winner`` once, only for a latched no-winner bracket (UC-025 A3)."""
        if not self._no_winner_latched or self._no_winner_announced:
            return
        self._no_winner_announced = True
        await self.event_publisher("tournament_no_winner", {})

    def _accepts_kill(self, serial: str) -> bool:
        """The Tournament kill gate (UC-025 BR-006, UC-030 step 9; #1383, #1737, D-G8).

        No kill once the champion is latched (BR-005). A disconnected player is
        always taken (A4: a fighter forfeits even in grace, a waiting player is
        out). Otherwise only a fighter of the undecided current match out of its
        grace can lose: a waiting or bye player, a fighter in grace, the winner of
        a decided match (a double hit, BR-004) and anyone between matches are
        declined before the base death accounting, so an ``eliminate_player`` on
        them is refused (``eliminate_refused``). Grace is the unified effective
        grace (#817): max(agent shield, match invincibility).
        """
        if self._result_latched:
            return False
        if serial in self._disconnected:
            return True
        player = self.players.get(serial)
        return player is not None and self._fights_undecided_match(serial) and not self.is_in_grace(player)

    async def _kill_player_impl(self, serial: str, accel_mag: float):
        """
        Handle player death during a match.

        The base kill path calls this only for a kill ``_accepts_kill`` took,
        synchronously after its second check; the check here keeps a direct call
        to the same rule.

        Args:
            serial: Controller serial number
            accel_mag: Acceleration magnitude that caused death
        """
        if not self._accepts_kill(serial):
            return
        player = self.players[serial]

        # A disconnected player that is not fighting an undecided match (a waiting
        # player, or the winner of a match decided in this frame) leaves the
        # bracket; its next opponent gets a walkover (UC-025 A4).
        if not self._fights_undecided_match(serial):
            player.tournament_state = TournamentState.ELIMINATED
            player.alive = False
            player.color = ELIMINATED_COLOR
            logger.info(f"Player {serial} disconnected while waiting: out of the bracket")
            return

        # Determine winner
        if serial == self.current_match.player1_serial:
            self.current_match.winner_serial = self.current_match.player2_serial
        else:
            self.current_match.winner_serial = self.current_match.player1_serial

        # Mark the loser ELIMINATED immediately so a SECOND near-simultaneous
        # kill (e.g. a test killing both fighters, or the #757 mock death-hold
        # firing on both controllers) is rejected by the FIGHTING guard above
        # and cannot flip the winner back to the player who just lost. Without
        # this, blind double-kills corrupted the bracket and it never finished.
        player.tournament_state = TournamentState.ELIMINATED
        player.alive = False

        # Signal the bracket step to finalise the match (advance the winner, emit
        # match_end) after this frame (_bracket_tick -> _finalize_match).
        self.current_match.is_complete = True

        winner = self.current_match.winner_serial
        # The final's decision latches the champion in this same step (UC-025 BR-005).
        if self._is_final(self.current_match):
            self._latch_champion(winner)
        logger.info(f"Match {self.current_match.match_id}: {serial} eliminated, winner: {winner}")

        # Add death event to player span
        if player.span:
            player.span.add_event(
                "match_death",
                attributes={
                    "match_id": self.current_match.match_id,
                    "accel_magnitude": accel_mag,
                    "sensitivity": self.sensitivity.name,
                },
            )

    def _fights_undecided_match(self, serial: str) -> bool:
        """Whether the player is a fighter of the current match and that match is not decided yet."""
        match = self.current_match
        return (
            match is not None
            and not match.is_complete
            and serial in (match.player1_serial, match.player2_serial)
            and self.players[serial].tournament_state == TournamentState.FIGHTING
        )

    def _sends_death_effect(self, serial: str) -> bool:
        """PLAYER_DEATH only for a player that is not alive after the kill (UC-025 BR-004, #1991).

        ``_kill_player_impl`` sets ``alive`` False only for the match loser; a kill it
        ignores (waiting player, grace, decided match) leaves the player alive, and
        the base kill path must not paint that player dead. A player already eliminated
        by another path (e.g. an intervention) may still get the effect; harmless.
        """
        return not self.players[serial].alive

    async def _end_game_impl(self):
        """Handle tournament ending."""
        logger.info("Ending Tournament game...")
        self.state = self.state.__class__.ENDING

        # Only the latched champion counts (UC-025 BR-003, BR-005): a force-end, a
        # stream end or a bracket that ends before the final is decided crown none.
        force_ended = self.game_outcome() == "force_ended"
        champion_serial = None if force_ended else self._champion

        # Celebrate champion
        if champion_serial:
            try:
                await self._finish_decided_final()
            except Exception as e:
                # The champion is latched: the result stands (UC-025 BR-005, D-G34).
                logger.error(f"Announcing the champion failed; the result stands: {e}", exc_info=True)
            if self.gameplay_stream:
                effect_cmd = controller_manager_pb2.GameplayStreamControl(
                    game_effect=controller_manager_pb2.GameEffectCommand(
                        serial=champion_serial,
                        effect=controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW,
                    )
                )
                await self._write_end_effect(effect_cmd)

            await self._play_sound(Sound.VOX_CONGRATULATIONS, priority=2)

            # Wait for the rainbow (interruptible by a force-end)
            await self._wait_for_rainbow_effect()

        # End remaining spans
        for serial, player in self.players.items():
            if player.span:
                is_champion = serial == champion_serial
                player.span.add_event(
                    "tournament_ended",
                    attributes={
                        "is_champion": is_champion,
                        "wins": player.wins,
                        "final_round": player.round_number,
                    },
                )
                player.span.set_status(Status(StatusCode.OK))
                player.span.end()
                player.span = None

        self.state = self.state.__class__.ENDED
        ended_payload = {
            "game_id": self.game_id,
            "duration": game_clock.now() - self.start_time if self.start_time else 0,
            "champion": champion_serial or "none",
            "total_matches": len([m for m in self.bracket if m.is_complete and not m.is_bye]),
            "outcome": self.game_outcome(),
        }
        if force_ended:
            ended_payload["reason"] = self.force_end_reason
        await self.event_publisher("game_ended", ended_payload)

        logger.info("Tournament game ended")

    async def cleanup(self) -> None:
        """End the match span; the shared run-end cleanup ends any player span still open (#1997)."""
        if self.match_span:
            self.match_span.end()
            self.match_span = None
        await super().cleanup()

    async def run(self):
        """
        Run the Tournament game.

        Same start and teardown as Fight Club and Werewolf (UC-025 step 1, Pre-1,
        BR-002): the bracket runs on the base game loop (D-G33, #1882).
        """
        # Set once the teardown step ended the shield feedback; otherwise the
        # finally ends it (UC-020 failure postcondition, #2357).
        shield_feedback_ended = False
        # The session game root span carries the outcome (#1328).
        self._capture_game_root_span()
        with tracer.start_as_current_span("tournament_game") as game_span:
            self.game_span = game_span
            game_span.set_attribute("game.id", self.game_id)
            game_span.set_attribute(GAME_MODE_ATTR, self.get_game_name())

            try:
                self.running = True
                await self.event_publisher(GameEvent.GAME_STARTING, {"game_id": self.game_id})

                # Initialization phase
                with tracer.start_as_current_span("initialization_phase") as init_span:
                    await self._initialize_players()
                    if len(self.players) < 2:
                        raise ValueError(f"Need at least 2 players, got {len(self.players)}")
                    # Before any task is created: tasks copy the context (#1273).
                    self._set_flag_eval_context()
                    self._create_player_spans(None)  # Uses current context
                    # The intro colours go over the stream, so it opens first; a drain
                    # discards the intro's frames and keeps disconnects (UC-025 step 1).
                    # The instruction clip follows the intro, before the countdown (UC-020 A5).
                    await self._run_streamed_intro_and_instructions(init_span)

                if not self.running:
                    return

                # Countdown
                with tracer.start_as_current_span("countdown_phase"):
                    await self._countdown()

                if not self.running:
                    return

                # Start gameplay
                self.state = GameState.RUNNING
                self.start_time = game_clock.now()

                # Emit game_started event (required for integration tests)
                await self.event_publisher(
                    GameEvent.GAME_STARTED, {"game_id": self.game_id, "player_count": len(self.players)}
                )

                # Start music (no tempo loop by design, UC-025 BR-002)
                await self._start_game_music()

                # Prime the EMA filters before death detection (UC-030 BR-003)
                await self._warmup_ema()

                if not self.running:
                    return

                # The bracket runs on the base game loop: one stream read for the whole
                # game, a bracket step per frame (D-G33, #1882). Match spans start
                # inside gameplay_phase.
                with tracer.start_as_current_span("gameplay_phase"):
                    await self._game_loop()
                if self._bracket_error is not None:
                    raise self._bracket_error
                self._latch_game_outcome()
                # The end step closes the player spans with tournament_ended; an early
                # exit or an error leaves them to the run-end cleanup (#1997).

                # End game
                with tracer.start_as_current_span("teardown_phase") as teardown_span:
                    # Shield LEDs end with the game, before the winner effects (#1301).
                    await self._end_shield_feedback()
                    shield_feedback_ended = True
                    await self._end_game_impl()
                    self._record_game_outcome(teardown_span)
                    # Winner effects are sent: close the stream (as base run(), UC-030 BR-006).
                    self._release_gameplay_stream()

                game_span.set_status(Status(StatusCode.OK))

            except Exception as e:
                self._run_failed = True
                logger.error(f"Game error: {e}", exc_info=True)
                game_span.record_exception(e)
                game_span.set_status(Status(StatusCode.ERROR, str(e)))
                raise
            finally:
                self._record_force_ended_outcome_if_unrecorded()
                force_ended = not self.running
                self.running = False
                # The music stops here on every path; the shield feedback only when
                # the teardown step did not end it (error, cancel or early return).
                await self._stop_run_feedback(False, shield_feedback_ended)
                if force_ended:
                    await self.on_force_end()
                await self._cleanup_after_run()
