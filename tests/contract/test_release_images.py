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
        re.findall(
            r'^\s*maybe_retag "\$CH_[A-Z_]+"\s+([a-z0-9-]+)\s*$', text, re.MULTILINE
        )
    )


def _check_retag_source_images() -> set[str]:
    text = (ic.REPO_ROOT / "scripts/ci/check-retag-source.sh").read_text()
    return set(re.findall(r"^\s+([a-z0-9-]+)\) filter=\w+ ;;\s*$", text, re.MULTILINE))


@pytest.mark.parametrize("compose", ["docker-compose.yml", "docker-compose.lite.yml"])
def test_compose_images_are_released(compose):
    missing = _compose_images(compose) - NOT_RELEASED - _release_images()
    assert not missing, (
        f"{compose} images not in release.yml tag-images: {sorted(missing)}"
    )


def test_release_images_cover_main_compose():
    # python-hid and operator-pad are profile-gated but still released.
    assert _release_images() == _compose_images("docker-compose.yml") - NOT_RELEASED


def test_ci_retag_list_equals_release_list():
    assert _ci_retag_images() == _release_images()


def test_check_retag_source_knows_every_release_image():
    assert _check_retag_source_images() == _release_images()


def _ci_ch_output_keys() -> dict[str, str]:
    """CH_X env name -> paths-filter output key, from the matrices step's env."""
    text = (ic.REPO_ROOT / ".github/workflows/ci.yml").read_text()
    return dict(
        re.findall(
            r"^\s+(CH_[A-Z_]+): \$\{\{ steps\.f\.outputs\.(\w+) \}\}\s*$",
            text,
            re.MULTILINE,
        )
    )


def _ci_retag_pairs() -> dict[str, str]:
    """image -> CH_X variable used in ``maybe_retag``."""
    text = (ic.REPO_ROOT / ".github/workflows/ci.yml").read_text()
    return {
        image: ch
        for ch, image in re.findall(
            r'^\s*maybe_retag "\$(CH_[A-Z_]+)"\s+([a-z0-9-]+)\s*$', text, re.MULTILINE
        )
    }


def _check_retag_source_filters() -> dict[str, str]:
    text = (ic.REPO_ROOT / "scripts/ci/check-retag-source.sh").read_text()
    return dict(
        re.findall(r"^\s+([a-z0-9-]+)\) filter=(\w+) ;;\s*$", text, re.MULTILINE)
    )


def _ci_filter_keys() -> set[str]:
    wf = ic.load_yaml(ic.REPO_ROOT / ".github/workflows/ci.yml")
    for step in wf["jobs"]["changes"]["steps"]:
        if step.get("id") == "f":
            return set(ic.yaml.safe_load(step["with"]["filters"]))
    raise AssertionError("no paths-filter step with id 'f'")


def test_retag_ch_variable_matches_check_retag_source_filter():
    """maybe_retag's CH_X must map to the same filter key check-retag-source.sh
    uses for that image, and that key must exist in ci.yml's paths-filter."""
    ch_keys = _ci_ch_output_keys()
    filters = _check_retag_source_filters()
    filter_keys = _ci_filter_keys()
    for image, ch in _ci_retag_pairs().items():
        assert ch in ch_keys, f"{ch} (for {image}) has no env entry in ci.yml"
        assert ch_keys[ch] == filters[image], (
            f"{image}: maybe_retag uses {ch} -> {ch_keys[ch]!r} but "
            f"check-retag-source.sh uses filter {filters[image]!r}"
        )
        assert ch_keys[ch] in filter_keys, (
            f"{image}: filter {ch_keys[ch]!r} not in ci.yml paths-filter"
        )
