"""
The streamed-intro run flow, shared by Fight Club, Zombies, Werewolf and Tournament.

These modes go through the base ``run()`` template (#2359) with these hooks:

- the run executes in a mode span (``<mode>_game``, also ``self.game_span``):
  status OK once the teardown ran, ERROR plus the exception on an error;
- the intro goes over the stream, so the stream opens first; the spoken
  instructions follow the intro (UC-020 A5), before the countdown;
- ``game_started`` is published after the countdown; the music starts then,
  with no tempo loop;
- a force-end during the intro or the countdown returns before the teardown,
  and run()'s finally records the force_ended outcome;
- the music stops in run()'s finally, before ``on_force_end``;
- an error is not published as ``game_error`` here: the session publishes it.

Spans and logs use the mode module's tracer and logger, as the per-mode run()
copies did.
"""

import contextlib
import logging
import sys

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from lib.types import GameEvent
from services.game_coordinator import game_clock
from services.game_coordinator.games.base import GAME_MODE_ATTR, BaseGameMode, GameState, _RunProgress


class StreamedIntroGameMode(BaseGameMode):
    """Base for the modes whose intro runs over the gameplay stream (see the module docstring)."""

    _RUN_RETURNS_EARLY_ON_FORCE_END = True

    # The mode span name, e.g. "fight_club_game".
    RUN_SPAN_NAME: str = ""

    def _run_tracer(self) -> trace.Tracer:
        """The mode module's ``tracer``, read at call time: the spans keep the mode's instrumentation scope."""
        module = sys.modules[type(self).__module__]
        return getattr(module, "tracer", None) or trace.get_tracer(type(self).__module__)

    def _run_logger(self) -> logging.Logger:
        return logging.getLogger(type(self).__module__)

    @contextlib.contextmanager
    def _run_span_scope(self):
        with self._run_tracer().start_as_current_span(self.RUN_SPAN_NAME) as game_span:
            self.game_span = game_span
            game_span.set_attribute("game.id", self.game_id)
            game_span.set_attribute(GAME_MODE_ATTR, self.get_game_name())
            yield game_span

    def _log_run_config(self) -> None:
        """Log the mode's run config at the start of the initialization phase (default: nothing)."""

    async def _run_until_teardown(self) -> bool:
        tracer = self._run_tracer()
        self.running = True
        await self.event_publisher(GameEvent.GAME_STARTING, {"game_id": self.game_id})

        # Initialization phase
        with tracer.start_as_current_span("initialization_phase") as init_span:
            self._log_run_config()
            await self._initialize_players()
            if len(self.players) < 2:
                raise ValueError(f"Need at least 2 players, got {len(self.players)}")
            # Before any task is created: tasks copy the context (#1273).
            self._set_flag_eval_context()
            self._create_player_spans(None)
            # The intro goes over the stream, so it opens first; a drain discards
            # the intro's frames and keeps disconnects (UC-022 A1, UC-023 step 1,
            # UC-025 step 1 / A2 step 1). The instruction clip follows the intro,
            # before the countdown (UC-020 A5).
            await self._run_streamed_intro_and_instructions(init_span)

        if not self.running:
            return False

        # Countdown
        with tracer.start_as_current_span("countdown_phase"):
            await self._countdown()

        if not self.running:
            return False

        # Start gameplay
        self.state = GameState.RUNNING
        self.start_time = game_clock.now()

        # game_started comes after the countdown (UC-022 A1, UC-023 step 1, UC-025 step 1).
        await self.event_publisher(GameEvent.GAME_STARTED, {"game_id": self.game_id, "player_count": len(self.players)})

        # Start music (no tempo loop by design, UC-022 BR-001, UC-023 BR-002, UC-025 BR-002)
        await self._start_game_music()

        # Prime the EMA filters before death detection (UC-030 BR-003)
        await self._warmup_ema()

        return await self._run_streamed_gameplay()

    async def _run_streamed_gameplay(self) -> bool:
        """After the warm-up: gameplay_phase on the game loop (the stream opened before the intro).

        Returns False when the run skips the teardown (force-ended).
        """
        with self._run_tracer().start_as_current_span("gameplay_phase"):
            await self._game_loop()
        return True

    async def _teardown_before_end(self, teardown_span: trace.Span, progress: _RunProgress) -> None:  # noqa: ARG002
        # Shield LEDs end with the game, before the winner effects (#1301).
        await self._end_shield_feedback()
        progress.shield_feedback_ended = True

    def _teardown_after_outcome(self) -> None:
        # Winner effects are sent: close the stream (as the base flow, UC-030 BR-006).
        self._release_gameplay_stream()

    async def _on_run_error(self, error: Exception, game_span: trace.Span | None) -> None:
        self._run_logger().error(f"Game error: {error}", exc_info=True)
        if game_span is not None:
            game_span.record_exception(error)
            game_span.set_status(Status(StatusCode.ERROR, str(error)))

    async def _stop_feedback_and_force_end(self, force_ended: bool, progress: _RunProgress) -> None:
        # The music stops here on every path; the shield feedback only when
        # the teardown step did not end it (error, cancel or early return).
        await self._stop_run_feedback(progress.music_stopped, progress.shield_feedback_ended)
        if force_ended:
            await self.on_force_end()
