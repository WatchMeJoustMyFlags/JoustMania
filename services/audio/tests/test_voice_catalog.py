"""The voice catalog against the Sound enum and the asset tree (#2107, UC-031 BR-007, BR-003).

Phase 1 covers the Menu area: every Menu voice id has a catalog line and every
catalog line has a file in every generated profile. The game announcer areas
join the catalog in phase 2 (#2107).
"""

import json
from pathlib import Path

import miniaudio
import pytest
import yaml

from lib.types import Sound

ROOT = Path(__file__).resolve().parents[3]
ASSETS = ROOT / "services" / "audio" / "assets"
CATALOG_PATH = ROOT / "services" / "audio" / "voice" / "lines.yaml"
MANIFEST_PATH = ASSETS / "voice_manifest.json"

# Recorded asymmetry today: only ivy recorded the Fight Club instructions.
RECORDED_ONLY_IN = {"Menu/Fightclub-instructions": {"ivy"}}
# Stems that already exist in more than one area (resolved by area priority).
KNOWN_STEM_COLLISIONS = {"1 minute", "game_over"}
AREAS = ("Joust", "Menu", "Zombie", "Fight_Club", "Commander")

pytestmark = pytest.mark.uc("UC-031", br=["BR-007"])


@pytest.fixture(scope="module")
def catalog():
    return yaml.safe_load(CATALOG_PATH.read_text())


@pytest.fixture(scope="module")
def manifest():
    return json.loads(MANIFEST_PATH.read_text())


def _menu_lines(catalog):
    return catalog["areas"]["Menu"]


def test_every_menu_voice_sound_has_a_catalog_line(catalog):
    """Both ways for the Menu area; __members__ so enum aliases count too."""
    ids = {line["id"] for line in _menu_lines(catalog)}
    menu_vox = {name: member.value for name, member in Sound.__members__.items() if name.startswith("MENU_VOX_")}
    missing = {name: value for name, value in menu_vox.items() if value not in ids}
    assert not missing, f"Sound members without a catalog line: {missing}"


def test_every_named_sound_is_a_real_member_with_the_line_id(catalog):
    for line in _menu_lines(catalog):
        if "sound" not in line:
            continue
        member = Sound.__members__.get(line["sound"])
        assert member is not None, f"{line['id']}: unknown Sound member {line['sound']}"
        assert member.value == line["id"], f"{line['sound']} is {member.value!r}, not {line['id']!r}"


def test_recorded_lines_match_the_recordings(catalog):
    """A line is `recorded` exactly when the recordings exist (BR-003)."""
    stems = {}
    for voice in catalog["recorded"]:
        for f in (ASSETS / "Menu" / "vox" / voice).glob("*.ogg"):
            stems.setdefault(f.stem, set()).add(voice)
    recorded = {line["id"] for line in _menu_lines(catalog) if line.get("recorded")}
    assert recorded == set(stems), f"catalog-only: {recorded - set(stems)}, recording-only: {set(stems) - recorded}"
    for stem, voices in stems.items():
        expected = RECORDED_ONLY_IN.get(f"Menu/{stem}", set(catalog["recorded"]))
        assert voices == expected, f"{stem}: recorded in {sorted(voices)}"


def test_new_ids_do_not_collide_with_other_areas(catalog):
    """A generated-only id that shadows another area's stem would play the wrong file."""
    other = {}
    for area in AREAS:
        if area == "Menu":
            continue
        for f in (ASSETS / area).rglob("*.ogg"):
            other.setdefault(f.stem, area)
    clashes = {
        line["id"]: other[line["id"]]
        for line in _menu_lines(catalog)
        if line["id"] in other and line["id"] not in KNOWN_STEM_COLLISIONS
    }
    assert not clashes, f"catalog ids already used in other areas: {clashes}"


def test_every_line_has_a_file_in_every_generated_profile(catalog, manifest):
    assert set(manifest["profiles"]) == set(catalog["profiles"])
    for profile in catalog["profiles"]:
        directory = ASSETS / "Menu" / "vox" / profile
        on_disk = {f.name for f in directory.iterdir()}
        expected = {f"{line['id']}.ogg" for line in _menu_lines(catalog)}
        assert on_disk == expected, f"{profile}: missing {expected - on_disk}, stray {on_disk - expected}"


def test_generated_files_decode_and_match_the_manifest(catalog, manifest):
    """Each file decodes at the recordings' format; duration and loudness are in range."""
    target = catalog["loudness"]["target_lufs"]
    tolerance = catalog["loudness"]["tolerance_lu"]
    for rel, entry in manifest["files"].items():
        decoded = miniaudio.decode_file(str(ASSETS / rel))
        assert decoded.sample_rate == catalog["audio"]["sample_rate"], rel
        assert decoded.nchannels == catalog["audio"]["channels"], rel
        duration = decoded.num_frames / decoded.sample_rate
        assert abs(duration - entry["duration_s"]) < 0.05, rel
        assert 0.2 <= duration <= 60.0, f"{rel}: {duration:.2f} s"
        assert abs(entry["lufs"] - target) <= tolerance, f"{rel}: {entry['lufs']} LUFS"
