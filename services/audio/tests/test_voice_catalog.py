"""The voice catalog against the Sound enum, the registry and the asset tree (#2107, UC-031 BR-007, BR-003).

The catalog holds every voice line the code plays, in every area. Recordings no
caller can reach are listed in NOT_CATALOGED (audited for #2147): they keep
their files, they are not generated. Those the maintainer chose to drop (#2166) are in
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

# Recordings no code path plays (audit for #2147). Each entry is <area>/<stem>.
_JOUST_UNUSED = {
    "10 werewolf", "30 werewolf", "3 minutes", "5 minutes", "Fakedout", "FakedoutCounter", "countered",
    "explosiondeath", "game_over", "werewolf intro_1", "werewolf reveal 2", "red team win",
    "orange team win", "pink team win", "purple team win",
}  # fmt: skip
_COMMANDER = {
    "10 seconds begins", "blue overdrive", "blue revive", "blue shift", "blue winner", "blue_power_ready",
    "commander intro", "commanders chosen", "power_ready", "red overdrive", "red revive",
    "red winner", "red_power_ready", "blue_overdrive", "red_overdrive",
}  # fmt: skip
_ZOMBIE_UNUSED = {"1 minute", "3 minutes", "5 minutes", "molotov found"}
_FIGHT_CLUB = {
    "5_rounds",
    "Defender_high_score",
    "defender_lead",
    "game_over",
    "last_round",
    "tie_game",
}
# Played, but not speech: no line, the recording plays for every profile (partner fallback, part 2).
NOT_SPEECH = {"Zombie/zombie_death"}
NOT_CATALOGED = NOT_SPEECH | (
    {f"Joust/{s}" for s in _JOUST_UNUSED}
    | {f"Commander/{s}" for s in _COMMANDER}
    | {f"Zombie/{s}" for s in _ZOMBIE_UNUSED}
    | {f"Fight_Club/{s}" for s in _FIGHT_CLUB}
)

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

# Recorded in one profile only; the other plays it through the partner fallback (part 2).
RECORDED_ONLY_IN = {"Menu/Fightclub-instructions": {"ivy"}}

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


def _recorded_stems(catalog):
    """<area>/<stem> -> the recorded profiles that have it."""
    stems = {}
    for area in AREAS:
        for voice in catalog["recorded"]:
            for f in (ASSETS / area / "vox" / voice).glob("*.ogg"):
                stems.setdefault(f"{area}/{f.stem}", set()).add(voice)
    return stems


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
        if key not in keys and key not in NOT_CATALOGED:
            missing[name] = key
    assert not missing, f"voice Sound members without a catalog line: {missing}"


def test_every_named_sound_is_a_real_member_with_the_line_id(catalog):
    for area, line in _lines(catalog):
        if "sound" not in line:
            continue
        member = Sound.__members__.get(line["sound"])
        assert member is not None, f"{area}/{line['id']}: unknown Sound member {line['sound']}"
        assert member.value == line["id"], f"{line['sound']} is {member.value!r}, not {line['id']!r}"


def test_every_recording_is_catalogued_or_known_unused(catalog):
    """Both ways (BR-003): a recorded line exists in every recorded profile, and no recording is unaccounted for."""
    stems = _recorded_stems(catalog)
    recorded = {f"{area}/{line['id']}" for area, line in _lines(catalog) if line.get("recorded")}
    assert not recorded & NOT_CATALOGED
    assert recorded - set(stems) == set(), "catalog says recorded, no file"
    unaccounted = set(stems) - recorded - NOT_CATALOGED
    assert not unaccounted, f"recordings neither catalogued nor listed as unused: {sorted(unaccounted)}"
    stale = NOT_CATALOGED - set(stems)
    assert not stale, f"NOT_CATALOGED names files that do not exist: {sorted(stale)}"
    for key in recorded:
        expected = RECORDED_ONLY_IN.get(key, set(catalog["recorded"]))
        assert stems[key] == expected, f"{key}: recorded only in {sorted(stems[key])}"


def test_new_ids_do_not_collide_with_any_area(catalog):
    """A generated-only id that matches an existing stem anywhere would resolve to the wrong file."""
    existing = {}
    for area in AREAS:
        for f in (ASSETS / area).rglob("*.ogg"):
            if "/gen_" not in f.as_posix():
                existing.setdefault(f.stem, area)
    clashes = {
        line["id"]: existing[line["id"]]
        for _, line in _lines(catalog)
        if not line.get("recorded") and line["id"] in existing
    }
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
    assert not offenders, f"deleted recordings are still named: {offenders}"
