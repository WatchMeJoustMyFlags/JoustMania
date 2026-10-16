"""
Sound catalog: the registry of playable sound ids (UC-040 BR-005, #1301).

A sound id is either the stem of an ``.ogg`` file in one of the scanned asset
directories (``<base>/vox/aaron`` and ``<base>/sounds``) or an **alias** that
names such a file. Aliases give semantic ids to existing assets so callers
(the agent's intervention writer, the game coordinator) never depend on an
asset file name:

- ``agent_cue`` -> ``Joust/sounds/beep_loud.ogg``: the lobby ready ping a
  controller makes when it joins (``services/menu/handlers/connected.py``). The
  agent's ``play_audio_cue`` writer default (``services/agent/actions/writer.go``).
- ``shield_up`` -> ``Commander/sounds/power_ready.ogg``: Commander's power-up
  sting (the sound effect, not the ``power_ready`` voice line), played when the
  agent grants a shield.

An alias names an exact, voice-independent asset path, so it can pick a sound
effect even where a voice line of the same name wins the registry lookup.

Run as a module to check one id against the assets on disk (used by
``scripts/talk-preflight.sh`` inside the running audio container)::

    python -m services.audio.sound_catalog agent_cue

Exit 0 and print the relative path when the id resolves to an existing file,
exit 1 otherwise.
"""

import sys
from pathlib import Path

ASSETS_DIR = Path(__file__).parent / "assets"

# Directories scanned for sounds, in priority order (first found wins).
SOUND_BASE_DIRS = ("Joust", "Menu", "Zombie", "Fight_Club", "Commander")

# Reference voice for vox scanning: every voice ships the same file names.
REFERENCE_VOICE = "aaron"

# alias -> asset path relative to ASSETS_DIR (voice-independent). Keys are lowercase.
SOUND_ALIASES: dict[str, str] = {
    "agent_cue": "Joust/sounds/beep_loud.ogg",
    "shield_up": "Commander/sounds/power_ready.ogg",
}

Registry = dict[str, tuple[str, str]]  # sound id -> (type "vox"|"sound", base_dir)


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


def build_sound_registry(assets_dir: Path = ASSETS_DIR) -> Registry:
    """Scan the asset tree into a registry (aliases are resolved separately)."""
    registry: Registry = {}
    for base_dir in SOUND_BASE_DIRS:
        base = assets_dir / base_dir
        scan_directory(registry, base / "vox" / REFERENCE_VOICE, "vox", base_dir)
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


def relative_path(registry: Registry, sound_name: str, voice: str = REFERENCE_VOICE) -> str | None:
    """Relative asset path for a simple id or alias, or None when it is unknown."""
    name = _strip_ogg(sound_name)
    aliased = alias_path(name)
    if aliased is not None:
        return aliased
    entry = registry.get(name) or registry.get(name.lower())
    if entry is None:
        return None
    sound_type, base_dir = entry
    if sound_type == "vox":
        return f"{base_dir}/vox/{voice}/{name}.ogg"
    return f"{base_dir}/sounds/{name}.ogg"


def main(argv: list[str]) -> int:
    """CLI: exit 0 when every given id resolves to an existing file."""
    if not argv:
        print("usage: python -m services.audio.sound_catalog <sound-id> [...]", file=sys.stderr)
        return 2
    registry = build_sound_registry(ASSETS_DIR)
    status = 0
    for sound_name in argv:
        path = relative_path(registry, sound_name)
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
