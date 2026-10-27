"""
Unit tests for audio service sound registry and path resolution.

These tests verify that:
1. All Sound enum values can be resolved to existing files
2. The registry correctly categorizes vox vs sound effects
3. Path resolution uses correct voice directories for vox sounds
4. All asset directories are scanned (Joust, Menu, Zombie, Fight_Club, Commander)
"""

from pathlib import Path

import pytest

from lib.types import Sound
from services.audio.sound_catalog import build_sound_registry, is_known, relative_path

# Get the assets directory relative to this test file
ASSETS_DIR = Path(__file__).parent.parent / "assets"


class TestSoundRegistry:
    """Test the sound registry building and lookup."""

    @pytest.fixture
    def registry(self):
        """Build a sound registry like the audio service does."""
        return build_sound_registry(ASSETS_DIR)

    def test_registry_scans_all_directories(self, registry):
        """Verify all asset directories are scanned."""
        base_dirs_found = set()
        for _sound_name, (_, base_dir) in registry.items():
            base_dirs_found.add(base_dir)

        # At minimum, Joust and Menu should have sounds
        assert "Joust" in base_dirs_found, "Joust directory not scanned"
        assert "Menu" in base_dirs_found, "Menu directory not scanned"

    def test_registry_has_sounds(self, registry):
        """Verify registry contains sounds."""
        assert len(registry) > 0, "Registry is empty"
        # Should have at least 100 sounds
        assert len(registry) > 100, f"Registry has only {len(registry)} sounds"

    def test_all_sound_enum_values_resolve(self, registry):
        """Verify every Sound enum value can be found in the registry."""
        missing = []
        for sound in Sound:
            value = sound.value
            if not is_known(registry, value):
                missing.append(f"{sound.name}: {value}")

        assert not missing, "Missing sounds in registry:\n" + "\n".join(missing)

    def test_explosion_sounds_registered(self, registry):
        """Verify explosion sounds are in the registry."""
        assert "Explosion34" in registry, "Explosion34 not in registry"
        assert "Explosion22" in registry, "Explosion22 not in registry"

        # Check they're registered as sound effects, not vox
        sound_type, base_dir = registry["Explosion34"]
        assert sound_type == "sound", f"Explosion34 should be 'sound', got '{sound_type}'"
        assert base_dir == "Joust", f"Explosion34 should be in 'Joust', got '{base_dir}'"

    def test_congratulations_is_vox(self, registry):
        """Verify congratulations is registered as vox sound."""
        assert "congratulations" in registry, "congratulations not in registry"

        sound_type, base_dir = registry["congratulations"]
        assert sound_type == "vox", f"congratulations should be 'vox', got '{sound_type}'"
        assert base_dir == "Joust", f"congratulations should be in 'Joust', got '{base_dir}'"

    def test_beep_loud_exists(self, registry):
        """Verify beep_loud is in registry (SFX_BEEP points to this)."""
        assert "beep_loud" in registry, "beep_loud not in registry"

        sound_type, base_dir = registry["beep_loud"]
        assert sound_type == "sound", f"beep_loud should be 'sound', got '{sound_type}'"

    def test_zombie_sounds_registered(self, registry):
        """Verify zombie game sounds are registered."""
        zombie_sounds = ["zombie_victory", "zombie_death", "human_victory"]
        for sound_name in zombie_sounds:
            assert sound_name in registry, f"{sound_name} not in registry"
            sound_type, base_dir = registry[sound_name]
            assert base_dir == "Zombie", f"{sound_name} should be in 'Zombie', got '{base_dir}'"


