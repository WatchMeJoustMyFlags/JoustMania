"""
Intervention Manager core for JoustMania (#730, PR A).

The game-side application point for agent interventions. The agent ACTs by
writing flag config to the flagd ``interventions`` domain; this manager
subscribes to ``PROVIDER_CONFIGURATION_CHANGED`` and re-evaluates all
intervention flags on every change, runs each candidate through the policy
enforcement chain, and dispatches surviving interventions to a registered
handler.

Design (from docs/research/722-intervention-surface.md §5/§6/§8/§9):

- Two flag shapes:
    * State-shaped flags (music_tempo_override, global_sensitivity_override,
      player_sensitivity_factor, shield_seconds, volume_override) — the game
      converges on the flag value; re-applied whenever the value changes.
    * Edge-triggered flags (eliminate_player, revive_player, audio_cue,
      controller_effect, end_game) — value is ``"<nonce>:<payload>"``; a changed
      nonce triggers exactly one application (idempotent across flagd
      reconnects / re-reads). The per-game nonce baseline is seeded when the
      game starts (``prime_session``), so a value left over from an earlier
      game counts as already consumed and is never replayed (UC-040 BR-006,
      #1299).
- Enforcement chain, evaluated *before* any handler runs (defense in depth; the
  agent also self-checks in #726):
    1. allowed-membership   — type must be in ``interventions_allowed`` (agent
       domain, default ``[]``)
    2. weighted rate limit  — sliding 60s window, weighted cost per class. This
       is the GENEROUS GLOBAL BACKSTOP (#919), budget from
       ``policy.coordinator_backstop_per_minute``; the per-game budget is owned
       authoritatively by the agent (see the defense-in-depth contract block
       below).
    3. battery guard        — player-targeted interventions blocked when the
       target's battery pct < ``policy.battery_threshold``
    4. mode-capability matrix — declarative per-mode gating (§9)
- Every outcome (applied stub or blocked) increments
  ``game_interventions_total{type, objective, blocked}`` and publishes
  ``GameEvent.AGENT_INTERVENTION`` on the EventBus.

PR A scope: handlers are no-op stubs that log + record metrics. The real
effects land in PRs C/D/E by registering handlers against the existing
registry — see ``register_handler`` and the handler contract documented there.
NO game effects are applied in this PR.
"""

import asyncio
import contextlib
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from openfeature.evaluation_context import EvaluationContext
from openfeature.flag_evaluation import Reason
from openfeature.provider import ProviderEvent

from lib.flag_eval_visibility import record_flag_fallback
from services.game_coordinator.game_loop_dispatch import (
    DEFAULT_DISPATCH_TIMEOUT_S,
    GameLoopDispatcher,
    GameLoopUnavailableError,
)

logger = logging.getLogger(__name__)

# Rate-limit weighting classes (§5). Cost per applied intervention.
WEIGHT_SOFT = 0.5  # psychological, self-reverting
WEIGHT_MEDIUM = 1.0  # tunable, reversible
WEIGHT_HARD = 2.0  # state-changing, player-visible as unfair if wrong

# Sliding rate-limit window in seconds (§5: "max_interventions_per_minute").
RATE_LIMIT_WINDOW_SECONDS = 60.0

# ---------------------------------------------------------------------------- #
# Defense-in-depth budget contract (#919)
# ---------------------------------------------------------------------------- #
# Intervention rate limiting is enforced in TWO layers, in two languages. They
# have DISTINCT, non-overlapping roles — they are NOT two copies of the same
# governor, and they intentionally read DIFFERENT flags so they cannot drift:
#
#   1. Go agent (AUTHORITATIVE, per-game). services/agent/decision/loopset.go
#      gives every game_id its OWN decision Loop, each with its OWN weighted
#      sliding-window budget read from ``policy.max_interventions_per_minute``
#      (services/agent/decision/ratelimit.go). This is the real per-game
#      governor: it decides how many interventions a single game may receive per
#      minute, and one game exhausting its budget never starves another. The
#      agent only emits an intervention (writes the flag) once its per-game
#      limiter admits it.
#
#   2. Coordinator ``_RateLimiter`` (BACKSTOP, process-global). The limiter in
#      this module is a single GLOBAL safety net shared across every concurrent
#      game in this coordinator process. Its job is NOT to govern normal
#      per-game pacing — that is the agent's. Its job is to cap a *runaway or
#      buggy* agent that floods flag writes far beyond any sane per-game rate
#      (e.g. a crash-loop rewriting nonces, or a misconfigured agent ignoring
#      its own limiter). It is therefore deliberately set to a GENEROUS budget
#      that normal multi-session traffic — several concurrent games each at
#      their own authoritative per-game budget — will never reach. It reads its
#      budget from a SEPARATE flag, ``policy.coordinator_backstop_per_minute``,
#      so it can be tuned independently and so the two layers never share a
#      value that could silently drift.
#
# The drift-prevention rule (closes bug classes #800/#848): the per-game pacing
# decision lives in exactly ONE place — the agent. The coordinator does not
# re-derive or mirror the per-game budget; it only owns the generous global cap.
# To change how aggressively a single game may be nudged, change
# ``policy.max_interventions_per_minute`` (agent). Touch the coordinator backstop
# (``policy.coordinator_backstop_per_minute``) only to move the global flood
# ceiling.

# Generous default global-backstop budget (weighted interventions / minute)
# applied across ALL concurrent games in this coordinator process. Picked so it
# never fires for legitimate traffic: even a busy fleet of several concurrent
# games each spending its full authoritative per-game budget (default 2/min,
# aggressive 4/min) stays well under this; the backstop only trips on an agent
# emitting flag writes at an order-of-magnitude-higher, clearly-runaway rate.
# Overridable via the ``policy.coordinator_backstop_per_minute`` flag.
DEFAULT_COORDINATOR_BACKSTOP_BUDGET = 60.0


class _UnknownNonce:
    """Edge baseline whose value could not be read (flagd error, #1299).

    A game primed (or evaluated) while flagd returns an error must not treat the
    next good read as a fresh trigger: the value it then sees may be the stale
    one from an earlier game. ``_UNKNOWN`` marks "baseline not known yet"; the
    next good read is adopted as the baseline WITHOUT firing (fail closed).
    """

    def __repr__(self) -> str:
        return "<unknown nonce>"


_UNKNOWN = _UnknownNonce()

# Event-list reader switch (FR-127, #1410). When on, the five one-shot flags are
# read as per-game event lists ``{"epoch","seq","events"}`` served by the flag
# service (architecture note §6) instead of ``"<nonce>:<payload>"`` strings.
# Default off until the interventions set is cut over to the flag service (#1411).
EVENT_LIST_READER_ENV = "INTERVENTION_EVENT_LIST_READER"


def _event_list_reader_from_env() -> bool:
    return os.getenv(EVENT_LIST_READER_ENV, "off").strip().lower() in ("1", "on", "true", "yes")


# Sentinel "no payload / no-op" values for edge-triggered flags.
_EDGE_EMPTY_VALUES = {"", "none"}


@dataclass(frozen=True)
class InterventionSpec:
    """Declarative description of one intervention flag.

    The registry maps flag key -> InterventionSpec. PRs C/D/E append entries (or
    attach real handlers) without restructuring this module.

    Attributes:
        flag_key: Flag identifier in the ``interventions`` flagd domain.
        type_id: Stable intervention identifier used as the ``type`` metric
            label and matched against ``interventions_allowed`` (§6). NOTE this
            differs from ``flag_key`` (e.g. flag ``audio_cue`` -> type
            ``play_audio_cue``).
        weight: Rate-limit cost class (WEIGHT_SOFT / MEDIUM / HARD).
        edge_triggered: True for one-shot ``"<nonce>:<payload>"`` flags; False
            for state-shaped flags.
        player_targeted: True if the intervention targets a specific player
            (subject to the battery guard); the payload's first ``:``-delimited
            field is the target serial.
        none_value: The flag value that means "no intervention" for
            state-shaped flags (e.g. ``0`` for shield_seconds, ``-1`` for
            overrides). Ignored for edge-triggered flags.
        value_kind: How to read the flag: ``"string"``, ``"int"`` or
            ``"float"``.
    """

    flag_key: str
    type_id: str
    weight: float
    edge_triggered: bool
    player_targeted: bool
    value_kind: str = "string"
    none_value: object = None
    # Edge-triggered only: whether a non-empty payload is required to fire.
    # True for serial/sound/effect-carrying flags; False for ``end_game`` whose
    # value is nonce-only (``"<nonce>"``) so a present nonce alone is the
    # command.
    payload_required: bool = True
    # Edge-triggered only: the payload names the game the decision was made for
    # (``end_game``: ``"<nonce>:<game_id>"``). The intervention applies only in
    # that session; an un-addressed value applies only in the real game
    # (UC-040 BR-011, #1310).
    game_addressed: bool = False
    # Player-targeted state only: a one-shot, time-boxed GRANT (shield, partial
    # shield, soft penalty), not a level. Change-detected per serial on the
    # resolved (variant, value), baselined at game start, and dispatched only for
    # the serials whose grant changed (UC-040 BR-012, #1313).
    per_serial_grant: bool = False


