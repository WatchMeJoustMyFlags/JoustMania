"""scripts/tools/gen_voice.py: the voice catalog generator (#2107, UC-031 BR-007, C-050).

The generator runs with the fake engine here: no model, no network, no ffmpeg.
The real Piper path is checked by `make voice-sample` (duration and loudness
within a tolerance), because Piper output is not bit-reproducible.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys

import pytest
import telemetry_inventory as ti

sys.path.insert(0, str(ti.REPO_ROOT / "scripts" / "tools"))
import gen_voice as gv  # noqa: E402

pytestmark = pytest.mark.uc("UC-031", br=["BR-007"])

CATALOG = {
    "version": 1,
    "loudness": {"target_lufs": -20.4, "tolerance_lu": 1.5},
    "audio": {"sample_rate": 44100, "channels": 2, "vorbis_quality": 4},
    "profiles": {
        "gen_male": {
            "partner": "gen_female",
            "model": "m1",
            "model_url": "https://example.invalid/m1.tar.bz2",
            "model_sha256": "a" * 64,
            "speaker": 0,
            "length_scale": 1.0,
            "noise_scale": 0.333,
            "noise_scale_w": 0.333,
        },
        "gen_female": {
            "partner": "gen_male",
            "model": "m2",
            "model_url": "https://example.invalid/m2.tar.bz2",
            "model_sha256": "b" * 64,
            "speaker": 0,
            "length_scale": 1.0,
            "noise_scale": 0.333,
            "noise_scale_w": 0.333,
        },
    },
    "areas": {
        "Menu": [
            {
                "id": "high",
                "sound": "MENU_VOX_SENSITIVITY_HIGH",
                "text": "High sensitivity.",
            },
            {"id": "label_sensitivity", "text": "Sensitivity"},
            {"id": "backend_jev", "text": "Jev", "say": "Jeff"},
        ]
    },
}


def _catalog():
    return copy.deepcopy(CATALOG)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def assets(tmp_path):
    """An asset tree with one sound effect the generator must never touch."""
    root = tmp_path / "assets"
    d = root / "Zombie" / "sounds"
    d.mkdir(parents=True)
    (d / "zombie_death.ogg").write_bytes(b"growl")
    return root


def _generate(catalog, assets, **kw):
    return gv.generate(
        catalog, assets, gv.FakeRenderer(catalog), log=lambda *_: None, **kw
    )


def test_generates_every_line_for_every_generated_profile(assets):
    result = _generate(_catalog(), assets)
    assert result.rendered == 6
    for profile in ("gen_male", "gen_female"):
        for line_id in ("high", "label_sensitivity", "backend_jev"):
            assert (assets / "Menu" / "vox" / profile / f"{line_id}.ogg").is_file()
    manifest = json.loads((assets / gv.MANIFEST_NAME).read_text())
    assert "recorded" not in manifest
    assert manifest["profiles"]["gen_male"]["partner"] == "gen_female"
    assert manifest["profiles"]["gen_female"]["partner"] == "gen_male"
    entry = manifest["files"]["Menu/vox/gen_female/high.ogg"]
    assert entry["line"] == "Menu/high"
    assert entry["sha256"] == _sha(assets / "Menu/vox/gen_female/high.ogg")
    assert gv.check(_catalog(), assets) == []


def test_second_run_renders_nothing(assets):
    _generate(_catalog(), assets)
    before = (assets / gv.MANIFEST_NAME).read_bytes()
    result = _generate(_catalog(), assets)
    assert result.rendered == 0
    assert (assets / gv.MANIFEST_NAME).read_bytes() == before


def test_say_change_regenerates_only_that_line(assets):
    _generate(_catalog(), assets)
    catalog = _catalog()
    catalog["areas"]["Menu"][2]["say"] = "Jev"
    assert sorted(gv.check(catalog, assets))  # stale until regenerated
    result = _generate(catalog, assets)
    assert result.rendered == 2
    assert sorted(result.paths) == [
        "Menu/vox/gen_female/backend_jev.ogg",
        "Menu/vox/gen_male/backend_jev.ogg",
    ]
    assert gv.check(catalog, assets) == []


def test_profile_parameter_change_regenerates_that_profile(assets):
    _generate(_catalog(), assets)
    catalog = _catalog()
    catalog["profiles"]["gen_male"]["length_scale"] = 1.1
    result = _generate(catalog, assets)
    assert result.rendered == 3
    assert all("/gen_male/" in p for p in result.paths)


def test_manifest_is_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _generate(_catalog(), a)
    _generate(_catalog(), b)
    assert (a / gv.MANIFEST_NAME).read_bytes() == (b / gv.MANIFEST_NAME).read_bytes()


def test_writes_only_under_a_generated_profile(assets):
    others = {p: _sha(p) for p in assets.rglob("*.ogg")}
    _generate(_catalog(), assets)
    assert {p: _sha(p) for p in others} == others
    for rel in (
        "Zombie/sounds/zombie_death.ogg",
        "Menu/vox/aaron/high.ogg",
        "Menu/vox/gen_male/../aaron/high.ogg",
    ):
        with pytest.raises(gv.CatalogError):
            gv.assert_generated_path(_catalog(), rel)


@pytest.mark.parametrize("partner", ["gen_male", "ivy", "bob"])
def test_partner_must_be_another_generated_profile(partner):
    """#2107 part 3: the partners are the generated profiles; itself or a deleted recording is refused."""
    catalog = _catalog()
    catalog["profiles"]["gen_male"]["partner"] = partner
    assert any("partner" in e for e in gv.validate_catalog(catalog))


