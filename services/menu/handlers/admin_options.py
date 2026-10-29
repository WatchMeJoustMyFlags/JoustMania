"""
Admin-mode option handlers: sensitivity, battery, instructions, option cycling,
value increase/decrease, agent control and their LED / voice feedback.

Moved out of ``admin.py`` unchanged (#2380); ``AdminModeHandler`` inherits these
methods, so ``self`` is the handler and all state stays on it.
"""

from __future__ import annotations

import asyncio
import logging

from lib.colors import Colors
from lib.telemetry import SpanAttr
from lib.types import Sound
from services.menu.settings_ops import (
    AGENT_LEVEL_COLORS,
    AGENT_LEVEL_VOICES,
    AGENT_POLICY_FLAG,
    FORCE_ALL_START_VOICES,
    NUM_TEAMS_VOICES,
    SENSITIVITY_COLORS,
    SENSITIVITY_VALUES,
    SENSITIVITY_VOICES,
    agent_level_next,
    variant_to_value,
)

logger = logging.getLogger(__name__)

# Admin cycle option for the on-device agent kill-switch / policy control (#819).
# Selecting it and pressing SELECT/START cycles the agent's GLOBAL
# ``interventions_allowed`` policy in agent.json. This is NOT a game.json flag —
# it is the human's control over the agent (docs/OWNERSHIP_MODEL.md §6.2), so it
# is routed through a separate writer/client and handled specially in the
# value-change paths rather than the generic game.json flag cycle.
AGENT_CONTROL_OPTION = "agent_control"
# Error cue when a write fails or the flag service cannot be read (UC-013 A3, UC-012 A5/A7): short fast red pulse.
AGENT_ERROR_PULSE_MS = 400
AGENT_ERROR_PULSE_SPEED = 10


