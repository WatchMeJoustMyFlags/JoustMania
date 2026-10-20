"""rust-hid's flagd provider syncs after a late flagd and follows flag-service writes (#1713).

rust-hid reads its flags (``grpc_rpc_spans``, observability set) through the
open-feature-flagd in-process resolver on flagd's sync port, selector
``flagSetId=observability``. The crate gives up on its first sync after 5 s, so
``services/rust-hid/src/feature_flags.rs`` builds the provider only once flagd
answers and retries otherwise.

This module starts the flag plane (real flag service + real flagd) and runs the Rust
test ``feature_flags::live_tests`` against it: flagd answers only after 7 s, then the
provider must serve the flag service's values, and five writes of
``flagservice.propagation_probe`` (the observability set's runtime-writable probe;
``grpc_rpc_spans`` itself is baseline-only) must each reach it within 1.5 s. Without
the env vars the Rust test returns at once, so ``cargo test`` stays hermetic.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.service.compositions import FLAG_PLANE

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = FLAG_PLANE

RUST_HID_DIR = Path(__file__).resolve().parents[2] / "services" / "rust-hid"
CARGO_TEST_TIMEOUT_SECONDS = 900  # includes the first build of rust-hid
TEST_NAME = "feature_flags::live_tests::live_flips_reach_the_provider_after_a_late_flagd"


@pytest.mark.uc("UC-035", flow="main")
def test_rust_hid_provider_syncs_after_late_flagd_and_follows_writes(service_stack):
    cargo = shutil.which("cargo")
    assert cargo, "cargo is required for the service tier (rust-hid live flag test)"
    flag_service = (
        f"http://{service_stack.get_service_host('flag-service', 8070)}:"
        f"{service_stack.get_service_port('flag-service', 8070)}"
    )
    env = {
        **os.environ,
        "RUST_HID_LIVE_FLAGD_PORT": str(service_stack.get_service_port("flagd", 8015)),
        "RUST_HID_LIVE_FLAG_SERVICE_URL": flag_service,
    }
    result = subprocess.run(
        [cargo, "test", "--bin", "rust-hid", TEST_NAME, "--", "--exact", "--nocapture"],
        cwd=RUST_HID_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=CARGO_TEST_TIMEOUT_SECONDS,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"cargo test {TEST_NAME} failed:\n{output[-6000:]}"
    # The test returns early without the env: the marker proves it ran against the plane.
    assert "RUST_HID_LIVE_FLIP_RAN" in output, output[-3000:]
