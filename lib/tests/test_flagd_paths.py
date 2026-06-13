"""Tests for the single-source-of-truth flag-dir accessor (issue #959)."""

import json

import pytest

from lib import flagd_paths


@pytest.fixture
def flag_dir(tmp_path):
    """A flag dir with a couple of plainly-named domain files."""
    (tmp_path / "game.json").write_text(json.dumps({"flags": {}}))
    (tmp_path / "interventions.json").write_text(json.dumps({"flags": {}}))
    return tmp_path


def test_flag_dir_default(monkeypatch):
    monkeypatch.delenv(flagd_paths.FLAG_DIR_ENV, raising=False)
    assert flagd_paths.flag_dir().name == "flagd"


def test_flag_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv(flagd_paths.FLAG_DIR_ENV, str(tmp_path))
    assert flagd_paths.flag_dir() == tmp_path.resolve()


def test_active_flag_file_is_plain_join(flag_dir):
    assert flagd_paths.active_flag_file("game", directory=flag_dir) == flag_dir / "game.json"
    assert flagd_paths.active_flag_file("interventions", directory=flag_dir) == flag_dir / "interventions.json"


def test_active_flag_file_reads_env(monkeypatch, flag_dir):
    monkeypatch.setenv(flagd_paths.FLAG_DIR_ENV, str(flag_dir))
    assert flagd_paths.active_flag_file("game") == flag_dir.resolve() / "game.json"
    assert flagd_paths.active_flag_file("interventions") == flag_dir.resolve() / "interventions.json"


def test_ci_dir_is_a_complete_set():
    """The CI flag dir must hold a plainly-named file for every active domain,
    including the three write-target domains (agent/interventions/rollout) that
    have no .ci.json overlay -- they must be REAL files, not symlinks to base, so
    a CI-time write never leaks into the base files."""
    ci_dir = flagd_paths.flag_dir() / "ci"
    for domain in (
        "system",
        "controller",
        "observability",
        "game",
        "user",
        "agent",
        "interventions",
        "rollout",
    ):
        path = flagd_paths.active_flag_file(domain, directory=ci_dir)
        assert path.is_file(), f"missing CI flag file: {path}"
        assert not path.is_symlink(), f"CI write target must be a real file, not a symlink: {path}"