class AdminOptionsMixin:
    async def handle_sensitivity(self, serial: str) -> None:
        """
        Handle sensitivity cycling in admin mode.

        Cycles through sensitivity variants via the flag service (FlagServiceWriter).

        Args:
            serial: Controller serial number
        """
        from proto import controller_manager_pb2

        if self._state_manager is None:
            return

        ctx = self._get_span_context()
        with self.tracer.start_as_current_span("admin_sensitivity", context=ctx) as span:
            span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)

            try:
                writer = self._state_manager.game_settings_writer
                # The CYCLE feedback must describe the baseline transition the
                # admin is actually making: cycle_variant() advances the on-disk
                # defaultVariant (the human-owned baseline), so old_variant must
                # be the baseline value being cycled FROM, captured before the
                # mutation. Using the effective value here would announce the
                # agent's override (e.g. ultra_fast) as the source while we
                # actually write baseline medium->fast — incoherent. Selecting an
                # option and the agent-overlay follow-up after a step announce
                # the effective value from the flag-service view (_overlay_variant, #818, UC-012 BR-006).
                old_variant, new_variant = await self._cycle_baseline_guarded(writer, "sensitivity", True)

                if new_variant is None:
                    logger.warning("Failed to cycle sensitivity variant")
                    await self._signal_write_failed(serial, voice=True)
                    return

                # Map variant name to numeric value for visual feedback
                new_value = SENSITIVITY_VALUES.get(new_variant, 2)
                color = SENSITIVITY_COLORS[new_value].value

                # Show sensitivity color (GAME_EFFECT_PULSE restores to base automatically)
                await self._send_game_effect(
                    serial,
                    controller_manager_pb2.GAME_EFFECT_PULSE,
                    color=color,
                    duration_ms=800,
                    speed=5,
                )

                # Play voice for sensitivity level (matches original JoustMania)
                await self._play_voice(SENSITIVITY_VOICES[new_value])
                await self._announce_agent_overlay(serial, "sensitivity", new_variant)

                span.add_event(
                    "sensitivity_changed",
                    {"old_variant": old_variant, "new_variant": new_variant},
                )
                logger.info(f"Sensitivity changed by admin controller {serial}: {old_variant} -> {new_variant}")

            except Exception as e:
                logger.error(f"Error changing sensitivity: {e}", exc_info=True)

    async def handle_battery(self, serial: str) -> None:
        """
        Handle battery display in admin mode (triangle press).

        Sends GAME_EFFECT_SHOW_BATTERY to controller_manager which shows
        battery levels on ALL connected controllers using color-coded LEDs
        for 1 second, then auto-restores to previous colors.

        Color scheme (from original JoustMania):
        - 100%: Green
        - 80%: Turquoise
        - 60%: Blue
        - 40%: Yellow
        - <40%: Red

        Args:
            serial: Controller serial number
        """
        from proto import controller_manager_pb2

        ctx = self._get_span_context()
        with self.tracer.start_as_current_span("admin_battery_display", context=ctx) as span:
            span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)

            try:
                # Send SHOW_BATTERY effect with empty serial to affect all controllers
                if await self._send_game_effect("", controller_manager_pb2.GAME_EFFECT_SHOW_BATTERY):
                    logger.info(f"Battery display triggered by admin controller {serial}")
                    span.add_event("battery_display_triggered")
                else:
                    logger.warning("Could not trigger battery display - stream not available")

            except Exception as e:
                logger.error(f"Error triggering battery display: {e}", exc_info=True)

    async def handle_instructions(self, serial: str) -> None:
        """
        Handle instruction toggle in admin mode.

        Toggles instruction display on/off via the flag service (FlagServiceWriter).

        Args:
            serial: Controller serial number
        """
        from proto import controller_manager_pb2

        if self._state_manager is None:
            return

        ctx = self._get_span_context()
        with self.tracer.start_as_current_span("admin_instructions", context=ctx) as span:
            span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)

            try:
                writer = self._state_manager.user_prefs_writer
                old_variant, new_variant = await self._cycle_baseline_guarded(writer, "game_instructions", True)

                if new_variant is None:
                    logger.warning("Failed to cycle game_instructions variant")
                    await self._signal_write_failed(serial, voice=True)
                    return

                enabled = new_variant == "on"

                # Visual feedback: Green (enabled) or Red (disabled)
                # GAME_EFFECT_PULSE restores to base color automatically
                pulse_color = (0, 255, 0) if enabled else (255, 0, 0)
                await self._send_game_effect(
                    serial,
                    controller_manager_pb2.GAME_EFFECT_PULSE,
                    color=pulse_color,
                    duration_ms=800,
                    speed=5,
                )

                # Play instructions toggle voice announcement
                voice = Sound.MENU_VOX_INSTRUCTIONS_ON if enabled else Sound.MENU_VOX_INSTRUCTIONS_OFF
                await self._play_voice(voice)

                span.add_event(
                    "instructions_toggled",
                    {"old_variant": old_variant, "new_variant": new_variant, "enabled": enabled},
                )
                logger.info(f"Instructions toggled by admin controller {serial}: {old_variant} -> {new_variant}")

            except Exception as e:
                logger.error(f"Error toggling instructions: {e}", exc_info=True)

    async def handle_cycle_option(self, serial: str) -> None:
        """
        Cycle through admin options.

        Options: sensitivity -> num_teams -> force_all_start -> sensitivity

        Args:
            serial: Controller serial number
        """
        ctx = self._get_span_context()
        with self.tracer.start_as_current_span("admin_cycle_option", context=ctx) as span:
            span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)

            # Cycle to next option
            self.current_option = (self.current_option + 1) % len(self.option_names)
            option_name = self.option_names[self.current_option]
            option_color = self.option_colors[self.current_option]

            span.set_attribute(SpanAttr.ADMIN_OPTION, option_name)

            try:
                # Show option color for 1 second
                await self._send_base_color(serial, option_color.value)

                # Restore white after option color finishes
                async def restore_white():
                    await asyncio.sleep(1.1)
                    if self.active and serial == self.controller_serial:
                        await self._send_base_color(serial, Colors.White.value)

                # Track task to prevent garbage collection
                task = asyncio.create_task(restore_white())
                self._pending_tasks.add(task)
                task.add_done_callback(self._pending_tasks.discard)

                span.add_event(
                    "admin_option_changed",
                    {"option": option_name, "option_index": self.current_option},
                )
                await self._announce_effective_value(serial, option_name)
                logger.info(f"Admin option changed to {option_name} by controller {serial}")

            except Exception as e:
                logger.error(f"Error cycling admin option: {e}", exc_info=True)

    async def handle_increase_value(self, serial: str) -> None:
        """
        Increase the value of the current admin option.

        Cycles through flag variants via the flag service (FlagServiceWriter).

        Args:
            serial: Controller serial number
        """
        if self._state_manager is None:
            return

        option_name = self.option_names[self.current_option]
        if option_name == AGENT_CONTROL_OPTION:
            await self.handle_agent_control(serial, forward=True)
            return

        ctx = self._get_span_context()
        with self.tracer.start_as_current_span("admin_increase_value", context=ctx) as span:
            span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)
            span.set_attribute(SpanAttr.ADMIN_OPTION, option_name)

            try:
                writer = self._state_manager.game_settings_writer
                # Cycle feedback describes the baseline transition (defaultVariant
                # being changed), captured before the mutation — NOT the effective
                # value, which may be an agent override (#818). The agent-overlay
                # follow-up (_announce_agent_overlay) announces the effective value.
                old_variant, new_variant = await self._cycle_baseline_guarded(writer, option_name, True)

                if new_variant is None:
                    logger.warning(f"Failed to cycle variant for {option_name}")
                    await self._signal_write_failed(serial, voice=True)
                    return

                # Get the actual value for visual/voice feedback
                new_value = self._variant_to_value(option_name, new_variant)

                # Visual feedback
                await self._show_value_feedback(serial, option_name, new_value)

                # Voice feedback
                await self._play_value_voice(option_name, new_value)
                await self._announce_agent_overlay(serial, option_name, new_variant)

                span.add_event(
                    "admin_value_increased",
                    {"option": option_name, "old_variant": str(old_variant), "new_variant": str(new_variant)},
                )
                logger.info(f"Admin increased {option_name}: {old_variant} -> {new_variant}")

            except Exception as e:
                logger.error(f"Error increasing admin value: {e}", exc_info=True)

    async def handle_decrease_value(self, serial: str) -> None:
        """
        Decrease the value of the current admin option.

        Cycles through flag variants backward via the flag service (FlagServiceWriter).

        Args:
            serial: Controller serial number
        """
        if self._state_manager is None:
            return

        option_name = self.option_names[self.current_option]
        if option_name == AGENT_CONTROL_OPTION:
            await self.handle_agent_control(serial, forward=False)
            return

        ctx = self._get_span_context()
        with self.tracer.start_as_current_span("admin_decrease_value", context=ctx) as span:
            span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)
            span.set_attribute(SpanAttr.ADMIN_OPTION, option_name)

            try:
                writer = self._state_manager.game_settings_writer
                # Cycle feedback describes the baseline transition (defaultVariant
                # being changed), captured before the mutation — NOT the effective
                # value, which may be an agent override (#818). The agent-overlay
                # follow-up (_announce_agent_overlay) announces the effective value.
                old_variant, new_variant = await self._cycle_baseline_guarded(writer, option_name, False)

                if new_variant is None:
                    logger.warning(f"Failed to cycle variant backward for {option_name}")
                    await self._signal_write_failed(serial, voice=True)
                    return

                # Get the actual value for visual/voice feedback
                new_value = self._variant_to_value(option_name, new_variant)

                # Visual feedback
                await self._show_value_feedback(serial, option_name, new_value)

                # Voice feedback
                await self._play_value_voice(option_name, new_value)
                await self._announce_agent_overlay(serial, option_name, new_variant)

                span.add_event(
                    "admin_value_decreased",
                    {"option": option_name, "old_variant": str(old_variant), "new_variant": str(new_variant)},
                )
                logger.info(f"Admin decreased {option_name}: {old_variant} -> {new_variant}")

            except Exception as e:
                logger.error(f"Error decreasing admin value: {e}", exc_info=True)

    async def _announce_effective_value(self, serial: str, option_name: str) -> None:
        """
        Announce the EFFECTIVE value of the selected option (colour pulse and voice line).

        The effective value is the human baseline plus any active agent delta
        (OWNERSHIP_MODEL section 6, #818; UC-012 step 3, BR-006). Options without
        a value ladder (agent_control has its own flow) are skipped. Best effort.
        """
        if self._state_manager is None or option_name not in ("sensitivity", "num_teams", "force_all_start"):
            return
        try:
            variant = await asyncio.to_thread(self._effective_variant, option_name)
            if variant is None:
                # Flag service unreadable (UC-012 A7): no value to show, so say so on the LED.
                await self._signal_write_failed(serial)
                return
            value = self._variant_to_value(option_name, variant)
            await self._show_value_feedback(serial, option_name, value)
            await self._play_value_voice(option_name, value)
        except Exception as e:
            logger.error(f"Error announcing effective value for {option_name}: {e}", exc_info=True)

    async def _announce_agent_overlay(self, serial: str, option_name: str, new_variant: str) -> None:
        """
        After a host step: when an agent overlay makes the EFFECTIVE value differ from the
        new baseline, follow the baseline announcement with the agent cue (magenta pulse,
        as the coordinator's #818 cue) and the effective value. Equal or unknown: silent.
        The overlay is read from the flag service (#2092), not the lagging in-process client.
        """
        from proto import controller_manager_pb2

        try:
            effective = await asyncio.to_thread(self._overlay_variant, option_name, new_variant)
            if effective is None:
                return
            await self._send_game_effect(
                serial,
                controller_manager_pb2.GAME_EFFECT_PULSE,
                color=Colors.Magenta.value,
                duration_ms=600,
                speed=6,
            )
            # Reinforces the magenta pulse (#1746); detached so a slow audio RPC never delays the value LED.
            self._play_voice_detached(Sound.MENU_VOX_ADMIN_AGENT)
            value = self._variant_to_value(option_name, effective)
            await self._show_value_feedback(serial, option_name, value)
            await self._play_value_voice(option_name, value)
        except Exception as e:
            logger.error(f"Error announcing agent overlay for {option_name}: {e}", exc_info=True)

    def _overlay_variant(self, option_name: str, new_variant: str) -> str | None:
        """
        The variant an overlay holds when it overrides the baseline just written, else None.

        Reads the flag service, whose answer is synchronous with the write, not the
        in-process flagd client: that one lags the write by up to a sync interval, and
        its stale value was mistaken for an agent overlay (#2092). The flag service's
        effective default is the stack overlay, else the admin value just written, so a
        difference from ``new_variant`` is a real overlay. Unreadable: None (silent).

        Blocking (one flag-service read): run via to_thread.
        """
        view = self._state_manager.game_settings_writer.get_flag_view(option_name)
        effective = view.get("effective_default") if view else None
        if effective is None or effective == new_variant:
            return None
        return effective

    @staticmethod
    def _cycle_baseline(writer, flag_key: str, forward: bool) -> tuple[str | None, str | None]:
        """(baseline variant before, variant after) one cycle step. Blocking: run via to_thread."""
        return writer.get_current_variant(flag_key), writer.cycle_variant(flag_key, forward=forward)

    def _effective_variant(self, option_name: str) -> str | None:
        """
        The EFFECTIVE variant of a game.json option, from the flag service (#818, #2092).

        The flag service's ``effective_default`` ranks the stack overlay above the admin
        value above the promotion overlay above the baseline, and it is synchronous with
        every write. The in-process flagd client is not read: it lags a write by up to a
        sync interval and would announce the previous value.

        Blocking (one flag-service read): run via to_thread.

        Returns:
            Effective variant name, or None when the flag service cannot be read (silent).
        """
        view = self._state_manager.game_settings_writer.get_flag_view(option_name)
        return view.get("effective_default") if view else None

    async def handle_agent_control(self, serial: str, forward: bool = True) -> None:
        """
        Cycle the agent intervention policy from the device (#819 kill-switch).

        Writes the GLOBAL ``interventions_allowed`` flag in agent.json through the
        menu's agent writer, cycling none -> ambient -> standard -> full (and
        back). "none" is the hard panic-off: the coordinator's allow-list then
        rejects every agent intervention, so the switch is load-bearing at the
        gate even if the agent process lags (docs/OWNERSHIP_MODEL.md §6.2).

        This is global agent policy (read with a bare context, not game-scoped),
        and it writes only agent.json — the menu/agent write surfaces stay
        disjoint, so no real-resolution invariant is touched. flagd hot-reloads
        the change within ~100ms; the agent re-reads it live per decision.

        Args:
            serial: Controller serial number
            forward: True to escalate permissiveness, False to restrict.
        """
        from proto import controller_manager_pb2

        if self._state_manager is None:
            return

        writer = getattr(self._state_manager, "agent_settings_writer", None)
        if writer is None:
            logger.warning("Agent control unavailable: no agent settings writer wired")
            return

        ctx = self._get_span_context()
        with self.tracer.start_as_current_span("admin_agent_control", context=ctx) as span:
            span.set_attribute(SpanAttr.CONTROLLER_SERIAL, serial)
            span.set_attribute(SpanAttr.ADMIN_OPTION, AGENT_CONTROL_OPTION)

            try:
                old_level = await asyncio.to_thread(self._current_agent_level)
                if old_level is None and forward:
                    # UC-013 A5: never raise a level that cannot be read; a restrict still writes none.
                    logger.warning("Agent level unreadable: escalation refused")
                    await self._signal_agent_write_failed(serial, controller_manager_pb2)
                    return
                new_level = self._next_agent_level(old_level, forward=forward)

                try:
                    written = await asyncio.to_thread(writer.update_default_variant, AGENT_POLICY_FLAG, new_level)
                except Exception as e:
                    # Only the write itself: errors in the feedback below must not cue a failure.
                    logger.error(f"Error writing agent policy '{new_level}': {e}", exc_info=True)
                    written = False
                if not written:
                    logger.warning(f"Failed to write agent policy '{new_level}'")
                    await self._signal_agent_write_failed(serial, controller_manager_pb2, voice=True)
                    return

                span.set_attribute("old_level", old_level or "")
                span.set_attribute("new_level", new_level)

                # Visual feedback: distinct color per level so the agent's
                # permission state is unambiguous at a glance. Red = off (panic),
                # escalating warmth toward full.
                color = AGENT_LEVEL_COLORS.get(new_level, Colors.Purple).value
                # The pulse is cosmetic: a failure or slow stream must not drop the
                # voice line that follows (#1643).
                try:
                    await self._send_game_effect(
                        serial,
                        controller_manager_pb2.GAME_EFFECT_PULSE,
                        color=color,
                        duration_ms=800,
                        speed=5,
                    )
                except Exception as e:
                    logger.warning(f"Agent policy pulse failed for {serial}: {e}")

                # Voice feedback: "off" announces the panic-off; permissive levels
                # announce a numbered level (1..3). Reuses existing vox assets.
                voice = AGENT_LEVEL_VOICES.get(new_level)
                if voice:
                    await self._play_voice(voice)

                span.add_event(
                    "agent_policy_changed",
                    {"old_level": str(old_level), "new_level": new_level},
                )
                logger.info(f"Agent policy changed by admin {serial}: {old_level} -> {new_level}")

            except Exception as e:
                logger.error(f"Error changing agent policy: {e}", exc_info=True)

    async def _cycle_baseline_guarded(self, writer, option_name: str, forward: bool) -> tuple[str | None, str | None]:
        """_cycle_baseline in a thread; an exception from the write is logged and reported as (None, None).

        Callers treat new_variant None as a failed write and give the error cue (UC-012 A5). Only the
        write is guarded, so a voice or LED error after a successful write never cues a failure.
        """
        try:
            return await asyncio.to_thread(self._cycle_baseline, writer, option_name, forward)
        except Exception as e:
            logger.error(f"Error writing {option_name} variant: {e}", exc_info=True)
            return None, None

    async def _signal_write_failed(self, serial: str, voice: bool = False) -> None:
        """Error cue for a failed admin step write or an unreadable flag service (UC-012 A5, A7).

        The same short red pulse as the agent-level cue (UC-013 A3). ``voice`` adds the "Not saved"
        line (#1746) and is set only where a write failed, not where a read did. Best effort.
        """
        from proto import controller_manager_pb2

        await self._signal_agent_write_failed(serial, controller_manager_pb2, voice=voice)

    async def _signal_agent_write_failed(self, serial: str, controller_manager_pb2, voice: bool = False) -> None:
        """Error cue for a refused or unreachable agent-level write (UC-013 A3, #1705).

        A short, fast red pulse, so it cannot be mistaken for the "none" level (red, 800 ms, "off"
        voice); LED first, and the "Not saved" line (#1746) only reinforces it, never a level voice
        line. ``voice`` is set only where a write failed. Best effort.
        """
        try:
            await self._send_game_effect(
                serial,
                controller_manager_pb2.GAME_EFFECT_PULSE,
                color=Colors.Red.value,
                duration_ms=AGENT_ERROR_PULSE_MS,
                speed=AGENT_ERROR_PULSE_SPEED,
            )
        except Exception as e:
            logger.warning(f"Agent write error pulse failed for {serial}: {e}")
        if voice:
            try:
                await self._play_voice(Sound.MENU_VOX_ADMIN_NOT_SAVED)
            except Exception as e:
                logger.warning(f"Not-saved voice failed for {serial}: {e}")

    def _current_agent_level(self) -> str | None:
        """The agent policy level the flag service serves (UC-013 BR-003), None if unreadable. Blocking: to_thread."""
        return self.settings_ops.current_agent_level()

    # The clamped level ladder (UC-013) and the variant values are shared with the pad (settings_ops).
    _next_agent_level = staticmethod(agent_level_next)
    _variant_to_value = staticmethod(variant_to_value)

    async def _show_value_feedback(self, serial: str, option_name: str, value: int | float | bool) -> None:
        """
        Show visual feedback for admin value change.

        Args:
            serial: Controller serial number
            option_name: Name of the option that changed
            value: New value (typed)
        """
        from proto import controller_manager_pb2

        try:
            # Determine feedback color using pattern matching
            match option_name:
                case "sensitivity":
                    # 5-color gradient for sensitivity levels (0-4)
                    pulse_color = SENSITIVITY_COLORS[int(value)].value

                case "num_teams":
                    # Gradient from green (2 teams) to red (6 teams)
                    num = int(value)
                    r = int(255 * (num - 2) / 4)
                    g = int(255 * (6 - num) / 4)
                    pulse_color = (r, g, 0)

                case "force_all_start":
                    # Green for True, red for False
                    pulse_color = Colors.Green.value if value else Colors.Red.value

                case _:
                    pulse_color = Colors.White.value

            # Show feedback color (GAME_EFFECT_PULSE restores to base automatically)
            await self._send_game_effect(
                serial,
                controller_manager_pb2.GAME_EFFECT_PULSE,
                color=pulse_color,
                duration_ms=600,
                speed=6,
            )

        except Exception as e:
            logger.error(f"Error showing value feedback: {e}", exc_info=True)

    async def _play_value_voice(self, option_name: str, value: int | float | bool) -> None:
        """
        Play voice feedback for admin option value change.

        Args:
            option_name: Name of the option that changed
            value: New value (typed from _variant_to_value)
        """
        try:
            if option_name == "sensitivity":
                # Same per-level voice as Circle (A1), so Select/Start announce it too (UC-012 step 5).
                if isinstance(value, int) and 0 <= value < len(SENSITIVITY_VOICES):
                    await self._play_voice(SENSITIVITY_VOICES[value])
            elif option_name == "num_teams":
                # Play number voice (adminop_2 through adminop_6)
                voice = NUM_TEAMS_VOICES.get(value) if isinstance(value, int) else None
                if voice:
                    await self._play_voice(voice)
            elif option_name == "force_all_start":
                # Play true/false voice
                voice = FORCE_ALL_START_VOICES[value is True]
                await self._play_voice(voice)
        except Exception as e:
            logger.error(f"Error playing value voice: {e}", exc_info=True)
