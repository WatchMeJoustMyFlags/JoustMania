"""
TeamsGameBase - Abstract base class for team-based game modes

Phase 36b: Extracts common team logic from Teams and Random Teams.
Provides hierarchical span management (team → player) and team-based win conditions.

Subclasses:
- SimpleTeamsGame (Teams): Round-robin team assignment
- RandomTeamsGame (Random Teams): Random team assignment with team_formation_phase
"""

import logging
from dataclasses import dataclass

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from lib.colors import Colors
from lib.types import GameEvent, Sound
from services.game_coordinator import game_clock
from services.game_coordinator.games.base import BaseGameMode
from services.game_coordinator.stream_writes import serialized_write

tracer = trace.get_tracer(__name__)
logger = logging.getLogger(__name__)


# Team colors (from lib/colors.py - first 8 colors)
TEAM_COLORS = [
    {"name": "Pink", "rgb": Colors.Pink.value},
    {"name": "Magenta", "rgb": Colors.Magenta.value},
    {"name": "Orange", "rgb": Colors.Orange.value},
    {"name": "Yellow", "rgb": Colors.Yellow.value},
    {"name": "Green", "rgb": Colors.Green.value},
    {"name": "Turquoise", "rgb": Colors.Turquoise.value},
    {"name": "Blue", "rgb": Colors.Blue.value},
    {"name": "Purple", "rgb": Colors.Purple.value},
]

# Map team names to victory sounds
# Teams without specific sounds fall back to congratulations
# Audio service resolves full path and voice folder based on menu_voice setting
TEAM_WIN_SOUNDS: dict[str, Sound] = {
    "Blue": Sound.VOX_BLUE_TEAM_WIN,
    "Magenta": Sound.VOX_MAGENTA_TEAM_WIN,
    "Green": Sound.VOX_GREEN_TEAM_WIN,
    "Yellow": Sound.VOX_YELLOW_TEAM_WIN,
    "Turquoise": Sound.VOX_CYAN_TEAM_WIN,  # Turquoise uses cyan sound
}


@dataclass
class Team:
    """Represents a team in the game."""

    team_num: int
    name: str
    color: tuple
    span: trace.Span | None = None  # OpenTelemetry span for team lifecycle


