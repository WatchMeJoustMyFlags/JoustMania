"""
FFA (Free-For-All) Game Mode - gRPC-based implementation

Modern implementation using gRPC for controller states and settings,
async/await patterns, proper state machine, and event publishing.

Phase 36b: Refactored to extend BaseGameMode, eliminating ~550 lines of duplicate code.
"""

import logging

from opentelemetry.trace import Status, StatusCode

from lib.telemetry import inject_trace_context
from lib.types import GameEvent, Sound
from services.game_coordinator import game_clock
from services.game_coordinator.games.analytics import PlayerAnalytics
from services.game_coordinator.games.base import BaseGameMode, Phase
from services.game_coordinator.runtime_config import get_config_manager

logger = logging.getLogger(__name__)


class FFAGame(BaseGameMode):
    """
    Free-For-All game mode using gRPC communication.

    Players try to keep their controllers still while jostling others.
    Last player standing wins.

    Phase 36b: Extends BaseGameMode to inherit:
    - Span orchestration (run() template method)
    - Common game operations (_load_settings, _countdown, _process_controller_state, etc.)
    - Consistent OpenTelemetry span hierarchy

    Implements FFA-specific behavior:
    - All players on team=0 (no teams)
    - Flat player hierarchy (players directly under gameplay_phase)
    - Last player standing win condition
    - Players stay dead permanently (spans end on death)
    """

    # UC-040 BR-019: the only mode where shield.activation=trigger applies (#1837).
    SHIELD_TRIGGER_ALLOWED = True

    # Set by the win check that publishes the result (UC-020 BR-003, #2020).
    _win_latched: bool = False

    def get_game_name(self) -> str:
        """Return game mode identifier."""
        return "FFA"

    def get_instruction_clip(self) -> Sound | None:
        """FFA's spoken instructions (UC-020 A5)."""
        return Sound.MENU_VOX_FFA_INSTRUCTIONS

    async def _initialize_players_impl(self, controllers: list):
        """
        Initialize players for FFA - all players on team=0.

        Args:
            controllers: List of controller protobuf messages from GetReadyControllers
        """
        from services.game_coordinator.games.base import Player

        config = get_config_manager().get_config()
        game_start_time = game_clock.now()

        for controller in controllers:
            # Initialize analytics if enabled
            analytics = None
            if config.analytics.enabled:
                analytics = PlayerAnalytics.from_config(
                    serial=controller.serial,
                    game_start_time=game_start_time,
                    config=config.analytics,
                )

            player = Player(
                serial=controller.serial,
                team=0,  # FFA - everyone on their own team
                alive=True,
                color=(255, 255, 255),
                analytics=analytics,
            )
            self.players[controller.serial] = player
            logger.debug(f"Added player: {controller.serial}")

    def _create_player_spans(self, game_context):
        """
        Create flat player lifecycle spans (directly under gameplay_phase).

        Args:
            game_context: Parent span context for proper hierarchy
        """
        for serial, player in self.players.items():
            player.span = self._create_player_lifecycle_span(serial, game_context)
            logger.debug(f"Started lifecycle span for player {serial}")

    async def _check_win_condition(self) -> bool:
        """
        Check if game has a winner (last player standing).

        The result is latched once, before ``game_winner`` / ``game_tie`` is
        published (UC-020 BR-003, #2020): the root-span outcome is latched first,
        because a force_end() can land (between awaits) after the
        ``force_ended`` check (also while the last kill awaited its explosion).
        A force-ended game publishes nothing; a later call does not republish.

        Returns:
            True if game should end, False otherwise
        """
        if self._win_latched:
            return True
        if self.force_ended:
            return False

        alive_players = [p for p in self.players.values() if p.alive]
        if len(alive_players) > 1:
            return False

        self._latch_game_outcome()
        if self.game_outcome() == "force_ended":
            return False
        self._win_latched = True

        if alive_players:
            winner = alive_players[0]
            logger.info(f"Winner: {winner.serial}")
            await self.event_publisher(GameEvent.GAME_WINNER, {"serial": winner.serial})
        else:
            logger.info("No winner - all players died simultaneously")
            await self.event_publisher(GameEvent.GAME_TIE, {})

        return True

    async def _kill_player_impl(self, serial: str, accel_mag: float):  # noqa: ARG002
        """
        Kill a player permanently (end their lifecycle span).

        Args:
            serial: Controller serial number
            accel_mag: Acceleration magnitude that caused death
        """
        player = self.players[serial]
        player.alive = False

        alive_count = len([p for p in self.players.values() if p.alive])

        # UC-020 step 5 / UC-046 BR-014: publish the death on the game event stream so
        # observers (the agent's shadow runner, the dashboard) learn the elimination
        # order from the coordinator itself rather than inferring it.
        await self.event_publisher(GameEvent.PLAYER_DEATH, {"serial": serial, "alive_remaining": str(alive_count)})

        # End the lifecycle span. The player_death SPAN event was already recorded by
        # the base kill path (_kill_player); emitting another here doubled it (#1327).
        if player.span:
            player.span.set_status(Status(StatusCode.OK))
            player.span.end()
            player.span = None  # Mark as closed to prevent double-ending
            logger.debug(f"Ended lifecycle span for player {serial}")

    async def _assign_ffa_colors(self):
        """
        Assign unique colors to each player in FFA mode.

        Colors are assigned to player objects but NOT displayed yet.
        Players see their colors when the game starts (after countdown),
        matching the original JoustMania behavior.
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

        FFA assigns colors silently - players see them at game start (after countdown),
        matching original JoustMania behavior.
        """
        return [Phase(name="color_assignment", execute=self._assign_ffa_colors)]

    def _winner_serial(self) -> str | None:
        """The winner's serial from the game's outcome, or None (tie, force-ended, ...).

        A game force-ended while it ran has no winner even if one player was left;
        a win already latched survives a later force-end (UC-020 BR-003).
        """
        return self._game_outcome_attributes().get("game.winner")

    def _natural_game_outcome(self) -> dict:
        """``winner`` (one alive) or ``tie`` (none alive) per UC-020 BR-001 / BR-003."""
        alive_players = [p for p in self.players.values() if p.alive]
        if len(alive_players) == 1:
            return {"game.outcome": "winner", "game.winner": alive_players[0].serial}
        if not alive_players:
            return {"game.outcome": "tie"}
        # Several alive without a force-end: the loop stopped for another reason
        # (e.g. the gameplay stream ended). No winner is claimed.
        return {"game.outcome": "completed"}

    def _close_all_player_spans(self):
        """
        Close all player lifecycle spans with analytics data.

        Override base implementation to add the end event and analytics summary to
        surviving players' spans (UC-020 BR-003, #1328): ``victory`` only on the
        winner; ``player_survived`` (with the force-end reason) on every survivor
        of a force-ended game.
        """
        winner_serial = self._winner_serial()

        for serial, player in self.players.items():
            if player.span:
                # Finalize health spans and summary attributes (#571)
                self._finalize_player_health(player)

                # Build attributes including analytics summary if available
                if player.alive:
                    end_attrs = {
                        "game_duration": game_clock.now() - self.start_time if self.start_time else 0,
                        "winner": serial == winner_serial,
                    }
                    outcome = self._game_outcome_attributes()
                    if outcome["game.outcome"] == "force_ended":
                        end_attrs.update(outcome)

                    # Add analytics summary to span
                    if player.analytics is not None:
                        analytics_summary = player.analytics.get_summary()
                        for key, value in analytics_summary.items():
                            end_attrs[f"analytics.{key}"] = value

                    event_name = "victory" if serial == winner_serial else "player_survived"
                    player.span.add_event(event_name, attributes=end_attrs)

                player.span.set_status(Status(StatusCode.OK))
                player.span.end()
                player.span = None  # Mark as closed
                logger.debug(f"Closed lifecycle span for player {serial}")

    async def _end_game_impl(self):
        """Handle game ending - show winner, cleanup."""
        from proto import controller_manager_pb2
        from services.game_coordinator import metrics
        from services.game_coordinator.games.base import GameState

        logger.info("Ending game...")
        self.state = GameState.ENDING

        # Find winner (if any); a force-ended game has none (UC-020 A4 / BR-003)
        winner_serial = self._winner_serial()
        logger.info(f"End game: winner_serial={winner_serial}, gameplay_stream={self.gameplay_stream is not None}")

        # Show rainbow effect on winner's controller and play victory sound
        if winner_serial and self.gameplay_stream:
            logger.info(f"Sending WINNER_RAINBOW effect for {winner_serial}")
            trace_parent, trace_state = inject_trace_context()
            effect_cmd = controller_manager_pb2.GameplayStreamControl(
                game_effect=controller_manager_pb2.GameEffectCommand(
                    serial=winner_serial,
                    effect=controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW,
                    trace_parent=trace_parent,
                    trace_state=trace_state,
                )
            )
            # Skipped with a warning when the server already ended the stream (UC-020 BR-003, #1956)
            if await self._write_end_effect(effect_cmd):
                logger.info(f"WINNER_RAINBOW effect sent for {winner_serial}")
            await self._play_sound(Sound.VOX_CONGRATULATIONS, priority=2)
        else:
            logger.warning(
                f"Skipping rainbow effect: winner_serial={winner_serial}, "
                f"stream_valid={self.gameplay_stream is not None}"
            )

        # Wait for rainbow effect to complete
        await self._wait_for_rainbow_effect()

        # Note: Player spans are already closed by _close_all_player_spans()
        # at the end of gameplay_phase (before teardown_phase starts)

        # Publish analytics summaries for all players
        for serial, player in self.players.items():
            if player.analytics is not None:
                summary = player.analytics.get_summary()
                summary["game_id"] = self.game_id
                summary["winner"] = serial == winner_serial
                summary["survival_time_ms"] = player.analytics.total_time_ms

                # Publish player analytics event
                await self.event_publisher(GameEvent.PLAYER_ANALYTICS, summary)

                # Update Prometheus metrics
                metrics.game_analytics_samples_total.labels(game_mode=self.get_game_name()).inc(
                    player.analytics.sample_count
                )
                metrics.near_death_events_total.labels(serial=serial, game_mode=self.get_game_name()).inc(
                    player.analytics.near_death_count
                )

                logger.info(
                    f"Player {serial} analytics: peak={summary['peak_accel']:.2f}g, "
                    f"playstyle={summary['playstyle']}, near_deaths={summary['near_death_count']}"
                )

        self.state = GameState.ENDED
        outcome = self._game_outcome_attributes()
        ended_data = {
            "game_id": self.game_id,
            "duration": game_clock.now() - self.start_time if self.start_time else 0,
            "outcome": outcome["game.outcome"],
        }
        if "game.end_reason" in outcome:
            ended_data["reason"] = outcome["game.end_reason"]
        await self.event_publisher(GameEvent.GAME_ENDED, ended_data)

        logger.info("Game ended")