class TestPathResolution:
    """Test sound path resolution logic."""

    @pytest.fixture
    def registry(self):
        """Build a minimal registry for path resolution tests."""
        return {
            "congratulations": ("vox", "Joust"),
            "Explosion34": ("sound", "Joust"),
            "zombie_victory": ("vox", "Zombie"),
            "beep_loud": ("sound", "Joust"),
            "menu Joust FFA": ("vox", "Menu"),
        }

    def resolve_sound_path(self, sound_input: str, registry: dict, menu_voice: str = "ivy") -> str:
        """Simulate the audio service's _resolve_sound_path method."""
        lookup_name = sound_input
        if lookup_name.endswith(".ogg"):
            lookup_name = lookup_name[:-4]

        if "/" not in lookup_name and "\\" not in lookup_name:
            registry_entry = registry.get(lookup_name) or registry.get(lookup_name.lower())
            if registry_entry:
                sound_type, base_dir = registry_entry
                if sound_type == "vox":
                    return f"{base_dir}/vox/{menu_voice}/{lookup_name}.ogg"
                return f"{base_dir}/sounds/{lookup_name}.ogg"
            return f"Joust/vox/{menu_voice}/{lookup_name}.ogg"

        if "/vox/" in sound_input:
            parts = sound_input.split("/vox/")
            if len(parts) == 2:
                remainder = parts[1]
                if not remainder.startswith("aaron/") and not remainder.startswith("ivy/"):
                    return f"{parts[0]}/vox/{menu_voice}/{remainder}"

        return sound_input

    def test_vox_sound_uses_voice_directory(self, registry):
        """Verify vox sounds include voice folder in path."""
        path = self.resolve_sound_path("congratulations", registry, menu_voice="ivy")
        assert path == "Joust/vox/ivy/congratulations.ogg"

        path = self.resolve_sound_path("congratulations", registry, menu_voice="aaron")
        assert path == "Joust/vox/aaron/congratulations.ogg"

    def test_sfx_sound_uses_sounds_directory(self, registry):
        """Verify sound effects use sounds folder, not vox."""
        path = self.resolve_sound_path("Explosion34", registry)
        assert path == "Joust/sounds/Explosion34.ogg"
        assert "/vox/" not in path

    def test_zombie_vox_uses_zombie_directory(self, registry):
        """Verify zombie vox sounds use Zombie base directory."""
        path = self.resolve_sound_path("zombie_victory", registry, menu_voice="ivy")
        assert path == "Zombie/vox/ivy/zombie_victory.ogg"

    def test_menu_vox_uses_menu_directory(self, registry):
        """Verify menu vox sounds use Menu base directory."""
        path = self.resolve_sound_path("menu Joust FFA", registry, menu_voice="ivy")
        assert path == "Menu/vox/ivy/menu Joust FFA.ogg"


class TestSoundEnumValues:
    """Test that Sound enum values match actual file names."""

    @pytest.fixture
    def registry(self):
        """Build full registry."""
        return build_sound_registry(ASSETS_DIR)

    def test_sfx_beep_points_to_existing_file(self, registry):
        """SFX_BEEP should point to beep_loud (beep.ogg doesn't exist)."""
        assert Sound.SFX_BEEP.value == "beep_loud", "SFX_BEEP should be 'beep_loud'"
        assert "beep_loud" in registry

    def test_vox_congratulations_exists(self, registry):
        """VOX_CONGRATULATIONS should resolve."""
        assert Sound.VOX_CONGRATULATIONS.value in registry

    def test_explosion_sounds_exist(self, registry):
        """Explosion sounds should resolve."""
        assert Sound.SFX_EXPLOSION.value in registry
        assert Sound.SFX_EXPLOSION_22.value in registry

    def test_team_win_sounds_exist(self, registry):
        """Team win sounds should all resolve."""
        team_sounds = [
            Sound.VOX_BLUE_TEAM_WIN,
            Sound.VOX_RED_TEAM_WIN,
            Sound.VOX_GREEN_TEAM_WIN,
            Sound.VOX_YELLOW_TEAM_WIN,
        ]
        for sound in team_sounds:
            assert sound.value in registry, f"{sound.name} ({sound.value}) not in registry"

    def test_zombie_sounds_exist(self, registry):
        """Zombie game sounds should resolve."""
        zombie_sounds = [
            Sound.VOX_ZOMBIE_VICTORY,
            Sound.VOX_ZOMBIE_DEATH,
            Sound.VOX_HUMAN_VICTORY,
            Sound.VOX_ZOMBIE_ONE_MINUTE,
            Sound.VOX_ZOMBIE_THIRTY_SECONDS,
            Sound.VOX_ZOMBIE_TEN_SECONDS,
        ]
        for sound in zombie_sounds:
            assert sound.value in registry, f"{sound.name} ({sound.value}) not in registry"

    def test_werewolf_vox_sounds_exist(self, registry):
        """Werewolf VOX sounds should resolve."""
        werewolf_sounds = [
            Sound.VOX_WEREWOLF_INTRO,
            Sound.VOX_WEREWOLF_REVEAL,
            Sound.VOX_WEREWOLF_WIN,
            Sound.VOX_HUMAN_WIN,
        ]
        for sound in werewolf_sounds:
            assert sound.value in registry, f"{sound.name} ({sound.value}) not in registry"


