"""ShieldMixin: the Free-for-All shield code of BaseGameMode (#2360).

Agent shields (``grant_shield``, the per-player limits), the shield feedback
(LED and rumble layers, sounds), trigger charges (UC-040 A30 to A32) and the hold
pool (UC-030 A7, UC-040 A36 to A39) moved here unchanged from ``games/base.py``;
``BaseGameMode`` inherits them, so every call site and test still goes through
the game object. The state they use is still set up in ``BaseGameMode.__init__``.

Import order: ``games/base.py`` imports this module after its module-level
constants and just before ``class BaseGameMode``, and this module imports those
constants back from it. Import ``BaseGameMode`` from ``games/base.py``; do not
import this module on its own first.

Module globals: the logger, the tracer and the constants are base.py's own
objects, so log records, the span instrumentation scope and the metric labels
are unchanged. The flag helpers ``read_float_flag``, ``read_int_flag``,
``read_string_flag`` and ``HOLD_ENERGY_KEYFRAME_SECONDS`` are looked up on the
base module at call time (``_base.``), so a test that patches them on
``games.base`` still reaches this code.
"""

import asyncio
import contextlib
import math
from typing import TYPE_CHECKING

from opentelemetry import trace

from lib.colors import Colors
from lib.feature_flags import GAME_KIND_SHADOW
from lib.telemetry import inject_trace_context
from lib.types import GameEvent, Sound
from services.game_coordinator import game_clock, metrics
from services.game_coordinator.games import base as _base
from services.game_coordinator.games.base import (
    _HOLD_CUE_PRIORITY,
    CHARGE_OUTCOME_ACTIVATED,
    CHARGE_OUTCOME_EXPIRED,
    CHARGE_OUTCOME_GAME_END,
    DEFAULT_CHARGE_EXPIRY_SECONDS,
    DEFAULT_MAX_SHIELDS_PER_PLAYER_PER_GAME,
    DEFAULT_SHIELD_ACTIVATION,
    DEFAULT_SHIELD_COOLDOWN_SECONDS,
    DEFAULT_SHIELD_END_GRACE_SECONDS,
    HOLD_CUE_EMPTY,
    HOLD_CUE_FULL,
    HOLD_CUE_NONE,
    HOLD_CUE_TOP_UP,
    HOLD_OUTCOME_BUDGET_EXHAUSTED,
    HOLD_OUTCOME_EMPTY,
    HOLD_OUTCOME_GAME_END,
    HOLD_OUTCOME_RELEASED,
    SHIELD_ACTIVATION_HOLD,
    SHIELD_ACTIVATION_INSTANT,
    SHIELD_ACTIVATION_TRIGGER,
    SHIELD_CHARGE_LAYER_MAX_SECONDS,
    SHIELD_CHARGE_RESEND_MARGIN_SECONDS,
    SHIELD_HOLD_CUE_FLAGS,
    SHIELD_HOLD_FLAGS,
    SHIELD_HOLD_POOL_FULL_ROOM_SECONDS,
    SHIELD_HOLD_SOUND_MIN_INTERVAL_SECONDS,
    SHIELD_REFUSED_BUDGET_EXHAUSTED,
    SHIELD_REFUSED_CAP,
    SHIELD_REFUSED_COOLDOWN,
    SHIELD_REFUSED_POOL_FULL,
    ShieldHoldCues,
    ShieldHoldParams,
    logger,
    player_identity_attributes,
    resolve_charge_expiry,
    resolve_end_grace,
    resolve_hold_value,
    resolve_shield_activation,
    resolve_shield_limit,
    tracer,
)
from services.game_coordinator.stream_writes import serialized_write

if TYPE_CHECKING:
    from services.game_coordinator.games.base import Player

# The "helped" cue of an agent shield (UC-040 A42, BR-031): a short magenta
# GAME_EFFECT_PULSE (the agent colour of #818) on the helped controller, sent before the
# shield's own LED command. The controller manager plays an overlay effect over every
# shield layer, so the layer shows once the pulse ends.
SHIELD_HELP_CUE_COLOR = Colors.Magenta.value
SHIELD_HELP_CUE_DURATION_MS = 500
SHIELD_HELP_CUE_SPEED = 4  # pulses per second: two pulses in 500 ms

# A grant_shield that lands once the game is ending (outcome latched, force-ended, or
# state ENDING / ENDED) is refused (UC-040 A42 step 3): no shield layer and no cue may
# appear over the winner rainbow (a PULSE takes over every effect but IDENTIFY and
# SHOW_BATTERY in the controller manager).
SHIELD_REFUSED_GAME_ENDING = "game_ending"

# player_death carries `shield.since_end_ms` only when the player's last shield ended
# less than this long ago (telemetry.md §4, UC-040 BR-035): the stage fairness check.
SHIELD_SINCE_END_REPORT_SECONDS = 3.0


