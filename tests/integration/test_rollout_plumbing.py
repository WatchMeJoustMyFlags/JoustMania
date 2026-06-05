"""
Rollout control-plane plumbing integration test (#737, M3 PR G).

Proves the CONTROL-PLANE half of the agent's remediation loop end-to-end: the
exact rollout.json mutation the agent's Go RolloutWriter performs (flip
``current_controller_count``'s defaultVariant along the stage ladder; set
``strategy=progressive`` + a ``target_backend``) is reloaded by the real flagd and
resolves to the expected values via the SAME flagd RPC the controller-manager
reads. This is the rollout-domain parallel to
``test_per_player_targeting_resolves_against_live_flagd`` in
test_intervention_flow.py.

Scope decision (documented in tests/integration/README.md, "Rollout plumbing"):

- The agent service (services/agent, Go) is NOT started in the integration
  compose — it sits behind the ``agent`` compose profile (docker-compose.ci.yml),
  exactly like the intervention ActionSink (test_intervention_flow.py scenario 6).
  So we cannot assert the agent itself emits ``agent.infrastructure.decision``
  spans here; that is covered by the Go narrative test
  (services/agent/decision/infra_narrative_test.go).
- The controller-manager runs in MOCK mode (backend=mock), so controllers route
  via the mock adapter (method="default"); the multiplexer rollout router — and
  thus ``controller_routing_decisions_total{method="rollout"}`` — is only reached
  with non-mock adapters (hardware profile), which CI does not run. The unstable
  adapter wraps python-hid and is likewise unreachable in CI.

What IS reachable, and what this test asserts, is that the agent's write SHAPE is
schema-valid and consumable by the live flagd that controller-manager reads:
write rollout.json as the RolloutWriter does, then resolve target_backend /
strategy / current_controller_count via flagd RPC. This catches schema/variant
regressions in the rollout write the Go unit tests (which never touch real flagd)
cannot.

rollout.json is a bind-mounted host file; the ``rollout_file`` fixture snapshots
and restores it byte-for-byte so mutations never leak (mirrors the ``flag_files``
fixture in test_intervention_flow.py).
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

# Repo-root-relative path to the bind-mounted flagd config dir (same dir the
# intervention-flow tests mutate).
_FLAGD_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../services/flagd"))
ROLLOUT_PATH = os.path.join(_FLAGD_DIR, "rollout.json")

# flagd debounces file-watch reloads; the RPC provider also needs a moment to
# reconnect and re-pull after a reload. Generous so this never flakes under CI
# load (#805 is a known timing flake on the game path; the rollout path is
# simpler but we stay conservative).
RELOAD_SETTLE_SECONDS = 2.0
RESOLVE_TIMEOUT_SECONDS = 15.0


# =============================================================================
# Rollout-file mutation helpers (mirror services/agent/actions/rollout_writer.go)
# =============================================================================


def _load() -> dict:
    with open(ROLLOUT_PATH) as fh:
        return json.load(fh)


def _dump_in_place(doc: dict) -> None:
    """In-place truncate+write — matches RolloutWriter.writeInPlace (no
    temp+rename, which would EBUSY on the flagd bind mount)."""
    text = json.dumps(doc, indent=2) + "\n"
    with open(ROLLOUT_PATH, "w") as fh:
        fh.write(text)


def set_controller_count_variant(variant: str) -> None:
    """Flip current_controller_count's defaultVariant to a named variant.

    This is EXACTLY what RolloutWriter.SetControllerCount does: it validates the
    variant already exists, then flips defaultVariant (the agent never invents
    variants). Raises if the variant is missing, just as the Go writer errors.
    """
    doc = _load()
    flag = doc["flags"]["current_controller_count"]
    assert variant in flag["variants"], (
        f"variant {variant!r} not in current_controller_count.variants "
        f"({sorted(flag['variants'])}) — agent never invents variants"
    )
    flag["defaultVariant"] = variant
    _dump_in_place(doc)


def set_default_variant(flag_key: str, variant: str) -> None:
    """Flip an arbitrary rollout flag's defaultVariant (strategy / target_backend)."""
    doc = _load()
    flag = doc["flags"][flag_key]
    assert variant in flag["variants"], (
        f"variant {variant!r} not in {flag_key}.variants ({sorted(flag['variants'])})"
    )
    flag["defaultVariant"] = variant
    _dump_in_place(doc)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def rollout_file(docker_compose):
    """Snapshot rollout.json; restore byte-for-byte on exit so no mutation leaks
    into the repo or later tests (mirrors test_intervention_flow.flag_files)."""
    with open(ROLLOUT_PATH) as fh:
        backup = fh.read()

    yield

    with open(ROLLOUT_PATH, "w") as fh:
        fh.write(backup)
    # Let flagd reload the restored baseline before the next test mutates it.
    time.sleep(RELOAD_SETTLE_SECONDS)


