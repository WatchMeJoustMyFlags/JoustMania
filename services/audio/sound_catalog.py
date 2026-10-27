"""
Sound catalog: the registry of playable sound ids (UC-040 BR-007, #1301) and the
voice profiles (UC-031 BR-007, #2107).

A sound id is either the stem of an ``.ogg`` file in one of the scanned asset
directories (``<base>/vox/<profile>`` for every voice profile, and
``<base>/sounds``) or an **alias** that names such a file. Aliases give semantic
ids to existing assets so callers (the agent's intervention writer, the game
coordinator) never depend on an asset file name:

- ``agent_cue`` -> ``Joust/sounds/beep_loud.ogg``: the lobby ready ping a
  controller makes when it joins (``services/menu/handlers/connected.py``). The
  agent's ``play_audio_cue`` writer default (``services/agent/actions/writer.go``).
- ``shield_up`` -> ``Commander/sounds/power_ready.ogg``: Commander's power-up
  sting (the sound effect, not the ``power_ready`` voice line), played when the
  agent grants a shield.

An alias names an exact, voice-independent asset path, so it can pick a sound
effect even where a voice line of the same name wins the registry lookup.

Voice profiles come from ``assets/voice_manifest.json`` (written by
``make voice``): the recorded profiles (``aaron``, ``ivy``) plus the generated
ones (``gen_male``, ``gen_female``), each generated profile with a recorded
partner. Not every profile has every file: a voice line missing in the selected
profile plays from its partner (``aaron`` <-> ``gen_male``, ``ivy`` <->
``gen_female``). Without a readable manifest the profiles are the recorded ones
and there is no partner.

Run as a module to check ids against the assets on disk (used by
``scripts/talk-preflight.sh`` inside the running audio container)::

    python -m services.audio.sound_catalog [--voice ivy] agent_cue

Exit 0 and print the relative path when every id resolves to an existing file,
exit 1 otherwise.
"""

import json
import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).parent / "assets"

# Directories scanned for sounds, in priority order (first found wins).
SOUND_BASE_DIRS = ("Joust", "Menu", "Zombie", "Fight_Club", "Commander")

# The voice a lookup without a voice uses (the CLI default is the flag default instead).
REFERENCE_VOICE = "aaron"

# The menu_voice flag default; also the voice kept when the flag names an unknown profile at start.
DEFAULT_VOICE = "ivy"

# The recorded profiles when the manifest is missing or unreadable.
RECORDED_VOICES = ("aaron", "ivy")

MANIFEST_NAME = "voice_manifest.json"

# alias -> asset path relative to ASSETS_DIR (voice-independent). Keys are lowercase.
SOUND_ALIASES: dict[str, str] = {
    "agent_cue": "Joust/sounds/beep_loud.ogg",
    "shield_up": "Commander/sounds/power_ready.ogg",
}

Registry = dict[str, tuple[str, str]]  # sound id -> (type "vox"|"sound", base_dir)


@dataclass(frozen=True)
class VoiceProfiles:
    """The selectable voice profiles and their partners (UC-031 BR-007)."""

    names: tuple[str, ...] = RECORDED_VOICES
    partners: Mapping[str, str] = field(default_factory=dict)

    def candidates(self, voice: str) -> tuple[str, ...]:
        """The profiles a voice line is looked up in, in order: the voice, then its partner."""
        partner = self.partners.get(voice)
        return (voice,) if partner is None or partner == voice else (voice, partner)


def load_voice_profiles(assets_dir: Path = ASSETS_DIR) -> VoiceProfiles:
    """Read the profiles and partners from the voice manifest.

    A missing or malformed manifest never stops the service: the recorded
    profiles stay selectable, without partners, and a warning says why.
    """
    path = Path(assets_dir) / MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        recorded = tuple(str(name) for name in (data.get("recorded") or RECORDED_VOICES))
        generated = data.get("profiles") or {}
        partners: dict[str, str] = {}
        names = list(recorded)
        for name in sorted(generated):
            entry = generated[name]
            if name in names:
                continue
            names.append(str(name))
            partner = entry.get("partner") if isinstance(entry, dict) else None
            if partner in recorded:
                partners[str(name)] = str(partner)
                partners.setdefault(str(partner), str(name))
        return VoiceProfiles(names=tuple(names), partners=partners)
    except FileNotFoundError:
        logger.warning("Voice manifest %s not found: only the recorded voices %s, no partner", path, RECORDED_VOICES)
    except (OSError, ValueError, AttributeError, TypeError) as e:
        logger.warning(
            "Voice manifest %s unreadable (%s): only the recorded voices %s, no partner", path, e, RECORDED_VOICES
        )
    return VoiceProfiles()


def register_sound(registry: Registry, sound_name: str, sound_type: str, base_dir: str) -> None:
    """Register a sound id (original case and lowercase); first found wins."""
    entry = (sound_type, base_dir)
    registry.setdefault(sound_name, entry)
    registry.setdefault(sound_name.lower(), entry)


