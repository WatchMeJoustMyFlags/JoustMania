"""The voice catalog against the Sound enum, the registry and the asset tree (#2107, UC-031 BR-007, BR-003).

The catalog holds every voice line the code plays, in every area, and every
profile is generated. The recorded profiles (aaron, ivy) were deleted after the
maintainer's listening check (#2107 part 3): no directory may come back and no
source may name them as a profile. The recordings dropped first (#2166) are in
DELETED_RECORDINGS: no file may come back and no source may name them.
"""

import json
from pathlib import Path

import miniaudio
import pytest
import yaml

from lib.types import Sound
from services.audio.sound_catalog import build_sound_registry

ROOT = Path(__file__).resolve().parents[3]
ASSETS = ROOT / "services" / "audio" / "assets"
CATALOG_PATH = ROOT / "services" / "audio" / "voice" / "lines.yaml"
MANIFEST_PATH = ASSETS / "voice_manifest.json"
AREAS = ("Joust", "Menu", "Zombie", "Fight_Club", "Commander")

# The recorded profiles, deleted with every file after the listening check (#2107 part 3).
DELETED_PROFILES = ("aaron", "ivy")

# Recordings deleted for #2166 (no code path played them): stems, in every recorded profile.
DELETED_RECORDINGS = {
    "Menu": {
        "menu ninjabomb", "menu Random", "commander-instructions", "Ninjabomb-instructions", "added_random",
        "removed_random", "tradeoff2", "adminop_force_all_start", "adminop_random_team_size",
        "adminop_freeze_mode", "adminop_lives", "adminop_resurrection", "adminop_sheilds",
        "adminop_traitor_team_size", "update_started", "joustmania_updated", "large_update", "joustmania_failed",
        "menu Commander",
    },
    "Commander": {"red shift"},
    "Fight_Club": {"defender_winning"},
}  # fmt: skip

pytestmark = pytest.mark.uc("UC-031", br=["BR-007"])


@pytest.fixture(scope="module")
def catalog():
    return yaml.safe_load(CATALOG_PATH.read_text())


@pytest.fixture(scope="module")
def manifest():
    return json.loads(MANIFEST_PATH.read_text())


def _lines(catalog):
    for area, lines in catalog["areas"].items():
        for line in lines:
            yield area, line


def _keys(catalog):
    return {f"{area}/{line['id']}" for area, line in _lines(catalog)}


def test_every_registered_voice_id_is_catalogued_or_known_unused(catalog):
    """Every Sound member (aliases included) that resolves to a voice file has a line in the area it plays from."""
    registry = build_sound_registry(ASSETS)
    keys = _keys(catalog)
    missing = {}
    for name, member in Sound.__members__.items():
        entry = registry.get(member.value)
        if entry is None or entry[0] != "vox":
            continue
        key = f"{entry[1]}/{member.value}"
        if key not in keys:
            missing[name] = key
    assert not missing, f"voice Sound members without a catalog line: {missing}"


def test_every_named_sound_is_a_real_member_with_the_line_id(catalog):
    for area, line in _lines(catalog):
        if "sound" not in line:
            continue
        member = Sound.__members__.get(line["sound"])
        assert member is not None, f"{area}/{line['id']}: unknown Sound member {line['sound']}"
        assert member.value == line["id"], f"{line['sound']} is {member.value!r}, not {line['id']!r}"


def test_every_voice_directory_is_a_generated_profile(catalog):
    """No recorded profile is left (#2107 part 3): every <area>/vox/<profile> is a catalog profile."""
    on_disk = {d.relative_to(ASSETS).as_posix() for d in ASSETS.glob("*/vox/*") if d.is_dir()}
    stray = {d for d in on_disk if d.split("/")[2] not in catalog["profiles"]}
    assert not stray, f"voice directories outside the generated profiles: {sorted(stray)}"
    assert not set(DELETED_PROFILES) & set(catalog["profiles"])


def test_new_ids_do_not_collide_with_any_area(catalog):
    """A line id that matches a sound effect or music stem anywhere would resolve to the wrong file."""
    existing = {}
    for area in AREAS:
        for f in (ASSETS / area).rglob("*.ogg"):
            if "/vox/" not in f.as_posix():
                existing.setdefault(f.stem, area)
    clashes = {line["id"]: existing[line["id"]] for _, line in _lines(catalog) if line["id"] in existing}
    assert not clashes, f"new catalog ids already used as asset stems: {clashes}"


def test_every_line_has_a_file_in_every_generated_profile(catalog, manifest):
    assert set(manifest["profiles"]) == set(catalog["profiles"])
    for profile in catalog["profiles"]:
        for area in AREAS:
            directory = ASSETS / area / "vox" / profile
            on_disk = {f.name for f in directory.iterdir()} if directory.is_dir() else set()
            expected = {f"{line['id']}.ogg" for line in catalog["areas"].get(area, [])}
            assert on_disk == expected, f"{area}/{profile}: missing {expected - on_disk}, stray {on_disk - expected}"


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


def test_deleted_recordings_stay_deleted_and_unreferenced():
    """A file that comes back, or a source that names a deleted stem, fails here (#2166)."""
    for area, stems in DELETED_RECORDINGS.items():
        for stem in stems:
            assert not list((ASSETS / area / "vox").glob(f"*/{stem}.ogg")), f"{area}/{stem} was deleted"
    needles = {stem for stems in DELETED_RECORDINGS.values() for stem in stems}
    # The deleted profiles as a value or a path (#2107 part 3), outside tests; prose mentions stay allowed.
    profile_needles = {f"{q}{name}{q}" for name in DELETED_PROFILES for q in ('"', "'")}
    profile_needles |= {f"vox/{name}/" for name in DELETED_PROFILES}
    suffixes = {".py", ".go", ".yaml", ".yml", ".json", ".sh", ".ts", ".tsx"}
    offenders = []
    for top in ("lib", "services", "scripts", "flags"):
        for path in (ROOT / top).rglob("*"):
            parts = path.relative_to(ROOT).parts
            if (
                path.suffix not in suffixes
                or not path.is_file()
                or path == Path(__file__).resolve()
                or "assets" in parts
                or any(p in {"node_modules", ".venv", "gen", "target"} for p in parts)
            ):
                continue
            text = path.read_text(errors="ignore")
            offenders += [f"{path.relative_to(ROOT)}: {n}" for n in sorted(needles) if n in text]
            if "tests" not in parts:
                offenders += [f"{path.relative_to(ROOT)}: {n}" for n in sorted(profile_needles) if n in text]
    assert not offenders, f"deleted recordings are still named: {offenders}"
