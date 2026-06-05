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
      reconnects / re-reads).
- Enforcement chain, evaluated *before* any handler runs (defense in depth; the
  agent also self-checks in #726):
    1. allowed-membership   — type must be in ``interventions_allowed`` (agent
       domain, default ``[]``)
    2. weighted rate limit  — sliding 60s window, weighted cost per class,
       budget from ``policy.max_interventions_per_minute``
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

import logging
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from openfeature.evaluation_context import EvaluationContext
from openfeature.provider import ProviderEvent

logger = logging.getLogger(__name__)

# Rate-limit weighting classes (§5). Cost per applied intervention.
WEIGHT_SOFT = 0.5  # psychological, self-reverting
WEIGHT_MEDIUM = 1.0  # tunable, reversible
WEIGHT_HARD = 2.0  # state-changing, player-visible as unfair if wrong

# Sliding rate-limit window in seconds (§5: "max_interventions_per_minute").
RATE_LIMIT_WINDOW_SECONDS = 60.0

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


# Registry of all 10 intervention flags (§8). Ordered to keep diffs stable.
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
        flag_key="shield_seconds",
        type_id="grant_shield",
        weight=WEIGHT_MEDIUM,
        edge_triggered=False,
        player_targeted=True,
        value_kind="float",
        none_value=0,
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
        payload_required=False,  # value is nonce-only: "<nonce>"
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
    """

    spec: InterventionSpec
    value: object
    payload: str
    target_serial: str | None
    game: object
    objective: str


class _RateLimiter:
    """Weighted sliding-window rate limiter (§5).

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
    """

    game_id: str
    game: object
    publish: "Callable[[str, dict], Awaitable[None]]"


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
        end_game_fn: Callable[[str], Awaitable[tuple[bool, str]]] | None = None,
        get_sessions: Callable[[], list["SessionView"]] | None = None,
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
            end_game_fn: Async ``end_game(reason) -> (success, error)`` used by
                the ``end_game`` intervention handler to share the servicer's
                force-end path (#730, PR E). None disables the handler's effect.
            get_sessions: Per-session seam (#838). Returns one ``SessionView``
                per LIVE session (primary + every shadow). The manager evaluates
                every intervention flag against each session with a per-session
                ``{gameId, targetingKey=serial}`` context and applies/publishes
                on THAT session. Flags with no ``gameId`` targeting rule resolve
                to their default for shadow games (no-op) and the configured
                value for the primary, preserving today's behavior.
        """
        self._publish = event_publisher
        self._get_game = get_game
        self._get_sessions = get_sessions
        self._battery_provider = battery_provider
        self._time_fn = time_fn
        self._end_game_fn = end_game_fn

        self._interventions_client = None  # interventions domain
        self._agent_client = None  # agent domain (backstop reads)

        # Per-session, per-flag last-applied nonce (edge-triggered exactly-once)
        # and last-applied state value (state-shaped change detection). Keyed
        # ``game_id -> flag_key -> value`` so each live session tracks its own
        # baseline independently (#838). The legacy single-primary path uses the
        # ``_PRIMARY_SENTINEL_GAME_ID`` ("") bucket.
        self._last_nonce: dict[str, dict[str, str]] = {}
        self._last_state_value: dict[str, dict[str, object]] = {}

        self._rate_limiter = _RateLimiter(budget=2.0, time_fn=time_fn)

        # Handler registry: flag_key -> Handler. Defaults to the no-op stub for
        # every spec. PRs C/D/E replace entries via register_handler().
        self._handlers: dict[str, Handler] = {spec.flag_key: self._noop_handler for spec in INTERVENTION_SPECS}
        # Optional per-flag handlers run when a state-shaped flag reverts to its
        # neutral value (e.g. volume_override -> default game volume). Additive;
        # absence means "do nothing on revert" (the default for all flags).
        self._revert_handlers: dict[str, Handler] = {}

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
    def register_handler(self, flag_key: str, handler: Handler) -> None:
        """Register the real handler for a flag (PRs C/D/E).

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
        self.register_handler("end_game", self._handle_end_game)
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

    async def _handle_end_game(self, _ctx: InterventionContext) -> None:
        """End the running game via the servicer's shared force-end path.

        No-ops safely when no game is running or no end_game_fn is wired.
        """
        if self._end_game_fn is None:
            logger.info("end_game: no end_game_fn wired, skipping")
            return
        success, error = await self._end_game_fn("agent_intervention")
        if success:
            logger.info("end_game: game force-ended via agent intervention")
        else:
            logger.info(f"end_game: no-op ({error})")

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
            for spec in INTERVENTION_SPECS:
                try:
                    raw = self._read_flag(spec, session)
                except Exception:
                    continue
                with self._lock:
                    if spec.edge_triggered:
                        nonce, _ = _split_nonce(str(raw))
                        self._nonce_bucket(session.game_id)[spec.flag_key] = nonce
                    else:
                        self._state_bucket(session.game_id)[spec.flag_key] = raw

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
        return [SessionView(game_id=_PRIMARY_SENTINEL_GAME_ID, game=game, publish=self._publish)]

    def _nonce_bucket(self, game_id: str) -> dict[str, str]:
        return self._last_nonce.setdefault(game_id, {})

    def _state_bucket(self, game_id: str) -> dict[str, object]:
        return self._last_state_value.setdefault(game_id, {})

    def _prune_stale_buckets(self, live_ids: set[str]) -> None:
        """Drop change-detection state for sessions that are no longer live.

        Prevents unbounded growth as shadow games come and go, and ensures a new
        game reusing a (vanishingly unlikely) recycled id starts from a clean
        baseline. The sentinel primary bucket is always retained.
        """
        with self._lock:
            keep = live_ids | {_PRIMARY_SENTINEL_GAME_ID}
            for stale in [gid for gid in self._last_nonce if gid not in keep]:
                self._last_nonce.pop(stale, None)
            for stale in [gid for gid in self._last_state_value if gid not in keep]:
                self._last_state_value.pop(stale, None)

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
        """Re-evaluate every intervention flag for every live session (#838).

        Public for tests and the change handler. Refreshes the rate-limit budget
        first (policy flag may have changed), then evaluates each live session
        independently: per session it walks the registry in order, evaluating
        every flag with that session's ``{gameId, targetingKey}`` context and
        applying/publishing on that session.

        Un-targeted flags resolve to their default for shadow games (no-op) and
        to the configured value for the primary, so a flag with no ``gameId``
        targeting rule behaves exactly as before — touching only the primary.
        """
        self._refresh_budget()
        sessions = self._live_sessions()
        self._prune_stale_buckets({s.game_id for s in sessions})
        for session in sessions:
            for spec in INTERVENTION_SPECS:
                try:
                    await self._evaluate_one(spec, session)
                except Exception as e:  # one bad flag must not stop the rest
                    logger.error(
                        f"InterventionManager: error evaluating {spec.flag_key} "
                        f"for game {session.game_id or '<primary>'}: {e}"
                    )

    async def _evaluate_one(self, spec: InterventionSpec, session: "SessionView") -> None:
        # Player-targeted state flags (player_sensitivity_factor, shield_seconds)
        # are driven entirely through flagd's per-serial targeting if-ladder; the
        # agent writer leaves the flag's global defaultVariant on its neutral
        # value, so the global evaluated read NEVER changes and can't be used for
        # change detection. Resolve the per-serial values instead and key change
        # detection on them (handled separately below).
        if spec.player_targeted and not spec.edge_triggered:
            await self._evaluate_targeted_state(spec, session)
            return

        raw = self._read_flag(spec, session)

        if spec.edge_triggered:
            nonce, payload = _split_nonce(str(raw))
            # A missing nonce is always a no-op. A payload-carrying flag with an
            # empty payload is also a no-op. ``end_game`` (payload_required=False)
            # fires on the nonce alone. The baseline is still advanced so a later
            # real trigger with a new nonce fires.
            no_nonce = nonce.strip().lower() in _EDGE_EMPTY_VALUES
            empty_payload = payload.strip().lower() in _EDGE_EMPTY_VALUES
            is_noop = no_nonce or (spec.payload_required and empty_payload)
            with self._lock:
                bucket = self._nonce_bucket(session.game_id)
                last = bucket.get(spec.flag_key)
                changed = nonce != last
                bucket[spec.flag_key] = nonce
            if not changed or is_noop:
                return
            target = _edge_target_serial(spec, payload)
            value = raw
        else:
            # State-shaped: only act on value change away from the none-value.
            with self._lock:
                bucket = self._state_bucket(session.game_id)
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
                revert = self._revert_handlers.get(spec.flag_key)
                if was_active and revert is not None:
                    ctx = InterventionContext(
                        spec=spec,
                        value=raw,
                        payload="",
                        target_serial=None,
                        game=session.game,
                        objective=self._dominant_objective(),
                    )
                    try:
                        await revert(ctx)
                    except Exception as e:
                        logger.error(f"InterventionManager: revert handler for {spec.flag_key} raised: {e}")
                return
            payload = ""
            target = self._state_target_serial(spec)
            value = raw

        await self._enforce_and_dispatch(spec, value, payload, target, session)

    async def _evaluate_targeted_state(self, spec: InterventionSpec, session: "SessionView") -> None:
        """Change-detect and dispatch a player-targeted state flag.

        The agent writer drives these flags via a per-serial targeting if-ladder
        and never moves the global ``defaultVariant`` off neutral, so a global
        read is useless for change detection. Instead resolve the flag per active
        serial and key change detection on that resolved map. The handler (PR C/D)
        re-resolves and applies; dispatch is idempotent so re-applying an
        unchanged map would be harmless, but we still gate on change to avoid
        spurious metrics/events and needless rate-limit consumption.

        With no live game there are no serials to target, so this is a no-op
        (and the baseline is reset to empty so a later game picks up the flag).
        """
        game = session.game
        if game is None:
            with self._lock:
                self._state_bucket(session.game_id)[spec.flag_key] = None
            return

        default = float(spec.none_value) if isinstance(spec.none_value, (int, float)) else 1.0
        resolved = self.resolve_player_targets(
            spec.flag_key,
            default,
            game,
            value_kind=spec.value_kind if spec.value_kind in ("int", "float") else "float",
            battery_gate=True,
            game_id=session.game_id,
        )

        # Change key: the set of (serial, value) pairs that differ from neutral.
        # Reverting all serials to neutral collapses to the empty key, matching a
        # never-applied baseline so a fresh revert does not spuriously re-fire.
        active = {s: v for s, v in resolved.items() if not _is_none_value(spec, v)}
        change_key = tuple(sorted(active.items()))

        with self._lock:
            bucket = self._state_bucket(session.game_id)
            last = bucket.get(spec.flag_key)
            changed = change_key != last
            bucket[spec.flag_key] = change_key
        if not changed:
            return
        if not active:
            # All serials reverted to neutral: nothing to apply (shields/factors
            # simply expire / reset to default). No enforced intervention.
            return

        # Per-serial battery gating already happened in resolve_player_targets;
        # the chain-level target is None (fan-out), so the chain's single-target
        # battery check is a no-op here.
        await self._enforce_and_dispatch(spec, default, "", None, session)

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
        )
        handler = self._handlers.get(spec.flag_key, self._noop_handler)
        try:
            await handler(ctx)
        except Exception as e:
            logger.error(f"InterventionManager: handler for {spec.flag_key} raised: {e}")
            await self._record(spec, target, objective, session, blocked=True, block_reason="handler_error")
            return

        await self._record(spec, target, objective, session, blocked=False, block_reason="")

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

        # (b) weighted rate limit — reserves budget; do last so the reservation
        # only happens for otherwise-allowed interventions.
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
        default: float,
        game: object,
        *,
        value_kind: str = "float",
        battery_gate: bool = True,
        game_id: str = "",
    ) -> dict[str, float]:
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
            value_kind: ``"float"`` or ``"int"`` (flag value type).
            battery_gate: If True, drop serials below the battery threshold.
            game_id: Owning session's game id, added as ``gameId`` to the context
                (#838). Empty string adds no ``gameId`` (legacy primary path).

        Returns:
            ``{serial: value}`` for every active serial that should be acted on.
            Battery-gated serials are omitted entirely.
        """
        result: dict[str, float] = {}
        client = self._interventions_client
        serials = _active_player_serials(game)
        threshold = self._battery_threshold() if battery_gate else None

        for serial in serials:
            if threshold is not None:
                pct = self._battery_pct(serial)
                if pct is not None and pct < threshold:
                    continue  # battery guard: skip low-battery controllers

            if client is None:
                value: float = default
            else:
                ctx = self._session_context(game_id, targeting_key=serial)
                try:
                    if value_kind == "int":
                        value = float(client.get_integer_value(flag_key, int(default), ctx))
                    else:
                        value = float(client.get_float_value(flag_key, float(default), ctx))
                except Exception as e:  # one bad serial must not drop the rest
                    logger.debug(f"InterventionManager: per-serial read failed for {serial}: {e}")
                    value = default
            result[serial] = value

        return result

    def _allowed_types(self) -> set[str]:
        if self._agent_client is None:
            return set()
        try:
            value = self._agent_client.get_object_value("interventions_allowed", [], EvaluationContext())
            if isinstance(value, list):
                return set(value)
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
        budget = 2.0
        if self._agent_client is not None:
            try:
                budget = float(
                    self._agent_client.get_integer_value("policy.max_interventions_per_minute", 2, EvaluationContext())
                )
            except Exception:
                budget = 2.0
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