def scan_directory(registry: Registry, directory: Path, sound_type: str, base_dir: str) -> None:
    """Register every ``.ogg`` file in ``directory`` (missing directories are skipped)."""
    if not directory.exists():
        return
    for ogg_file in sorted(directory.glob("*.ogg")):
        register_sound(registry, ogg_file.stem, sound_type, base_dir)


def build_sound_registry(assets_dir: Path = ASSETS_DIR, profiles: VoiceProfiles | None = None) -> Registry:
    """Scan the asset tree into a registry (aliases are resolved separately).

    Per area, the voice directories of every profile are scanned (the union, so
    a line only one profile has is registered), then the sound effects.
    """
    if profiles is None:
        profiles = load_voice_profiles(assets_dir)
    registry: Registry = {}
    for base_dir in SOUND_BASE_DIRS:
        base = Path(assets_dir) / base_dir
        for voice in profiles.names:
            scan_directory(registry, base / "vox" / voice, "vox", base_dir)
        scan_directory(registry, base / "sounds", "sound", base_dir)
    return registry


def _strip_ogg(sound_name: str) -> str:
    return sound_name[:-4] if sound_name.endswith(".ogg") else sound_name


def alias_path(sound_name: str) -> str | None:
    """The asset path an alias names, or None when ``sound_name`` is not an alias."""
    return SOUND_ALIASES.get(_strip_ogg(sound_name).lower())


def is_known(registry: Registry, sound_name: str) -> bool:
    """Whether a simple id is an alias or a registered file stem."""
    name = _strip_ogg(sound_name)
    return alias_path(name) is not None or name in registry or name.lower() in registry


def relative_path(
    registry: Registry,
    sound_name: str,
    voice: str = REFERENCE_VOICE,
    profiles: VoiceProfiles | None = None,
    assets_dir: Path = ASSETS_DIR,
) -> str | None:
    """Relative asset path for a simple id or alias, or None when it is unknown.

    A voice line plays from the first of ``voice`` and its partner that has the
    file under ``assets_dir`` (BR-007); when neither has it, the path in
    ``voice`` is returned (it then counts as ``file_not_found`` at playback).
    """
    name = _strip_ogg(sound_name)
    aliased = alias_path(name)
    if aliased is not None:
        return aliased
    entry = registry.get(name) or registry.get(name.lower())
    if entry is None:
        return None
    sound_type, base_dir = entry
    if sound_type != "vox":
        return f"{base_dir}/sounds/{name}.ogg"
    if profiles is None:
        profiles = load_voice_profiles(assets_dir)
    return voice_line_path(base_dir, f"{name}.ogg", voice, profiles, assets_dir)


def voice_line_path(
    base_dir: str, filename: str, voice: str, profiles: VoiceProfiles, assets_dir: Path = ASSETS_DIR
) -> str:
    """Path of a voice line: the first of ``voice`` and its partner that has the file under ``assets_dir``.

    When neither has it, the path in ``voice`` is returned (``file_not_found`` at playback).
    """
    for candidate in profiles.candidates(voice):
        path = f"{base_dir}/vox/{candidate}/{filename}"
        if (Path(assets_dir) / path).is_file():
            return path
    return f"{base_dir}/vox/{voice}/{filename}"


def resolution_table(
    registry: Registry, voice: str, profiles: VoiceProfiles, assets_dir: Path = ASSETS_DIR
) -> dict[str, str]:
    """Precompute ``id -> relative path`` for every registered id under one voice.

    The audio service rebuilds it when ``menu_voice`` changes, so a ``PlaySound``
    is one dict lookup and never touches the disk to pick the profile.
    """
    table: dict[str, str] = {}
    for sound_name in registry:
        path = relative_path(registry, sound_name, voice=voice, profiles=profiles, assets_dir=assets_dir)
        if path is not None:
            table[sound_name] = path
    return table


def main(argv: list[str]) -> int:
    """CLI: exit 0 when every given id resolves to an existing file."""
    voice = DEFAULT_VOICE
    if len(argv) >= 2 and argv[0] == "--voice":
        voice, argv = argv[1], argv[2:]
    if not argv:
        print("usage: python -m services.audio.sound_catalog [--voice <profile>] <sound-id> [...]", file=sys.stderr)
        return 2
    profiles = load_voice_profiles(ASSETS_DIR)
    if voice not in profiles.names:
        print(f"--voice {voice}: not a voice profile ({', '.join(profiles.names)})", file=sys.stderr)
        return 2
    registry = build_sound_registry(ASSETS_DIR, profiles)
    status = 0
    for sound_name in argv:
        path = relative_path(registry, sound_name, voice=voice, profiles=profiles)
        if path is None:
            print(f"{sound_name}: not in the sound registry")
            status = 1
        elif not (ASSETS_DIR / path).is_file():
            print(f"{sound_name}: {path} is registered but missing on disk")
            status = 1
        else:
            print(f"{sound_name}: {path}")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