class TestActualFileExistence:
    """Test that resolved paths point to actual files."""

    def resolve_full_path(self, sound_value: str) -> Path | None:
        """Resolve a sound value to its full file path."""
        relative = relative_path(build_sound_registry(ASSETS_DIR), sound_value, voice="ivy")
        return None if relative is None else ASSETS_DIR / relative

    def test_all_sound_enum_files_exist(self):
        """Every Sound enum value should resolve to an existing file."""
        missing_files = []
        for sound in Sound:
            path = self.resolve_full_path(sound.value)
            if path is None:
                missing_files.append(f"{sound.name}: {sound.value} - not in registry")
            elif not path.exists():
                missing_files.append(f"{sound.name}: {sound.value} - file not found at {path}")

        assert not missing_files, "Missing sound files:\n" + "\n".join(missing_files)

    def test_explosion34_file_exists(self):
        """Explosion34.ogg should exist."""
        path = ASSETS_DIR / "Joust" / "sounds" / "Explosion34.ogg"
        assert path.exists(), f"Explosion34.ogg not found at {path}"

    def test_beep_loud_file_exists(self):
        """beep_loud.ogg should exist."""
        path = ASSETS_DIR / "Joust" / "sounds" / "beep_loud.ogg"
        assert path.exists(), f"beep_loud.ogg not found at {path}"

    def test_congratulations_file_exists_for_both_voices(self):
        """congratulations.ogg should exist for both aaron and ivy."""
        for voice in ["aaron", "ivy"]:
            path = ASSETS_DIR / "Joust" / "vox" / voice / "congratulations.ogg"
            assert path.exists(), f"congratulations.ogg not found for voice '{voice}' at {path}"


REPO_ROOT = Path(__file__).resolve().parents[3]
WRITER_GO = REPO_ROOT / "services" / "agent" / "actions" / "writer.go"


def _writer_default_audio_cue() -> str:
    """The agent's play_audio_cue writer default, read from the Go source."""
    import re

    match = re.search(r'defaultAudioCue\s*=\s*"([^"]+)"', WRITER_GO.read_text())
    assert match, f"defaultAudioCue not found in {WRITER_GO}"
    return match.group(1)


