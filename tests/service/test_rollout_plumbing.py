"""
Rollout control-plane plumbing integration test (#737, M3 PR G; UC-053 A1).

Proves the CONTROL-PLANE half of the agent's remediation loop end-to-end: the
writes the agent's Go RolloutWriter sends to the flag service
(``PUT /rollout/current_controller_count`` along the stage ladder; plus the
operator intent ``strategy=progressive`` and a ``target_backend``) reach the
real flagd over the gRPC sync source and resolve to the expected values via the
SAME flagd RPC the controller-manager reads.

Scope decision (tests/integration/README.md, "Rollout plumbing"):

- The agent service (services/agent, Go) is NOT started in the integration
  compose — it sits behind the ``agent`` compose profile. So we cannot assert the
  agent itself emits ``agent.infrastructure.decision`` spans here; that is
  covered by the Go narrative test (services/agent/decision/infra_narrative_test.go).
- The controller-manager runs in MOCK mode (backend=mock), so the multiplexer
  rollout router — and thus ``controller_routing_decisions_total{method="rollout"}``
  — is only reached with non-mock adapters (hardware profile), which CI does not run.

The ``flag_service`` fixture clears every overlay this test sets.
"""

import pytest

from tests.integration.helpers import poll_until
from tests.service.compositions import FLAG_PLANE

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = FLAG_PLANE

ROLLOUT = "rollout"


def _rollout_client(stack):
    """An OpenFeature flagd RPC client bound to the rollout flag set — the same
    flagd (port 8013) the controller-manager reads the rollout domain from."""
    from openfeature import api
    from openfeature.contrib.provider.flagd import FlagdProvider
    from openfeature.contrib.provider.flagd.config import CacheType, ResolverType

    from tests.contract.flagd_rpc_retry import retry_decode_failures

    provider = FlagdProvider(
        host=stack.get_service_host("flagd", 8013),
        port=int(stack.get_service_port("flagd", 8013)),
        resolver_type=ResolverType.RPC,
        selector="flagSetId=rollout",
        # Live re-resolution is the point: no RPC cache (CI run 27029212634).
        cache=CacheType.DISABLED,
    )
    # Rare unparsable ResolveInt response on CI (#2184): flagd_rpc_retry.
    retry_decode_failures(provider)
    api.set_provider(provider, domain="it_rollout")
    return api.get_client("it_rollout"), provider


@pytest.mark.uc("UC-053", flow="A1", br=["BR-001"])
@pytest.mark.asyncio
@pytest.mark.usefixtures("fresh_stack")
async def test_rollout_write_shape_resolves_via_live_flagd(flag_service, stack):
    """The agent's rollout writes (strategy=progressive, target_backend, and the
    current_controller_count stage ladder) resolve via the live flagd RPC the
    controller-manager reads, walking none -> one -> three -> six -> all and the
    rollback to none.
    """
    from openfeature.evaluation_context import EvaluationContext

    # Operator intent at the start of an episode.
    flag_service.put(ROLLOUT, "strategy", variant="progressive")
    flag_service.put(ROLLOUT, "target_backend", variant="rust")

    client, provider = _rollout_client(stack)
    try:
        empty = EvaluationContext()
        assert poll_until(lambda: client.get_string_value("strategy", "off", empty) == "progressive"), (
            f"strategy resolved to {client.get_string_value('strategy', 'off', empty)!r}"
        )
        assert poll_until(lambda: client.get_string_value("target_backend", "python", empty) == "rust"), (
            "target_backend did not resolve to the written rust"
        )

        # Walk the expansion ladder: each stage write resolves to its integer.
        for variant, expected in [
            ("none", 0),
            ("one", 1),
            ("three", 3),
            ("six", 6),
            ("all", 99),
        ]:
            flag_service.put(ROLLOUT, "current_controller_count", variant=variant, writer="agent")
            assert poll_until(
                lambda e=expected: client.get_integer_value("current_controller_count", -1, empty) == e
            ), (
                f"current_controller_count={variant!r} resolved to "
                f"{client.get_integer_value('current_controller_count', -1, empty)}, want {expected}"
            )

        # Rollback shape: the loop resets current_controller_count to "none" (0).
        flag_service.put(ROLLOUT, "current_controller_count", variant="none", writer="agent")
        assert poll_until(lambda: client.get_integer_value("current_controller_count", -1, empty) == 0), (
            "rollback-to-none did not resolve to 0"
        )
        # An unknown stage is rejected by the registry, not written (UC-053 A5).
        status, body = flag_service.request("PUT", f"/{ROLLOUT}/current_controller_count", {"variant": "seven"})
        assert (status, body.get("error")) == (422, "out_of_bounds"), (status, body)
    finally:
        provider.shutdown()
