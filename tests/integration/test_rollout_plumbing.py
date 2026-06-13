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

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

# All flagd-write waits here are condition polls via helpers.poll_until (#894:
# the old fixed settles became deadlines). Its generous default deadline and
# `rewrite` self-heal exist because one CI run (27028642150) showed a
# defaultVariant flip never resolving within 15s while EARLIER flips on the
# same file did — a missed/coalesced file-watch reload under load; re-writing
# the file mid-poll produces a fresh inotify event and self-heals that case.
from tests.integration.helpers import (  # noqa: E402
    active_flag_file,
    poll_until,
    wait_flag_file_restored,
)

# Active host rollout flag-file path via the single source of truth (#959):
# $FLAGD_FLAG_DIR/rollout.json — exactly the file flagd serves.
ROLLOUT_PATH = active_flag_file("rollout")


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

    with open(ROLLOUT_PATH) as fh:
        mutated = fh.read()

    def _restore():
        with open(ROLLOUT_PATH, "w") as fh:
            fh.write(backup)

    _restore()
    # Wait until flagd actually SERVES the restored baseline before the next
    # test mutates it (#894: condition poll instead of a fixed settle).
    wait_flag_file_restored(docker_compose, "rollout", backup, mutated, _restore)


# =============================================================================
# Live-flagd-RPC resolution against the rollout domain
# =============================================================================


def _rollout_client(docker_compose):
    """An OpenFeature flagd RPC client bound to the rollout flag set — the same
    flagd (port 8013) the controller-manager reads the rollout domain from."""
    from openfeature import api
    from openfeature.contrib.provider.flagd import FlagdProvider
    from openfeature.contrib.provider.flagd.config import CacheType, ResolverType

    host = docker_compose.get_service_host("flagd", 8013)
    port = int(docker_compose.get_service_port("flagd", 8013))

    provider = FlagdProvider(
        host=host,
        port=port,
        resolver_type=ResolverType.RPC,
        selector="flagSetId=rollout",
        # The RPC resolver's default LRU cache serves the FIRST resolution of a
        # flag forever if change-event invalidation doesn't arrive for the
        # selector'd flag set — observed in CI (run 27029212634): the file had
        # defaultVariant='one' while the client kept resolving the cached 0.
        # This test asserts live re-resolution, so the cache must be off.
        cache=CacheType.DISABLED,
    )
    api.set_provider(provider, domain="it_rollout")
    return api.get_client("it_rollout"), provider


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
    # No settle here: the first poll_until below IS the wait for these writes
    # to be served (with rewrite self-heal), so a fixed sleep is pure cost.

    client, provider = _rollout_client(docker_compose)
    try:
        empty = EvaluationContext()

        # strategy + target_backend resolve as written. Both polled: the two
        # writes are separate file versions, so flagd serving the strategy flip
        # does NOT prove it loaded the later target_backend write too.
        assert poll_until(
            lambda: client.get_string_value("strategy", "off", empty) == "progressive",
            rewrite=lambda: set_default_variant("strategy", "progressive"),
        ), f"strategy resolved to {client.get_string_value('strategy', 'off', empty)!r}"
        assert poll_until(
            lambda: client.get_string_value("target_backend", "python", empty) == "rust",
            rewrite=lambda: set_default_variant("target_backend", "rust"),
        ), "target_backend did not resolve to the written rust"

        # Walk the expansion ladder: each variant flip resolves to its integer.
        ladder = [("none", 0), ("one", 1), ("three", 3), ("six", 6), ("all", 99)]
        for variant, expected in ladder:
            set_controller_count_variant(variant)
            assert poll_until(
                lambda e=expected: client.get_integer_value("current_controller_count", -1, empty) == e,
                rewrite=lambda v=variant: set_controller_count_variant(v),
            ), (
                f"current_controller_count={variant!r} resolved to "
                f"{client.get_integer_value('current_controller_count', -1, empty)}, want {expected}; "
                f"file defaultVariant="
                f"{_load()['flags']['current_controller_count']['defaultVariant']!r}"
            )

        # Rollback shape: the loop resets current_controller_count to "none" (0) on
        # an allowed rollback — assert that exact flip lands too.
        set_controller_count_variant("none")
        assert poll_until(
            lambda: client.get_integer_value("current_controller_count", -1, empty) == 0,
            rewrite=lambda: set_controller_count_variant("none"),
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
