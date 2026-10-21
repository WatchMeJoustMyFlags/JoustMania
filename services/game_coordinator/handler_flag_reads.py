# Flags an intervention handler reads, and the flagd targeting variables each may use (#1872).
#
# Pure literal, no imports: tests/contract/test_flagd_resolution.py (a venv without the
# coordinator's dependencies) reads it with ast.literal_eval.
#
# A handler (and a revert handler and the agent cue) runs on the game's loop through
# GameLoopDispatcher.run, which copies the contextvars of the servicer's MAIN loop. A
# flag read there therefore sees the API-level attributes (service_name,
# service_namespace, language, environment, hostname, game_kind=real) plus whatever the
# read passes itself, and NOT the game's transaction context (targetingKey, game_mode,
# controller_count, sensitivity, experiment_id, arm; flags.md section 1.2). Hence:
#
# - "targeting": the only variables the flagd targeting of that flag may reference
#   (always allowed on top: the API-level attributes above and $flagd.*). The contract
#   test fails the build when game.json or ci/game.json uses any other variable.
# - "experiment_rung_inert": the flag is in the experiment catalog (registry.yaml scope
#   experiment+arm), but the rung needs game_kind != real, which a handler read never
#   has, so an experiment value never reaches this read.
# - "readers": where the read is made.
#
# tests/test_handler_flag_reads.py drives every registered handler and fails when one
# reads a flag that is not listed here, or when a handler has no census case.
# tempo_schedule_mode is read on the flagd event thread (tempo_schedule.py), not in a
# handler, so it is not listed.
HANDLER_FLAG_READS = {
    ("game", "shield.activation"): {
        "targeting": ["gameId"],
        "readers": "games/base.py _shield_activation_mode <- grant_shield <- handle_shield_seconds",
    },
    ("game", "shield.charge_expiry_seconds"): {
        "targeting": ["gameId"],
        "readers": "games/base.py _read_charge_expiry <- grant_shield <- handle_shield_seconds",
    },
    ("game", "windows"): {
        "targeting": ["targetingKey"],
        "experiment_rung_inert": True,
        "readers": "difficulty_handlers.py handle_pacing_profile (targetingKey = the profile name)",
    },
    ("user", "agent_intervention_cue"): {
        "targeting": [],
        "readers": "interventions.py _cue_enabled <- _emit_agent_cue (no targeting)",
    },
}