def test_the_recorded_keys_are_gone():
    """No `recorded` list and no `recorded` line flag since the recordings were deleted (#2107 part 3)."""
    catalog = _catalog()
    catalog["recorded"] = ["aaron", "ivy"]
    assert any("unknown key 'recorded'" in e for e in gv.validate_catalog(catalog))
    catalog = _catalog()
    catalog["areas"]["Menu"][0]["recorded"] = True
    assert any("unknown key 'recorded'" in e for e in gv.validate_catalog(catalog))


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (
            lambda c: c["areas"]["Menu"].append({"id": "high", "text": "again"}),
            "duplicate",
        ),
        (lambda c: c["areas"]["Menu"].append({"id": "a/b", "text": "x"}), "id"),
        (lambda c: c["areas"]["Menu"].append({"id": "x", "text": ""}), "text"),
        (
            lambda c: c["areas"]["Menu"].append(
                {"id": "x", "text": "x", "colour": "red"}
            ),
            "unknown key",
        ),
        (lambda c: c["profiles"]["gen_male"].update(partner="bob"), "partner"),
        (lambda c: c["profiles"]["gen_male"].pop("model_sha256"), "model_sha256"),
    ],
)
def test_invalid_catalog_is_reported(mutate, needle):
    catalog = _catalog()
    mutate(catalog)
    errors = gv.validate_catalog(catalog)
    assert any(needle in e for e in errors), errors


def test_check_reports_drift(assets):
    _generate(_catalog(), assets)
    assert gv.check(_catalog(), assets) == []

    stray = assets / "Menu/vox/gen_male/hand_made.ogg"
    stray.write_bytes(b"x")
    assert any("hand_made.ogg" in p for p in gv.check(_catalog(), assets))
    stray.unlink()

    edited = assets / "Menu/vox/gen_female/high.ogg"
    original = edited.read_bytes()
    edited.write_bytes(original + b"!")
    assert any("sha256" in p for p in gv.check(_catalog(), assets))
    edited.write_bytes(original)

    edited.unlink()
    assert any("missing" in p for p in gv.check(_catalog(), assets))
    edited.write_bytes(original)

    catalog = _catalog()
    catalog["areas"]["Menu"][1]["text"] = "Sensitivity level"
    assert any("stale" in p for p in gv.check(catalog, assets))

    catalog = _catalog()
    catalog["areas"]["Menu"].append({"id": "status_maximum", "text": "Maximum."})
    assert any("status_maximum" in p for p in gv.check(catalog, assets))


def test_check_without_manifest_fails(assets):
    assert any("manifest" in p for p in gv.check(_catalog(), assets))


def test_text_edit_without_say_change_is_drift(assets):
    """backend_jev has `say`: editing only its text must still fail the check (review F1)."""
    _generate(_catalog(), assets)
    catalog = _catalog()
    catalog["areas"]["Menu"][2]["text"] = "Jev backend"
    problems = gv.check(catalog, assets)
    assert any("backend_jev" in p and "text" in p for p in problems), problems
    result = _generate(catalog, assets)
    assert result.rendered == 0  # the spoken audio is unchanged
    assert result.refreshed == 2
    assert gv.check(catalog, assets) == []


def test_a_manifest_that_still_lists_recorded_profiles_is_stale(assets):
    _generate(_catalog(), assets)
    path = assets / gv.MANIFEST_NAME
    manifest = json.loads(path.read_text())
    path.write_text(json.dumps({**manifest, "recorded": ["aaron", "ivy"]}))
    assert any("recorded" in p for p in gv.check(_catalog(), assets))


def test_stray_file_in_an_area_outside_the_catalog_is_reported(assets):
    """A gen_* directory of an area the catalog does not list yet still counts (review F3)."""
    _generate(_catalog(), assets)
    stray = assets / "Joust/vox/gen_male/congratulations.ogg"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"x")
    assert any(
        "Joust/vox/gen_male/congratulations.ogg" in p
        for p in gv.check(_catalog(), assets)
    )
    _generate(_catalog(), assets)
    assert not stray.exists()


def test_removed_line_is_pruned(assets):
    _generate(_catalog(), assets)
    catalog = _catalog()
    catalog["areas"]["Menu"].pop()  # backend_jev
    _generate(catalog, assets)
    assert not (assets / "Menu/vox/gen_male/backend_jev.ogg").exists()
    assert gv.check(catalog, assets) == []


def test_only_renders_the_named_line(assets):
    result = _generate(_catalog(), assets, only={"label_sensitivity"})
    assert sorted(result.paths) == [
        "Menu/vox/gen_female/label_sensitivity.ogg",
        "Menu/vox/gen_male/label_sensitivity.ogg",
    ]


def test_dry_run_writes_nothing(assets):
    result = _generate(_catalog(), assets, dry_run=True)
    assert result.rendered == 6
    assert not (assets / gv.MANIFEST_NAME).exists()
    assert not (assets / "Menu/vox/gen_male").exists()


def test_repository_voice_assets_are_fresh():
    """`make voice-check` on the committed catalog, manifest and files."""
    catalog = gv.load_catalog(gv.CATALOG)
    assert gv.check(catalog, gv.ASSETS) == []