# Registry of all intervention flags (§8). Ordered to keep diffs stable.
# PRs C/D/E attach real handlers via register_handler(); they do NOT edit this
# table. Adding a brand-new intervention appends one row here.
INTERVENTION_SPECS: tuple[InterventionSpec, ...] = (
    # --- State-shaped flags ---
    InterventionSpec(
        flag_key="music_tempo_override",
        type_id="adjust_music_tempo",
        weight=WEIGHT_MEDIUM,
        edge_triggered=False,
        player_targeted=False,
        value_kind="float",
        none_value=0,
    ),
    InterventionSpec(
        flag_key="global_sensitivity_override",
        type_id="adjust_global_sensitivity",
        weight=WEIGHT_HARD,
        edge_triggered=False,
        player_targeted=False,
        value_kind="int",
        none_value=-1,
    ),
    InterventionSpec(
        flag_key="player_sensitivity_factor",
        type_id="adjust_player_sensitivity",
        weight=WEIGHT_MEDIUM,
        edge_triggered=False,
        player_targeted=True,
        value_kind="float",
        # 1.0 is the neutral default; a value != 1.0 is an intervention.
        none_value=1.0,
    ),
    InterventionSpec(
        flag_key="player_handicap_factor",
        type_id="set_player_handicap",
        weight=WEIGHT_MEDIUM,
        edge_triggered=False,
        player_targeted=True,
        value_kind="float",
        # 1.0 is the neutral default; a value != 1.0 is an intervention (#1107,
        # #1103 MVP action 1). MULTIPLICATIVE per-player handicap that COMPOSES
        # with player_sensitivity_factor (>1 harder to die / "help", <1 easier /
        # "rein in"), clamped [0.5, 2.0] in base.py. SHADOW-game-only initially:
        # the agent's interventions_allowed allow-list (the load-bearing gate)
        # only includes this type in the new `shadow_experimental` variant.
        none_value=1.0,
    ),
    InterventionSpec(
        flag_key="shield_seconds",
        type_id="grant_shield",
        weight=WEIGHT_MEDIUM,
        edge_triggered=False,
        player_targeted=True,
        value_kind="float",
        none_value=0,
        per_serial_grant=True,
    ),
    InterventionSpec(
        flag_key="partial_shield_seconds",
        type_id="partial_shield",
        weight=WEIGHT_MEDIUM,
        edge_triggered=False,
        player_targeted=True,
        # STRING value "<seconds>" or "<seconds>:<boost>" (#1129, #1103 Phase 2):
        # a TIME-BOXED handicap boost (much harder to die, but NOT immune — vs
        # grant_shield's total grace_until immunity). The handler parses both
        # forms; malformed values are a safe no-op. "0" (and "none"/empty) means
        # no partial shield. SHADOW-game-only initially: allow-listed ONLY via the
        # `shadow_experimental` interventions_allowed variant (the load-bearing
        # gate), never the real-facing ambient/standard/full variants.
        value_kind="string",
        none_value="0",
        per_serial_grant=True,
    ),
    InterventionSpec(
        flag_key="soft_penalty_action",
        type_id="soft_penalty",
        weight=WEIGHT_MEDIUM,
        edge_triggered=False,
        player_targeted=True,
        # STRING value "warn" (default) | "tighten" | "tighten:<seconds>:<factor>"
        # (#1134, #1103 Phase 3): a graduated, RECOVERABLE alternative to the
        # permanent eliminate_player. "warn" fires the visible warning feedback
        # (no threshold change). "tighten" applies a short EXPIRING handicap
        # REDUCTION (factor < 1.0, the MIRROR of partial_shield) making the player
        # temporarily EASIER to die — still clamped [0.5, 2.0], never instant-
        # death. The handler parses all forms; malformed -> safe default ("warn").
        # "none"/empty means no penalty. SHADOW-game-only initially: allow-listed
        # ONLY via the `shadow_experimental` interventions_allowed variant (the
        # load-bearing gate), never the real-facing ambient/standard/full variants.
        value_kind="string",
        none_value="none",
        per_serial_grant=True,
    ),
    # #1143 (#1103 Phase 3) — auto_rubberband: a SINGLE global decision the
    # COORDINATOR expands across players from the live skill gap. STRING value
    # "off" (default / neutral) | "gentle" | "strong" (compression strength). The
    # handler ranks alive players by headroom to their death threshold and applies
    # a bounded, time-boxed handicap BOOST to the trailing player(s) — compressing
    # the gap without inverting standings or causing instant death (bounded in the
    # method + the [0.5,2.0] clamp). Reuses the #1107 handicap mechanism, composes
    # by MAX (alongside partial_shield). NOT player_targeted — the global value is
    # read once and expanded. SHADOW-game-only initially: allow-listed ONLY via the
    # `shadow_experimental` interventions_allowed variant (the load-bearing gate),
    # never the real-facing ambient/standard/full variants.
    InterventionSpec(
        flag_key="auto_rubberband",
        type_id="auto_rubberband",
        weight=WEIGHT_MEDIUM,
        edge_triggered=False,
        player_targeted=False,
        value_kind="string",
        # "off" (and "none"/empty) means no rubberbanding (the default-safe value).
        none_value="off",
    ),
    InterventionSpec(
        flag_key="volume_override",
        type_id="adjust_volume",
        weight=WEIGHT_SOFT,
        edge_triggered=False,
        player_targeted=False,
        value_kind="float",
        none_value=-1,
    ),
    # #766 F6 — two new state-shaped difficulty/pacing levers (weight Medium).
    InterventionSpec(
        flag_key="global_difficulty_factor",
        type_id="adjust_global_difficulty",
        weight=WEIGHT_MEDIUM,
        edge_triggered=False,
        player_targeted=False,
        value_kind="float",
        # 1.0 is the neutral default; a value != 1.0 is an intervention.
        none_value=1.0,
    ),
    InterventionSpec(
        flag_key="pacing_profile",
        type_id="set_pacing_profile",
        weight=WEIGHT_MEDIUM,
        edge_triggered=False,
        player_targeted=False,
        value_kind="string",
        # "none" (and "default") mean the neutral init-resolved windows.
        none_value="none",
    ),
    # #1117 (#1103 MVP action 2) — ramp_tempo: a scheduled tempo CURVE that fills
    # the #1114 ``agent``-mode tempo seam. Session-scoped STRING state flag whose
    # value is "<target>:<seconds>:<curve>" (e.g. "1.3:8:linear"); the handler
    # records a RampDescriptor and the game's 100ms _check_music_speed loop
    # interpolates toward target via the single tempo-owner seam (no second
    # writer). SHADOW-game-only initially: allow-listed ONLY via the
    # `shadow_experimental` interventions_allowed variant (the load-bearing gate),
    # never the real-facing ambient/standard/full variants.
    InterventionSpec(
        flag_key="tempo_ramp",
        type_id="ramp_tempo",
        weight=WEIGHT_MEDIUM,
        edge_triggered=False,
        player_targeted=False,
        value_kind="string",
        # "none" means no ramp (the default-safe neutral value).
        none_value="none",
    ),
    # --- Edge-triggered (one-shot) flags ---
    InterventionSpec(
        flag_key="eliminate_player",
        type_id="eliminate_player",
        weight=WEIGHT_HARD,
        edge_triggered=True,
        player_targeted=True,
    ),
    InterventionSpec(
        flag_key="revive_player",
        type_id="revive_player",
        weight=WEIGHT_HARD,
        edge_triggered=True,
        player_targeted=True,
    ),
    InterventionSpec(
        flag_key="audio_cue",
        type_id="play_audio_cue",
        weight=WEIGHT_SOFT,
        edge_triggered=True,
        player_targeted=False,
    ),
    InterventionSpec(
        flag_key="controller_effect",
        type_id="send_controller_effect",
        weight=WEIGHT_SOFT,
        edge_triggered=True,
        # serial may be empty (broadcast); guard treats missing target as
        # "don't block", so broadcast effects are not battery-gated.
        player_targeted=True,
    ),
    InterventionSpec(
        flag_key="end_game",
        type_id="end_game",
        weight=WEIGHT_HARD,
        edge_triggered=True,
        player_targeted=False,
        # value is "<nonce>:<game_id>" (addressed, #1310) or legacy "<nonce>"
        payload_required=False,
        game_addressed=True,
    ),
)

# Map game mode name (BaseGameMode.get_game_name()) -> set of type_ids that are
# DISALLOWED in that mode (§9). Absence from this table = allowed everywhere.
# Only modes with a restriction appear. ``send_controller_effect`` is restricted
# in hidden-role modes because LED effects can leak roles; ``revive_player`` is
# gated where reviving breaks the mode; ``adjust_global_sensitivity`` is gated
# where it interacts with per-role threshold overrides. ``adjust_global_difficulty``
# (#766 F6) inherits the ``adjust_global_sensitivity`` row (same role-threshold
# interaction); ``set_pacing_profile`` inherits ``adjust_music_tempo`` (allowed
# everywhere — no deny rows).
MODE_CAPABILITY_DENY: dict[str, frozenset[str]] = {
    # Hidden-role modes: LED effects leak roles; revive breaks hidden roles.
    "Werewolf": frozenset(
        {"send_controller_effect", "revive_player", "adjust_global_sensitivity", "adjust_global_difficulty"}
    ),
    "Traitor": frozenset(
        {"send_controller_effect", "revive_player", "adjust_global_sensitivity", "adjust_global_difficulty"}
    ),
    # Bracket / queue modes: revive breaks the bracket/queue.
    "Tournament": frozenset({"revive_player"}),
    "Fight Club": frozenset({"revive_player"}),
    # Permanent-elimination modes: revive is opt-in only (off by default in M2).
    "FFA": frozenset({"revive_player"}),
    "Teams": frozenset({"revive_player"}),
    "Random Teams": frozenset({"revive_player"}),
    # Zombie role thresholds are asymmetric; global override/difficulty interacts.
    "Zombie": frozenset({"adjust_global_sensitivity", "adjust_global_difficulty"}),
    # Swapper: revive leaves team state ambiguous.
    "Swapper": frozenset({"revive_player"}),
}


# A handler receives the InterventionContext and applies the effect. In PR A all
# handlers are the no-op stub. Handlers are async to match game_coordinator
# patterns and because PRs C/D/E will await RPCs / game-state mutations.
Handler = Callable[["InterventionContext"], Awaitable[None]]


@dataclass
class InterventionContext:
    """Everything a handler needs to apply one intervention.

    Passed to the registered handler after the enforcement chain passes. PRs
    C/D/E read ``payload``/``target_serial``/``value`` to do the real work.

    Attributes:
        spec: The InterventionSpec for this flag.
        value: The raw evaluated flag value (typed per ``spec.value_kind`` for
            state-shaped flags; the raw ``"<nonce>:<payload>"`` string for
            edge-triggered flags).
        payload: For edge-triggered flags, the part after the nonce
            (``"<serial>"``, ``"<sound_id>"``, ``"<serial>:<effect>"`` or ``""``
            for end_game). Empty string for state-shaped flags.
        target_serial: Resolved target controller serial, or ``None`` if the
            intervention is not player-targeted / is a broadcast.
        game: The live game instance (BaseGameMode) or ``None`` if no game is
            running.
        objective: Dominant session objective label for metrics/events.
        game_id: The owning session's game id (#838). Handlers that re-resolve
            per-player targets (e.g. player_sensitivity_factor) MUST pass this
            into ``resolve_player_targets(game_id=...)`` so per-game targeting
            composes. ``""`` for the synthesized primary session (no gameId).
    """

    spec: InterventionSpec
    value: object
    payload: str
    target_serial: str | None
    game: object
    objective: str
    game_id: str = ""


class _RateLimiter:
    """Weighted sliding-window rate limiter — the GLOBAL BACKSTOP (#919).

    This is the process-global defense-in-depth backstop described in the
    "Defense-in-depth budget contract" block at the top of this module. It is
    NOT the per-game governor — the Go agent owns that
    (``policy.max_interventions_per_minute``, per-game Loop). This limiter is one
    generous global cap shared across all concurrent games, instantiated with
    ``DEFAULT_COORDINATOR_BACKSTOP_BUDGET`` and tuned via the separate
    ``policy.coordinator_backstop_per_minute`` flag. It only trips when a runaway
    agent floods flag writes well beyond all sane per-game traffic combined.

    Tracks (timestamp, cost) of applied interventions over the last
    ``RATE_LIMIT_WINDOW_SECONDS``. ``check(cost)`` returns True (and reserves the
    cost) when ``current_weight + cost <= budget``; otherwise returns False and
    reserves nothing.
    """

    def __init__(self, budget: float, time_fn: Callable[[], float] = time.monotonic):
        self._budget = budget
        self._time_fn = time_fn
        self._events: deque[tuple[float, float]] = deque()
        self._lock = threading.Lock()

    def set_budget(self, budget: float) -> None:
        with self._lock:
            self._budget = budget

    def _evict(self, now: float) -> None:
        cutoff = now - RATE_LIMIT_WINDOW_SECONDS
        while self._events and self._events[0][0] <= cutoff:
            self._events.popleft()

    def current_weight(self) -> float:
        with self._lock:
            self._evict(self._time_fn())
            return sum(cost for _, cost in self._events)

    def check(self, cost: float) -> bool:
        """Reserve ``cost`` if budget allows. Returns True if reserved."""
        with self._lock:
            now = self._time_fn()
            self._evict(now)
            used = sum(c for _, c in self._events)
            if used + cost > self._budget:
                return False
            self._events.append((now, cost))
            return True


# Reason codes for blocked interventions (event payload ``block_reason``).
BLOCK_NOT_ALLOWED = "not_allowed"
BLOCK_RATE_LIMITED = "rate_limited"
BLOCK_LOW_BATTERY = "low_battery"
BLOCK_MODE_UNSUPPORTED = "mode_unsupported"
BLOCK_NO_GAME = "no_game"

# Sentinel game_id for the synthesized primary-only session when the manager is
# wired with the legacy ``get_game`` callback (no session registry). An empty
# game_id adds NO ``gameId`` attribute to the EvaluationContext, so un-targeted
# flags resolve exactly as before (today's primary-only semantics, #838).
_PRIMARY_SENTINEL_GAME_ID = ""


