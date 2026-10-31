"""
Swapper Game Mode - gRPC-based implementation

Players switch teams when they die. Last team standing wins,
but the last player to die is excluded from winners.

Original JoustMania behavior preserved.
"""

import logging

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from lib.types import Sound
from proto import controller_manager_pb2
from services.game_coordinator import game_clock, metrics
from services.game_coordinator.games.base import Phase, Player
from services.game_coordinator.games.teams_base import TEAM_WIN_SOUNDS, TeamsGameBase
from services.game_coordinator.stream_writes import serialized_write

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class SwapperGame(TeamsGameBase):
    """
    Swapper game mode using gRPC communication.

    Players switch teams when they die instead of being eliminated.
    When all players end up on the same team, the game ends.
    The last player to die is excluded from the winners.

    Extends TeamsGameBase for team management and hierarchical spans.
    """

    SWAP_FLASH_SECONDS = 0.5

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
        Initialize Swapper game.

        Args:
            controller_manager_client: gRPC stub for ControllerManager service
            event_publisher: Callback function to publish game events
            audio_client: gRPC stub for Audio service
            game_id: Unique identifier for this game instance
            initial_players: Optional list of Player protobuf messages
            sensitivity: Sensitivity level 0-4 (passed from StartGameConfig)
        """
        # Swapper always uses 2 teams
        super().__init__(
            controller_manager_client=controller_manager_client,
            event_publisher=event_publisher,
            audio_client=audio_client,
            game_id=game_id,
            num_teams=2,  # Force 2 teams for swapper
            initial_players=initial_players,
            sensitivity=sensitivity,
            random_assignment=True,
            rng_seed=rng_seed,
        )

        # Track the last player to die (excluded from winners)
        self.last_death_serial: str | None = None

        # Players eliminated by a controller disconnect (UC-024 A1). They stay
        # in ``players`` but no longer count for a team, convergence or the winners.
        # Marked before the base kill path runs, so a swap still in flight sees it.
        # revive_player is denied for Swapper (interventions.py capability matrix); if
        # that ever changes, a revive must refuse these serials.
        self._eliminated: set[str] = set()

        # The win, latched once when ``swapper_winner`` is published (UC-024 BR-004,
        # #1343): the end-of-game step uses only this snapshot, never the live teams.
        self._win_latched = False
        self._latched_team: int | None = None
        self._latched_winners: list[str] = []
        self._latched_excluded: str | None = None

    def get_game_name(self) -> str:
        """Return game mode identifier."""
        return "Swapper"

    def get_instruction_clip(self) -> Sound | None:
        """Spoken instructions before the countdown (UC-024 A2, UC-020 A5)."""
        return Sound.MENU_VOX_SWAPPER_INSTRUCTIONS

    async def _initialize_players_impl(self, controllers: list):
        """
        Initialize players with round-robin team assignment.

        Args:
            controllers: List of controller protobuf messages
        """
        for idx, controller in enumerate(controllers):
            team_num = idx % self.num_teams

            player = Player(
                serial=controller.serial,
                team=team_num,
                alive=True,
                color=(255, 255, 255),  # Default white, assigned in color_assignment phase
            )
            self.players[controller.serial] = player
            logger.debug(f"Added player: {controller.serial} to team {team_num}")

        logger.info(f"Initialized {len(self.players)} players across {self.num_teams} teams")

        # Publish event with team assignments
        team_assignments = {serial: player.team for serial, player in self.players.items()}
        await self.event_publisher(
            "players_initialized",
            {
                "player_count": len(self.players),
                "num_teams": self.num_teams,
                "team_assignments": str(team_assignments),
                "serials": list(self.players.keys()),
            },
        )

    async def _assign_team_colors(self):
        """
        Assign team colors to each player.

        Colors are assigned to player objects but NOT displayed yet.
        Players see their colors when the game starts (after countdown).
        """
        logger.info("Assigning team colors...")

        try:
            for _serial, player in self.players.items():
                team_color = self.team_colors[player.team]["rgb"]
                player.color = team_color

            logger.info(f"Assigned team colors to {len(self.players)} players")

        except Exception as e:
            logger.error(f"Failed to assign colors: {e}", exc_info=True)

    def _get_additional_phases(self) -> list:
        """Return color assignment phase."""
        return [Phase(name="color_assignment", execute=self._assign_team_colors)]

    async def _kill_player_impl(self, serial: str, accel_mag: float):
        """
        Handle player death by switching their team.

        Instead of dying, players switch to the opposing team.
        The player is temporarily marked as dead (to stop base class processing),
        then respawned on the new team after the death animation.

        Span handling:
        - End the player's span under the old team with "team_swap_out" event
        - Create a new span under the new team with "team_swap_in" event

        Args:
            serial: Controller serial number
            accel_mag: Acceleration magnitude that caused death
        """
        player = self.players[serial]
        if serial in self._eliminated:
            # UC-024 A1: a disconnected player is eliminated, not swapped.
            self._eliminate(serial)
            return
        if self._win_latched or not self.running:
            # The win was latched, or the game ended, while the base kill path awaited
            # (shield discard, explosion sound): no swap, no write (BR-003, BR-004).
            # ``alive`` stays True, so no death effect follows either.
            return

        old_team = player.team
        old_team_obj = self.teams[old_team]

        # Mark player as temporarily dead to stop base class from processing
        # the held high-acceleration data from SimulateDeath
        player.alive = False

        # Switch teams
        new_team = 1 - old_team  # Toggle between 0 and 1
        new_team_obj = self.teams[new_team]
        player.team = new_team
        player.color = self.team_colors[new_team]["rgb"]

        # Track last death for winner exclusion
        self.last_death_serial = serial

        # Post-swap invincibility window so the death spike that triggered the swap
        # cannot instantly re-kill the player on their new team. Sourced from
        # the ``game.death_grace_period_seconds`` flag, frozen per
        # game at init in BaseGameMode.
        player.grace_until = game_clock.now() + self.death_grace_period

        # Log the swap
        swap_count = getattr(player, "swap_count", 0) + 1
        player.swap_count = swap_count
        logger.info(
            f"Player {serial} swapped (#{swap_count}): Team {old_team} ({self.team_colors[old_team]['name']}) "
            f"-> Team {new_team} ({self.team_colors[new_team]['name']})"
        )

        # End old span with swap event, create new span under new team
        if player.span:
            player.span.add_event(
                "team_swap_out",
                attributes={
                    "accel_magnitude": accel_mag,
                    "old_team": old_team,
                    "new_team": new_team,
                    "old_team_name": self.team_colors[old_team]["name"],
                    "new_team_name": self.team_colors[new_team]["name"],
                    "swap_count": swap_count,
                },
            )
            player.span.set_status(Status(StatusCode.OK))
            player.span.end()
            player.span = None  # Mark as closed before creating new span
            logger.debug(f"Ended player {serial} span under team {old_team}")

        # Create new span under new team
        if new_team_obj.span:
            with trace.use_span(new_team_obj.span, end_on_exit=False):
                new_span = tracer.start_span(
                    "player_lifecycle",
                    context=otel_context.get_current(),
                    attributes={
                        "player.serial": serial,
                        "player.team": new_team,
                        "player.team_name": new_team_obj.name,
                        "player.color": str(player.color),
                        "player.swap_count": swap_count,
                        "game.mode": self.get_game_name(),
                    },
                )
                new_span.add_event(
                    "team_swap_in",
                    attributes={
                        "from_team": old_team,
                        "from_team_name": old_team_obj.name,
                        "swap_count": swap_count,
                    },
                )
                player.span = new_span
                logger.debug(f"Created new player {serial} span under team {new_team}")

        # Update controller LED to new team color
        # Use flash effect (not death effect) since player is swapping, not dying
        # Death effect would set base_colors to black which causes race conditions
        effect_cmd = controller_manager_pb2.GameplayStreamControl(
            game_effect=controller_manager_pb2.GameEffectCommand(
                serial=serial,
                effect=controller_manager_pb2.GAME_EFFECT_FLASH,
                duration_ms=400,
            )
        )
        await serialized_write(self.gameplay_stream, effect_cmd)

        # After flash, set new team color
        await self._swap_pause()
        if serial in self._eliminated:
            # The controller disconnected during the swap (UC-024 A1 step 3): the swap
            # does not finish (no colour, no gauge reset, no player_swapped, no beep).
            self._eliminate(serial)
            return
        if not self.running or self._win_latched:
            # The game ended, or the win was latched, during the pause (BR-003, BR-004):
            # the end effects own the LED. No colour, gauge, event or sound. The player
            # is back in the game on its new team, so no death effect follows either.
            player.alive = True
            return
        await self._set_player_color(serial, player.color)

        # Respawn the player on their new team
        # This re-enables base class processing for this player
        player.alive = True

        # The base kill path zeroed the per-player gauge before the swap; the swapped
        # player is still in the game. The base recounts the aggregate after
        # this impl. Skipped after a force end, which cleared the player analytics (no
        # gauge series recreated).
        if self.running:
            metrics.player_alive.labels(serial=serial, game_id=self.game_id).set(1)
        logger.debug(f"Player {serial} respawned on team {new_team}")
        if self._swap_cut_short():
            # The win latched, or the game ended, while the colour was written (BR-003,
            # BR-004): the player is on its new team, but no event or beep.
            return

        # Publish swap event
        await self.event_publisher(
            "player_swapped",
            {
                "serial": serial,
                "old_team": old_team,
                "new_team": new_team,
                "teams_count": str(self._get_team_counts()),
            },
        )

        # Play swap sound, unless the win latched or the game ended during the publish
        if not self._swap_cut_short():
            await self._play_sound(Sound.SFX_BEEP, priority=1)

    def _swap_cut_short(self) -> bool:
        """True once the win is latched or the game stopped: a swap in flight ends quietly."""
        return self._win_latched or not self.running

    def _eliminate(self, serial: str) -> None:
        """Take a disconnected player out of the game (UC-024 A1).

        ``alive`` stays False, so the base kill path sends the death effect and the
        aggregate drops the player. The last swap is cleared at the mark
        (``_handle_disconnect``), not here (BR-001).
        """
        player = self.players[serial]
        player.alive = False
        if player.span:
            player.span.add_event("player_eliminated", attributes={"reason": "controller_disconnect"})
            player.span.set_status(Status(StatusCode.OK))
            player.span.end()
            player.span = None
        logger.info(f"Player {serial} eliminated (controller disconnect)")

    def _accepts_kill(self, serial: str) -> bool:
        """No swap once the win is latched or the game stopped (BR-003, BR-004).

        The base checks this at the kill's start and again right before the death
        bookkeeping, so a kill whose gate closed while the kill path awaited counts
        no death (#1988). A player marked eliminated before the latch (A1) is still
        taken out: its disconnect is real.
        """
        return serial in self._eliminated or (self.running and not self._win_latched)

    async def _handle_disconnect(self, serial: str) -> None:
        """Eliminate a disconnected player instead of swapping it (UC-024 A1, #1344).

        The serial is marked BEFORE the base call: the base returns early for a player
        that is not alive, which is the case while its own swap sleeps, and drops the
        kill while another kill of the same player is in flight. The mark makes that
        swap, or that kill, eliminate the player.

        The last swap is cleared only when this elimination completes the convergence
        itself (BR-001, D2). An eliminated player is never the excluded one
        (``_excluded_serial``), and when the teams had already converged by the last
        swap (an agent swap whose win check has not run yet), that swap completed the
        convergence and its player stays excluded. Ignored once the win is latched.
        """
        if self._win_latched:
            return
        if serial in self.players and serial not in self._eliminated:
            converged_before = len(self._get_alive_teams()) <= 1
            self._eliminated.add(serial)
            if not converged_before and len(self._get_alive_teams()) <= 1:
                self.last_death_serial = None
        await super()._handle_disconnect(serial)

    def _excluded_serial(self) -> str | None:
        """The last swapped player, excluded from the winners (BR-001).

        Never an eliminated player, and never the only player left on its team: the
        winners list is never empty (D2).
        """
        last = self.last_death_serial
        if last is None or last in self._eliminated or last not in self.players:
            return None
        team = self.players[last].team
        if not any(
            player.team == team and serial not in self._eliminated and serial != last
            for serial, player in self.players.items()
        ):
            return None
        return last

    def _team_winners(self, team: int) -> list[str]:
        """Members of ``team`` who win: not eliminated, not the last swapped player."""
        excluded = self._excluded_serial()
        return [
            serial
            for serial, player in self.players.items()
            if player.team == team and serial not in self._eliminated and serial != excluded
        ]

    async def _swap_pause(self) -> None:
        """Let the swap flash play before the new team colour is written.

        A seam: tests replace it on the instance to skip or hold the pause.
        test_game_clock_waits.py pins the duration.
        """
        await game_clock.sleep(self.SWAP_FLASH_SECONDS)

    def _sends_death_effect(self, serial: str) -> bool:
        """No base death effect for a swapped player, who stays in the game (#1342).

        The swap already set ``alive`` back to True (after the flash and the new team
        colour); the base kill path reads this right after the impl returns. A player
        still not alive (not swapped) gets the default death effect.
        """
        return not self.players[serial].alive

    async def _set_player_color(self, serial: str, color: tuple):
        """Set a player's controller LED color via stream."""
        base_color_cmd = controller_manager_pb2.GameplayStreamControl(
            base_color=controller_manager_pb2.ControllerColorConfig(
                serial=serial,
                color=controller_manager_pb2.RGB(r=color[0], g=color[1], b=color[2]),
            )
        )
        await serialized_write(self.gameplay_stream, base_color_cmd)

    def _get_team_counts(self) -> dict[int, int]:
        """Get count of players on each team."""
        counts = {0: 0, 1: 0}
        for serial, player in self.players.items():
            if serial in self._eliminated:
                continue
            counts[player.team] = counts.get(player.team, 0) + 1
        return counts

    async def _check_win_condition(self) -> bool:
        """
        Check if all players are on the same team.

        Returns:
            True if all players are on one team, False otherwise
        """
        if self._win_latched:
            return True
        if not self.running:
            # A force-end declares no winner (BR-003): nothing is latched or published.
            return False

        teams = self._get_alive_teams()

        if not teams:
            # Every player was eliminated (UC-024 A1 step 4): nobody wins, never team 0.
            logger.info("Swapper ended with no player left: no winner")
            self._win_latched = True
            self._latch_game_outcome()
            return True

        if len(teams) == 1:
            # All remaining players on one team: latch before publishing (BR-004).
            # The outcome latches first, before the winner and the publish awaits: a
            # force-end that won the race declares no winner (BR-003), one landing
            # during the publish keeps this winner (BR-004).
            self._latch_game_outcome()
            if self.game_outcome() == "force_ended":
                return False
            winning_team = next(iter(teams))
            winners = self._team_winners(winning_team)
            excluded = self._excluded_serial()
            self._win_latched = True
            self._latched_team = winning_team
            self._latched_winners = winners
            self._latched_excluded = excluded

            team_name = self.team_colors[winning_team]["name"]
            logger.info(f"Team {winning_team} ({team_name}) wins! Winners: {len(winners)}, Excluded: {excluded}")

            await self.event_publisher(
                "swapper_winner",
                {
                    "team": winning_team,
                    "team_name": team_name,
                    "winners": winners,
                    "excluded_serial": excluded or "",
                },
            )

            return True

        return False

    def _get_alive_teams(self) -> set[int]:
        """
        Get set of teams with players.

        In Swapper, players are not killed - they just switch teams, so this does
        not filter on ``alive`` (a swapping player is briefly not alive). Only
        players eliminated by a disconnect (UC-024 A1) are left out.

        Returns:
            Set of team numbers that have at least one remaining player
        """
        return {player.team for serial, player in self.players.items() if serial not in self._eliminated}

    async def _end_game_impl(self):
        """Handle game ending for Swapper."""
        logger.info("Ending Swapper game...")
        self.state = self.state.__class__.ENDING

        # Only the latched win counts (BR-003, BR-004): a force-end declares no winner,
        # and a stream end without a win has none either.
        force_ended = self.game_outcome() == "force_ended"
        winning_team = None if force_ended else self._latched_team
        winners = set(self._latched_winners) if winning_team is not None else set()
        excluded = self._latched_excluded if winning_team is not None else None

        # Show rainbow effect on winners (excluding last swapper)
        if self.gameplay_stream:
            for serial in self.players:
                if serial in winners:
                    effect_cmd = controller_manager_pb2.GameplayStreamControl(
                        game_effect=controller_manager_pb2.GameEffectCommand(
                            serial=serial,
                            effect=controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW,
                        )
                    )
                    await self._write_end_effect(effect_cmd)

        # Show loser effect on excluded player (dim gray color)
        if excluded and self.gameplay_stream:
            color_cmd = controller_manager_pb2.GameplayStreamControl(
                base_color=controller_manager_pb2.ControllerColorConfig(
                    serial=excluded,
                    color=controller_manager_pb2.RGB(r=50, g=50, b=50),  # Dim gray
                )
            )
            await self._write_end_effect(color_cmd)

        # Play team victory sound (audio service handles voice selection)
        winning_team_obj = self.teams.get(winning_team) if winning_team is not None else None
        if winning_team_obj:
            sound = TEAM_WIN_SOUNDS.get(winning_team_obj.name, Sound.VOX_CONGRATULATIONS)
            await self._play_sound(sound, priority=2)

        # Wait for celebration (interruptible); only a declared winner is celebrated
        for _ in range(20 if winning_team is not None else 0):  # 2 seconds
            if not self.running:
                break
            await game_clock.sleep(0.1)

        # End all player spans
        for serial, player in self.players.items():
            if player.span:
                player.span.add_event(
                    "game_ended",
                    attributes={
                        "winner": serial in winners,
                        "team": player.team,
                        "excluded": serial == excluded,
                    },
                )
                player.span.set_status(Status(StatusCode.OK))
                player.span.end()

        # End team spans
        for team_num, team in self.teams.items():
            if team.span:
                is_winning_team = team_num == winning_team
                team.span.add_event(
                    "game_ended",
                    attributes={"winner": is_winning_team},
                )
                team.span.set_status(Status(StatusCode.OK))
                team.span.end()

        self.state = self.state.__class__.ENDED
        ended_payload = {
            "game_id": self.game_id,
            "duration": game_clock.now() - self.start_time if self.start_time else 0,
            "winning_team": winning_team if winning_team is not None else "none",
            "outcome": self.game_outcome(),
        }
        if force_ended:
            ended_payload["reason"] = self.force_end_reason
        await self.event_publisher("game_ended", ended_payload)

        logger.info("Swapper game ended")