# =============================================================================
# Live-flagd-RPC resolution against the rollout domain
# =============================================================================


def _rollout_client(docker_compose):
    """An OpenFeature flagd RPC client bound to the rollout flag set — the same
    flagd (port 8013) the controller-manager reads the rollout domain from."""
    from openfeature import api
    from openfeature.contrib.provider.flagd import FlagdProvider
    from openfeature.contrib.provider.flagd.config import ResolverType

    host = docker_compose.get_service_host("flagd", 8013)
    port = int(docker_compose.get_service_port("flagd", 8013))

    provider = FlagdProvider(
        host=host,
        port=port,
        resolver_type=ResolverType.RPC,
        selector="flagSetId=rollout",
    )
    api.set_provider(provider, domain="it_rollout")
    return api.get_client("it_rollout"), provider


def _poll_until(predicate, timeout: float = RESOLVE_TIMEOUT_SECONDS) -> bool:
    """Poll predicate() until true or timeout (provider connect + flagd reload may
    lag the file write). Returns the final predicate value."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.3)
    return predicate()


@pytest.mark.asyncio
async def test_rollout_write_shape_resolves_via_live_flagd(rollout_file, docker_compose):
    """The agent's rollout write shape (strategy=progressive, target_backend flip,
    current_controller_count variant flip along the ladder) is schema-valid and
    resolves to the expected values via the live flagd RPC the controller-manager
    reads.

    This walks the same none -> one -> three -> six -> all ladder the
    RolloutWriter/loop walks, asserting each flip resolves to the right integer —
    catching any drift between the agent's variant names and the shipped schema.
    """
    from openfeature.evaluation_context import EvaluationContext

    # Activate a progressive rollout toward the rust backend, the way the agent's
    # operator-intent + loop would have the file look at the start of an episode.
    set_default_variant("strategy", "progressive")
    set_default_variant("target_backend", "rust")
    time.sleep(RELOAD_SETTLE_SECONDS)

    client, provider = _rollout_client(docker_compose)
    try:
        empty = EvaluationContext()

        # strategy + target_backend resolve as written.
        assert _poll_until(
            lambda: client.get_string_value("strategy", "off", empty) == "progressive"
        ), f"strategy resolved to {client.get_string_value('strategy', 'off', empty)!r}"
        assert client.get_string_value("target_backend", "python", empty) == "rust", (
            "target_backend did not resolve to the written rust"
        )

        # Walk the expansion ladder: each variant flip resolves to its integer.
        ladder = [("none", 0), ("one", 1), ("three", 3), ("six", 6), ("all", 99)]
        for variant, expected in ladder:
            set_controller_count_variant(variant)
            assert _poll_until(
                lambda e=expected: client.get_integer_value("current_controller_count", -1, empty) == e
            ), (
                f"current_controller_count={variant!r} resolved to "
                f"{client.get_integer_value('current_controller_count', -1, empty)}, want {expected}"
            )

        # Rollback shape: the loop resets current_controller_count to "none" (0) on
        # an allowed rollback — assert that exact flip lands too.
        set_controller_count_variant("none")
        assert _poll_until(
            lambda: client.get_integer_value("current_controller_count", -1, empty) == 0
        ), "rollback-to-none did not resolve to 0"
    finally:
        provider.shutdown()


def test_remediation_allowed_flag_is_resolvable_shape(rollout_file, docker_compose):
    """The remediation_allowed gate the agent reads (fail-closed) exists in the
    rollout schema with both boolean variants, and flips resolve. The agent's
    RemediationReader resolves this same flag's defaultVariant from rollout.json;
    this asserts the schema the reader depends on is present and well-formed.

    Synchronous (no live RPC needed): we assert the schema shape + that a flip
    round-trips through the file, which is the property the Go RemediationReader
    unit tests rely on being true in the shipped schema.
    """
    doc = _load()
    flag = doc["flags"]["remediation_allowed"]
    assert set(flag["variants"]) == {"on", "off"}, (
        f"remediation_allowed variants drifted: {sorted(flag['variants'])}"
    )
    assert flag["variants"]["on"] is True and flag["variants"]["off"] is False
    # Default ships OFF (fail-closed): the agent only auto-rolls-back when an
    # operator explicitly flips this on.
    assert flag["defaultVariant"] == "off", (
        "remediation_allowed must ship default OFF (fail-closed)"
    )

    # A flip round-trips through the in-place write the agent/operator would use.
    set_default_variant("remediation_allowed", "on")
    assert _load()["flags"]["remediation_allowed"]["defaultVariant"] == "on"