@dataclass(frozen=True)
class SessionView:
    """A live session the manager evaluates interventions against (#838).

    The servicer supplies one of these per live game (primary + every shadow)
    via ``get_sessions``. Each carries the game instance, its ``game_id`` (used
    as the ``gameId`` EvaluationContext attribute so flagd targeting rules can
    scope an intervention to one game), and the publish callable for THAT
    session's EventBus (so effects/blocked events land on the right stream).

    Backward compatibility (#838): a manager wired with the legacy ``get_game``
    callback synthesizes a single primary view with ``game_id=""`` (no ``gameId``
    in context) and the primary publisher — so un-targeted interventions behave
    exactly as today and only touch the primary game.

    Attributes:
        game_id: The session's game id. ``""`` means the synthesized primary
            session (legacy path); no ``gameId`` is added to the context.
        game: The live BaseGameMode instance (or ``None``).
        publish: Async ``publish(event_type, data)`` for this session's bus.
        is_real: Whether this is the real (primary) game. Defaults to False so a
            caller that does not say is treated as a shadow (fail closed).
        loop: The asyncio loop the session's game runs on (its own thread). The
            game's gRPC clients and gameplay stream belong to it, so handler side
            effects run there (UC-040 BR-018, #1250). ``None`` (legacy / tests)
            runs effects inline on the caller's loop.
    """

    game_id: str
    game: object
    publish: "Callable[[str, dict], Awaitable[None]]"
    # True only for the real (primary) game. An un-addressed ``end_game`` applies
    # only to the real game (UC-040 BR-011); unknown kind fails closed (shadow).
    is_real: bool = False
    loop: asyncio.AbstractEventLoop | None = None


