"""UC-053: per-player intervention targeting on live flagd (moved from test_intervention_flow, #1561).

Flag plane only: the flag service renders the per-(game, serial) targeting and
flagd evaluates it; no game service is involved.
"""

import pytest
from openfeature.evaluation_context import EvaluationContext

from tests.integration.flag_service import set_player_value
from tests.integration.helpers import flagd_probe_client, poll_until
from tests.service.compositions import FLAG_PLANE

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = FLAG_PLANE

APPLY_TIMEOUT_SECONDS = 12.0


@pytest.mark.uc("UC-053", flow="main", br=["BR-003"])
@pytest.mark.asyncio
async def test_per_player_targeting_resolves_against_live_flagd(flag_service, stack):
    """Per-player values written through the flag service resolve on LIVE flagd
    RPC for their (game, serial) only: other serials and other games get the
    neutral default. Catches targeting the service renders but flagd rejects.
    """
    game_a, game_b = "game_itpertarget0a", "game_itpertarget0b"
    serial_a, serial_b, unknown = "SER_A", "SER_B", "SER_UNKNOWN"
    for game_id in (game_a, game_b):
        status, body = flag_service.request(
            "PUT",
            f"/games/{game_id}",
            {
                "coordinator_instance": "integration/targeting",
                "game_kind": "shadow",
                "serials": [serial_a, serial_b],
            },
        )
        assert status == 200, body
    try:
        set_player_value("player_sensitivity_factor", game_a, serial_a, 1.5)
        set_player_value("player_sensitivity_factor", game_a, serial_b, 0.7)

        # resolver="rpc" deliberately: flagd's server-side evaluation of the
        # rendered targeting (in-process would evaluate it client-side).
        with flagd_probe_client(stack, "interventions", resolver="rpc") as client:

            def _eval(game_id: str, serial: str) -> float:
                return client.get_float_value(
                    "player_sensitivity_factor",
                    1.0,
                    EvaluationContext(targeting_key=serial, attributes={"gameId": game_id}),
                )

            assert poll_until(
                lambda: abs(_eval(game_a, serial_a) - 1.5) <= 0.01 and abs(_eval(game_a, serial_b) - 0.7) <= 0.01,
                APPLY_TIMEOUT_SECONDS,
            ), f"game_a: serial_a={_eval(game_a, serial_a)} (want 1.5), serial_b={_eval(game_a, serial_b)} (want 0.7)"
            assert abs(_eval(game_a, unknown) - 1.0) <= 0.01, "an unknown serial must resolve the default"
            assert abs(_eval(game_b, serial_a) - 1.0) <= 0.01, "another game must resolve the default"
    finally:
        for game_id in (game_a, game_b):
            flag_service.request("DELETE", f"/games/{game_id}")