class TestUC040InterventionSounds:
    """UC-040 BR-007: every sound id an intervention plays resolves in the registry."""

    @pytest.fixture
    def registry(self):
        return build_sound_registry(ASSETS_DIR)

    @pytest.mark.uc("UC-040", flow="A16", br=["BR-007"])
    def test_every_intervention_sound_id_resolves_to_a_file(self, registry):
        """UC-040 BR-007: the writer default cue and the shield sound resolve to existing files."""
        sound_ids = {
            _writer_default_audio_cue(),
            Sound.SFX_AGENT_CUE.value,
            Sound.SFX_SHIELD_UP.value,
        }
        for sound_id in sorted(sound_ids):
            relative = relative_path(registry, sound_id)
            assert relative is not None, f"{sound_id!r} is not in the sound registry"
            assert (ASSETS_DIR / relative).is_file(), f"{sound_id!r} -> {relative} missing on disk"

    @pytest.mark.uc("UC-040", flow="main", br=["BR-007"])
    def test_writer_default_is_the_agent_cue_alias(self):
        """UC-040 BR-007: the Go writer default and the Sound enum name the same alias."""
        assert _writer_default_audio_cue() == Sound.SFX_AGENT_CUE.value == "agent_cue"

    @pytest.mark.uc("UC-040", flow="main", br=["BR-007"])
    def test_agent_cue_plays_the_lobby_ready_ping(self, registry):
        """UC-040 BR-007: agent_cue is the ping the lobby plays when a controller readies."""
        assert relative_path(registry, "agent_cue") == relative_path(registry, Sound.SFX_BEEP_LOUD.value)
        assert relative_path(registry, "agent_cue") == "Joust/sounds/beep_loud.ogg"

    @pytest.mark.uc("UC-040", flow="A16", br=["BR-007"])
    def test_shield_up_plays_commander_power_ready(self, registry):
        """UC-040 BR-007: shield_up is an alias of an existing sting, no new asset."""
        assert relative_path(registry, "shield_up") == "Commander/sounds/power_ready.ogg"

    @pytest.mark.uc("UC-040", flow="main", br=["BR-007"])
    def test_every_alias_target_is_registered(self, registry):
        """UC-040 BR-007: an alias names a shipped, voice-independent sound effect."""
        from services.audio.sound_catalog import SOUND_ALIASES

        for alias, target in SOUND_ALIASES.items():
            assert (ASSETS_DIR / target).is_file(), f"alias {alias!r} -> {target!r} missing on disk"
            assert "/vox/" not in target, f"alias {alias!r} must name a voice-independent sound effect"

    @pytest.mark.uc("UC-040", flow="main", br=["BR-007"])
    def test_cli_exit_code_reports_resolution(self, capsys):
        """UC-040 BR-007: the pre-flight CLI exits 0 for a resolvable id and 1 for an unknown one."""
        from services.audio.sound_catalog import main

        assert main(["agent_cue"]) == 0
        assert "Joust/sounds/beep_loud.ogg" in capsys.readouterr().out
        assert main(["no_such_sound_xyz"]) == 1
        assert "not in the sound registry" in capsys.readouterr().out
        assert main([]) == 2


