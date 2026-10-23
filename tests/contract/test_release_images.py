"""Every released image is tagged on release and retagged on main pushes (#2027 F1).

``release.yml`` retags ``IMAGE:<sha>`` to ``IMAGE:<version>``; ``ci.yml`` keeps a
``:<sha>`` tag on every image it did not rebuild; ``check-retag-source.sh`` must know
each retagged image. A service in ``docker-compose.yml`` missing from any of the three
(the agent was, until #2027) has no ``:<version>`` tag, so a Pi pinned to a release
cannot pull it.
"""

from __future__ import annotations

import re

import infra_config as ic
import pytest

PREFIX = "ghcr.io/watchmejoustmyflags/joustmania/"
# Compose images under PREFIX that CI deliberately does not release. None today.
NOT_RELEASED: set[str] = set()


def _compose_images(name: str) -> set[str]:
    services = ic.load_yaml(ic.REPO_ROOT / name)["services"]
    return {
        m.group(1)
        for svc in services.values()
        if (m := re.match(re.escape(PREFIX) + r"([a-z0-9-]+):", svc.get("image", "")))
    }


def _release_images() -> set[str]:
    wf = ic.load_yaml(ic.REPO_ROOT / ".github/workflows/release.yml")
    return set(wf["jobs"]["tag-images"]["strategy"]["matrix"]["image"])


def _ci_retag_images() -> set[str]:
    text = (ic.REPO_ROOT / ".github/workflows/ci.yml").read_text()
    return set(
        re.findall(r'^\s*maybe_retag "\$CH_[A-Z_]+"\s+([a-z0-9-]+)\s*$', text, re.M)
    )


def _check_retag_source_images() -> set[str]:
    text = (ic.REPO_ROOT / "scripts/ci/check-retag-source.sh").read_text()
    return set(re.findall(r"^\s+([a-z0-9-]+)\) filter=\w+ ;;\s*$", text, re.M))


@pytest.mark.parametrize("compose", ["docker-compose.yml", "docker-compose.lite.yml"])
def test_compose_images_are_released(compose):
    missing = _compose_images(compose) - NOT_RELEASED - _release_images()
    assert not missing, (
        f"{compose} images not in release.yml tag-images: {sorted(missing)}"
    )


def test_release_images_cover_main_compose():
    # python-hid is profile-gated but still released.
    assert _release_images() == _compose_images("docker-compose.yml") - NOT_RELEASED


def test_ci_retag_list_equals_release_list():
    assert _ci_retag_images() == _release_images()


def test_check_retag_source_knows_every_release_image():
    assert _check_retag_source_images() == _release_images()
