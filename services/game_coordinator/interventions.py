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
# where it interacts with per-role threshold overrides.
MODE_CAPABILITY_DENY: dict[str, frozenset[str]] = {
    # Hidden-role modes: LED effects leak roles; revive breaks hidden roles.
    "Werewolf": frozenset({"send_controller_effect", "revive_player", "adjust_global_sensitivity"}),
    "Traitor": frozenset({"send_controller_effect", "revive_player", "adjust_global_sensitivity"}),
    # Bracket / queue modes: revive breaks the bracket/queue.
    "Tournament": frozenset({"revive_player"}),
    "Fight Club": frozenset({"revive_player"}),
    # Permanent-elimination modes: revive is opt-in only (off by default in M2).
    "FFA": frozenset({"revive_player"}),
    "Teams": frozenset({"revive_player"}),
    "Random Teams": frozenset({"revive_player"}),
    # Zombie role thresholds are asymmetric; global override interacts.
    "Zombie": frozenset({"adjust_global_sensitivity"}),
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
        get_game: Callable[[], object | None],
        battery_provider: Callable[[str], float | None] | None = None,
        time_fn: Callable[[], float] = time.monotonic,
    ):
        """
        Args:
            event_publisher: Async ``publish(event_type, data)`` (EventBus).
            get_game: Returns the live BaseGameMode instance or None.
            battery_provider: ``(serial) -> battery_pct | None``. None means the
                battery is unknown and the guard does not block. Injected for
                testability (the real source is the controller manager's
                ``controller_battery_pct`` metric).
            time_fn: Monotonic clock (injectable for tests).
        """
        self._publish = event_publisher
        self._get_game = get_game
        self._battery_provider = battery_provider
        self._time_fn = time_fn

        self._interventions_client = None  # interventions domain
        self._agent_client = None  # agent domain (backstop reads)

        # Per-flag last-applied nonce (edge-triggered exactly-once).
        self._last_nonce: dict[str, str] = {}
        # Per-flag last-applied state value (state-shaped change detection).
        self._last_state_value: dict[str, object] = {}

        self._rate_limiter = _RateLimiter(budget=2.0, time_fn=time_fn)

        # Handler registry: flag_key -> Handler. Defaults to the no-op stub for
        # every spec. PRs C/D/E replace entries via register_handler().
        self._handlers: dict[str, Handler] = {spec.flag_key: self._noop_handler for spec in INTERVENTION_SPECS}

        self._lock = threading.RLock()
        self._started = False

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
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Initialize flag clients, register change handler, initial evaluate."""
        if self._started:
            return
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
        as brand-new triggers.
        """
        for spec in INTERVENTION_SPECS:
            try:
                raw = self._read_flag(spec)
            except Exception:
                continue
            with self._lock:
                if spec.edge_triggered:
                    nonce, _ = _split_nonce(str(raw))
                    self._last_nonce[spec.flag_key] = nonce
                else:
                    self._last_state_value[spec.flag_key] = raw

    # ------------------------------------------------------------------ #
    # Flag change handling
    # ------------------------------------------------------------------ #
    def _on_flags_changed(self, _event_details) -> None:
        """Event handler: re-evaluate all intervention flags.

        Synchronous (OpenFeature handler), so it schedules the async evaluation
        on the running loop. Falls back to a fresh loop if none is running
        (e.g. unit-test direct invocation handled via ``evaluate_all``).
        """
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            loop.create_task(self.evaluate_all())
        else:
            asyncio.run(self.evaluate_all())

    async def evaluate_all(self) -> None:
        """Re-evaluate every intervention flag and apply survivors.

        Public for tests and the change handler. Refreshes the rate-limit budget
        first (policy flag may have changed), then walks the registry in order.
        """
        self._refresh_budget()
        for spec in INTERVENTION_SPECS:
            try:
                await self._evaluate_one(spec)
            except Exception as e:  # one bad flag must not stop the rest
                logger.error(f"InterventionManager: error evaluating {spec.flag_key}: {e}")

    async def _evaluate_one(self, spec: InterventionSpec) -> None:
        raw = self._read_flag(spec)

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
                last = self._last_nonce.get(spec.flag_key)
                changed = nonce != last
                self._last_nonce[spec.flag_key] = nonce
            if not changed or is_noop:
                return
            target = _edge_target_serial(spec, payload)
            value = raw
        else:
            # State-shaped: only act on value change away from the none-value.
            with self._lock:
                last = self._last_state_value.get(spec.flag_key)
                changed = raw != last
                self._last_state_value[spec.flag_key] = raw
            if not changed:
                return
            if _is_none_value(spec, raw):
                return  # reverting to neutral is not an intervention to dispatch
            payload = ""
            target = self._state_target_serial(spec)
            value = raw

        await self._enforce_and_dispatch(spec, value, payload, target)

    # ------------------------------------------------------------------ #
    # Enforcement chain
    # ------------------------------------------------------------------ #
    async def _enforce_and_dispatch(
        self, spec: InterventionSpec, value: object, payload: str, target: str | None
    ) -> None:
        objective = self._dominant_objective()
        game = self._get_game()

        block_reason = self._check_chain(spec, target, game)
        if block_reason is not None:
            await self._record(spec, target, objective, blocked=True, block_reason=block_reason)
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
            await self._record(spec, target, objective, blocked=True, block_reason="handler_error")
            return

        await self._record(spec, target, objective, blocked=False, block_reason="")

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
        self, spec: InterventionSpec, target: str | None, objective: str, *, blocked: bool, block_reason: str
    ) -> None:
        try:
            from services.game_coordinator import metrics

            metrics.interventions_total.labels(
                type=spec.type_id,
                objective=objective,
                blocked=str(blocked).lower(),
            ).inc()
        except Exception as e:
            logger.debug(f"InterventionManager: metric record failed: {e}")

        from lib.types import GameEvent

        await self._publish(
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
    def _read_flag(self, spec: InterventionSpec) -> object:
        client = self._interventions_client
        if client is None:
            return _spec_default(spec)
        ctx = EvaluationContext()
        if spec.value_kind == "int":
            return client.get_integer_value(spec.flag_key, spec.none_value, ctx)
        if spec.value_kind == "float":
            return client.get_float_value(spec.flag_key, float(spec.none_value), ctx)
        return client.get_string_value(spec.flag_key, "", ctx)

    def _state_target_serial(self, _spec: InterventionSpec) -> str | None:
        """Per-player state flags target via flagd targeting (serial), which is
        not resolvable from a global read in PR A. Returns None (no battery gate)
        until per-serial evaluation lands in PR D."""
        return None

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
    return ""


def _game_mode_name(game: object) -> str:
    """Best-effort mode name from a live game via get_game_name()."""
    getter = getattr(game, "get_game_name", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            return ""
    return ""