class InterventionManager:
    """Owns the intervention flag clients and the enforcement/dispatch loop.

    Lifecycle mirrors RuntimeConfigManager: ``start()`` initializes flag
    domains, registers the change handler, and does an initial evaluation;
    ``stop()`` removes the handler. ``get_game`` is a callable returning the
    live game (or None).
    """

    def __init__(
        self,
        event_publisher: Callable[[str, dict], Awaitable[None]],
        get_game: Callable[[], object | None] | None = None,
        battery_provider: Callable[[str], float | None] | None = None,
        time_fn: Callable[[], float] = time.monotonic,
        end_game_fn: Callable[[str, str], Awaitable[tuple[bool, str]]] | None = None,
        get_sessions: Callable[[], list["SessionView"]] | None = None,
        event_list_reader: bool | None = None,
    ):
        """
        Args:
            event_publisher: Async ``publish(event_type, data)`` (EventBus) for
                the PRIMARY session — used as the publisher for the synthesized
                primary view in the legacy ``get_game`` path.
            get_game: Legacy seam. Returns the live PRIMARY BaseGameMode instance
                or None. When supplied without ``get_sessions``, the manager
                evaluates against a single primary-only session (today's
                semantics) — un-targeted interventions touch only the primary
                game. Mutually exclusive with ``get_sessions`` in practice
                (``get_sessions`` wins when both are given).
            battery_provider: ``(serial) -> battery_pct | None``. None means the
                battery is unknown and the guard does not block. Injected for
                testability (the real source is the controller manager's
                ``controller_battery_pct`` metric).
            time_fn: Monotonic clock (injectable for tests).
            end_game_fn: Async ``end_game(game_id, reason) -> (success, error)``
                used by the ``end_game`` intervention handler to share the
                servicer's force-end path (#730, PR E). It must end exactly the
                session ``game_id`` (``""`` = the legacy single primary), never
                "the current game" (UC-040 BR-011, #1310). None disables the
                handler's effect.
            get_sessions: Per-session seam (#838). Returns one ``SessionView``
                per LIVE session (primary + every shadow). The manager evaluates
                every intervention flag against each session with a per-session
                ``{gameId, targetingKey=serial}`` context and applies/publishes
                on THAT session. A flag with no ``gameId`` targeting rule (the
                agent writes none) resolves to the same value for every live
                session; ``end_game`` is addressed by payload instead (BR-011).
        """
        self._publish = event_publisher
        # Event-list reader (FR-127): None reads INTERVENTION_EVENT_LIST_READER.
        self._event_lists = _event_list_reader_from_env() if event_list_reader is None else event_list_reader
        # Per game, per one-shot flag: the last applied (epoch, seq). Starts at
        # ("", 0) when the game registers; advanced under the lock BEFORE the
        # events are dispatched (re-review N1/N2).
        self._last_event: dict[str, dict[str, tuple[str, int]]] = {}
        # Single-flight evaluate_all (re-review N1): one flag-service write
        # wakes every flagd domain watcher, so PROVIDER_CONFIGURATION_CHANGED
        # fires once per domain; overlapping passes coalesce into one follow-up.
        self._eval_state_lock = threading.Lock()
        self._eval_running = False
        self._eval_dirty = False
        self._get_game = get_game
        self._get_sessions = get_sessions
        self._battery_provider = battery_provider
        self._time_fn = time_fn
        self._end_game_fn = end_game_fn

        self._interventions_client = None  # interventions domain
        self._agent_client = None  # agent domain (backstop reads)
        self._user_client = None  # user domain (on-device cue gate, #818)

        # Per-session, per-flag last-applied nonce (edge-triggered exactly-once)
        # and last-applied state value (state-shaped change detection). Keyed
        # ``game_id -> flag_key -> value`` so each live session tracks its own
        # baseline independently (#838). The legacy single-primary path uses the
        # ``_PRIMARY_SENTINEL_GAME_ID`` ("") bucket.
        # A nonce baseline is a nonce string, or ``_UNKNOWN`` when the read
        # failed (adopt the next good value without firing, #1299).
        self._last_nonce: dict[str, dict[str, str | _UnknownNonce]] = {}
        self._last_state_value: dict[str, dict[str, object]] = {}
        # Sessions whose edge baseline was primed at game start (prime_session)
        # but which are not yet live (their game instance is still being built,
        # so get_sessions skips them). Pruning keeps their buckets until they are
        # first seen live; the servicer calls forget_session when it retires a
        # session, so a game that never gets built does not linger (#1299).
        self._pending_primed: set[str] = set()

        # Global BACKSTOP limiter (#919): generous, process-global, NOT the
        # per-game governor (that is the agent's). See the defense-in-depth
        # contract block at the top of this module.
        self._rate_limiter = _RateLimiter(budget=DEFAULT_COORDINATOR_BACKSTOP_BUDGET, time_fn=time_fn)

        # Handler registry: flag_key -> Handler. Defaults to the no-op stub for
        # every spec. PRs C/D/E replace entries via register_handler().
        self._handlers: dict[str, Handler] = {spec.flag_key: self._noop_handler for spec in INTERVENTION_SPECS}
        # Optional per-flag handlers run when a state-shaped flag reverts to its
        # neutral value (e.g. volume_override -> default game volume). Additive;
        # absence means "do nothing on revert" (the default for all flags).
        #
        # IMPORTANT (#922): a revert-to-neutral is routed HERE, not back to the
        # flag's apply-handler. So a state flag that mutates live game state on
        # apply MUST register a revert handler to undo it — the apply-handler's
        # own "neutral value" branch is never reached on revert. music_tempo_override
        # is a concrete example: its apply-handler clears game.tempo_override for a
        # 0 value, but that branch only runs when the value CHANGES TO a non-neutral
        # value and back via a separate apply; on a genuine revert the dedicated
        # handle_music_tempo_override_revert (registered in difficulty_handlers.py)
        # is what actually resets the override.
        self._revert_handlers: dict[str, Handler] = {}

        # Flags whose handler must run on the coordinator's main loop instead of
        # the game's loop (UC-040 BR-018): end_game force-ends the session, which
        # joins the game's thread, so running it on that thread would deadlock.
        self._main_loop_flags: set[str] = set()
        # Per-session dispatchers that run handler side effects on the owning
        # game's loop, in decision order (UC-040 BR-018, #1250). Keyed by game_id.
        self._dispatchers: dict[str, GameLoopDispatcher] = {}
        self._dispatch_timeout_s = DEFAULT_DISPATCH_TIMEOUT_S

        self._lock = threading.RLock()
        self._started = False

        # The asyncio loop the servicer runs on, captured at start(). The
        # OpenFeature provider fires PROVIDER_CONFIGURATION_CHANGED on a
        # background thread, so evaluate_all() (which makes gRPC calls bound to
        # this loop) MUST be scheduled back onto it via run_coroutine_threadsafe.
        # Scheduling it on the firing thread's loop (or a fresh asyncio.run loop)
        # binds the gRPC futures to a different loop and they fail with
        # "attached to a different loop".
        self._loop: object | None = None

    # ------------------------------------------------------------------ #
    # Handler registry (the contract PRs C/D/E follow)
    # ------------------------------------------------------------------ #
    def register_handler(self, flag_key: str, handler: Handler, *, on_game_loop: bool = True) -> None:
        """Register the real handler for a flag (PRs C/D/E).

        ``on_game_loop`` (default True): the handler runs on the owning game's
        event loop (UC-040 BR-018). Pass False only for a handler that must run on
        the coordinator's main loop (``end_game``: it joins the game thread).

        Handler contract:
        - signature: ``async def handler(ctx: InterventionContext) -> None``
        - called ONLY after the enforcement chain passes; the handler must NOT
          re-check policy.
        - read ``ctx.value`` (typed for state flags / raw string for edge
          flags), ``ctx.payload``, ``ctx.target_serial``, ``ctx.game``.
        - the manager records the metric and publishes the EventBus event around
          the handler call; the handler does not.
        - raising propagates as a blocked-with-error outcome; handlers should be
          defensive.
        """
        if flag_key not in self._handlers:
            raise KeyError(f"Unknown intervention flag key: {flag_key}")
        with self._lock:
            self._handlers[flag_key] = handler
            if on_game_loop:
                self._main_loop_flags.discard(flag_key)
            else:
                self._main_loop_flags.add(flag_key)
            logger.info(f"Registered intervention handler for '{flag_key}'")

    async def _noop_handler(self, ctx: InterventionContext) -> None:
        """PR A stub: log only, apply no game effect."""
        logger.info(
            f"[intervention stub] {ctx.spec.type_id} "
            f"(flag={ctx.spec.flag_key}, target={ctx.target_serial}, "
            f"value={ctx.value!r}, payload={ctx.payload!r}) — no-op (PR A)"
        )

    # ------------------------------------------------------------------ #
    # Ambient + session intervention handlers (#730, PR E)
    #
    # These reuse the exact audio/effect paths the games already use; they do
    # NOT touch the audio/controller services. Registered via
    # register_ambient_handlers(); the enforcement chain has already passed when
    # they run, so they only do the effect (defensively).
    # ------------------------------------------------------------------ #
    def register_ambient_handlers(self) -> None:
        """Register the PR E ambient/session handlers (one line per flag).

        Additive: PR C registers its own difficulty handlers separately. Call
        once after construction (the servicer does this on startup).
        """
        self.register_handler("audio_cue", self._handle_audio_cue)
        self.register_handler("volume_override", self._handle_volume_override)
        self.register_handler("controller_effect", self._handle_controller_effect)
        # end_game stays on the main loop: force-end joins the game's thread.
        self.register_handler("end_game", self._handle_end_game, on_game_loop=False)
        # volume_override restores the configured game volume when reverted.
        self._revert_handlers["volume_override"] = self._handle_volume_revert

    async def _handle_audio_cue(self, ctx: InterventionContext) -> None:
        """Play ``ctx.payload`` as a sound id through the live game's audio path.

        Unknown/empty sound id or no running game -> log and return.
        """
        sound_id = ctx.payload.strip()
        if not sound_id:
            logger.info("audio_cue: empty sound id, skipping")
            return
        game = ctx.game
        play = getattr(game, "_play_sound", None) if game is not None else None
        if not callable(play):
            logger.info(f"audio_cue: no live game audio path for sound '{sound_id}', skipping")
            return
        # _play_sound already swallows unknown-sound / audio errors defensively.
        await play(sound_id, priority=2)
        logger.info(f"audio_cue: played sound '{sound_id}'")

    async def _handle_volume_override(self, ctx: InterventionContext) -> None:
        """Set the audio volume via the game's audio client.

        State-shaped: ``ctx.value`` is the float volume. ``none_value`` (-1)
        never reaches here (the manager skips reverts-to-neutral), so reverting
        to the configured game volume is handled in ``_handle_volume_revert``,
        invoked by the manager's neutral-revert path.
        """
        volume = _coerce_float(ctx.value)
        if volume is None or volume < 0:
            logger.info(f"volume_override: non-applicable value {ctx.value!r}, skipping")
            return
        await self._set_volume(ctx.game, volume)
        logger.info(f"volume_override: set volume to {volume}")

    async def _handle_volume_revert(self, ctx: InterventionContext) -> None:
        """Restore the configured game volume when the override reverts to none.

        Dispatched by the manager when ``volume_override`` returns to its
        ``none_value`` (-1). Restores exactly ``games.base.GAME_VOLUME``.
        """
        from services.game_coordinator.games.base import GAME_VOLUME

        await self._set_volume(ctx.game, GAME_VOLUME)
        logger.info(f"volume_override: reverted volume to game default {GAME_VOLUME}")

    async def _set_volume(self, game: object, volume: float) -> None:
        """Send a SetVolume RPC via the live game's audio client (no-op if
        there is no running game / audio client)."""
        audio_client = getattr(game, "audio_client", None) if game is not None else None
        if audio_client is None:
            logger.info("volume_override: no live game audio client, skipping")
            return
        from proto import audio_pb2

        await audio_client.SetVolume(audio_pb2.SetVolumeRequest(volume=float(volume)))

    async def _handle_controller_effect(self, ctx: InterventionContext) -> None:
        """Send a controller effect via the live game's gameplay stream.

        Payload is ``"<serial>:<effect>"``; an empty serial broadcasts the
        effect to all active (alive) players. Effect names are validated against
        the supported set; unknown effects / malformed payloads -> log + return.
        """
        serial, effect_name = _parse_effect_payload(ctx.payload)
        if effect_name is None:
            logger.info(f"controller_effect: malformed payload {ctx.payload!r}, skipping")
            return
        effect_enum = _resolve_effect(effect_name)
        if effect_enum is None:
            logger.info(f"controller_effect: unknown effect '{effect_name}', skipping")
            return

        game = ctx.game
        stream = getattr(game, "gameplay_stream", None) if game is not None else None
        if stream is None:
            logger.info("controller_effect: no live gameplay stream, skipping")
            return

        targets = _effect_targets(game, serial)
        if not targets:
            logger.info(f"controller_effect: no active target for serial '{serial}', skipping")
            return

        from proto import controller_manager_pb2

        for target_serial in targets:
            cmd = controller_manager_pb2.GameplayStreamControl(
                game_effect=controller_manager_pb2.GameEffectCommand(
                    serial=target_serial,
                    effect=effect_enum,
                )
            )
            await stream.write(cmd)
        logger.info(f"controller_effect: sent '{effect_name}' to {len(targets)} controller(s)")

    async def _handle_end_game(self, ctx: InterventionContext) -> None:
        """End the session this intervention was evaluated for (UC-040 BR-011).

        The manager only dispatches ``end_game`` to the session it is addressed
        to (``_addressed_to``), and this handler ends exactly that session
        (``ctx.game_id``) via the servicer's shared force-end path. It never
        resolves the primary: before #1310 a shadow session's evaluation ended
        the real game. No-ops safely when the session is gone or no end_game_fn
        is wired.
        """
        if self._end_game_fn is None:
            logger.info("end_game: no end_game_fn wired, skipping")
            return
        success, error = await self._end_game_fn(ctx.game_id, "agent_intervention")
        game_label = ctx.game_id or "<primary>"
        if success:
            logger.info(f"end_game: game {game_label} force-ended via agent intervention")
        else:
            logger.info(f"end_game: no-op for game {game_label} ({error})")

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Initialize flag clients, register change handler, initial evaluate."""
        if self._started:
            return

        # Capture the servicer's running loop so the (background-thread) flag
        # change handler can schedule evaluate_all() back onto it.
        import asyncio

        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        try:
            from openfeature import api

            from lib.feature_flags import get_flag_client, init_flag_domain

            init_flag_domain("interventions")
            self._interventions_client = get_flag_client("interventions")

            init_flag_domain("agent")
            self._agent_client = get_flag_client("agent")

            # User domain holds the operator-facing cue mute toggle (#818).
            init_flag_domain("user")
            self._user_client = get_flag_client("user")

            api.add_handler(ProviderEvent.PROVIDER_CONFIGURATION_CHANGED, self._on_flags_changed)
            logger.info("InterventionManager: registered PROVIDER_CONFIGURATION_CHANGED handler")

            self._started = True
        except ImportError:
            logger.warning("InterventionManager: OpenFeature unavailable, interventions disabled")
            return
        except Exception as e:
            logger.error(f"InterventionManager: failed to initialize: {e}")
            return

        # Initial evaluation establishes the nonce/state baseline so that flags
        # already set at startup are NOT re-fired as fresh edge triggers.
        self._refresh_budget()
        self._prime_baseline()

    def stop(self) -> None:
        """Remove the change handler."""
        if not self._started:
            return
        try:
            from openfeature import api

            api.remove_handler(ProviderEvent.PROVIDER_CONFIGURATION_CHANGED, self._on_flags_changed)
        except Exception as e:
            logger.debug(f"InterventionManager: handler removal failed: {e}")
        self._started = False

    def _prime_baseline(self) -> None:
        """Record current flag values without applying them.

        Prevents flags that were already non-default before the game coordinator
        started (or after a flagd reconnect on first connect) from being treated
        as brand-new triggers. Primes every live session's baseline (#838) so a
        flag already set for a specific gameId is not re-fired on first connect.
        """
        for session in self._live_sessions():
            self._prime_edge_bucket(session)
            serials = _active_player_serials(session.game)
            self._prime_grant_bucket(session.game_id, serials)
            if session.game is not None:
                self._prime_level_bucket(session.game_id, serials)
            for spec in INTERVENTION_SPECS:
                if spec.edge_triggered or spec.per_serial_grant or _is_level_spec(spec):
                    continue
                try:
                    raw = self._read_flag(spec, session)
                except Exception:
                    continue
                with self._lock:
                    self._state_bucket(session.game_id)[spec.flag_key] = raw

    def prime_session(self, game_id: str, serials=()) -> None:
        """Seed a new game's edge-trigger baseline from the current flag values.

        Called by the servicer when a game session is admitted, before its game
        runs (UC-040 BR-006, #1299). Each edge-triggered flag's current nonce is
        recorded for ``game_id`` WITHOUT applying it: any value present when the
        game starts belongs to an earlier game and counts as already consumed.
        Only a nonce change observed during this game fires. Without this, the
        game's bucket started empty and the first unrelated flag change replayed
        the previous game's last ``audio_cue`` / ``end_game`` /
        ``eliminate_player`` into the new game.

        Per-serial grant flags (shield, partial shield, soft penalty) are primed
        the same way for ``serials``, the players of the new game: a grant still
        in the flag belongs to an earlier decision (UC-040 BR-012, #1313).

        Per-player level flags (sensitivity / handicap factor) are primed too:
        a level still in the flag was decided for an earlier game, so the new
        game does not inherit it (UC-040 BR-017, #1317/#1269).

        Session-scoped state flags are not primed: their contract is "the game
        converges on the current value", so a still-set state value applies to a
        new game (not game-scoped yet, #1269).
        """
        if self._event_lists:
            # A fresh game scope starts with empty event lists, neutral grants
            # and neutral levels: the flag service drops a game's values with
            # its scope, so nothing of an earlier game is left (BR-006, #1317).
            with self._lock:
                self._last_event[game_id] = {}
                self._state_bucket(game_id)
                self._pending_primed.add(game_id)
            return
        serials = list(serials)
        session = SessionView(game_id=game_id, game=None, publish=self._publish)
        self._prime_edge_bucket(session)
        self._prime_grant_bucket(game_id, serials)
        self._prime_level_bucket(game_id, serials)
        with self._lock:
            self._pending_primed.add(game_id)

    def forget_session(self, game_id: str) -> None:
        """Drop all change-detection state for a retired session (#1299).

        Called by the servicer when it retires a session, including one whose
        game was never built, so primed-but-never-live baselines do not linger.
        """
        with self._lock:
            self._pending_primed.discard(game_id)
            self._last_nonce.pop(game_id, None)
            self._last_state_value.pop(game_id, None)
            self._dispatchers.pop(game_id, None)
            self._last_event.pop(game_id, None)

    def _prime_edge_bucket(self, session: "SessionView") -> None:
        """Record every edge flag's current nonce for ``session`` (no apply).

        A flag whose read fails is recorded as ``_UNKNOWN``: the next good read
        becomes the baseline without firing, so a stale value can never look
        fresh just because flagd was erroring at game start.
        """
        for spec in INTERVENTION_SPECS:
            if not spec.edge_triggered:
                continue
            raw = self._read_edge_value(spec, session)
            nonce = raw if raw is _UNKNOWN else _split_nonce(raw)[0]
            with self._lock:
                self._nonce_bucket(session.game_id)[spec.flag_key] = nonce

    # ------------------------------------------------------------------ #
    # Session resolution + per-session change-detection buckets (#838)
    # ------------------------------------------------------------------ #
    def _live_sessions(self) -> list["SessionView"]:
        """Resolve the live sessions to evaluate (#838).

        Prefers ``get_sessions`` (the per-session seam). Falls back to the legacy
        ``get_game`` callback, synthesizing a single primary-only view with an
        empty game_id (no ``gameId`` context) and the primary publisher — exactly
        today's behavior. Returns an empty list when neither yields a session.
        """
        if self._get_sessions is not None:
            try:
                return list(self._get_sessions() or [])
            except Exception as e:
                logger.error(f"InterventionManager: get_sessions failed: {e}")
                return []
        # Legacy path: a single synthesized primary session.
        game = self._get_game() if self._get_game is not None else None
        return [SessionView(game_id=_PRIMARY_SENTINEL_GAME_ID, game=game, publish=self._publish, is_real=True)]

    def _nonce_bucket(self, game_id: str) -> dict[str, str]:
        return self._last_nonce.setdefault(game_id, {})

    def _state_bucket(self, game_id: str) -> dict[str, object]:
        return self._last_state_value.setdefault(game_id, {})

    # Mid-pass accessors: never (re)create a bucket. ``None`` means the session
    # was forgotten (retired) while evaluate_all was suspended in it (#1299).
    def _existing_nonce_bucket(self, game_id: str) -> "dict[str, str | _UnknownNonce] | None":
        return self._last_nonce.get(game_id)

    def _existing_state_bucket(self, game_id: str) -> dict[str, object] | None:
        return self._last_state_value.get(game_id)

    def _prune_stale_buckets(self, live_ids: set[str]) -> None:
        """Drop change-detection state for sessions that are no longer live.

        Prevents unbounded growth as shadow games come and go, and ensures a new
        game reusing a (vanishingly unlikely) recycled id starts from a clean
        baseline. The sentinel primary bucket is always retained.
        """
        with self._lock:
            # A session seen live is no longer pending; a primed session not yet
            # live keeps its baseline (#1299).
            self._pending_primed -= live_ids
            keep = live_ids | self._pending_primed | {_PRIMARY_SENTINEL_GAME_ID}
            for stale in [gid for gid in self._last_nonce if gid not in keep]:
                self._last_nonce.pop(stale, None)
            for stale in [gid for gid in self._last_state_value if gid not in keep]:
                self._last_state_value.pop(stale, None)
            for stale in [gid for gid in self._dispatchers if gid not in live_ids]:
                self._dispatchers.pop(stale, None)
            for stale in [gid for gid in self._last_event if gid not in keep]:
                self._last_event.pop(stale, None)

    # ------------------------------------------------------------------ #
    # Flag change handling
    # ------------------------------------------------------------------ #
    def _on_flags_changed(self, _event_details) -> None:
        """Event handler: re-evaluate all intervention flags.

        Synchronous (OpenFeature handler) and fired on a background thread, so it
        schedules evaluate_all() onto the servicer's loop (captured at start())
        via run_coroutine_threadsafe — the only safe way to drive loop-bound gRPC
        clients from another thread. Falls back to the current running loop, then
        to a fresh loop, for direct/test invocations where no servicer loop was
        captured.
        """
        import asyncio

        loop = self._loop
        if loop is not None and loop.is_running():
            # Cross-thread-safe: bind the coroutine to the servicer's loop.
            asyncio.run_coroutine_threadsafe(self.evaluate_all(), loop)
            return

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is not None:
            running.create_task(self.evaluate_all())
        else:
            asyncio.run(self.evaluate_all())

    async def evaluate_all(self) -> None:
        """Single-flight wrapper around :meth:`_evaluate_all_once` (re-review N1).

        A call while a pass runs only marks the pass dirty and returns; the
        running pass then loops once more, so N overlapping change events cost
        at most two passes and two passes never interleave.
        """
        with self._eval_state_lock:
            if self._eval_running:
                self._eval_dirty = True
                return
            self._eval_running = True
            self._eval_dirty = False
        try:
            while True:
                await self._evaluate_all_once()
                with self._eval_state_lock:
                    if not self._eval_dirty:
                        self._eval_running = False
                        return
                    self._eval_dirty = False
        except BaseException:
            with self._eval_state_lock:
                self._eval_running = False
            raise

    async def _evaluate_all_once(self) -> None:
        """Re-evaluate every intervention flag for every live session (#838).

        Public for tests and the change handler. Refreshes the rate-limit budget
        first (policy flag may have changed), then evaluates each live session
        independently: per session it walks the registry in order, evaluating
        every flag with that session's ``{gameId, targetingKey}`` context and
        applying/publishing on that session.

        A flag with no ``gameId`` targeting rule (the agent writes none) resolves
        to the same value for every live session, primary and shadow alike.
        """
        self._refresh_budget()
        sessions = self._live_sessions()
        self._prune_stale_buckets({s.game_id for s in sessions})
        for session in sessions:
            # Create both buckets once, at the start of this session's
            # iteration. Inside the pass they are only read with .get(): if the
            # servicer retires the session while a handler is awaited
            # (forget_session), the remaining specs see no bucket and skip,
            # instead of starting from an empty baseline and replaying a stale
            # nonce (#1299).
            self._ensure_edge_baseline(session)
            with self._lock:
                self._nonce_bucket(session.game_id)
                self._state_bucket(session.game_id)
                if self._event_lists:
                    self._last_event.setdefault(session.game_id, {})
            for spec in INTERVENTION_SPECS:
                try:
                    await self._evaluate_one(spec, session)
                except Exception as e:  # one bad flag must not stop the rest
                    logger.error(
                        f"InterventionManager: error evaluating {spec.flag_key} "
                        f"for game {session.game_id or '<primary>'}: {e}"
                    )

    def _ensure_edge_baseline(self, session: "SessionView") -> None:
        """Fail-closed fallback for a live session that was never primed (#1299).

        The servicer primes every session at game start (``prime_session``). If a
        live session still has no edge baseline (for example priming raised in
        the servicer), record the current edge values as consumed instead of
        treating them as fresh triggers (``_UNKNOWN`` where the read fails). This
        can at worst swallow one write; it can never replay a stale ``end_game``
        or ``eliminate_player``.
        """
        # The legacy single-primary path (sentinel id) is primed once in start()
        # and its bucket outlives games, so it never replays a stale value.
        if session.game_id == _PRIMARY_SENTINEL_GAME_ID or self._event_lists:
            # Event lists need no baseline: a game scope starts empty (BR-006).
            return
        with self._lock:
            if session.game_id in self._last_nonce:
                return
        logger.warning(
            f"InterventionManager: game {session.game_id or '<primary>'} had no edge baseline; "
            "treating current edge values and per-player grants as consumed (#1299, #1313)"
        )
        self._prime_edge_bucket(session)
        serials = _active_player_serials(session.game)
        self._prime_grant_bucket(session.game_id, serials)
        self._prime_level_bucket(session.game_id, serials)

    async def _evaluate_one(self, spec: InterventionSpec, session: "SessionView") -> None:
        # Player-targeted state flags are driven entirely through flagd's
        # per-serial targeting if-ladder (grants: shield/partial shield/soft
        # penalty, per serial; levels: sensitivity/handicap factor); the
        # agent writer leaves the flag's global defaultVariant on its neutral
        # value, so the global evaluated read NEVER changes and can't be used for
        # change detection. Resolve the per-serial values instead and key change
        # detection on them (handled separately below).
        if spec.per_serial_grant:
            await self._evaluate_targeted_grant(spec, session)
            return
        if spec.player_targeted and not spec.edge_triggered:
            await self._evaluate_targeted_state(spec, session)
            return

        if spec.edge_triggered and self._event_lists:
            await self._evaluate_event_list(spec, session)
            return

        if spec.edge_triggered:
            raw = self._read_edge_value(spec, session)
            if raw is _UNKNOWN:
                # Never move the baseline on an error read: the default ("")
                # would make the consumed nonce look fresh on recovery (#1299).
                return
            nonce, payload = _split_nonce(raw)
            # A missing nonce is always a no-op. A payload-carrying flag with an
            # empty payload is also a no-op. ``end_game`` (payload_required=False)
            # fires on the nonce alone. The baseline is still advanced so a later
            # real trigger with a new nonce fires.
            no_nonce = nonce.strip().lower() in _EDGE_EMPTY_VALUES
            empty_payload = payload.strip().lower() in _EDGE_EMPTY_VALUES
            is_noop = no_nonce or (spec.payload_required and empty_payload)
            with self._lock:
                bucket = self._existing_nonce_bucket(session.game_id)
                if bucket is None:
                    return  # session retired mid-pass: never fire into it
                last = bucket.get(spec.flag_key)
                bucket[spec.flag_key] = nonce
            # An unknown baseline (read failed at priming) adopts this value
            # without firing: it may be a stale nonce from an earlier game.
            changed = last is not _UNKNOWN and nonce != last
            if not changed or is_noop:
                return
            if spec.game_addressed and not _addressed_to(session, payload):
                # Consumed for this session (baseline advanced above) but meant
                # for another game: no dispatch, no event, no budget (#1310).
                logger.debug(
                    f"{spec.flag_key}: nonce addressed to {payload.strip() or '<real game>'}, "
                    f"not game {session.game_id or '<primary>'}; skipping"
                )
                return
            target = _edge_target_serial(spec, payload)
            value = raw
        else:
            raw = self._read_flag(spec, session)
            # State-shaped: only act on value change away from the none-value.
            with self._lock:
                bucket = self._existing_state_bucket(session.game_id)
                if bucket is None:
                    return  # session retired mid-pass
                last = bucket.get(spec.flag_key)
                changed = raw != last
                bucket[spec.flag_key] = raw
            if not changed:
                return
            if _is_none_value(spec, raw):
                # Reverting to neutral is not an enforced intervention, but some
                # state flags need to actively restore a default on revert (e.g.
                # volume_override -> configured game volume, #730 PR E). Such
                # flags register a revert handler; it runs without enforcement
                # (restoring a default is always safe / not rate-limited).
                # Only fire when the PREVIOUS value was a real (non-none)
                # intervention — a fresh none-to-none transition (unknown prior
                # baseline) must not spuriously restore.
                was_active = last is not None and not _is_none_value(spec, last)
                if was_active:
                    await self._run_revert(spec, raw, session)
                return
            payload = ""
            target = self._state_target_serial(spec)
            value = raw

        await self._enforce_and_dispatch(spec, value, payload, target, session)

    async def _evaluate_event_list(self, spec: InterventionSpec, session: "SessionView") -> None:
        """Apply every new event of a one-shot flag's per-game event list (FR-127).

        The flag resolves, for this game's ``gameId``, to
        ``{"epoch","seq","events":[{"seq","payload"}, ...]}`` (the last K
        events). Rules (architecture note §6, re-review N2):

        - a failed read (error code or reason ERROR) does nothing: it neither
          applies nor moves the last applied ``(epoch, seq)``;
        - an empty epoch is the baseline value: nothing to apply or reset;
        - only a non-empty, different epoch (a flag-service restart) resets the
          last seq to 0;
        - a seq at or below the last applied one in the same epoch is ignored;
        - more than K events between two reads is logged and counted;
        - the last applied ``(epoch, seq)`` advances under the lock BEFORE the
          events are dispatched, so an overlapping pass cannot apply them twice.
        """
        value = self._read_event_list(spec, session)
        if value is None:
            return
        epoch, seq, events = value
        if not epoch:
            return
        with self._lock:
            bucket = self._last_event.get(session.game_id)
            if bucket is None:
                return  # session retired mid-pass: never fire into it
            last_epoch, last_seq = bucket.get(spec.flag_key, ("", 0))
            if epoch != last_epoch:
                last_seq = 0
            elif seq <= last_seq:
                return
            fresh = sorted((e for e in events if e[0] > last_seq), key=lambda e: e[0])
            gap = bool(fresh) and fresh[0][0] > last_seq + 1
            bucket[spec.flag_key] = (epoch, max([seq, last_seq] + [e[0] for e in fresh]))
        if gap:
            logger.warning(
                f"{spec.flag_key}: game {session.game_id}: events {last_seq + 1}..{fresh[0][0] - 1} "
                "fell out of the event list before they were read (more than K between two reads)"
            )
            from services.game_coordinator import metrics

            metrics.intervention_event_gaps_total.labels(flag=spec.flag_key).inc()
        for _, payload in fresh:
            payload_str = _event_payload_string(spec, payload)
            if spec.payload_required and not payload_str.strip(" :"):
                continue
            target = _edge_target_serial(spec, payload_str)
            await self._enforce_and_dispatch(spec, payload_str, payload_str, target, session)

    def _read_event_list(
        self, spec: InterventionSpec, session: "SessionView"
    ) -> "tuple[str, int, list[tuple[int, dict]]] | None":
        """Read an event list as ``(epoch, seq, [(seq, payload), ...])``, or None
        on a failed or malformed read (fail closed: nothing applies)."""
        client = self._interventions_client
        if client is None:
            return None
        ctx = self._session_context(session.game_id)
        try:
            details = client.get_object_details(spec.flag_key, {}, ctx)
        except Exception as e:
            record_flag_fallback(spec.flag_key, type(e).__name__, str(e))
            return None
        if details.error_code is not None or details.reason == Reason.ERROR:
            error_code = getattr(details.error_code, "value", None) or str(details.error_code or "ERROR")
            record_flag_fallback(spec.flag_key, error_code, details.error_message or "")
            return None
        value = details.value
        if not isinstance(value, dict):
            return None
        try:
            epoch = str(value.get("epoch") or "")
            seq = int(value.get("seq") or 0)
            events = [
                (int(e["seq"]), e.get("payload") if isinstance(e.get("payload"), dict) else {})
                for e in value.get("events") or []
                if isinstance(e, dict) and "seq" in e
            ]
        except (TypeError, ValueError):
            return None
        return epoch, seq, events

    async def _evaluate_targeted_state(self, spec: InterventionSpec, session: "SessionView") -> None:
        """Change-detect and dispatch a player-targeted LEVEL flag.

        Levels (``player_sensitivity_factor``, ``player_handicap_factor``) are
        idempotent: the handler sets every player's factor, so re-applying an
        unchanged factor is a no-op. One-shot grants use
        :meth:`_evaluate_targeted_grant` instead (UC-040 BR-012).

        The agent writer drives these flags via a per-serial targeting if-ladder
        and never moves the global ``defaultVariant`` off neutral, so a global
        read is useless for change detection. Instead resolve the flag per active
        serial and key change detection on that resolved map. The handler (PR C/D)
        re-resolves and applies; dispatch is idempotent so re-applying an
        unchanged map would be harmless, but we still gate on change to avoid
        spurious metrics/events and needless rate-limit consumption.

        With no live game there are no serials to target, so this is a no-op
        (and the baseline is reset to empty so a later game picks up the flag).

        The per-serial baseline is seeded at game start (``prime_session``), and
        only the players whose level changed since the last read in this game
        are dispatched (as a ``{serial: level}`` value), so a level left over
        from an earlier game is never applied (UC-040 BR-017). Players whose
        level moved to neutral go through the flag's revert handler, which
        resets their factors without the permission chain (UC-040 A24, #1317).
        """
        game = session.game
        if game is None:
            with self._lock:
                bucket = self._existing_state_bucket(session.game_id)
                if bucket is not None:
                    bucket[spec.flag_key] = None
            return

        # Each alive, battery-eligible serial is read with evaluation details
        # (float getter, value_kind="float" for both levels): a flagd error
        # reads as ``_UNKNOWN`` instead of the neutral default, so it can never
        # look like "level reverted" nor move the baseline (fail closed, as for
        # grants, #1313).
        default = _spec_default(spec)
        reads = self._read_levels(spec, session.game_id, _active_player_serials(game))

        # Per-serial baseline for this game: ``{serial: level | _UNKNOWN}``,
        # seeded at game start (``prime_session``). Once primed, a serial with no
        # entry adopts its current level without applying it (e.g. a revived
        # player the fallback prime skipped, or one battery-gated at prime). An
        # unprimed bucket (legacy single-primary path) treats a missing serial
        # as neutral, so a first non-neutral level applies.
        changed: dict = {}
        with self._lock:
            bucket = self._existing_state_bucket(session.game_id)
            if bucket is None:
                return  # session retired mid-pass
            last = bucket.get(spec.flag_key)
            primed = isinstance(last, dict)
            baseline = dict(last) if primed else {}
            missing = _UNKNOWN if primed else default
            for serial, value in reads.items():
                if value is _UNKNOWN:
                    continue  # never move the baseline on an error read
                previous = baseline.get(serial, missing)
                baseline[serial] = value
                if previous is _UNKNOWN or _same_value(value, previous):
                    continue
                changed[serial] = value
            bucket[spec.flag_key] = baseline
        if not changed:
            return

        # Only the players whose level changed in this game are dispatched, so a
        # level left over from an earlier game is never applied (UC-040 BR-017).
        # A move to neutral is a revert: it bypasses the permission chain and the
        # backstop, so a blocked chain (kill switch, exhausted budget) can never
        # leave a factor stuck after the baseline moved (UC-040 A24, #1317).
        to_neutral = {s: v for s, v in changed.items() if _is_none_value(spec, v)}
        applied = {s: v for s, v in changed.items() if s not in to_neutral}
        if to_neutral:
            await self._run_revert(spec, to_neutral, session)
        if applied:
            # Battery gating already happened in _read_levels; the chain-level
            # target is None (fan-out), so its single-target battery check is a
            # no-op here. The handler applies ``applied``.
            await self._enforce_and_dispatch(spec, applied, "", None, session)

    async def _run_revert(self, spec: InterventionSpec, value: object, session: "SessionView") -> None:
        """Run ``spec``'s revert handler for ``session``, if one is registered.

        Reverts restore a default; they bypass the permission chain and the
        backstop, and a failing revert handler is logged, never raised. Like an
        apply, the revert runs on the owning game's loop through the session's
        dispatcher, so it queues behind any effect already queued there
        (UC-040 BR-018, #1250).
        """
        revert = self._revert_handlers.get(spec.flag_key)
        if revert is None:
            return
        ctx = InterventionContext(
            spec=spec,
            value=value,
            payload="",
            target_serial=None,
            game=session.game,
            objective=self._dominant_objective(),
            game_id=session.game_id,
        )
        try:
            await self._run_on_game_loop(session, spec, revert, ctx)
        except GameLoopUnavailableError:
            pass  # already logged + counted once by _run_on_game_loop
        except Exception as e:
            logger.error(f"InterventionManager: revert handler for {spec.flag_key} raised: {e}")

    def _read_levels(self, spec: InterventionSpec, game_id: str, serials) -> dict:
        """Read a per-player LEVEL flag for each battery-eligible serial as its
        float value, or ``_UNKNOWN`` on a flagd error. Battery-gated serials are
        left out (their level is neither applied nor baselined)."""
        threshold = self._battery_threshold()
        reads: dict = {}
        for serial in serials:
            pct = self._battery_pct(serial)
            if pct is not None and pct < threshold:
                continue  # battery guard: skip low-battery controllers
            grant = self._read_grant(spec, game_id, serial)
            reads[serial] = grant if grant is _UNKNOWN else grant[1]
        return reads

    def _prime_level_bucket(self, game_id: str, serials) -> None:
        """Record each per-player LEVEL flag's per-serial level for ``game_id``'s
        players without applying it (UC-040 BR-017, #1317/#1269). A level still
        in the flag when the game starts was decided for an earlier game; the
        new game's players start neutral and keep it that way until a level
        changes during this game. A read error records ``_UNKNOWN`` (adopt the
        next good read, never apply it). Battery-gated serials are left out at
        prime; once readable they adopt their level without applying it.
        """
        serials = list(serials)
        for spec in INTERVENTION_SPECS:
            if not _is_level_spec(spec):
                continue
            reads = self._read_levels(spec, game_id, serials)
            with self._lock:
                self._state_bucket(game_id)[spec.flag_key] = reads

    async def _evaluate_targeted_grant(self, spec: InterventionSpec, session: "SessionView") -> None:
        """Change-detect a per-serial GRANT flag and dispatch only new grants.

        Grants (shield, partial shield, soft penalty) are one-shot effects, so a
        serial whose grant did not change since this game last read it must not
        be granted again (UC-040 BR-012, #1313). Each live serial's
        ``(variant, value)`` is compared with the game's per-serial baseline
        (seeded at game start by ``prime_session``); the agent writes a fresh
        variant per decision, so even a same-value re-grant is a change. One
        intervention is dispatched per changed, non-neutral serial with
        ``target`` = that serial, so the battery guard and the backstop apply
        per grant. Fail closed: an error read never moves that serial's
        baseline, and an ``_UNKNOWN`` or missing baseline adopts the next good
        read without firing. A serial pruned by the agent (UC-040 BR-016)
        resolves to the neutral variant, which is a change but never fires.
        """
        game = session.game
        if game is None:
            return
        reads = {serial: self._read_grant(spec, session.game_id, serial) for serial in _active_player_serials(game)}
        fired: list[tuple[str, object]] = []
        with self._lock:
            bucket = self._existing_state_bucket(session.game_id)
            if bucket is None:
                return  # session retired mid-pass
            baseline = bucket.get(spec.flag_key)
            primed = isinstance(baseline, dict)
            if not primed:
                baseline = {}
                bucket[spec.flag_key] = baseline
            # Once the bucket is primed, a serial with no baseline entry (e.g.
            # revived after the fallback prime, which records alive players
            # only) adopts its current grant like ``_UNKNOWN``: it may predate
            # this game (UC-040 A19, #1323).
            missing = _UNKNOWN if primed else None
            for serial, grant in reads.items():
                if grant is _UNKNOWN:
                    continue  # never move the baseline on an error read
                last = baseline.get(serial, missing)
                baseline[serial] = grant
                if last is _UNKNOWN or grant == last:
                    continue
                value = grant[1]
                if not _is_none_value(spec, value):
                    fired.append((serial, value))
        for serial, value in fired:
            await self._enforce_and_dispatch(spec, value, "", serial, session)

    def _prime_grant_bucket(self, game_id: str, serials) -> None:
        """Record each grant flag's current per-serial grant for ``game_id``
        without applying it (UC-040 BR-012, #1313). An entry present when the
        game starts belongs to an earlier decision; ``_UNKNOWN`` on a read error.
        """
        serials = list(serials)
        for spec in INTERVENTION_SPECS:
            if not spec.per_serial_grant:
                continue
            grants = {serial: self._read_grant(spec, game_id, serial) for serial in serials}
            with self._lock:
                self._state_bucket(game_id)[spec.flag_key] = grants

    def _read_grant(self, spec: InterventionSpec, game_id: str, serial: str) -> "tuple[object, object] | _UnknownNonce":
        """Read one serial's grant as ``(variant, value)`` or ``_UNKNOWN``.

        Read with evaluation details: the value getters never raise and return
        the default on error, which would look like "no grant" and move the
        baseline (#1299). The variant is part of the grant identity because the
        agent writes a fresh ``agent_<serial>#<nonce>`` variant per decision.
        """
        client = self._interventions_client
        if client is None:
            return _UNKNOWN
        ctx = self._session_context(game_id, targeting_key=serial)
        default = _spec_default(spec)
        try:
            if spec.value_kind == "string":
                details = client.get_string_details(spec.flag_key, str(default), ctx)
            else:
                details = client.get_float_details(spec.flag_key, float(default), ctx)
        except Exception as e:
            record_flag_fallback(spec.flag_key, type(e).__name__, str(e))
            return _UNKNOWN
        if details.error_code is not None or details.reason == Reason.ERROR:
            error_code = getattr(details.error_code, "value", None) or str(details.error_code or "ERROR")
            record_flag_fallback(spec.flag_key, error_code, details.error_message or "")
            return _UNKNOWN
        value = details.value
        if spec.value_kind == "string":
            if not isinstance(value, str):
                return _UNKNOWN
        else:
            value = _coerce_float(value)
            if value is None:
                return _UNKNOWN
        return (details.variant, value)

    # ------------------------------------------------------------------ #
    # Enforcement chain
    # ------------------------------------------------------------------ #
    async def _enforce_and_dispatch(
        self, spec: InterventionSpec, value: object, payload: str, target: str | None, session: "SessionView"
    ) -> None:
        objective = self._dominant_objective()
        game = session.game

        block_reason = self._check_chain(spec, target, game)
        if block_reason is not None:
            await self._record(spec, target, objective, session, blocked=True, block_reason=block_reason)
            return

        ctx = InterventionContext(
            spec=spec,
            value=value,
            payload=payload,
            target_serial=target,
            game=game,
            objective=objective,
            game_id=session.game_id,
        )
        handler = self._handlers.get(spec.flag_key, self._noop_handler)
        try:
            if spec.flag_key in self._main_loop_flags:
                await handler(ctx)
            else:
                await self._run_on_game_loop(session, spec, handler, ctx)
        except GameLoopUnavailableError as e:
            # UC-040 BR-018, already logged + counted. game_loop_late: the effect
            # started and may still apply, so it is not reported as blocked;
            # otherwise it was never applied (fail closed). No cue either way.
            await self._record(spec, target, objective, session, blocked=not e.applied_unknown, block_reason=e.reason)
            return
        except Exception as e:
            logger.error(f"InterventionManager: handler for {spec.flag_key} raised: {e}")
            await self._record(spec, target, objective, session, blocked=True, block_reason="handler_error")
            return

        await self._record(spec, target, objective, session, blocked=False, block_reason="")

        # On-device visibility cue (#818): surface HARD-class agent actions at the
        # controller so an operator can tell agent activity from a malfunction.
        # Gated behind a user.json mute toggle so it can be silenced for demos.
        #
        # Not for main-loop flags: end_game has just ended the game, so its loop
        # is gone and there is no controller left to cue. The mute toggle is read
        # here, on the main loop, so a muted cue is never dispatched.
        if spec.weight >= WEIGHT_HARD and spec.flag_key not in self._main_loop_flags and self._cue_enabled():
            # Best-effort cue: a game loop that just ended is not a failure.
            with contextlib.suppress(GameLoopUnavailableError):
                await self._run_on_game_loop(session, spec, self._emit_agent_cue, spec, target, game, quiet=True)

    # ------------------------------------------------------------------ #
    # Game-loop dispatch (UC-040 BR-018, #1250)
    # ------------------------------------------------------------------ #
    def _dispatcher_for(self, session: "SessionView") -> GameLoopDispatcher:
        """Return the session's dispatcher, bound to its current game loop.

        A session id whose loop changed (a new game reusing the id, or the legacy
        sentinel across games) gets a fresh dispatcher for the new loop.
        """
        loop = session.loop
        with self._lock:
            dispatcher = self._dispatchers.get(session.game_id)
            if dispatcher is None or dispatcher.loop is not loop:
                dispatcher = GameLoopDispatcher(loop, timeout_s=self._dispatch_timeout_s)
                self._dispatchers[session.game_id] = dispatcher
            return dispatcher

    async def _run_on_game_loop(
        self, session: "SessionView", spec: InterventionSpec, fn, *args, quiet: bool = False
    ) -> None:
        """Run one side effect ``fn(*args)`` on the session's game loop.

        Raises ``GameLoopUnavailableError`` when the effect did not complete on
        the game's loop: not applied (closed / stopping loop, or not started in
        time) or ``game_loop_late`` (started, outcome unknown). It is logged and
        counted once here; ``quiet`` (the best-effort agent cue) logs at debug
        and does not count. Errors raised by ``fn`` itself propagate unchanged.
        """
        try:
            await self._dispatcher_for(session).run(fn, *args)
        except GameLoopUnavailableError as e:
            if quiet:
                game_label = session.game_id or "<primary>"
                logger.debug(f"InterventionManager: {spec.flag_key} cue for game {game_label} skipped ({e})")
                raise
            logger.warning(
                f"InterventionManager: {spec.flag_key} effect for game {session.game_id or '<primary>'} "
                f"not applied ({e})"
            )
            try:
                from services.game_coordinator import metrics

                metrics.intervention_dispatch_failures_total.labels(type=spec.type_id, reason=e.reason).inc()
            except Exception as metric_error:
                logger.debug(f"InterventionManager: dispatch failure metric failed: {metric_error}")
            raise

    def _cue_enabled(self) -> bool:
        """Whether the on-device agent-intervention cue is enabled (#818).

        Reads the ``agent_intervention_cue`` toggle from the user domain. Defaults
        to ON (fail-open to visibility) when the client is unavailable or the read
        fails — the whole point of #818 is that the operator can SEE agent action.
        """
        if self._user_client is None:
            return True
        try:
            return bool(self._user_client.get_boolean_value("agent_intervention_cue", True, EvaluationContext()))
        except Exception as e:
            logger.debug(f"agent cue gate read failed, defaulting to enabled: {e}")
            return True

    async def _emit_agent_cue(self, spec: InterventionSpec, target: str | None, game: object) -> None:
        """Pulse a distinct 'agent acted' LED on the controller(s) (#818).

        Minimal, non-disruptive: a short magenta LED PULSE (GAME_EFFECT_PULSE
        auto-restores to the base color) on the affected player when the
        intervention is player-targeted, else broadcast to all active players. A
        player-targeted intervention on a now-dead/unknown serial yields no target
        and is skipped. Best-effort — any failure is swallowed so the cue can
        never break the intervention path.
        """
        if not self._cue_enabled():
            return
        stream = getattr(game, "gameplay_stream", None) if game is not None else None
        if stream is None:
            return
        try:
            cue_serial = target if spec.player_targeted and target else ""
            targets = _effect_targets(game, cue_serial)
            if not targets:
                return

            import asyncio

            from proto import controller_manager_pb2

            # Distinct magenta pulse = "agent acted" — not a game event color.
            # Dispatch the per-target writes concurrently so a broadcast cue
            # (e.g. an end_game HARD intervention with many players) doesn't add N
            # sequential writes to the dispatch tail. Best-effort: any per-write
            # failure is collected (return_exceptions) and never raised into the
            # intervention path.
            writes = [
                stream.write(
                    controller_manager_pb2.GameplayStreamControl(
                        game_effect=controller_manager_pb2.GameEffectCommand(
                            serial=target_serial,
                            effect=controller_manager_pb2.GAME_EFFECT_PULSE,
                        )
                    )
                )
                for target_serial in targets
            ]
            await asyncio.gather(*writes, return_exceptions=True)
            logger.debug(f"agent cue: pulsed {len(targets)} controller(s) for '{spec.type_id}'")
        except Exception as e:
            logger.debug(f"agent cue emission failed for '{spec.type_id}': {e}")

    def _check_chain(self, spec: InterventionSpec, target: str | None, game: object) -> str | None:
        """Run the enforcement chain. Returns a block reason or None if allowed.

        Order: allowed-membership -> rate limit -> battery guard -> mode matrix.
        The rate-limit reservation happens last among the cheap checks so a
        blocked intervention never consumes budget.
        """
        # (a) allowed-membership
        if spec.type_id not in self._allowed_types():
            return BLOCK_NOT_ALLOWED

        # (d) mode-capability matrix (cheap, no budget side effect) — checked
        # before the rate limiter so unsupported types don't consume budget.
        if game is not None:
            mode_name = _game_mode_name(game)
            if spec.type_id in MODE_CAPABILITY_DENY.get(mode_name, frozenset()):
                return BLOCK_MODE_UNSUPPORTED

        # (c) battery guard (player-targeted only)
        if spec.player_targeted and target:
            pct = self._battery_pct(target)
            if pct is not None and pct < self._battery_threshold():
                return BLOCK_LOW_BATTERY

        # (b) weighted rate limit — the GENEROUS GLOBAL BACKSTOP (#919), NOT the
        # per-game governor (that is the agent's). Reserves budget; do last so
        # the reservation only happens for otherwise-allowed interventions. A
        # block here means a process-wide flood across ALL games, not a single
        # game pacing itself out.
        if not self._rate_limiter.check(spec.weight):
            return BLOCK_RATE_LIMITED

        return None

    # ------------------------------------------------------------------ #
    # Metric + event emission
    # ------------------------------------------------------------------ #
    async def _record(
        self,
        spec: InterventionSpec,
        target: str | None,
        objective: str,
        session: "SessionView",
        *,
        blocked: bool,
        block_reason: str,
    ) -> None:
        try:
            from services.game_coordinator import metrics

            metrics.interventions_total.labels(
                type=spec.type_id,
                objective=objective,
                blocked=str(blocked).lower(),
                block_reason=block_reason,
            ).inc()
        except Exception as e:
            logger.debug(f"InterventionManager: metric record failed: {e}")

        from lib.types import GameEvent

        # Publish on THIS session's bus so the effect/blocked event lands on the
        # stream the owning game's subscribers (and the agent runner) are reading
        # (#838). The session's game_id is stamped onto the event by the bus.
        await session.publish(
            GameEvent.AGENT_INTERVENTION,
            {
                "type": spec.type_id,
                "target": target or "",
                "blocked": str(blocked).lower(),
                "block_reason": block_reason,
            },
        )

    # ------------------------------------------------------------------ #
    # Flag reads / policy values
    # ------------------------------------------------------------------ #
    def _session_context(self, game_id: str, *, targeting_key: str | None = None) -> EvaluationContext:
        """Build the per-session EvaluationContext (#838).

        Adds ``gameId`` so flagd targeting rules can scope an intervention to one
        game. An empty ``game_id`` (the synthesized primary session in the legacy
        ``get_game`` path) adds NO ``gameId`` attribute, so un-targeted flags
        resolve exactly as before. ``targeting_key`` (the player serial) is set
        for per-player resolution so per-player and per-game rules compose.
        """
        attributes = {"gameId": game_id} if game_id else {}
        return EvaluationContext(targeting_key=targeting_key, attributes=attributes)

    def _read_flag(self, spec: InterventionSpec, session: "SessionView") -> object:
        client = self._interventions_client
        if client is None:
            return _spec_default(spec)
        ctx = self._session_context(session.game_id)
        if spec.value_kind == "int":
            return client.get_integer_value(spec.flag_key, spec.none_value, ctx)
        if spec.value_kind == "float":
            return client.get_float_value(spec.flag_key, float(spec.none_value), ctx)
        # State-shaped string flags carry a string none_value (e.g. pacing_profile
        # "none"); edge-triggered flags leave none_value None and default to "".
        string_default = spec.none_value if isinstance(spec.none_value, str) else ""
        return client.get_string_value(spec.flag_key, string_default, ctx)

    def _read_edge_value(self, spec: InterventionSpec, session: "SessionView") -> "str | _UnknownNonce":
        """Read an edge flag's raw ``"<nonce>:<payload>"`` or ``_UNKNOWN`` on error.

        OpenFeature's value getters never raise: on a resolution error they
        return the default (``""``), which is indistinguishable from "no
        intervention" and would move the nonce baseline. Read the details
        instead and report any error as ``_UNKNOWN`` (#1299).
        """
        client = self._interventions_client
        if client is None:
            return _UNKNOWN
        ctx = self._session_context(session.game_id)
        try:
            details = client.get_string_details(spec.flag_key, "", ctx)
        except Exception as e:
            record_flag_fallback(spec.flag_key, type(e).__name__, str(e))
            return _UNKNOWN
        if details.error_code is not None or details.reason == Reason.ERROR:
            error_code = getattr(details.error_code, "value", None) or str(details.error_code or "ERROR")
            record_flag_fallback(spec.flag_key, error_code, details.error_message or "")
            return _UNKNOWN
        value = details.value
        return value if isinstance(value, str) else _UNKNOWN

    def _state_target_serial(self, _spec: InterventionSpec) -> str | None:
        """Player-targeted state flags fan out across all active serials via
        flagd targeting (keyed on ``targeting_key = serial``); there is no single
        chain-level target. The battery guard is therefore applied per serial
        inside :meth:`resolve_player_targets`, not here, so this returns None
        (the chain's single-target battery check is a no-op for fan-out flags).
        """
        return None

    # ------------------------------------------------------------------ #
    # Per-player targeting resolution (reusable; PR D shield_seconds uses it)
    # ------------------------------------------------------------------ #
    def resolve_player_targets(
        self,
        flag_key: str,
        default,
        game: object,
        *,
        value_kind: str = "float",
        battery_gate: bool = True,
        game_id: str = "",
    ) -> dict:
        """Evaluate a per-player targeted flag for every active player serial.

        For each active serial in ``game.players`` the flag is evaluated with an
        ``EvaluationContext(targeting_key=serial, attributes={gameId})`` so
        flagd's per-serial AND per-game targeting rules resolve and compose in a
        single rule (#838: a rule may match both ``targetingKey == serial`` and
        ``gameId == <id>``). Serials with no targeting match receive ``default``.
        When ``battery_gate`` is True, any serial whose battery pct is below
        ``policy.battery_threshold`` is dropped from the result (the per-serial
        equivalent of the chain's battery guard — closes the
        player-targeted-state-flag battery-guard gap documented in PR A's
        ``_state_target_serial``).

        Reusable across player-targeted state interventions: PR C uses it for
        ``player_sensitivity_factor``; PR D uses it for ``shield_seconds``.

        Args:
            flag_key: Flag to evaluate in the ``interventions`` domain.
            default: Neutral value returned for serials with no targeting match
                (e.g. 1.0 for sensitivity factor, 0 for shield seconds).
            game: Live game whose ``players`` dict supplies the active serials.
            value_kind: ``"float"``, ``"int"`` or ``"string"`` (flag value type).
                ``"string"`` (#1129 partial_shield) returns raw per-serial strings
                for the handler to parse; float/int return coerced numbers.
            battery_gate: If True, drop serials below the battery threshold.
            game_id: Owning session's game id, added as ``gameId`` to the context
                (#838). Empty string adds no ``gameId`` (legacy primary path).

        Returns:
            ``{serial: value}`` for every active serial that should be acted on.
            Battery-gated serials are omitted entirely.
        """
        result: dict = {}
        client = self._interventions_client
        serials = _active_player_serials(game)
        threshold = self._battery_threshold() if battery_gate else None

        for serial in serials:
            if threshold is not None:
                pct = self._battery_pct(serial)
                if pct is not None and pct < threshold:
                    continue  # battery guard: skip low-battery controllers

            if client is None:
                value = default
            else:
                ctx = self._session_context(game_id, targeting_key=serial)
                try:
                    if value_kind == "int":
                        value = float(client.get_integer_value(flag_key, int(default), ctx))
                    elif value_kind == "string":
                        # Raw string returned for the handler to parse (#1129).
                        value = str(client.get_string_value(flag_key, str(default), ctx))
                    else:
                        value = float(client.get_float_value(flag_key, float(default), ctx))
                except Exception as e:  # one bad serial must not drop the rest
                    logger.debug(f"InterventionManager: per-serial read failed for {serial}: {e}")
                    value = default
            result[serial] = value

        return result

    def _allowed_types(self) -> set[str]:
        # interventions_allowed is a STRING flag whose value is a comma-separated
        # list of intervention ids (`none` == ""). It used to be a LIST/object
        # flag, but the agent's flagd RPC resolver silently TYPE_MISMATCHes
        # get_object_value on a top-level LIST flag and falls back to its empty
        # default, blocking EVERY intervention even with a non-`none` variant
        # active (#1127). Both sides now read a string and parse it identically;
        # the coordinator's in-process resolver reads the string cleanly too.
        if self._agent_client is None:
            return set()
        try:
            value = self._agent_client.get_string_value("interventions_allowed", "", EvaluationContext())
            if isinstance(value, str):
                return {token.strip() for token in value.split(",") if token.strip()}
        except Exception as e:
            logger.debug(f"InterventionManager: interventions_allowed read failed: {e}")
        return set()

    def _battery_threshold(self) -> float:
        if self._agent_client is None:
            return 20.0
        try:
            return float(self._agent_client.get_integer_value("policy.battery_threshold", 20, EvaluationContext()))
        except Exception:
            return 20.0

    def _refresh_budget(self) -> None:
        """Refresh the GLOBAL BACKSTOP budget (#919).

        Reads ``policy.coordinator_backstop_per_minute`` — NOT
        ``policy.max_interventions_per_minute``. The latter is the agent's
        authoritative per-game budget; the coordinator must not mirror it (that
        is exactly the drift the contract forbids). This limiter owns only the
        generous global flood ceiling, which falls back to
        ``DEFAULT_COORDINATOR_BACKSTOP_BUDGET`` when the flag is unreachable.
        """
        budget = DEFAULT_COORDINATOR_BACKSTOP_BUDGET
        if self._agent_client is not None:
            try:
                budget = float(
                    self._agent_client.get_integer_value(
                        "policy.coordinator_backstop_per_minute",
                        int(DEFAULT_COORDINATOR_BACKSTOP_BUDGET),
                        EvaluationContext(),
                    )
                )
            except Exception:
                budget = DEFAULT_COORDINATOR_BACKSTOP_BUDGET
        self._rate_limiter.set_budget(budget)

    def _dominant_objective(self) -> str:
        """Return the highest-weight objective from the agent ``objectives``
        flag (weighted dict). Defaults to ``balanced``."""
        if self._agent_client is None:
            return "balanced"
        try:
            weights = self._agent_client.get_object_value("objectives", {}, EvaluationContext())
            if isinstance(weights, dict) and weights:
                return max(weights, key=lambda k: weights.get(k, 0.0))
        except Exception:
            pass
        return "balanced"

    def _battery_pct(self, serial: str) -> float | None:
        if self._battery_provider is None:
            return None
        try:
            return self._battery_provider(serial)
        except Exception:
            return None


# ---------------------------------------------------------------------- #
# Module-level helpers (pure; easy to unit-test)
# ---------------------------------------------------------------------- #
def _event_payload_string(spec: InterventionSpec, payload: dict) -> str:
    """Map an event payload (FR-127) to the payload string the handlers take:
    the serial (eliminate/revive), the sound id (audio_cue),
    ``"<serial>:<effect>"`` (controller_effect) or ``""`` (end_game; the game
    is the scope)."""
    serial = str(payload.get("serial") or "")
    if spec.flag_key == "audio_cue":
        return str(payload.get("sound") or "")
    if spec.flag_key == "controller_effect":
        return f"{serial}:{payload.get('effect') or ''}"
    if spec.flag_key == "end_game":
        return ""
    return serial


def _split_nonce(raw: str) -> tuple[str, str]:
    """Split an edge-triggered value ``"<nonce>:<payload>"``.

    Returns ``(nonce, payload)``. A value with no ``:`` is treated as
    nonce-only (payload empty) — matches ``end_game`` shape ``"<nonce>"``.
    Empty string -> ("", "").
    """
    if raw == "":
        return "", ""
    if ":" not in raw:
        return raw, ""
    nonce, payload = raw.split(":", 1)
    return nonce, payload


def _addressed_to(session: "SessionView", payload: str) -> bool:
    """Whether a game-addressed edge payload (``end_game``) targets ``session``.

    A payload names the deciding game's id: only that session applies it. An
    empty payload (legacy / hand-written ``"<nonce>"``) applies only to the real
    game, never to a shadow (UC-040 BR-011, #1310).
    """
    addressed = payload.strip()
    if addressed:
        return addressed == session.game_id
    return session.is_real


def _edge_target_serial(spec: InterventionSpec, payload: str) -> str | None:
    """Resolve the target serial for an edge-triggered intervention.

    - eliminate_player / revive_player: payload IS the serial.
    - controller_effect: payload is ``"<serial>:<effect>"``; empty serial =
      broadcast (-> None).
    - audio_cue / end_game: not player-targeted (-> None).
    """
    if not spec.player_targeted:
        return None
    serial = payload.split(":", 1)[0] if ":" in payload else payload
    serial = serial.strip()
    return serial or None


def _is_none_value(spec: InterventionSpec, value: object) -> bool:
    """True if a state-shaped flag value equals its neutral/none value."""
    none_val = spec.none_value
    if isinstance(none_val, (int, float)) and isinstance(value, (int, float)):
        return abs(float(value) - float(none_val)) < 1e-9
    return value == none_val


def _same_value(a: object, b: object) -> bool:
    """Equal values, comparing numbers with a float tolerance."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


def _is_level_spec(spec: InterventionSpec) -> bool:
    """True for a per-player LEVEL flag (player_sensitivity_factor,
    player_handicap_factor): player-targeted state, not edge, not a grant."""
    return spec.player_targeted and not spec.edge_triggered and not spec.per_serial_grant


def _spec_default(spec: InterventionSpec) -> object:
    if spec.edge_triggered:
        return ""
    if spec.value_kind in ("int", "float"):
        return spec.none_value
    # State-shaped string flag: fall back to its string none_value (e.g. "none").
    return spec.none_value if isinstance(spec.none_value, str) else ""


def _active_player_serials(game: object) -> list[str]:
    """Best-effort list of active (alive) player serials from a live game.

    Returns serials of players that are still alive; falls back to all serials
    if aliveness can't be read. Empty list when there is no game / no players.
    """
    if game is None:
        return []
    players = getattr(game, "players", None)
    if not isinstance(players, dict):
        return []
    serials: list[str] = []
    for serial, player in players.items():
        alive = getattr(player, "alive", True)
        if alive:
            serials.append(serial)
    return serials


def _game_mode_name(game: object) -> str:
    """Best-effort mode name from a live game via get_game_name()."""
    getter = getattr(game, "get_game_name", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            return ""
    return ""


# --- PR E ambient/session helpers -------------------------------------- #

# Effect names the agent may request -> controller_manager GameEffectType enum
# value name. Only the agent-safe subset (signalling / ambient feedback) is
# exposed; per-player lifecycle effects (warning/death/respawn/countdown) and
# admin effects are intentionally NOT addressable via this intervention. The
# mode-capability matrix already blocks send_controller_effect in hidden-role
# modes (LED leak), so this map does not re-encode that policy.
_CONTROLLER_EFFECT_NAMES: dict[str, str] = {
    "rumble": "GAME_EFFECT_RUMBLE",
    "pulse": "GAME_EFFECT_PULSE",
    "flash": "GAME_EFFECT_FLASH",
    "show_battery": "GAME_EFFECT_SHOW_BATTERY",
}


def _coerce_float(value: object) -> float | None:
    """Best-effort float coercion; returns None on failure."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_effect_payload(payload: str) -> tuple[str, str | None]:
    """Split a controller_effect payload ``"<serial>:<effect>"``.

    Returns ``(serial, effect_name)``. ``serial`` may be empty (broadcast).
    A payload with no ``:`` or an empty effect is malformed -> effect_name None.
    """
    if ":" not in payload:
        return "", None
    serial, effect = payload.split(":", 1)
    serial = serial.strip()
    effect = effect.strip().lower()
    if not effect:
        return serial, None
    return serial, effect


def _resolve_effect(effect_name: str) -> int | None:
    """Map a validated effect name to its GameEffectType enum value, or None."""
    enum_name = _CONTROLLER_EFFECT_NAMES.get(effect_name)
    if enum_name is None:
        return None
    try:
        from proto import controller_manager_pb2

        return getattr(controller_manager_pb2, enum_name)
    except Exception:
        return None


def _effect_targets(game: object, serial: str) -> list[str]:
    """Resolve controller serials for an effect.

    Empty ``serial`` broadcasts to all alive players; a specific serial routes
    to that one player when present and alive. Unknown / dead serials -> [].
    """
    players = getattr(game, "players", None) if game is not None else None
    if not isinstance(players, dict):
        # No player map (e.g. test stub): honor an explicit serial, else nothing.
        return [serial] if serial else []
    if serial:
        player = players.get(serial)
        return [serial] if player is not None and getattr(player, "alive", False) else []
    return [s for s, p in players.items() if getattr(p, "alive", False)]