class TestVoiceProfilesUC031:
    """UC-031 BR-007 / A4 (#2107): manifest-driven profiles, union registry, partner fallback."""

    @pytest.fixture(scope="class")
    def profiles(self):
        from services.audio.sound_catalog import load_voice_profiles

        return load_voice_profiles(ASSETS_DIR)

    @pytest.fixture(scope="class")
    def registry(self, profiles):
        return build_sound_registry(ASSETS_DIR, profiles)

    @pytest.mark.uc("UC-031", br=["BR-007"])
    def test_profiles_and_partners_come_from_the_manifest(self, profiles):
        assert set(profiles.names) == {"aaron", "ivy", "gen_male", "gen_female"}
        assert profiles.names[:2] == ("aaron", "ivy")
        assert profiles.candidates("aaron") == ("aaron", "gen_male")
        assert profiles.candidates("gen_male") == ("gen_male", "aaron")
        assert profiles.candidates("ivy") == ("ivy", "gen_female")
        assert profiles.candidates("gen_female") == ("gen_female", "ivy")

    @pytest.mark.uc("UC-031", br=["BR-003", "BR-007"])
    def test_registry_is_the_union_of_every_profile_directory(self, registry):
        """Fightclub-instructions is only in ivy, label_sensitivity only in gen_*: both are registered."""
        assert registry["Fightclub-instructions"] == ("vox", "Menu")
        assert registry[Sound.MENU_VOX_LABEL_SENSITIVITY.value] == ("vox", "Menu")
        assert registry[Sound.MENU_VOX_STATUS_NO_CONNECTION.value] == ("vox", "Menu")

    @pytest.mark.uc("UC-031", br=["BR-003", "BR-007"])
    def test_union_keeps_every_id_the_aaron_scan_registered(self, registry):
        """The union only adds ids: an id the old aaron-only scan knew keeps its type and area."""
        from services.audio.sound_catalog import SOUND_BASE_DIRS, scan_directory

        aaron_only: dict[str, tuple[str, str]] = {}
        for base_dir in SOUND_BASE_DIRS:
            scan_directory(aaron_only, ASSETS_DIR / base_dir / "vox" / "aaron", "vox", base_dir)
            scan_directory(aaron_only, ASSETS_DIR / base_dir / "sounds", "sound", base_dir)
        changed = {k: (v, registry.get(k)) for k, v in aaron_only.items() if registry.get(k) != v}
        assert not changed

    @pytest.mark.uc("UC-031", flow="A4", br=["BR-007"])
    def test_generated_only_line_plays_from_the_partner_of_a_recorded_voice(self, registry, profiles):
        label = Sound.MENU_VOX_LABEL_SENSITIVITY.value
        assert relative_path(registry, label, voice="aaron", profiles=profiles) == f"Menu/vox/gen_male/{label}.ogg"
        assert relative_path(registry, label, voice="ivy", profiles=profiles) == f"Menu/vox/gen_female/{label}.ogg"

    @pytest.mark.uc("UC-031", flow="A4", br=["BR-007"])
    def test_line_not_generated_plays_from_the_recorded_partner(self, registry, profiles):
        """zombie_death is a growl with no catalog line: a generated voice plays the recording."""
        death = Sound.VOX_ZOMBIE_DEATH.value
        assert relative_path(registry, death, voice="gen_female", profiles=profiles) == f"Zombie/vox/ivy/{death}.ogg"
        assert relative_path(registry, death, voice="gen_male", profiles=profiles) == f"Zombie/vox/aaron/{death}.ogg"

    @pytest.mark.uc("UC-031", flow="A4", br=["BR-007"])
    def test_selected_profile_wins_when_it_has_the_line(self, registry, profiles):
        high = Sound.MENU_VOX_SENSITIVITY_HIGH.value
        for voice in profiles.names:
            assert relative_path(registry, high, voice=voice, profiles=profiles) == f"Menu/vox/{voice}/{high}.ogg"

    @pytest.mark.uc("UC-031", flow="A4", br=["BR-007"])
    @pytest.mark.parametrize("voice", ["aaron", "ivy", "gen_male", "gen_female"])
    def test_every_sound_member_resolves_to_a_file_in_every_profile(self, registry, profiles, voice):
        """Aliases included (__members__): any profile can be selected without a silent line."""
        missing = {}
        for name, member in Sound.__members__.items():
            rel = relative_path(registry, member.value, voice=voice, profiles=profiles)
            if rel is None or not (ASSETS_DIR / rel).is_file():
                missing[name] = rel
        assert not missing, f"{voice}: {missing}"

    @pytest.mark.uc("UC-031", br=["BR-007"])
    def test_resolution_table_matches_relative_path(self, registry, profiles):
        from services.audio.sound_catalog import resolution_table

        table = resolution_table(registry, "gen_female", profiles, ASSETS_DIR)
        assert set(table) == set(registry)
        for sound_id in ("high", "label_backend", "zombie_death", "Explosion34", "congratulations"):
            assert table[sound_id] == relative_path(registry, sound_id, voice="gen_female", profiles=profiles)

    @pytest.mark.uc("UC-031", br=["BR-007"])
    def test_missing_manifest_keeps_the_recorded_voices_without_partners(self, tmp_path):
        from services.audio.sound_catalog import load_voice_profiles

        profiles = load_voice_profiles(tmp_path)
        assert profiles.names == ("aaron", "ivy")
        assert profiles.candidates("ivy") == ("ivy",)
        (tmp_path / "voice_manifest.json").write_text("{not json")
        assert load_voice_profiles(tmp_path).names == ("aaron", "ivy")

    @pytest.mark.uc("UC-031", br=["BR-007"])
    def test_line_in_neither_profile_keeps_the_selected_voice_path(self, tmp_path):
        from services.audio.sound_catalog import VoiceProfiles

        profiles = VoiceProfiles(names=("aaron", "ivy", "gen_female"), partners={"ivy": "gen_female"})
        registry = {"ghost": ("vox", "Menu")}
        rel = relative_path(registry, "ghost", voice="ivy", profiles=profiles, assets_dir=tmp_path)
        assert rel == "Menu/vox/ivy/ghost.ogg"

    @pytest.mark.uc("UC-031", flow="A4", br=["BR-007"])
    def test_cli_takes_a_voice(self, capsys):
        from services.audio.sound_catalog import main

        assert main(["--voice", "aaron", Sound.MENU_VOX_LABEL_AGENT.value]) == 0
        assert "Menu/vox/gen_male/label_agent.ogg" in capsys.readouterr().out
        assert main(["--voice", "nobody", "agent_cue"]) == 2
