"""
Tempo-schedule SOURCE listener (#1109).

Keeps each live game's tempo-schedule SOURCE mode (``game.tempo_schedule_mode``)
fresh by subscribing to flagd's ``PROVIDER_CONFIGURATION_CHANGED`` event — the
same live-reload mechanism :class:`RuntimeConfigManager` and
:class:`InterventionManager` already use. Flipping the flag mid-game switches the
pacing SOURCE LIVE (no restart): on the next change event this manager re-reads
``tempo_schedule_mode`` for every live session's ``gameId`` and atomically swaps
the game's ``self.tempo_schedule_mode`` (the same single-store swap pattern as the
F6 ``music_windows`` seam). The 100ms music loop / ``_decide_next_change_delay``
then reads the new mode on its next tempo decision.

This is the SOURCE selector only — the rule engine (default rule = today's
random-window timing) and the agent-directive seam both live on the game in
``games/base.py``. This manager does not change the threshold math (#1107) or
interventions; it is game-coordinator-only and reads a human/ops-set flag (the
agent never persists it).
"""

import logging
import threading

from openfeature.provider import ProviderEvent

from lib.feature_flags import read_string_flag
from services.game_coordinator.games.base import TEMPO_SCHEDULE_MODE_DEFAULT

logger = logging.getLogger(__name__)


class TempoScheduleManager:
    """Owns the ``game.tempo_schedule_mode`` flag client + live-reload handler.

    Lifecycle mirrors :class:`InterventionManager`: ``start()`` initializes the
    ``game`` flag domain, registers the change handler, and does an initial
    evaluation; ``stop()`` removes the handler. ``get_sessions`` is the per-game
    seam (one :class:`SessionView` per live game) so each game's mode is resolved
    against its own ``gameId`` context — a shadow game can run a different source
    than the primary, live.
    """

    def __init__(self, get_sessions):
        # get_sessions() -> list[SessionView]; same callable the servicer feeds
        # the InterventionManager. Each view carries game_id (-> gameId context)
        # and the live game instance.
        self._get_sessions = get_sessions
        self._game_client = None
        self._started = False
        self._lock = threading.RLock()

    def start(self) -> None:
        """Initialize the game flag client, register the handler, prime modes."""
        if self._started:
            return
        try:
            from openfeature import api

            from lib.feature_flags import get_flag_client, init_flag_domain

            init_flag_domain("game")
            self._game_client = get_flag_client("game")

            api.add_handler(ProviderEvent.PROVIDER_CONFIGURATION_CHANGED, self._on_flags_changed)
            logger.info("TempoScheduleManager: registered PROVIDER_CONFIGURATION_CHANGED handler")
            self._started = True
        except ImportError:
            logger.warning("TempoScheduleManager: OpenFeature unavailable, tempo mode stays init-frozen")
            return
        except Exception as e:
            logger.error(f"TempoScheduleManager: failed to initialize: {e}")
            return

        # Initial evaluation applies any already-set mode to live games (none at
        # startup) and is harmless / idempotent.
        self.evaluate_all()

    def stop(self) -> None:
        """Remove the change handler."""
        if not self._started:
            return
        try:
            from openfeature import api

            api.remove_handler(ProviderEvent.PROVIDER_CONFIGURATION_CHANGED, self._on_flags_changed)
        except Exception as e:
            logger.debug(f"TempoScheduleManager: handler removal failed: {e}")
        self._started = False

    def _on_flags_changed(self, _event_details) -> None:
        """Event handler: re-read tempo_schedule_mode for every live game.

        Synchronous (OpenFeature handler), fired on a background thread. The swap
        it performs is a plain attribute store on each game (no awaitable, no
        loop-bound gRPC client), so unlike :class:`InterventionManager` it can run
        the evaluation inline on the handler thread — the music loop reads the new
        value on its next tick.
        """
        self.evaluate_all()

    def evaluate_all(self) -> None:
        """Re-read ``tempo_schedule_mode`` for each live session and swap it in.

        Each session's value is resolved with that session's ``gameId`` context
        so flagd targeting can scope the source per game. ``resolve_tempo_schedule_mode``
        (applied in ``apply_tempo_schedule_mode``) keeps the swap default-safe: any
        missing/unknown value resolves to ``rule`` => today's behavior.
        """
        if self._get_sessions is None:
            return
        try:
            sessions = list(self._get_sessions() or [])
        except Exception as e:
            logger.error(f"TempoScheduleManager: get_sessions failed: {e}")
            return

        for session in sessions:
            game = getattr(session, "game", None)
            if game is None:
                continue
            value = self._read_mode(getattr(session, "game_id", "") or None)
            try:
                game.apply_tempo_schedule_mode(value)
            except Exception as e:  # one bad session must not stop the rest
                logger.warning(f"TempoScheduleManager: failed to apply mode for {session.game_id}: {e}")

    def _read_mode(self, game_id):
        """Read the live ``game.tempo_schedule_mode`` value for a ``gameId``.

        Returns the raw flag string (validation happens in
        ``apply_tempo_schedule_mode``). On any failure returns the default so the
        swap stays behavior-neutral.
        """
        with self._lock:
            return read_string_flag("game", "tempo_schedule_mode", TEMPO_SCHEDULE_MODE_DEFAULT, game_id=game_id)