class ShieldMixin:
    """Shield behaviour of every game mode, mixed into ``BaseGameMode`` (#2360)."""

    async def grant_shield(self, serial: str, seconds: float) -> bool:
        """
        Grant a temporary shield (invulnerability) to a player (#730, N3).

        A shield extends the player's ``grace_until`` timestamp: the per-frame
        check in ``_process_controller_state`` already skips all death/warning
        checks while ``game_clock.now() < grace_until``, so this is the natural,
        mode-agnostic shield primitive (Tournament/Fight Club ``invincible_until``
        and Nonstop ``spawn_protected`` are the same idea). FFA-first per
        docs/research/722-intervention-surface.md §3, but the primitive lives on
        ``BaseGameMode`` so every mode inherits it. Mode-level opt-outs (e.g.
        hidden-role LED leaks) are enforced by the intervention manager's §9
        capability matrix BEFORE the handler runs — this primitive does not
        re-check mode.

        Idempotent-extend semantics: a new shield only ever extends an existing
        one. If the player is already shielded past ``now + seconds`` the existing
        (longer) grace is kept; a shorter request never shortens it.

        Shield limits (#2044, UC-040 A34, BR-022): after the alive check (and in
        trigger mode after ``already_charged``) a grant is refused with
        ``InterventionNotApplied`` once the player has had
        ``policy.max_shields_per_player_per_game`` agent shields in this game
        (``shield_cap_reached``), or while ``now`` is before the end of the last
        agent shield plus ``policy.shield_cooldown_seconds`` (``shield_cooldown``).
        So with a cooldown set, a running agent shield is never extended; cooldown
        0 restores extend-not-shorten. A kept (no-op) grant does not count.

        Every caller is an agent grant (``handle_shield_seconds``, the
        ``shield_seconds`` intervention), so an applied grant first pulses the
        helped controller magenta (#2395, UC-040 A42, BR-031) in all three modes;
        a refused or kept grant sends no cue. Once the game is ending (outcome
        latched, force-ended, ENDING / ENDED) a grant is refused ``game_ending``.

        Shield feedback (#1301, UC-040 A16): the ``shield_up`` sound, a
        ``GAME_EFFECT_SHIELD`` for the remaining protection (rendered by the
        controller manager: fade, last-second blink, restore) and a
        ``player_shielded`` event. A kept (no-op) grant sends no feedback.

        Args:
            serial: Controller serial number.
            seconds: Shield duration in seconds. Non-positive durations are a
                no-op (returns False).

        Returns:
            True if a shield was granted/extended, False if the player is
            unknown/dead or the duration was non-positive.
        """
        if seconds <= 0:
            return False

        player = self.players.get(serial)
        if not player or not player.alive:
            return False

        if self._shield_grants_closed():
            # UC-040 A42 step 3: after the win (or a force-end) nothing is granted,
            # so no layer and no cue cover the winner effect.
            from services.game_coordinator.intervention_errors import InterventionNotApplied

            logger.info(f"Shield for {serial} refused: the game is ending")
            raise InterventionNotApplied(SHIELD_REFUSED_GAME_ENDING)

        # UC-040 BR-027: a hold game adds the grant to the player's energy (A36).
        latched_here = not self._shield_hold_latched
        hold = self._latch_shield_hold()
        if hold is not None:
            granted = self._grant_shield_energy(player, seconds, hold)
            # UC-040 BR-031: the helped cue; it takes the stream lock before the
            # queued SHIELD_ENERGY / SHIELD_HOLD write of this grant.
            await self._send_shield_help_cue(player)
            return granted

        # UC-040 BR-019: in trigger mode the grant is a charge the player activates
        # (A30). Read per grant, so a flip applies to the next grant only; the grant
        # that latched the game reuses the latch's read.
        mode, self._shield_mode_at_latch = self._shield_mode_at_latch, None
        if mode is None or not latched_here:
            mode = self._shield_activation_mode()
        trigger = mode == SHIELD_ACTIVATION_TRIGGER
        if trigger and player.shield_charge_seconds > 0:
            # UC-040 A31: a held charge is refused before the shield limits.
            from services.game_coordinator.intervention_errors import InterventionNotApplied

            logger.info(f"Shield charge for {serial} refused: a charge is already held")
            raise InterventionNotApplied("already_charged")

        now = game_clock.now()
        self._check_shield_limits(player, now)

        if trigger:
            granted = await self._grant_shield_charge(player, seconds)
            # BR-022: in trigger mode the cooldown runs from the grant (the charge
            # stored), not from the activation.
            self._record_agent_shield(serial, now, seconds)
            return granted

        new_grace = now + seconds
        # Extend-not-shorten: the agent only ever writes its own
        # grace_until field — never invincible_until (the admin baseline). If the
        # player's effective grace (max of agent shield + admin invincibility) is
        # already longer than the request, this shield is a no-op extension: it
        # adds nothing, and crucially does NOT shorten an active admin baseline.
        if new_grace <= self.effective_grace_until(player):
            logger.info(
                f"Shield for {serial}: existing effective grace "
                f"({self.effective_grace_until(player):.2f}) already exceeds "
                f"requested ({new_grace:.2f}); kept"
            )
            return True

        # Sound only on the unprotected -> protected transition (UC-040
        # BR-010); an extension restarts the LED fade silently.
        was_protected = self._shield_protected(player, game_clock.now())
        player.grace_until = new_grace
        self._arm_shield_end_grace(player, new_grace)  # UC-040 BR-035: the end is known now
        self._record_agent_shield(serial, now, seconds)
        logger.info(f"Shield granted to {serial} for {seconds:.1f}s (grace_until={new_grace:.2f})")

        if player.span:
            player.span.add_event(
                "shield_granted",
                attributes={
                    "shield_seconds": seconds,
                    "shield.end_grace_s": self._shield_end_grace,
                    **player_identity_attributes(serial, player.name),
                },
            )

        # The helped cue (UC-040 BR-031), then the shield's own feedback (UC-040 A16).
        await self._send_shield_help_cue(player)
        await self._send_shield_feedback(player, new_grace - game_clock.now(), kind="full", sound=not was_protected)

        return True

    async def _send_shield_help_cue(self, player: "Player") -> None:
        """Pulse the helped controller magenta before the shield colour (#2395, UC-040 A42, BR-031).

        Called only by an applied ``grant_shield`` (the agent's grant), after the
        protection, charge or energy is in place and before the shield's LED command.
        Best-effort: a failed write is logged and never fails the grant. Never sent
        once the game is ending (A42 step 3): ``grant_shield`` refuses such a grant
        already; this re-check covers a latch that lands while the grant runs.
        """
        if not self.gameplay_stream or self._shield_grants_closed():
            return
        try:
            from proto import controller_manager_pb2

            red, green, blue = SHIELD_HELP_CUE_COLOR
            trace_parent, trace_state = inject_trace_context(player.span)
            await serialized_write(
                self.gameplay_stream,
                controller_manager_pb2.GameplayStreamControl(
                    game_effect=controller_manager_pb2.GameEffectCommand(
                        serial=player.serial,
                        effect=controller_manager_pb2.GAME_EFFECT_PULSE,
                        color=controller_manager_pb2.RGB(r=red, g=green, b=blue),
                        duration_ms=SHIELD_HELP_CUE_DURATION_MS,
                        speed=SHIELD_HELP_CUE_SPEED,
                        trace_parent=trace_parent,
                        trace_state=trace_state,
                    )
                ),
            )
        except Exception as e:
            logger.warning(f"Shield help cue for {player.serial} failed (shield unaffected): {e}")

    def _shield_grants_closed(self) -> bool:
        """True once the game is ending: outcome latched, force-ended, or ENDING / ENDED (UC-040 A42 step 3)."""
        return (
            self._latched_outcome is not None
            or self.force_ended
            or self.state in (_base.GameState.ENDING, _base.GameState.ENDED)
        )

    def _shield_limits(self) -> tuple[int, int]:
        """``(max shields per player per game, cooldown seconds)`` for the next grant (UC-040 BR-022).

        Operator flags in the ``agent`` set, read per grant with the game's context
        (flags.md §3). 0 means unlimited / off. A read error or a negative value
        gives the default (3 / 15 s), so the limits stay on.
        """
        # Literal flag keys, so the contract scan resolves the reads.
        max_shields = _base.read_int_flag(
            "agent",
            "policy.max_shields_per_player_per_game",
            DEFAULT_MAX_SHIELDS_PER_PLAYER_PER_GAME,
            game_id=self.game_id,
        )
        cooldown = _base.read_int_flag(
            "agent", "policy.shield_cooldown_seconds", DEFAULT_SHIELD_COOLDOWN_SECONDS, game_id=self.game_id
        )
        return (
            resolve_shield_limit(max_shields, DEFAULT_MAX_SHIELDS_PER_PLAYER_PER_GAME),
            resolve_shield_limit(cooldown, DEFAULT_SHIELD_COOLDOWN_SECONDS),
        )

    def _check_shield_limits(self, player: "Player", now: float) -> None:
        """Raise ``InterventionNotApplied`` when the cap or the cooldown refuses a grant (UC-040 A34)."""
        max_shields, cooldown = self._shield_limits()
        used = self._agent_shield_grants.get(player.serial, 0)
        if max_shields > 0 and used >= max_shields:
            self._refuse_shield(player, SHIELD_REFUSED_CAP, used, 0.0)
        last_end = self._agent_shield_last_end.get(player.serial)
        if cooldown > 0 and last_end is not None and now < last_end + cooldown:
            self._refuse_shield(player, SHIELD_REFUSED_COOLDOWN, used, last_end + cooldown - now)

    def _refuse_shield(self, player: "Player", reason: str, used: int, cooldown_remaining: float) -> None:
        """Log, record ``shield_refused`` on the player's span and raise (UC-040 A34)."""
        from services.game_coordinator.intervention_errors import InterventionNotApplied

        logger.info(
            f"Shield for {player.serial} refused: {reason} "
            f"(grants used {used}, cooldown remaining {cooldown_remaining:.1f}s)"
        )
        if player.span:
            with contextlib.suppress(Exception):
                player.span.add_event(
                    "shield_refused",
                    attributes={
                        "shield.refusal_reason": reason,
                        "shield.grants_used": used,
                        "shield.cooldown_remaining_s": round(cooldown_remaining, 3),
                        **player_identity_attributes(player.serial, player.name),
                    },
                )
        raise InterventionNotApplied(reason)

    def _record_agent_shield(self, serial: str, now: float, seconds: float) -> None:
        """Count an applied agent shield and remember its own end (UC-040 BR-022; never ``grace_until``)."""
        self._agent_shield_grants[serial] = self._agent_shield_grants.get(serial, 0) + 1
        self._agent_shield_last_end[serial] = now + seconds

    def _shield_protected(self, player: "Player", now: float) -> bool:
        """Whether any shield protection (grace / invincibility / toughness boost) is active."""
        return now < self.effective_grace_until(player) or now < player.toughness_boost_until

    async def _send_shield_feedback(
        self,
        player: "Player",
        remaining_s: float,
        *,
        kind: str,
        sound: bool = True,
        charge_delay_ms: int | None = None,
    ) -> None:
        """Shield LED effect + shield sound + ``player_shielded`` event (#1301).

        UC-040 A16 / BR-008..BR-010. One command per grant: the controller manager
        renders the fade / last-second blink / restore, so nothing is streamed and
        no task is kept here. ``sound`` is False for an extension of an already
        protected player (the LED restarts silently). Each channel is tried on
        its own, so one failing channel never skips the others; feedback never
        fails the intervention.

        ``charge_delay_ms`` marks the activation of a shield charge (UC-030 A6,
        #1837): the swell rumble follows the shield LED, and the span event and
        ``player_shielded`` carry ``activation=trigger`` and the grant-to-use delay.
        """
        if remaining_s <= 0:
            return
        serial = player.serial
        duration_ms = int(round(remaining_s * 1000))
        until = game_clock.now() + remaining_s
        # Keep the latest end of any running shield: a shorter toughness boost after
        # a longer full one must not hide the full one from _end_shield_feedback.
        self._shield_feedback_until[serial] = max(self._shield_feedback_until.get(serial, 0.0), until)
        activated = charge_delay_ms is not None
        if player.span:
            with contextlib.suppress(Exception):
                attributes = {
                    "shield.kind": kind,
                    "shield.duration_ms": duration_ms,
                    "shield.sound": Sound.SFX_SHIELD_UP.value if sound else "none",
                }
                if activated:
                    attributes["shield.activation"] = SHIELD_ACTIVATION_TRIGGER
                player.span.add_event("shield_feedback", attributes=attributes)
        if self.gameplay_stream:
            try:
                from proto import controller_manager_pb2

                trace_parent, trace_state = inject_trace_context(player.span)
                await serialized_write(
                    self.gameplay_stream,
                    controller_manager_pb2.GameplayStreamControl(
                        game_effect=controller_manager_pb2.GameEffectCommand(
                            serial=serial,
                            effect=(
                                controller_manager_pb2.GAME_EFFECT_TOUGHNESS_BOOST
                                if kind == "toughness_boost"
                                else controller_manager_pb2.GAME_EFFECT_SHIELD
                            ),
                            duration_ms=duration_ms,
                            trace_parent=trace_parent,
                            trace_state=trace_state,
                        )
                    ),
                )
            except Exception as e:
                logger.warning(f"Shield LED feedback for {serial} failed (protection unaffected): {e}")
        if activated:
            # UC-030 A6 step 2 / BR-021: the activation swell (rumble only) right
            # after the shield LED, before the slower sound RPC.
            await self._write_shield_effect(player, "GAME_EFFECT_SHIELD_SWELL", 0)
        if sound:
            try:
                await self._play_sound(
                    Sound.SFX_SHIELD_UP, priority=2, volume=0.5 if kind == "toughness_boost" else 1.0
                )
            except Exception as e:
                logger.warning(f"Shield sound for {serial} failed (protection unaffected): {e}")
        try:
            await self.event_publisher(
                GameEvent.PLAYER_SHIELDED,
                {
                    "serial": serial,
                    "kind": kind,
                    "seconds": f"{remaining_s:.1f}",
                    "until_ms": str(int(game_clock.to_wall(until) * 1000)),  # wall clock: the browser compares it
                    **(
                        {"activation": SHIELD_ACTIVATION_TRIGGER, "charge_delay_ms": str(charge_delay_ms)}
                        if activated
                        else {}
                    ),
                },
            )
        except Exception as e:
            logger.warning(f"player_shielded event for {serial} failed (protection unaffected): {e}")

    async def _end_shield_feedback(self) -> None:
        """End every shield effect still running before the end-of-game effects (#1301, UC-040 A17).

        A zero-duration SHIELD ends only a running shield on the controller (the
        controller manager never cancels another effect for it) and restores the
        base colour. Best-effort; clears the record either way.

        A held shield charge ends here too, with ``game_end`` (UC-040 A32, #1837).
        """
        for player in self.players.values():
            if player.shield_charge_seconds > 0:
                await self._discard_shield_charge(player, CHARGE_OUTCOME_GAME_END)
        now = game_clock.now()
        # UC-040 A39: a hold game drops every pool; an open activation ends game_end.
        if self._shield_hold is not None:
            for player in self.players.values():
                self._drop_shield_hold(player, HOLD_OUTCOME_GAME_END, now)
        # UC-040 BR-026: no hold command after this point; SHIELD 0 below ends the hold layers too.
        hold_serials = await self._stop_hold_feedback()
        running = [serial for serial, until in self._shield_feedback_until.items() if until > now]
        running += [serial for serial in hold_serials if serial not in running]
        self._shield_feedback_until.clear()
        if not running or not self.gameplay_stream:
            return
        try:
            from proto import controller_manager_pb2

            for serial in running:
                await serialized_write(
                    self.gameplay_stream,
                    controller_manager_pb2.GameplayStreamControl(
                        game_effect=controller_manager_pb2.GameEffectCommand(
                            serial=serial,
                            effect=controller_manager_pb2.GAME_EFFECT_SHIELD,
                            duration_ms=0,
                        )
                    ),
                )
        except Exception as e:
            logger.warning(f"Ending shield feedback failed: {e}")

    # ------------------------------------------------------------------
    # Shield charge (FR-160; UC-040 A30-A32, BR-019-BR-021; UC-030 A6)
    # ------------------------------------------------------------------

    def _shield_mode_flag_applies(self) -> bool:
        """UC-040 BR-019: the allow-listed mode, in a real game or a shadow game whose pattern presses the trigger."""
        if not self.SHIELD_TRIGGER_ALLOWED:
            return False
        return self.game_kind != GAME_KIND_SHADOW or self.pattern_trigger_policy is True

    def _shield_activation_mode(self) -> str:
        """``instant``, ``trigger`` or ``hold`` for the next grant_shield (UC-040 BR-019, BR-027).

        Only an allow-listed mode (``SHIELD_TRIGGER_ALLOWED``, Free-for-All) in a
        real game, or in a shadow game whose archetype presses the trigger
        (``pattern_trigger_policy``, UC-046 BR-018), reads the flag; every other
        game is instant. A game
        latched to hold stays hold; otherwise the flag is read per grant, never per
        frame, and a ``hold`` read in a game not latched to hold gives the mode the
        game latched at its start (``instant`` or ``trigger``).
        """
        if self._shield_hold is not None:
            return SHIELD_ACTIVATION_HOLD
        if not self._shield_mode_flag_applies():
            return SHIELD_ACTIVATION_INSTANT
        # Literal flag key, so the contract scan resolves the read.
        value = _base.read_string_flag("game", "shield.activation", DEFAULT_SHIELD_ACTIVATION, game_id=self.game_id)
        mode = resolve_shield_activation(value)
        if mode == SHIELD_ACTIVATION_HOLD and self._shield_hold_latched:
            # BR-027: a game not latched to hold keeps the mode it latched at its start.
            return self._shield_mode_at_start
        return mode

    def _read_charge_expiry(self) -> float:
        """Seconds a new charge is held before it expires, 0 = never (flags.md §4, read per grant)."""
        value = _base.read_float_flag(
            "game", "shield.charge_expiry_seconds", DEFAULT_CHARGE_EXPIRY_SECONDS, game_id=self.game_id
        )
        return resolve_charge_expiry(value)

    async def _write_shield_effect(
        self, player: "Player", effect_name: str, duration_ms: int, shield_hold=None
    ) -> None:
        """Best-effort ``GameEffectCommand`` for a shield-charge (or hold, #2096) effect on the player's controller."""
        if not self.gameplay_stream:
            return
        try:
            from proto import controller_manager_pb2

            trace_parent, trace_state = inject_trace_context(player.span)
            await serialized_write(
                self.gameplay_stream,
                controller_manager_pb2.GameplayStreamControl(
                    game_effect=controller_manager_pb2.GameEffectCommand(
                        serial=player.serial,
                        effect=getattr(controller_manager_pb2, effect_name),
                        duration_ms=duration_ms,
                        trace_parent=trace_parent,
                        trace_state=trace_state,
                        shield_hold=shield_hold,
                    )
                ),
            )
        except Exception as e:
            logger.warning(f"{effect_name} for {player.serial} failed (charge state unaffected): {e}")

    async def _grant_shield_charge(self, player: "Player", seconds: float) -> bool:
        """Store one shield charge for the player (UC-040 A30, BR-020).

        ``grace_until`` is left alone: the player is not protected until they
        activate the charge with the trigger (UC-030 A6). A grant while a charge
        is held is refused by ``grant_shield`` before this runs (``already_charged``,
        A31); the intervention manager records it as blocked and refunds the
        backstop reservation.
        """
        now = game_clock.now()
        expiry = self._read_charge_expiry()
        player.shield_charge_seconds = seconds
        player.shield_charge_granted_at = now
        player.shield_charge_expires_at = now + expiry if expiry > 0 else 0.0
        identity = player_identity_attributes(player.serial, player.name)
        logger.info(f"Shield charge granted to {player.serial}: {seconds:.1f}s, expiry {expiry:.0f}s (0 = never)")

        if player.span:
            with contextlib.suppress(Exception):
                player.span.add_event(
                    "shield_charge_granted",
                    attributes={"shield.seconds": seconds, "shield.charge_expiry_s": expiry, **identity},
                )
            with contextlib.suppress(Exception):
                player._shield_charge_span = tracer.start_span(
                    "game.shield_charge",
                    context=trace.set_span_in_context(player.span),
                    attributes={
                        **identity,
                        "game.id": self.game_id,
                        "shield.seconds": seconds,
                        "shield.charge_expiry_s": expiry,
                    },
                )

        await self._send_shield_help_cue(player)  # UC-040 BR-031: before the charge layer
        await self._send_shield_charge_layer(player, now)
        try:
            await self.event_publisher(
                GameEvent.PLAYER_SHIELD_CHARGED,
                {
                    "serial": player.serial,
                    "seconds": f"{seconds:.1f}",
                    "expires_ms": str(
                        int(game_clock.to_wall(player.shield_charge_expires_at) * 1000)
                        if player.shield_charge_expires_at
                        else 0
                    ),  # wall clock ("0" = never): the browser compares it
                },
            )
        except Exception as e:
            logger.warning(f"player_shield_charged event for {player.serial} failed (charge unaffected): {e}")
        return True

    async def _send_shield_charge_layer(self, player: "Player", now: float) -> None:
        """Send (or re-send) the charge LED layer for the time left, capped at 120 s (BR-021)."""
        if player.shield_charge_expires_at:
            layer_s = min(player.shield_charge_expires_at - now, SHIELD_CHARGE_LAYER_MAX_SECONDS)
        else:
            layer_s = SHIELD_CHARGE_LAYER_MAX_SECONDS
        if layer_s <= 0:
            return
        # Recorded before the write, so a failing stream never re-sends every frame.
        player.shield_charge_layer_until = now + layer_s
        await self._write_shield_effect(player, "GAME_EFFECT_SHIELD_CHARGE", int(round(layer_s * 1000)))

    async def _process_shield_charge(self, player: "Player", edge: bool, now: float) -> None:
        """Frame-path work for a player who holds a charge or pressed the trigger (UC-030 BR-005).

        Lazy expiry (no timer, BR-020), the re-send before the 120 s layer cap
        (BR-021), then the activation when the press is valid: a charge is held
        and no grace, shield or invincibility window is open. An invalid press does
        nothing and is not remembered.
        """
        if player.shield_charge_seconds > 0:
            expires_at = player.shield_charge_expires_at
            if expires_at and now >= expires_at:
                await self._discard_shield_charge(player, CHARGE_OUTCOME_EXPIRED)
            elif now >= player.shield_charge_layer_until - SHIELD_CHARGE_RESEND_MARGIN_SECONDS and (
                not expires_at or expires_at > player.shield_charge_layer_until
            ):
                await self._send_shield_charge_layer(player, now)
        if not edge or player.shield_charge_seconds <= 0:
            return
        if now < self.effective_grace_until(player):
            return
        await self._activate_shield_charge(player, now)

    def _clear_shield_charge(self, player: "Player") -> None:
        player.shield_charge_seconds = 0.0
        player.shield_charge_granted_at = 0.0
        player.shield_charge_expires_at = 0.0
        player.shield_charge_layer_until = 0.0

    @staticmethod
    def _end_shield_charge_span(player: "Player", outcome: str, delay_ms: int | None = None) -> None:
        span, player._shield_charge_span = player._shield_charge_span, None
        if span is None:
            return
        with contextlib.suppress(Exception):
            span.set_attribute("shield.charge_outcome", outcome)
            if delay_ms is not None:
                span.set_attribute("shield.charge_delay_ms", delay_ms)
            span.end()

    async def _activate_shield_charge(self, player: "Player", now: float) -> None:
        """The player activates the charge (UC-030 A6): today's shield plus the swell."""
        seconds = player.shield_charge_seconds
        delay_ms = int(round((now - player.shield_charge_granted_at) * 1000))
        self._clear_shield_charge(player)
        player.grace_until = max(player.grace_until, now + seconds)
        self._arm_shield_end_grace(player, player.grace_until)  # UC-040 BR-035
        logger.info(f"Shield charge activated by {player.serial}: {seconds:.1f}s after {delay_ms} ms")

        # A6 step 2: SHIELD_CHARGE 0, then SHIELD and SWELL (in _send_shield_feedback).
        await self._write_shield_effect(player, "GAME_EFFECT_SHIELD_CHARGE", 0)
        if player.span:
            with contextlib.suppress(Exception):
                player.span.add_event(
                    "shield_activated",
                    attributes={
                        "shield.seconds": seconds,
                        "shield.charge_delay_ms": delay_ms,
                        "shield.end_grace_s": self._shield_end_grace,
                        **player_identity_attributes(player.serial, player.name),
                    },
                )
        self._end_shield_charge_span(player, CHARGE_OUTCOME_ACTIVATED, delay_ms)
        await self._publish_charge_ended(player, CHARGE_OUTCOME_ACTIVATED)
        # The player acted, so the sound always plays (UC-040 A16 / UC-030 A6).
        await self._send_shield_feedback(
            player, player.grace_until - now, kind="full", sound=True, charge_delay_ms=delay_ms
        )

    async def _discard_shield_charge(self, player: "Player", reason: str) -> None:
        """Drop a held charge unused: expired, death or game end (UC-040 A32)."""
        if player.shield_charge_seconds <= 0:
            return
        self._clear_shield_charge(player)
        logger.info(f"Shield charge of {player.serial} ended unused: {reason}")
        await self._write_shield_effect(player, "GAME_EFFECT_SHIELD_CHARGE", 0)
        if player.span:
            with contextlib.suppress(Exception):
                player.span.add_event(
                    "shield_charge_ended",
                    attributes={
                        "shield.charge_outcome": reason,
                        **player_identity_attributes(player.serial, player.name),
                    },
                )
        self._end_shield_charge_span(player, reason)
        await self._publish_charge_ended(player, reason)

    async def _publish_charge_ended(self, player: "Player", reason: str) -> None:
        try:
            await self.event_publisher(
                GameEvent.PLAYER_SHIELD_CHARGE_ENDED, {"serial": player.serial, "reason": reason}
            )
        except Exception as e:
            logger.warning(f"player_shield_charge_ended event for {player.serial} failed: {e}")

    async def _resend_shield_layers(self) -> None:
        """Re-send held charges and running shields on a (re)created stream (UC-040 BR-021).

        Nothing is held when the stream is first created, so this only sends
        after a mid-game reconnect, on every attempt. A running full shield or
        toughness boost is re-sent with the time left; a charge with its layer.
        """
        now = game_clock.now()
        self._queue_hold_feedback_all()  # UC-040 BR-026: the hold state of every living player
        for serial, player in self.players.items():
            if not player.alive:
                continue
            if player.shield_charge_seconds > 0:
                await self._send_shield_charge_layer(player, now)
            if self._shield_feedback_until.get(serial, 0.0) <= now:
                continue
            if player.grace_until > now:
                await self._write_shield_effect(
                    player, "GAME_EFFECT_SHIELD", int(round((player.grace_until - now) * 1000))
                )
            if player.toughness_boost_until > now:
                await self._write_shield_effect(
                    player, "GAME_EFFECT_TOUGHNESS_BOOST", int(round((player.toughness_boost_until - now) * 1000))
                )

    def _close_shield_charge_spans(self) -> None:
        """End every open ``game.shield_charge`` and ``game.shield_hold`` span (error teardown path); no I/O."""
        now = game_clock.now()
        self._cancel_hold_feedback()
        for player in self.players.values():
            if player._shield_charge_span is not None:
                self._clear_shield_charge(player)
                self._end_shield_charge_span(player, CHARGE_OUTCOME_GAME_END)
            if player.hold_started_at:
                self._drop_shield_hold(player, HOLD_OUTCOME_GAME_END, now)

    # ------------------------------------------------------------------
    # Shield hold (FR-161; UC-030 A7, BR-008, BR-009; UC-040 A36-A39,
    # BR-024-BR-027). Everything here is synchronous: no I/O, nothing awaited.
    # ------------------------------------------------------------------

    def _latch_shield_hold(self) -> ShieldHoldParams | None:
        """Latch hold mode and its values once per game (UC-040 BR-027); None = not a hold game.

        Called at the start of the game loop and by the first grant, in the game's
        flag context, so an experiment arm resolves. Only Free-for-All games (the
        BR-019 allow-list), real or shadow with ``pattern_trigger_policy``, can be hold
        games. Every player starts with the starting energy, clamped to the cap and the budget.
        """
        if self._shield_hold_latched:
            return self._shield_hold
        # UC-040 BR-035: the end grace is latched here in every mode and branch, because
        # instant shields exist in every mode.
        self._latch_shield_end_grace()
        if not self._shield_mode_flag_applies():
            self._shield_hold_latched = True
            self._publish_shield_mode_metrics(SHIELD_ACTIVATION_INSTANT)
            return None
        # Before the latch is set this read keeps `hold` (it is not mapped to instant).
        mode = self._shield_activation_mode()
        self._shield_hold_latched = True
        if mode != SHIELD_ACTIVATION_HOLD:
            self._shield_mode_at_latch = mode
            self._shield_mode_at_start = mode
            self._publish_shield_mode_metrics(mode)
            return None
        raw = {
            "shield.energy_max_seconds": _base.read_float_flag(
                "game",
                "shield.energy_max_seconds",
                SHIELD_HOLD_FLAGS["shield.energy_max_seconds"][0],
                game_id=self.game_id,
            ),
            "shield.energy_budget_seconds": _base.read_float_flag(
                "game",
                "shield.energy_budget_seconds",
                SHIELD_HOLD_FLAGS["shield.energy_budget_seconds"][0],
                game_id=self.game_id,
            ),
            "shield.energy_initial_seconds": _base.read_float_flag(
                "game",
                "shield.energy_initial_seconds",
                SHIELD_HOLD_FLAGS["shield.energy_initial_seconds"][0],
                game_id=self.game_id,
            ),
            "shield.recharge_per_second": _base.read_float_flag(
                "game",
                "shield.recharge_per_second",
                SHIELD_HOLD_FLAGS["shield.recharge_per_second"][0],
                game_id=self.game_id,
            ),
            "shield.min_burst_seconds": _base.read_float_flag(
                "game",
                "shield.min_burst_seconds",
                SHIELD_HOLD_FLAGS["shield.min_burst_seconds"][0],
                game_id=self.game_id,
            ),
        }
        values = {key: resolve_hold_value(raw[key], *bounds) for key, bounds in SHIELD_HOLD_FLAGS.items()}
        params = ShieldHoldParams(
            cap=values["shield.energy_max_seconds"],
            budget=values["shield.energy_budget_seconds"],
            initial=values["shield.energy_initial_seconds"],
            recharge=values["shield.recharge_per_second"],
            min_burst=values["shield.min_burst_seconds"],
        )
        self._shield_hold_cues = self._read_shield_hold_cues()
        initial = min(params.initial, params.cap, params.budget)
        for player in self.players.values():
            player.hold_energy = initial
            player.hold_energy_at = 0.0
        self._shield_hold = params
        self._publish_shield_mode_metrics(SHIELD_ACTIVATION_HOLD, values)
        logger.info(f"Shield hold latched for {self.game_id}: {params}")
        return params

    def _latch_shield_end_grace(self) -> None:
        """Read ``shield.end_grace_seconds`` once per game, in the game's flag context (UC-040 BR-035)."""
        # A literal key, so the contract scan resolves the read.
        raw = _base.read_float_flag(
            "game", "shield.end_grace_seconds", DEFAULT_SHIELD_END_GRACE_SECONDS, game_id=self.game_id
        )
        self._shield_end_grace = resolve_end_grace(raw)

    def _arm_shield_end_grace(self, player: "Player", shield_end: float) -> None:
        """A shield ends at ``shield_end``: open the end grace window after it (UC-040 BR-035).

        Assigned, never added, so windows never stack. With a window the first
        unprotected frame after it primes the EMA as the release tail does (UC-030 BR-009).
        """
        player.shield_end_at = shield_end
        grace = self._shield_end_grace
        if grace <= 0.0:
            return
        player.end_grace_until = shield_end + grace
        player.hold_prime_cap = True

    def _shield_since_end_attributes(self, player: "Player", now: float) -> dict[str, int]:
        """``shield.since_end_ms`` for player_death when a shield ended less than 3 s ago (telemetry.md §4)."""
        if not player.shield_end_at:
            return {}
        since = now - player.shield_end_at
        if since < 0.0 or since >= SHIELD_SINCE_END_REPORT_SECONDS:
            return {}
        return {"shield.since_end_ms": int(round(since * 1000))}

    def _shield_metric_labels(self) -> dict[str, str]:
        return {
            "game_kind": self.game_kind,
            "game_id": self.game_id,
            "experiment_id": self.experiment_id or "",
            "arm": self.arm or "",
        }

    def _publish_shield_mode_metrics(self, mode: str, hold_values: dict[str, float] | None = None) -> None:
        """Set the latched mode and, in a hold game, every latched hold value (telemetry.md §6; #2096).

        The agent reads the mode and the values from these gauges, never from flagd, so an
        experiment arm or a latched promotion reads right (decide-contract.md §1.2).
        """
        with contextlib.suppress(Exception):
            labels = self._shield_metric_labels()
            metrics.shield_activation_mode.labels(mode=mode, **labels).set(1)
            self._shield_metric_mode = mode
            if hold_values is not None:
                cues = self._shield_hold_cues or ShieldHoldCues()
                settings = {
                    **hold_values,
                    "shield.charge_rumble_intensity": cues.rumble_intensity,
                    "shield.charge_rumble_pulse_ms": cues.pulse_ms,
                    "shield.ending_rumble_seconds": cues.ending_lead_ms / 1000.0,
                    "shield.hold_rumble_intensity": cues.hold_rumble_intensity,
                    "shield.empty_pulse_ms": cues.empty_pulse_ms,
                    "shield.hold_rumble_max_intensity": cues.hold_rumble_max_intensity,
                    "shield.hold_led_floor_percent": cues.hold_led_floor_pct,
                    "shield.end_grace_seconds": self._shield_end_grace,
                }
                for key, value in settings.items():
                    metrics.shield_hold_setting.labels(setting=key, **labels).set(float(value))
                self._shield_metric_settings = tuple(settings)

    def _clear_shield_metrics(self) -> None:
        """Remove this game's shield mode and hold-value series (game end; idempotent)."""
        labels = tuple(self._shield_metric_labels().values())
        mode, self._shield_metric_mode = self._shield_metric_mode, None
        if mode is not None:
            with contextlib.suppress(KeyError, ValueError):
                metrics.shield_activation_mode.remove(mode, *labels)
        settings, self._shield_metric_settings = self._shield_metric_settings, ()
        for key in settings:
            with contextlib.suppress(KeyError, ValueError):
                metrics.shield_hold_setting.remove(key, *labels)

    def _set_hold_gauges(self, player: "Player", now: float) -> None:
        """Set a player's hold gauges for the agent (telemetry.md §6, UC-040 A37, UC-042 BR-007); no I/O.

        A dead player's pool reads 0 and inactive; the totals keep their last value
        (energy + used = initial + granted + recharged holds only while the pool lives).
        """
        hold = self._shield_hold
        if hold is None:
            return
        with contextlib.suppress(Exception):
            serial, game_id = player.serial, self.game_id
            alive = player.alive
            energy = self._shield_hold_energy(player, now) if alive else 0.0
            budget_left = self._shield_hold_budget_left(player, now)
            active = alive and bool(player.hold_started_at) and now < player.hold_until
            used = max(0.0, hold.budget - budget_left)
            totals = {
                "granted_seconds": player.hold_granted_total,
                "used_seconds": used,
                "activations": player.hold_activations,
                "empties": player.hold_empties,
            }
            if alive:
                initial = min(hold.initial, hold.cap, hold.budget)
                totals["recharged_seconds"] = max(0.0, energy + used - initial - player.hold_granted_total)
            metrics.player_shield_energy_seconds.labels(serial=serial, game_id=game_id).set(round(energy, 3))
            metrics.player_shield_budget_remaining_seconds.labels(serial=serial, game_id=game_id).set(
                round(budget_left, 3)
            )
            metrics.player_shield_active.labels(serial=serial, game_id=game_id).set(1 if active else 0)
            for total, value in totals.items():
                metrics.player_shield_stats.labels(serial=serial, game_id=game_id, stat=total).set(round(value, 3))

    def _read_shield_hold_cues(self) -> ShieldHoldCues:
        """Read the hold feedback cue values once, with the hold values (UC-040 BR-026, BR-027)."""
        # Literal flag keys, so the contract scan resolves each read.
        raw = {
            "shield.charge_rumble_intensity": _base.read_int_flag(
                "game", "shield.charge_rumble_intensity", 255, game_id=self.game_id
            ),
            "shield.charge_rumble_pulse_ms": _base.read_int_flag(
                "game", "shield.charge_rumble_pulse_ms", 150, game_id=self.game_id
            ),
            "shield.ending_rumble_seconds": _base.read_float_flag(
                "game", "shield.ending_rumble_seconds", 1.0, game_id=self.game_id
            ),
            "shield.hold_rumble_intensity": _base.read_int_flag(
                "game", "shield.hold_rumble_intensity", 70, game_id=self.game_id
            ),
            "shield.empty_pulse_ms": _base.read_int_flag("game", "shield.empty_pulse_ms", 400, game_id=self.game_id),
            "shield.hold_rumble_max_intensity": _base.read_int_flag(
                "game", "shield.hold_rumble_max_intensity", 200, game_id=self.game_id
            ),
            "shield.hold_led_floor_percent": _base.read_int_flag(
                "game", "shield.hold_led_floor_percent", 20, game_id=self.game_id
            ),
        }
        values = {key: resolve_hold_value(raw[key], *bounds) for key, bounds in SHIELD_HOLD_CUE_FLAGS.items()}
        return ShieldHoldCues(
            rumble_intensity=int(values["shield.charge_rumble_intensity"]),
            pulse_ms=int(values["shield.charge_rumble_pulse_ms"]),
            ending_lead_ms=int(round(values["shield.ending_rumble_seconds"] * 1000)),
            hold_rumble_intensity=int(values["shield.hold_rumble_intensity"]),
            empty_pulse_ms=int(values["shield.empty_pulse_ms"]),
            hold_rumble_max_intensity=int(values["shield.hold_rumble_max_intensity"]),
            hold_led_floor_pct=int(values["shield.hold_led_floor_percent"]),
        )

    def _start_shield_hold_clock(self, now: float) -> None:
        """Start the recharge clock of every pool at the start of the game loop (UC-040 A38)."""
        if self._shield_hold is None:
            return
        for player in self.players.values():
            if not player.hold_energy_at and not player.hold_started_at:
                player.hold_energy_at = now

    def _shield_hold_budget_left(self, player: "Player", now: float) -> float:
        """Budget remaining at ``now``, counting the running activation (UC-040 BR-024)."""
        used = player.hold_budget_used
        if player.hold_started_at:
            used += max(0.0, min(now, player.hold_until) - player.hold_started_at)
        return max(0.0, self._shield_hold.budget - used)

    def _shield_hold_energy(self, player: "Player", now: float) -> float:
        """The player's energy at ``now``: drained while active, recharged while idle (UC-040 BR-024, BR-025, A38)."""
        hold = self._shield_hold
        if player.hold_started_at:
            spent = max(0.0, min(now, player.hold_until) - player.hold_started_at)
            return max(0.0, player.hold_energy_start - spent)
        energy = player.hold_energy
        if hold.recharge > 0 and player.hold_energy_at and now > player.hold_energy_at:
            energy += hold.recharge * (now - player.hold_energy_at)
        return max(0.0, min(energy, hold.cap, self._shield_hold_budget_left(player, now)))

    def _process_shield_hold(self, player: "Player", pressed: bool, now: float, hold: ShieldHoldParams) -> None:
        """Frame-path state machine of the shield hold (UC-030 A7, BR-008); compares and field writes only."""
        if player.hold_started_at:
            if player.hold_pressed:
                if now >= player.hold_until:
                    # Held until the usable energy ran out: the budget or the pool.
                    budget_limited = player.hold_budget_start <= player.hold_energy_start
                    self._end_shield_hold(
                        player, HOLD_OUTCOME_BUDGET_EXHAUSTED if budget_limited else HOLD_OUTCOME_EMPTY, now
                    )
                    # BR-008: a new activation needs a release first.
                    player.trigger_armed = False
                    return
                if pressed:
                    player.hold_last_pressed_at = now
                    return
                self._release_shield_hold(player, now, hold)
            elif pressed and now < player.hold_until:
                # BR-008: a press inside the minimum-burst tail continues the activation.
                player.hold_pressed = True
                player.hold_until = player.hold_started_at + player.hold_usable_start
                player.hold_last_pressed_at = now
                return
            if now < player.hold_until:
                return
            self._end_shield_hold(player, HOLD_OUTCOME_RELEASED, now)
        if pressed and player.trigger_armed:
            self._start_shield_hold(player, now, hold)

    def _start_shield_hold(self, player: "Player", now: float, hold: ShieldHoldParams) -> None:
        """Start an activation when it is valid (UC-030 A7 trigger); otherwise do nothing."""
        if player.serial in self._kills_in_progress:
            return
        if now < self._other_grace_until(player):
            return  # BR-008: holding during another grace does not drain
        energy = self._shield_hold_energy(player, now)
        budget_left = self._shield_hold_budget_left(player, now)
        usable = min(energy, budget_left)
        if usable < hold.min_burst:
            return
        player.hold_started_at = now
        player.hold_pressed = True
        # UC-040 BR-035: a re-hold inside the end grace window starts at once and closes it.
        player.end_grace_until = 0.0
        player.shield_end_at = 0.0
        player.hold_energy_start = energy
        player.hold_budget_start = budget_left
        player.hold_usable_start = usable
        player.hold_until = now + usable
        player.hold_last_pressed_at = now
        player.hold_min_burst_applied = False
        player.hold_activations += 1
        self._queue_hold_feedback(player)  # UC-040 BR-026: SHIELD_HOLD, sent by the outbound task
        if player.span:
            with contextlib.suppress(Exception):
                player._shield_hold_span = tracer.start_span(
                    "game.shield_hold",
                    context=trace.set_span_in_context(player.span),
                    attributes={
                        **player_identity_attributes(player.serial, player.name),
                        "game.id": self.game_id,
                        "shield.energy_start_s": round(energy, 3),
                        "shield.budget_remaining_start_s": round(budget_left, 3),
                        "shield.min_burst_s": hold.min_burst,
                    },
                )

    def _release_shield_hold(self, player: "Player", released_at: float, hold: ShieldHoldParams) -> None:
        """The trigger reads released: the cost is ``max(held, min burst)``, at most the usable energy (BR-024)."""
        held = max(0.0, released_at - player.hold_started_at)
        cost = min(max(held, hold.min_burst), player.hold_usable_start)
        player.hold_min_burst_applied = held < hold.min_burst
        player.hold_until = player.hold_started_at + cost
        player.hold_pressed = False

    def _end_shield_hold(self, player: "Player", outcome: str, now: float) -> None:
        """End the running activation: book its cost, close its span, count it (UC-030 A7 step 4, UC-040 A39)."""
        hold = self._shield_hold
        ended_at = min(now, player.hold_until)
        cost = max(0.0, ended_at - player.hold_started_at)
        energy_end = max(0.0, player.hold_energy_start - cost)
        player.hold_budget_used += cost
        player.hold_energy = energy_end
        player.hold_energy_at = ended_at
        player.hold_started_at = 0.0
        player.hold_until = 0.0
        player.hold_pressed = False
        player.hold_energy_start = 0.0
        player.hold_budget_start = 0.0
        player.hold_usable_start = 0.0
        ran_out = outcome in (HOLD_OUTCOME_EMPTY, HOLD_OUTCOME_BUDGET_EXHAUSTED)
        end_grace = 0.0
        if ran_out:
            player.hold_empties += 1
            # UC-040 BR-035: a window only when the energy ran out, never after a release.
            self._arm_shield_end_grace(player, ended_at)
            end_grace = self._shield_end_grace
        elif outcome == HOLD_OUTCOME_RELEASED:
            player.shield_end_at = ended_at
        if outcome in (HOLD_OUTCOME_RELEASED, HOLD_OUTCOME_EMPTY, HOLD_OUTCOME_BUDGET_EXHAUSTED):
            player.hold_prime_cap = True  # UC-030 BR-009
            # UC-040 BR-026: SHIELD_HOLD 0 and the idle energy, with the empty pulse when it ran out.
            self._queue_hold_feedback(player, HOLD_CUE_NONE if outcome == HOLD_OUTCOME_RELEASED else HOLD_CUE_EMPTY)
        span, player._shield_hold_span = player._shield_hold_span, None
        if span is not None:
            with contextlib.suppress(Exception):
                span.set_attribute("shield.hold_outcome", outcome)
                span.set_attribute("shield.hold_cost_s", round(cost, 3))
                span.set_attribute("shield.min_burst_applied", player.hold_min_burst_applied)
                span.set_attribute("shield.energy_end_s", round(energy_end, 3))
                span.set_attribute("shield.end_grace_s", end_grace)
                span.end()
        player.hold_min_burst_applied = False
        with contextlib.suppress(Exception):
            metrics.shield_hold_activations_total.labels(
                mode=self.get_game_name(), game_kind=self.game_kind, outcome=outcome
            ).inc()
        if (
            hold is not None
            and not player.hold_budget_exhausted_noted
            and hold.budget - player.hold_budget_used < hold.min_burst
        ):
            player.hold_budget_exhausted_noted = True
            if player.span:
                with contextlib.suppress(Exception):
                    player.span.add_event(
                        "shield_budget_exhausted",
                        attributes={
                            "shield.budget_s": hold.budget,
                            **player_identity_attributes(player.serial, player.name),
                        },
                    )

    def _drop_shield_hold(self, player: "Player", outcome: str, now: float) -> None:
        """Drop the pool on death, disconnect or game end (UC-040 A39); a running activation ends with ``outcome``."""
        if player.hold_started_at:
            self._end_shield_hold(player, outcome, now)
        player.hold_energy = 0.0
        player.hold_energy_at = 0.0
        player.hold_prime_cap = False
        self._set_hold_gauges(player, now)  # the agent sees the dropped pool

    def _force_release_shield_holds(self) -> None:
        """Release every held activation at its last held frame (UC-030 BR-008, stream reopen)."""
        hold = self._shield_hold
        if hold is None:
            return
        for player in self.players.values():
            if player.hold_started_at and player.hold_pressed:
                self._release_shield_hold(player, player.hold_last_pressed_at, hold)

    # ------------------------------------------------------------------
    # Hold feedback (UC-040 BR-026): one ordered outbound task per hold game.
    # The frame path only marks a player; nothing here is awaited in the frame.
    # ------------------------------------------------------------------

    def _queue_hold_feedback(self, player: "Player", cue: int = HOLD_CUE_NONE) -> None:
        """Mark the player for the outbound task, keeping the strongest pending cue; no I/O."""
        if self._shield_hold is None or self._hold_feedback_stopped:
            return
        pending = self._hold_feedback_pending
        previous = pending.get(player.serial, HOLD_CUE_NONE)
        pending[player.serial] = cue if _HOLD_CUE_PRIORITY[cue] >= _HOLD_CUE_PRIORITY[previous] else previous
        self._wake_hold_feedback()

    def _queue_hold_feedback_all(self) -> None:
        """Mark every living player (the start of the game loop, a reconnect)."""
        for player in self.players.values():
            if player.alive:
                self._queue_hold_feedback(player)

    def _wake_hold_feedback(self) -> None:
        """Start the outbound task on the game's loop if needed and wake it."""
        if self._hold_feedback_stopped:
            return  # the game ended: a late mark never restarts the task
        if self._hold_feedback_wakeup is None:
            self._hold_feedback_wakeup = asyncio.Event()
        task = self._hold_feedback_task
        if task is None or task.done():
            try:
                task = asyncio.get_running_loop().create_task(self._shield_hold_feedback_loop())
            except RuntimeError:
                return  # no running loop (a synchronous test): the mark waits for the next wake
            self._hold_feedback_task = self._track_task(task)
        self._hold_feedback_wakeup.set()

    async def _shield_hold_feedback_loop(self) -> None:
        """Send the marked players' hold state, in mark order, one write at a time (UC-040 BR-026)."""
        wakeup = self._hold_feedback_wakeup
        while True:
            try:
                await asyncio.wait_for(wakeup.wait(), timeout=_base.HOLD_ENERGY_KEYFRAME_SECONDS)
            except TimeoutError:
                await self._publish_hold_energy_keyframes()
                continue
            wakeup.clear()
            await self._publish_hold_energy_keyframes()
            pending, self._hold_feedback_pending = self._hold_feedback_pending, {}
            granted, self._hold_granted_pending = self._hold_granted_pending, set()
            for serial, cue in pending.items():
                await self._publish_hold_energy(serial, serial in granted)
                try:
                    await self._send_hold_feedback(serial, cue)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"Hold feedback for {serial} failed (hold state unaffected): {e}")

    def _hold_energy_event(self, player: "Player", now: float, granted: bool) -> dict[str, str]:
        """The ``player_shield_energy`` payload of a player now (UC-035 BR-008, telemetry.md §5).

        Relative values only (no timestamp): the dashboard interpolates from its own
        receipt time with the signed rate, up to ``remaining_ms`` or ``full_ms``.
        """
        hold = self._shield_hold
        energy = self._shield_hold_energy(player, now)
        budget_left = self._shield_hold_budget_left(player, now)
        full = min(hold.cap, budget_left)
        remaining = 0.0
        rate = 0.0
        if player.hold_started_at and now < player.hold_until:
            state, rate, remaining = "active", -1.0, player.hold_until - now
        elif budget_left < hold.min_burst:
            state = "exhausted"
        elif hold.recharge > 0 and player.hold_energy_at and energy < full - 1e-9:
            state, rate = "recharging", hold.recharge
        elif energy < hold.min_burst:
            state = "empty"
        else:
            state = "charged"
        source = "agent" if granted else "recharge" if state == "recharging" else "none"

        def ms(seconds: float) -> str:
            return str(int(round(seconds * 1000)))

        return {
            "serial": player.serial,
            "state": state,
            "source": source,
            "energy_ms": ms(energy),
            "full_ms": ms(full),
            "cap_ms": ms(hold.cap),
            "min_burst_ms": ms(hold.min_burst),
            "budget_ms": ms(hold.budget),
            "budget_remaining_ms": ms(budget_left),
            "rate_ms_per_s": ms(rate),
            "remaining_ms": ms(remaining),
        }

    async def _publish_hold_energy_keyframes(self) -> None:
        """Re-publish the energy of every living player without an event for a keyframe interval (UC-035 BR-008).

        Pending marks are skipped: their event follows in the same pass of the outbound task.
        """
        now = asyncio.get_running_loop().time()
        for serial, player in list(self.players.items()):
            if not player.alive or serial in self._hold_feedback_pending:
                continue
            if now - self._hold_energy_published_at.get(serial, -math.inf) >= _base.HOLD_ENERGY_KEYFRAME_SECONDS:
                await self._publish_hold_energy(serial, False)

    async def _publish_hold_energy(self, serial: str, granted: bool) -> None:
        """Publish a player's ``player_shield_energy`` for the dashboard bar (UC-035 BR-008); best effort.

        Runs in the outbound hold task, never in the frame path. A dead or gone
        player and an ended game publish nothing: the dashboard clears its bar on
        ``player_death`` and the game end.
        """
        player = self.players.get(serial)
        if player is None or not player.alive or self._shield_hold is None or self._hold_feedback_stopped:
            return
        self._hold_energy_published_at[serial] = asyncio.get_running_loop().time()
        self._set_hold_gauges(player, game_clock.now())  # the agent's view, with every event and keyframe
        if self._is_shadow_game():
            return  # UC-035 BR-008: no player_shield_energy from a shadow game (no dashboard bar, bus queues)
        try:
            await self.event_publisher(
                GameEvent.PLAYER_SHIELD_ENERGY, self._hold_energy_event(player, game_clock.now(), granted)
            )
        except Exception as e:
            logger.warning(f"player_shield_energy event for {serial} failed (hold state unaffected): {e}")

    def _hold_state_message(self, player: "Player", now: float, cue: int):
        """The ``ShieldHoldState`` of a player now: energy, cap, full level, rate, cue and the latched cue values.

        A running activation also carries its activation energy (the usable energy at its start plus
        any grant during it), the reference of the controller's rumble ramp and LED dimming (#2463).
        """
        from proto import controller_manager_pb2

        hold = self._shield_hold
        cues = self._shield_hold_cues or ShieldHoldCues()
        energy = self._shield_hold_energy(player, now)
        full = min(hold.cap, self._shield_hold_budget_left(player, now))
        recharging = not player.hold_started_at and player.hold_energy_at and energy < full
        return controller_manager_pb2.ShieldHoldState(
            energy_ms=int(round(energy * 1000)),
            cap_ms=int(round(hold.cap * 1000)),
            full_ms=int(round(full * 1000)),
            min_burst_ms=int(round(hold.min_burst * 1000)),
            rate_ms_per_s=int(round(hold.recharge * 1000)) if recharging else 0,
            cue=cue,
            cues=controller_manager_pb2.ShieldCueParams(
                rumble_intensity=cues.rumble_intensity,
                pulse_ms=cues.pulse_ms,
                ending_lead_ms=cues.ending_lead_ms,
                hold_rumble_intensity=cues.hold_rumble_intensity,
                empty_pulse_ms=cues.empty_pulse_ms,
                hold_rumble_max_intensity=cues.hold_rumble_max_intensity,
                hold_led_floor_pct=cues.hold_led_floor_pct,
            ),
            activation_ms=int(round(player.hold_usable_start * 1000)) if player.hold_started_at else 0,
        )

    async def _send_hold_feedback(self, serial: str, cue: int) -> None:
        """Send one player's hold state as it is now (UC-040 BR-026).

        Active: ``SHIELD_HOLD`` with the time until the energy runs out (a new activation
        also plays ``shield_up``, at most once per 2 s). Idle: ``SHIELD_HOLD`` 0 when one
        was running, then ``SHIELD_ENERGY``. A dead or gone player gets nothing: the death
        effect and the disconnect end the layers on the controller.
        """
        player = self.players.get(serial)
        if (
            player is None
            or not player.alive
            or self._shield_hold is None
            or self._hold_feedback_stopped
            or not self.gameplay_stream
        ):
            return
        now = game_clock.now()
        if player.hold_started_at and now < player.hold_until:
            # Keyed by the activation's start: an end and a new start between two sends coalesce.
            new_activation = self._hold_layer_sent.get(serial) != player.hold_started_at
            self._hold_layer_sent[serial] = player.hold_started_at
            self._hold_commands_sent.add(serial)
            await self._write_shield_effect(
                player,
                "GAME_EFFECT_SHIELD_HOLD",
                max(1, int(round((player.hold_until - now) * 1000))),
                self._hold_state_message(player, now, cue),
            )
            if (
                new_activation
                and now - self._hold_sound_at.get(serial, -math.inf) >= SHIELD_HOLD_SOUND_MIN_INTERVAL_SECONDS
            ):
                self._hold_sound_at[serial] = now
                try:
                    await self._play_sound(Sound.SFX_SHIELD_UP, priority=2, volume=1.0)
                except Exception as e:
                    logger.warning(f"Shield sound for {serial} failed (protection unaffected): {e}")
            return
        if serial in self._hold_layer_sent:
            del self._hold_layer_sent[serial]
            await self._write_shield_effect(player, "GAME_EFFECT_SHIELD_HOLD", 0)
            if not player.alive or self._hold_feedback_stopped:
                return  # died (or the game ended) during the write: the death effect or SHIELD 0 owns the LED
        self._hold_commands_sent.add(serial)
        state = self._hold_state_message(player, now, cue)
        await self._write_shield_effect(player, "GAME_EFFECT_SHIELD_ENERGY", max(1, state.cap_ms), state)

    def _cancel_hold_feedback(self) -> None:
        """Stop the outbound task for good and drop pending marks (no I/O)."""
        self._hold_feedback_stopped = True
        task, self._hold_feedback_task = self._hold_feedback_task, None
        if task is not None and not task.done():
            task.cancel()
        self._hold_feedback_pending.clear()
        self._hold_granted_pending.clear()

    async def _stop_hold_feedback(self) -> list[str]:
        """Stop the outbound task at game end; returns the serials whose controller got a hold command."""
        task = self._hold_feedback_task
        self._cancel_hold_feedback()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        serials = sorted(self._hold_commands_sent)
        self._hold_commands_sent.clear()
        self._hold_layer_sent.clear()
        return serials

    def _prime_ema_after_hold(self, player: "Player", accel_mag: float) -> bool:
        """UC-030 BR-009: prime the EMA with min(raw, warning threshold); True when it primed."""
        player.hold_prime_cap = False
        if player.smoothed_accel >= 1e-9:
            return False
        effective_warn, _ = self._compute_effective_thresholds(player)
        player.smoothed_accel = min(accel_mag, effective_warn)
        player.last_accel_mag = player.smoothed_accel
        return True

    def _grant_shield_energy(self, player: "Player", seconds: float, hold: ShieldHoldParams) -> bool:
        """Add a grant to the player's energy (UC-040 A36); refuse one that could add nothing (A37).

        Order: budget used up, pool full, then the BR-022 cap and cooldown. A
        refusal raises ``InterventionNotApplied``, so the backstop is refunded and
        nothing counts. During a held activation the deadline moves out.
        """
        now = game_clock.now()
        used = self._agent_shield_grants.get(player.serial, 0)
        budget_left = self._shield_hold_budget_left(player, now)
        if budget_left < hold.min_burst:
            self._refuse_shield(player, SHIELD_REFUSED_BUDGET_EXHAUSTED, used, 0.0)
        energy = self._shield_hold_energy(player, now)
        full = min(hold.cap, budget_left)
        room = full - energy
        if room < SHIELD_HOLD_POOL_FULL_ROOM_SECONDS:
            self._refuse_shield(player, SHIELD_REFUSED_POOL_FULL, used, 0.0)
        self._check_shield_limits(player, now)

        added = min(seconds, room)
        if player.hold_started_at:
            player.hold_energy_start += added
            player.hold_usable_start = min(player.hold_energy_start, player.hold_budget_start)
            if player.hold_pressed:
                player.hold_until = player.hold_started_at + player.hold_usable_start
        else:
            player.hold_energy = energy + added
            if player.hold_energy_at:
                player.hold_energy_at = now
        player.hold_granted_total += added
        self._record_agent_shield(player.serial, now, seconds)
        self._hold_granted_pending.add(player.serial)  # UC-035 BR-008: the next energy event says source=agent
        energy_after = energy + added
        # UC-040 A36 step 4: the grant pattern when the pool is full, one pulse for a top-up.
        self._queue_hold_feedback(player, HOLD_CUE_FULL if energy_after >= full - 1e-9 else HOLD_CUE_TOP_UP)
        logger.info(f"Shield energy granted to {player.serial}: +{added:.2f}s of {seconds:.1f}s ({energy_after:.2f}s)")
        if player.span:
            identity = player_identity_attributes(player.serial, player.name)
            with contextlib.suppress(Exception):
                player.span.add_event(
                    "shield_energy_granted",
                    attributes={
                        "shield.seconds_requested": seconds,
                        "shield.seconds_added": round(added, 3),
                        "shield.energy_s": round(energy_after, 3),
                        **identity,
                    },
                )
                if energy_after >= full - 1e-9:
                    player.span.add_event(
                        "shield_energy_full", attributes={"shield.energy_source": "agent", **identity}
                    )
        return True
