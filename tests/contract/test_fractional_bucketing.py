"""Pinned ``fractional`` bucketing (#1945).

flagd v0.18 (core v0.19) and the Python provider 0.6.0 changed how
``fractional`` hashes: murmur3 over the canonical CBOR of
``[flagKey, targetingKey]`` instead of over the string ``flagKey + targetingKey``.
Every serial in a ``bluetooth_backend`` canary fraction overlay (UC-052 step 2,
the only fraction overlay, flags.md) can land in the other bucket after such a
change, and every flagd component must hash the same way or the runtimes
disagree about one serial.

``fractional_buckets.json`` pins the bucket of 16 serials. The same table is
checked against:

- the Python provider's own evaluation (file resolver, no docker): the code the
  controller manager and the pairing daemon evaluate with (in-process);
- the real flagd image, on the RPC and the in-process resolver (docker; skips
  locally without docker, required in CI);
- the Go flagd core the agent links: services/agent/flags/fractional_bucketing_test.go.

A failure here means an upgrade re-buckets the canary serials: move every
component together and document the one-time re-bucketing (flags.md, §13).
"""

from __future__ import annotations

import json
import os
import uuid

import flagd_harness as fh
import infra_config as ic
import pytest

FIXTURE = ic.REPO_ROOT / "tests" / "contract" / "fractional_buckets.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def _require(available: bool, what: str) -> None:
    if available:
        return
    if os.environ.get("CI"):
        pytest.fail(f"{what} is required in CI for the flagd contract tier")
    pytest.skip(f"{what} not available")


try:
    from openfeature import api
    from openfeature.evaluation_context import EvaluationContext

    _HAVE_OPENFEATURE = True
except ImportError:  # pragma: no cover - exercised only without the extra deps
    _HAVE_OPENFEATURE = False


def _flag_doc(fixture: dict) -> dict:
    return {
        "flags": {
            fixture["flag_key"]: {
                "state": "ENABLED",
                "variants": fixture["variants"],
                "defaultVariant": fixture["default_variant"],
                "targeting": fixture["targeting"],
            }
        }
    }


def _resolved(client, flag_key: str, serials) -> dict[str, str]:
    return {
        serial: client.get_string_value(
            flag_key, "", EvaluationContext(targeting_key=serial)
        )
        for serial in serials
    }


def test_fixture_is_a_balanced_split():
    """The table pins both buckets, so any hash change flips some serials."""
    fixture = _fixture()
    assert fixture["targeting"] == {"fractional": [["python", 50], ["rust", 50]]}, (
        "keep the render.go shape: [[canary, N], [default, 100 - N]]"
    )
    values = list(fixture["cases"].values())
    assert values.count("python") >= 4 and values.count("rust") >= 4, values


@pytest.mark.uc("UC-052", flow="main")
def test_python_provider_buckets_match_the_pinned_table(tmp_path):
    """The Python provider (in-process evaluation, as lib/feature_flags reads)
    puts every pinned serial in its pinned bucket."""
    _require(
        _HAVE_OPENFEATURE, "openfeature-provider-flagd (make test-contract installs it)"
    )
    from openfeature.contrib.provider.flagd import FlagdProvider
    from openfeature.contrib.provider.flagd.config import ResolverType

    fixture = _fixture()
    path = tmp_path / "flags.json"
    path.write_text(json.dumps(_flag_doc(fixture)))
    provider = FlagdProvider(
        resolver_type=ResolverType.FILE, offline_flag_source_path=str(path)
    )
    domain = f"contract_fractional_{uuid.uuid4().hex[:6]}"
    api.set_provider(provider, domain=domain)
    try:
        client = api.get_client(domain)
        serials = list(fixture["cases"])
        assert fh.poll_until(
            lambda: all(_resolved(client, fixture["flag_key"], serials).values()),
            timeout=10,
        ), "file resolver never served the flag"
        assert _resolved(client, fixture["flag_key"], serials) == fixture["cases"]
    finally:
        provider.shutdown()


@pytest.fixture(scope="module")
def flagd(tmp_path_factory):
    """One flagd container for the module, on a temp copy of services/flagd/ci."""
    _require(
        _HAVE_OPENFEATURE, "openfeature-provider-flagd (make test-contract installs it)"
    )
    _require(ic.docker_available(), "docker")
    flag_dir = fh.copy_ci_flag_dir(tmp_path_factory.mktemp("flagd") / "ci")
    container = fh.FlagdContainer(flag_dir)
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.mark.uc("UC-052", flow="main")
@pytest.mark.parametrize("resolver", ["rpc", "in_process"])
def test_flagd_image_buckets_match_the_pinned_table(flagd, resolver):
    """The flagd image docker-compose.yml runs (RPC: flagd evaluates; in-process:
    the provider evaluates flagd's sync payload) agrees with the pinned table,
    with the targeting set on the real ``controller`` flag set."""
    fixture = _fixture()
    flag_key = fixture["flag_key"]
    path = flagd.file("controller")
    original = fh.load(path)
    doc = json.loads(json.dumps(original))
    doc["flags"][flag_key]["targeting"] = fixture["targeting"]
    assert set(fixture["variants"]) <= set(doc["flags"][flag_key]["variants"])
    fh.dump_in_place(path, doc)
    serials = list(fixture["cases"])
    try:
        with flagd.client("controller", resolver=resolver) as client:
            served = fh.poll_until(
                lambda: _resolved(client, flag_key, serials) == fixture["cases"],
                rewrite=lambda: fh.dump_in_place(path, doc),
            )
            assert served, (
                f"{resolver}: {_resolved(client, flag_key, serials)} != pinned "
                f"{fixture['cases']}\n{flagd.logs()}"
            )
    finally:
        fh.dump_in_place(path, original)
