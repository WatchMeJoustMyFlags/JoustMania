# Flags an intervention handler reads, and the flagd targeting variables each may use.
#
# Pure literal, no imports: tests/contract/test_flagd_resolution.py (a venv without the
# coordinator's dependencies) reads it with ast.literal_eval.
#
# A handler (and a revert handler and the agent cue) runs on the game's loop through
# GameLoopDispatcher.run, and the InterventionManager sets the game's own flag transaction
# context around it (interventions.py run_in_game_flag_context). A flag read there
# therefore sees the API-level attributes (service_name, service_namespace, language,
# hostname, game_kind), the game's context (targetingKey=game_mode, game_mode,
# controller_count, sensitivity, game_kind, experiment_id, arm; flags.md section 1.2) and
# whatever the read passes itself, which wins. Hence:
#
# - "targeting": the only variables the flagd targeting of that flag may reference
#   (always allowed on top: the API-level attributes above and $flagd.*). The contract
#   test fails the build when game.json or ci/game.json uses any other variable. The
#   game's context is available, but a flag keeps its list narrow on purpose when
#   another reader resolves it with less: the agent reads the shield flags with gameId,
#   the game's game_kind and, in an experiment, experiment_id and arm
#   (services/agent/flags/game.go shieldChargeContext), so their targeting must not use
#   targetingKey (now the game mode there) or game_mode. game_kind is not listed: it is
#   an API-level attribute, always allowed (the experiment rung tests it).
# - "experiment_rung": "applies" when the flag is in the experiment catalog (registry.yaml
#   scope experiment+arm): the handler read sees the game's game_kind, experiment_id and
#   arm, so in an experimental-arm game the experiment's value wins (UC-043 BR-005).
# - "readers": where the read is made.
#
# tests/test_handler_flag_reads.py drives every registered handler and fails when one
# reads a flag that is not listed here, or when a handler has no census case.
HANDLER_FLAG_READS = {
    # Agent-tunable since FR-162 (UC-043 A17): an experiment arm reaches the read.
    ("game", "shield.activation"): {
        "targeting": ["gameId", "experiment_id", "arm"],
        "experiment_rung": "applies",
        "readers": "games/base.py _shield_activation_mode, _latch_shield_hold <- grant_shield <- handle_shield_seconds",
    },
    ("game", "shield.charge_expiry_seconds"): {
        "targeting": ["gameId"],
        "readers": "games/base.py _read_charge_expiry <- grant_shield <- handle_shield_seconds",
    },
    # Hold mode (UC-040 BR-027): latched once per game, at the start of the game
    # loop or by the first grant, whichever is first, in the game's flag context, so an
    # experiment arm resolves (FR-162, UC-043 A17).
    ("game", "shield.energy_max_seconds"): {
        "targeting": ["gameId", "experiment_id", "arm"],
        "experiment_rung": "applies",
        "readers": "games/base.py _latch_shield_hold <- grant_shield <- handle_shield_seconds (first grant)",
    },
    ("game", "shield.energy_budget_seconds"): {
        "targeting": ["gameId", "experiment_id", "arm"],
        "experiment_rung": "applies",
        "readers": "games/base.py _latch_shield_hold <- grant_shield <- handle_shield_seconds (first grant)",
    },
    ("game", "shield.energy_initial_seconds"): {
        "targeting": ["gameId", "experiment_id", "arm"],
        "experiment_rung": "applies",
        "readers": "games/base.py _latch_shield_hold <- grant_shield <- handle_shield_seconds (first grant)",
    },
    ("game", "shield.recharge_per_second"): {
        "targeting": ["gameId", "experiment_id", "arm"],
        "experiment_rung": "applies",
        "readers": "games/base.py _latch_shield_hold <- grant_shield <- handle_shield_seconds (first grant)",
    },
    ("game", "shield.min_burst_seconds"): {
        "targeting": ["gameId", "experiment_id", "arm"],
        "experiment_rung": "applies",
        "readers": "games/base.py _latch_shield_hold <- grant_shield <- handle_shield_seconds (first grant)",
    },
    # Hold feedback cue values (UC-040 BR-026): latched with the hold values and
    # shipped to the controller manager with every hold command.
    ("game", "shield.hold_rumble_intensity"): {
        "targeting": ["gameId", "experiment_id", "arm"],
        "experiment_rung": "applies",
        "readers": "games/base.py _latch_shield_hold <- grant_shield <- handle_shield_seconds (first grant)",
    },
    ("game", "shield.empty_pulse_ms"): {
        "targeting": ["gameId", "experiment_id", "arm"],
        "experiment_rung": "applies",
        "readers": "games/base.py _latch_shield_hold <- grant_shield <- handle_shield_seconds (first grant)",
    },
    # Hold feedback of #2463: stack-only (no experiment arm), latched with the hold values.
    ("game", "shield.hold_rumble_max_intensity"): {
        "targeting": ["gameId"],
        "readers": "games/base.py _latch_shield_hold <- grant_shield <- handle_shield_seconds (first grant)",
    },
    ("game", "shield.hold_led_floor_percent"): {
        "targeting": ["gameId"],
        "readers": "games/base.py _latch_shield_hold <- grant_shield <- handle_shield_seconds (first grant)",
    },
    # Shield end grace of #2464 (UC-040 BR-034): stack-only and human-only, latched in
    # every mode (instant shields exist in every mode).
    ("game", "shield.end_grace_seconds"): {
        "targeting": ["gameId"],
        "readers": "games/base.py _latch_shield_end_grace <- _latch_shield_hold <- grant_shield (first grant)",
    },
    ("game", "shield.charge_rumble_intensity"): {
        "targeting": ["gameId"],
        "readers": "games/base.py _latch_shield_hold <- grant_shield <- handle_shield_seconds (first grant)",
    },
    ("game", "shield.charge_rumble_pulse_ms"): {
        "targeting": ["gameId"],
        "readers": "games/base.py _latch_shield_hold <- grant_shield <- handle_shield_seconds (first grant)",
    },
    ("game", "shield.ending_rumble_seconds"): {
        "targeting": ["gameId"],
        "readers": "games/base.py _latch_shield_hold <- grant_shield <- handle_shield_seconds (first grant)",
    },
    # Shield limits (UC-040 BR-022): operator flags the agent is to read with an empty
    # context too (#2044 agent half), so no targeting at all, or the two sides could differ.
    ("agent", "policy.max_shields_per_player_per_game"): {
        "targeting": [],
        "readers": "games/base.py _shield_limits <- grant_shield <- handle_shield_seconds (no targeting)",
    },
    ("agent", "policy.shield_cooldown_seconds"): {
        "targeting": [],
        "readers": "games/base.py _shield_limits <- grant_shield <- handle_shield_seconds (no targeting)",
    },
    ("game", "windows"): {
        "targeting": ["targetingKey", "experiment_id", "arm"],
        "experiment_rung": "applies",
        "readers": "difficulty_handlers.py handle_pacing_profile (targetingKey = the profile name)",
    },
    ("user", "agent_intervention_cue"): {
        "targeting": [],
        "readers": "interventions.py _cue_enabled <- _emit_agent_cue (no targeting)",
    },
}