class TeamsGameBase(BaseGameMode):
    """
    Abstract base class for team-based game modes.

    Provides:
    - Team management (teams dict, team colors)
    - Hierarchical span creation (team spans → player spans)
    - Team-based win condition checking
    - Team elimination detection

    Subclasses must implement:
    - get_game_name() - return "Teams" or "Random Teams"
    - _initialize_players_impl() - assign players to teams (round-robin vs random)
    - _get_additional_phases() - return [] for Teams, [TeamFormationPhase] for Random Teams
    """

    def __init__(
        self,
        controller_manager_client,
        event_publisher,
        audio_client=None,
        game_id: str = "",
        num_teams: int = 2,
        initial_players: list | None = None,
        sensitivity: int = 2,
        random_assignment: bool = True,
        rng_seed: int = 0,
    ):
        """
        Initialize team-based game mode.

        Args:
            controller_manager_client: gRPC stub for ControllerManager service
            event_publisher: Callback function to publish game events
            audio_client: gRPC stub for Audio service (Phase 29)
            game_id: Unique identifier for this game instance
            num_teams: Number of teams (default 2)
            initial_players: Optional list of Player protobuf messages from StartGame RPC
            sensitivity: Sensitivity level 0-4 (passed from StartGameConfig)
            random_assignment: Whether to randomize player order before round-robin assignment
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
        # Override random_teams from base class with passed parameter
        self.random_teams = random_assignment

        self.num_teams = num_teams
        self.teams: dict[int, Team] = {}

        # Team tracking
        self.team_colors = TEAM_COLORS[:num_teams]

        # The result, latched once in the step that decides it (UC-021 BR-003,
        # UC-022 BR-005). ``_latched_team`` None with ``_win_latched`` is a tie;
        # nothing latched (force-end, stream end) means no winner.
        self._win_latched = False
        self._latched_team: int | None = None

        # Initialize team objects
        for i in range(num_teams):
            self.teams[i] = Team(team_num=i, name=self.team_colors[i]["name"], color=self.team_colors[i]["rgb"])

        logger.info(f"{self.get_game_name()} game initialized with {num_teams} teams")

    # ========================================================================
    # Concrete Methods - Team-specific implementation
    # ========================================================================

    def _create_player_spans(self, game_context):
        """
        Create hierarchical team → player lifecycle spans.

        Team spans are created first, then player spans are created as children
        of their respective team spans using trace.use_span() to register them
        with the OpenTelemetry SDK for proper export.

        Args:
            game_context: Parent span context for proper hierarchy
        """
        # Get current context if not provided
        if game_context is None:
            from opentelemetry import context as otel_context

            game_context = otel_context.get_current()

        # Create team lifecycle spans and their player children
        # Using trace.use_span() to register team spans with SDK for export
        for team_num, team in self.teams.items():
            team_span = tracer.start_span(
                "team_lifecycle",  # Consistent name for all teams (OpenTelemetry best practice)
                context=game_context,
                attributes={
                    "team.number": team_num,
                    "team.name": team.name,
                    "team.color": str(team.color),
                    "game.mode": self.get_game_name(),
                },
            )

            # FIX: Use trace.use_span() to register team span with SDK
            # end_on_exit=False prevents automatic span ending (we end manually)
            with trace.use_span(team_span, end_on_exit=False):
                team.span = team_span  # Store reference for manual .end() later

                # Create player spans while team span is current (registered with SDK)
                team_players = [(s, p) for s, p in self.players.items() if p.team == team_num]
                for serial, player in team_players:
                    # Get current context (now the registered team span)
                    from opentelemetry import context as otel_context

                    player_span = tracer.start_span(
                        "player_lifecycle",  # Consistent name for all players (OpenTelemetry best practice)
                        context=otel_context.get_current(),  # Gets team context (registered!)
                        attributes={
                            "player.serial": serial,
                            "player.team": player.team,
                            "player.team_name": team.name,
                            "player.color": str(player.color),
                            "game.mode": self.get_game_name(),
                        },
                    )
                    player.span = player_span

    async def _set_team_colors(self, pulse_effect: bool = False, duration_ms: int = 0):
        """
        Set controller LED colors to match team assignments (Phase 39 - Task 3).

        Args:
            pulse_effect: If True, use pulse effect for emphasis (good for team announcements)
            duration_ms: Duration of the color/effect. 0 = persistent until changed
        """
        from proto import controller_manager_pb2

        try:
            # Store every team colour for the stream config first, so a write error
            # part-way through still reopens the stream with all team colours.
            for player in self.players.values():
                player.color = self.team_colors[player.team]["rgb"]

            for serial, player in self.players.items():
                team_color = player.color

                if pulse_effect and self.gameplay_stream:
                    # Use pulse effect for team announcement via stream
                    pulse_cmd = controller_manager_pb2.GameplayStreamControl(
                        game_effect=controller_manager_pb2.GameEffectCommand(
                            serial=serial,
                            effect=controller_manager_pb2.GAME_EFFECT_PULSE,
                            color=controller_manager_pb2.RGB(r=team_color[0], g=team_color[1], b=team_color[2]),
                            duration_ms=duration_ms,
                            speed=3,  # Medium pulse speed
                        )
                    )
                    await serialized_write(self.gameplay_stream, pulse_cmd)
                    logger.debug(
                        f"Set {serial} to team {player.team} ({self.team_colors[player.team]['name']}) with pulse"
                    )
                elif self.gameplay_stream:
                    # Set persistent team color via stream
                    base_color_cmd = controller_manager_pb2.GameplayStreamControl(
                        base_color=controller_manager_pb2.ControllerColorConfig(
                            serial=serial,
                            color=controller_manager_pb2.RGB(r=team_color[0], g=team_color[1], b=team_color[2]),
                        )
                    )
                    await serialized_write(self.gameplay_stream, base_color_cmd)
                    logger.debug(f"Set {serial} to team {player.team} ({self.team_colors[player.team]['name']}) color")

            logger.info(
                f"Set team colors for {len(self.players)} players ({'pulsing' if pulse_effect else 'persistent'})"
            )

        except Exception as e:
            logger.error(f"Failed to set team colors: {e}", exc_info=True)

    async def _countdown(self):
        """
        Run countdown with team colors (Phase 30 - Controller Feedback Completion).

        Team-specific countdown sequence:
        - 3 seconds: Team color (each player sees their team color)
        - 2 seconds: White flash (neutral, heightens anticipation)
        - 1 second: Green (universal GO signal)

        This overrides the base class countdown which uses Red -> Yellow -> Green.
        Respects countdown_phase_duration_ms=0 to skip countdown (Issue #464).
        """
        from proto import controller_manager_pb2
        from services.game_coordinator.runtime_config import get_config_manager

        config = get_config_manager().get_config()
        phase_duration_ms = config.countdown_phase_duration_ms

        # Skip countdown entirely if phase duration is 0 (the "skip" variant)
        if phase_duration_ms == 0:
            logger.info("Team countdown skipped (phase_duration_ms=0)")
            await self.event_publisher(GameEvent.COUNTDOWN_START, {"phase_duration_ms": 0})
            await self.event_publisher(GameEvent.COUNTDOWN_END, {})
            return

        logger.info(f"Starting team countdown (phase_duration={phase_duration_ms}ms)...")
        await self.event_publisher(GameEvent.COUNTDOWN_START, {"phase_duration_ms": phase_duration_ms})

        # Countdown sequence specific to team-based games
        countdown_phases = [
            {
                "name": "team_colors",
                "set_team_colors": True,  # Each player sees their team color
            },
            {
                "name": "white_flash",
                "color": Colors.White.value,
            },
            {
                "name": "green_go",
                "color": Colors.Green.value,
            },
        ]

        for phase in countdown_phases:
            if not self.running:
                logger.info("Countdown interrupted by force_end")
                return

            # Play countdown beep (Phase 29)
            await self._play_sound(Sound.SFX_BEEP_LOUD, priority=2)

            if phase.get("set_team_colors"):
                # Set each player to their team color
                await self._set_team_colors(pulse_effect=False, duration_ms=0)
            else:
                # Set all players to same color via stream
                r, g, b = phase["color"]
                for serial in self.players:
                    base_color_cmd = controller_manager_pb2.GameplayStreamControl(
                        base_color=controller_manager_pb2.ControllerColorConfig(
                            serial=serial,
                            color=controller_manager_pb2.RGB(r=r, g=g, b=b),
                        )
                    )
                    await serialized_write(self.gameplay_stream, base_color_cmd)

            # Wait for phase duration (in 50ms increments to allow interruption)
            wait_iterations = phase_duration_ms // 50
            for _ in range(wait_iterations):
                if not self.running:
                    logger.info("Countdown interrupted by force_end")
                    return
                await game_clock.sleep(0.05)

        # Play start sound (Phase 29)
        await self._play_sound(Sound.SFX_START3, priority=2)

        await self.event_publisher(GameEvent.COUNTDOWN_END, {})
        logger.info("Team countdown complete")

    def _player_death_event_attributes(self, serial: str) -> dict:
        """Team attributes on the base kill path's player_death event (#1327)."""
        player = self.players[serial]
        teammates_alive = any(p.alive and p.team == player.team for s, p in self.players.items() if s != serial)
        return {"team": player.team, "team_eliminated": not teammates_alive}

    def _get_alive_teams(self) -> set[int]:
        """
        Get set of teams that still have alive players.

        Returns:
            Set of team numbers with at least one alive player
        """
        alive_teams = set()
        for player in self.players.values():
            if player.alive:
                alive_teams.add(player.team)
        return alive_teams

    def _latch_team_result(self, winning_team: int | None) -> bool:
        """Latch the result (a team, or None for a tie) and the root-span outcome.

        Synchronous, no await: called in the step that decides the result and
        before its event is published, so a force-end that lands afterwards (same
        tick, the publish, the hold, the rainbow) keeps it (UC-021 BR-003).

        The outcome is latched first: a force_end() from outside the game
        can land between the caller's ``force_ended`` check and this call. When the
        latched outcome is ``force_ended`` no result is latched and the caller
        publishes nothing.

        Returns:
            True if the result was latched, False if the game was force-ended.
        """
        self._latch_game_outcome()
        if self.game_outcome() == "force_ended":
            return False
        self._win_latched = True
        self._latched_team = winning_team
        return True

    async def _check_win_condition(self) -> bool:
        """
        Check if a team has won.

        The result is latched once (UC-021 BR-003): later calls do not republish
        it, and a force-ended game latches and publishes none.

        Returns:
            True if game should end (<=1 team remaining), False otherwise
        """
        if self._win_latched:
            return True
        if self.force_ended:
            return False

        alive_teams = self._get_alive_teams()
        if len(alive_teams) > 1:
            return False

        winning_team = self._get_winning_team_num(alive_teams)
        if not self._latch_team_result(winning_team):
            return False

        if winning_team is not None:
            team_name = self.team_colors[winning_team]["name"]
            winners = [p.serial for p in self.players.values() if p.alive and p.team == winning_team]

            logger.info(f"Team {winning_team} ({team_name}) wins with {len(winners)} players!")

            await self.event_publisher(
                "team_winner",
                {
                    "team": winning_team,
                    "team_name": team_name,
                    "team_color": str(self.team_colors[winning_team]["rgb"]),
                    "winning_players": winners,
                    "winner_count": len(winners),
                },
            )
        else:
            logger.info("No winner - all players died simultaneously")
            await self.event_publisher(GameEvent.GAME_TIE, {})

        return True

    async def _kill_player_impl(self, serial: str, accel_mag: float):  # noqa: ARG002
        """
        Handle player death with team elimination detection.

        Args:
            serial: Controller serial number
            accel_mag: Acceleration magnitude that caused death
        """
        player = self.players[serial]
        player.alive = False
        team = self.teams[player.team]

        alive_count = len([p for p in self.players.values() if p.alive])
        alive_teams = self._get_alive_teams()

        # Check if this death eliminated the team
        team_eliminated = player.team not in alive_teams

        logger.info(
            f"Player died: {serial} (Team {player.team}), {alive_count} players remaining on {len(alive_teams)} teams"
        )

        # End the lifecycle span. The player_death event (with team_eliminated, via
        # _player_death_event_attributes) was recorded by the base kill path.
        if player.span:
            player.span.set_status(Status(StatusCode.OK))
            player.span.end()
            player.span = None  # Mark as closed to prevent double-ending
            logger.debug(f"Ended lifecycle span for player {serial}")

        # If team eliminated, end team span
        if team_eliminated and team.span:
            team.span.add_event(
                "team_eliminated",
                attributes={"last_player": serial, "alive_teams_count": len(alive_teams)},
            )
            team.span.set_status(Status(StatusCode.OK))
            team.span.end()
            team.span = None  # Mark as closed to prevent double-ending
            logger.info(f"Team {team.name} eliminated! Ended team lifecycle span")

    def _get_winning_team_num(self, alive_teams: set[int]) -> int | None:
        """
        Determine the winning team number from alive teams.

        Args:
            alive_teams: Set of team numbers that still have alive players

        Returns:
            Winning team number if exactly one team remains, None otherwise
        """
        if len(alive_teams) == 1:
            return list(alive_teams)[0]
        return None

    async def _send_winner_rainbow_effects(self, winning_team_num: int):
        """
        Send rainbow LED effects to all alive members of the winning team.

        Args:
            winning_team_num: The winning team number
        """
        from proto import controller_manager_pb2

        if not self.gameplay_stream:
            return

        for serial, player in self.players.items():
            if player.alive and player.team == winning_team_num:
                effect_cmd = controller_manager_pb2.GameplayStreamControl(
                    game_effect=controller_manager_pb2.GameEffectCommand(
                        serial=serial,
                        effect=controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW,
                    )
                )
                await self._write_end_effect(effect_cmd)

    async def _play_team_victory_sound(self, winning_team_num: int):
        """
        Play the victory sound for the winning team.

        Falls back to congratulations sound if team has no specific sound.

        Args:
            winning_team_num: The winning team number
        """
        winning_team = self.teams.get(winning_team_num)
        if winning_team:
            sound = TEAM_WIN_SOUNDS.get(winning_team.name, Sound.VOX_CONGRATULATIONS)
            await self._play_sound(sound, priority=2)

    def _build_survived_span_attrs(self, player, is_winner: bool) -> dict:
        """
        Build span attributes for a surviving player.

        Includes game duration, winner status, team, and analytics summary if available.

        Args:
            player: Player object
            is_winner: Whether this player is on the winning team

        Returns:
            Dict of span attributes
        """
        survived_attrs = {
            "game_duration": game_clock.now() - self.start_time if self.start_time else 0,
            "winner": is_winner,
            "team": player.team,
        }

        survived_attrs.update(self._analytics_span_attributes(player))
        return survived_attrs

    def _end_surviving_player_spans(self, winning_team_num: int | None):
        """
        End lifecycle spans for surviving players after the celebration.

        This ensures winners' spans are longer than losers'.

        Args:
            winning_team_num: Winning team number, or None if no winner
        """
        for serial, player in self.players.items():
            if not (player.span and player.alive):
                continue

            is_winner = winning_team_num is not None and player.team == winning_team_num
            survived_attrs = self._build_survived_span_attrs(player, is_winner)

            player.span.add_event("player_survived", attributes=survived_attrs)
            player.span.set_status(Status(StatusCode.OK))
            player.span.end()
            logger.debug(f"Ended lifecycle span for surviving player {serial}")

    def _player_analytics_extras(self, player) -> dict:
        """Team summaries add the (visible) team to the survival time."""
        return {"team": player.team, "survival_time_ms": player.analytics.total_time_ms}

    async def _publish_player_analytics(self, winning_team_num: int | None):
        """
        Publish analytics summaries and update metrics for all players.

        Args:
            winning_team_num: Winning team number, or None if no winner
        """
        winners = (
            set()
            if winning_team_num is None
            else {serial for serial, player in self.players.items() if player.team == winning_team_num}
        )
        await self._publish_analytics_summaries(winners)

    def _end_surviving_team_spans(self, alive_teams: set[int], winning_team_num: int | None):
        """
        End lifecycle spans for surviving teams after the celebration.

        Args:
            alive_teams: Set of team numbers that still have alive players
            winning_team_num: Winning team number, or None if no winner
        """
        for team_num, team in self.teams.items():
            if not (team.span and team_num in alive_teams):
                continue

            is_winning_team = winning_team_num is not None and team_num == winning_team_num
            team.span.add_event(
                "team_victory" if is_winning_team else "team_survived",
                attributes={
                    "game_duration": game_clock.now() - self.start_time if self.start_time else 0,
                    "winner": is_winning_team,
                },
            )
            team.span.set_status(Status(StatusCode.OK))
            team.span.end()
            logger.info(f"Ended lifecycle span for team {team.name} ({'WINNER' if is_winning_team else 'survived'})")

    async def _end_game_impl(self):
        """Handle game ending for team-based games."""
        logger.info("Ending game...")
        self.state = self.state.__class__.ENDING

        # Only the latched result counts (UC-021 BR-003): a force-end before it, or a
        # stream end without one, declares no winner and shows no winner effects.
        force_ended = self.game_outcome() == "force_ended"
        winning_team_num = None if force_ended else self._latched_team
        alive_teams = self._get_alive_teams()

        if winning_team_num is not None:
            await self._send_winner_rainbow_effects(winning_team_num)
            await self._play_team_victory_sound(winning_team_num)
            await self._wait_for_rainbow_effect()

        self._end_surviving_player_spans(winning_team_num)
        await self._publish_player_analytics(winning_team_num)
        self._end_surviving_team_spans(alive_teams, winning_team_num)

        self.state = self.state.__class__.ENDED
        ended_payload = {
            "game_id": self.game_id,
            "duration": game_clock.now() - self.start_time if self.start_time else 0,
            "winning_team": winning_team_num if winning_team_num is not None else "none",
            "outcome": self.game_outcome(),
        }
        if force_ended:
            ended_payload["reason"] = self.force_end_reason
        await self.event_publisher(GameEvent.GAME_ENDED, ended_payload)

        logger.info("Game ended")
