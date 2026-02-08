"""
Unit tests for audio service sound registry and path resolution.

These tests verify that:
1. The registry correctly scans directories and categorizes vox vs sound effects
2. Path resolution uses correct voice directories for vox sounds
3. Sound enum values have expected string values

These tests use temporary files instead of real audio assets (which are in Git LFS).
Asset-completeness (do real .ogg files match Sound enum?) is verified by integration tests.
"""

from pathlib import Path

import pytest

from lib.types import Sound


@pytest.fixture
def assets_dir(tmp_path):
    """Create a fake asset directory tree with empty .ogg files.

    Mirrors the real layout with a representative subset of files
    across all base directories and both sound types (vox and sfx).
    """
    structure = {
        "Joust": {
            "vox": ["congratulations", "game_over", "blue team win", "Fakedout"],
            "sounds": ["Explosion34", "Explosion22", "beep_loud", "death", "start"],
        },
        "Menu": {
            "vox": ["menu Joust FFA", "notenoughplayers", "high", "medium"],
            "sounds": ["slow_sensitivity", "mid_sensitivity", "fast_sensitivity"],
        },
        "Zombie": {
            "vox": ["zombie_victory", "zombie_death", "human_victory"],
            "sounds": ["death", "shotgun"],
        },
        "Fight_Club": {
            "vox": ["5_rounds", "last_round", "game_over"],
            "sounds": [],
        },
        "Commander": {
            "vox": ["commander intro", "blue winner"],
            "sounds": ["overdrive", "shift"],
        },
    }

    for base_dir, categories in structure.items():
        for voice in ["aaron", "ivy"]:
            vox_dir = tmp_path / base_dir / "vox" / voice
            vox_dir.mkdir(parents=True)
            for name in categories["vox"]:
                (vox_dir / f"{name}.ogg").touch()

        sounds_dir = tmp_path / base_dir / "sounds"
        sounds_dir.mkdir(parents=True, exist_ok=True)
        for name in categories["sounds"]:
            (sounds_dir / f"{name}.ogg").touch()

    return tmp_path


def build_registry(assets_dir: Path) -> dict[str, tuple[str, str]]:
    """Build a sound registry from an assets directory, same logic as the audio service."""
    sound_registry: dict[str, tuple[str, str]] = {}

    for base_dir in ["Joust", "Menu", "Zombie", "Fight_Club", "Commander"]:
        assets_path = assets_dir / base_dir

        vox_dir = assets_path / "vox" / "aaron"
        if vox_dir.exists():
            for ogg_file in vox_dir.glob("*.ogg"):
                sound_name = ogg_file.stem
                if sound_name not in sound_registry:
                    sound_registry[sound_name] = ("vox", base_dir)
                if sound_name.lower() not in sound_registry:
                    sound_registry[sound_name.lower()] = ("vox", base_dir)

        sounds_dir = assets_path / "sounds"
        if sounds_dir.exists():
            for ogg_file in sounds_dir.glob("*.ogg"):
                sound_name = ogg_file.stem
                if sound_name not in sound_registry:
                    sound_registry[sound_name] = ("sound", base_dir)
                if sound_name.lower() not in sound_registry:
                    sound_registry[sound_name.lower()] = ("sound", base_dir)

    return sound_registry


class TestSoundRegistry:
    """Test the sound registry building and lookup."""

    @pytest.fixture
    def registry(self, assets_dir):
        return build_registry(assets_dir)

    def test_registry_scans_all_directories(self, registry):
        """Verify all asset directories are scanned."""
        base_dirs_found = {base_dir for _, (_, base_dir) in registry.items()}
        for expected in ["Joust", "Menu", "Zombie", "Fight_Club", "Commander"]:
            assert expected in base_dirs_found, f"{expected} directory not scanned"

    def test_registry_has_sounds(self, registry):
        assert len(registry) > 0, "Registry is empty"

    def test_vox_categorized_correctly(self, registry):
        """Verify vox sounds are categorized as vox."""
        assert "congratulations" in registry
        sound_type, base_dir = registry["congratulations"]
        assert sound_type == "vox"
        assert base_dir == "Joust"

    def test_sfx_categorized_correctly(self, registry):
        """Verify sound effects are categorized as sound."""
        assert "Explosion34" in registry
        sound_type, base_dir = registry["Explosion34"]
        assert sound_type == "sound"
        assert base_dir == "Joust"

    def test_case_insensitive_lookup(self, registry):
        """Verify lowercase aliases are created."""
        assert "fakedout" in registry
        sound_type, _ = registry["fakedout"]
        assert sound_type == "vox"

    def test_first_directory_wins(self, registry):
        """Verify that when the same sound name appears in multiple dirs, first wins.

        'death' exists in both Joust/sounds and Zombie/sounds. Joust is scanned first.
        """
        sound_type, base_dir = registry["death"]
        assert base_dir == "Joust"
        assert sound_type == "sound"

    def test_zombie_sounds_in_correct_directory(self, registry):
        """Verify zombie-specific sounds are found in Zombie directory."""
        for name in ["zombie_victory", "zombie_death", "human_victory"]:
            assert name in registry, f"{name} not in registry"
            _, base_dir = registry[name]
            assert base_dir == "Zombie", f"{name} should be in Zombie, got {base_dir}"

    def test_menu_sounds_registered(self, registry):
        """Verify menu sounds are found."""
        assert "menu Joust FFA" in registry
        sound_type, base_dir = registry["menu Joust FFA"]
        assert sound_type == "vox"
        assert base_dir == "Menu"

    def test_menu_sfx_registered(self, registry):
        """Verify menu sound effects are found."""
        assert "slow_sensitivity" in registry
        sound_type, base_dir = registry["slow_sensitivity"]
        assert sound_type == "sound"
        assert base_dir == "Menu"

    def test_empty_directory_handled(self, assets_dir):
        """Verify directories with no sounds don't cause errors."""
        empty_dir = assets_dir / "Empty"
        empty_dir.mkdir()
        (empty_dir / "vox" / "aaron").mkdir(parents=True)
        (empty_dir / "sounds").mkdir()

        # Should not raise
        registry = build_registry(assets_dir)
        assert len(registry) > 0

    def test_missing_directory_handled(self, tmp_path):
        """Verify completely missing directories don't cause errors."""
        # Only create Joust with one file
        vox_dir = tmp_path / "Joust" / "vox" / "aaron"
        vox_dir.mkdir(parents=True)
        (vox_dir / "test.ogg").touch()

        registry = build_registry(tmp_path)
        assert "test" in registry


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
    """Test that Sound enum values have expected string values.

    These are pure enum assertions — no filesystem access needed.
    """

    def test_sfx_beep_points_to_beep_loud(self):
        """SFX_BEEP should point to beep_loud (beep.ogg doesn't exist)."""
        assert Sound.SFX_BEEP.value == "beep_loud"

    def test_vox_congratulations_value(self):
        assert Sound.VOX_CONGRATULATIONS.value == "congratulations"

    def test_explosion_sound_values(self):
        assert Sound.SFX_EXPLOSION.value == "Explosion34"
        assert Sound.SFX_EXPLOSION_22.value == "Explosion22"

    def test_team_win_sound_values(self):
        expected = {
            Sound.VOX_BLUE_TEAM_WIN: "blue team win",
            Sound.VOX_RED_TEAM_WIN: "red team win",
            Sound.VOX_GREEN_TEAM_WIN: "green team win",
            Sound.VOX_YELLOW_TEAM_WIN: "yellow team win",
        }
        for sound, value in expected.items():
            assert sound.value == value, f"{sound.name} should be '{value}'"

    def test_zombie_sound_values(self):
        expected = {
            Sound.VOX_ZOMBIE_VICTORY: "zombie_victory",
            Sound.VOX_ZOMBIE_DEATH: "zombie_death",
            Sound.VOX_HUMAN_VICTORY: "human_victory",
        }
        for sound, value in expected.items():
            assert sound.value == value, f"{sound.name} should be '{value}'"

    def test_werewolf_sound_values(self):
        expected = {
            Sound.VOX_WEREWOLF_INTRO: "werewolf intro",
            Sound.VOX_WEREWOLF_REVEAL: "werewolf reveal",
            Sound.VOX_WEREWOLF_WIN: "werewolf win",
            Sound.VOX_HUMAN_WIN: "human win",
        }
        for sound, value in expected.items():
            assert sound.value == value, f"{sound.name} should be '{value}'"

    def test_fight_club_sound_values(self):
        expected = {
            Sound.VOX_FIGHT_CLUB_5_ROUNDS: "5_rounds",
            Sound.VOX_FIGHT_CLUB_LAST_ROUND: "last_round",
            Sound.VOX_FIGHT_CLUB_GAME_OVER: "game_over",
        }
        for sound, value in expected.items():
            assert sound.value == value, f"{sound.name} should be '{value}'"

    def test_menu_sensitivity_sfx_values(self):
        expected = {
            Sound.MENU_SFX_SENSITIVITY_SLOW: "slow_sensitivity",
            Sound.MENU_SFX_SENSITIVITY_MID: "mid_sensitivity",
            Sound.MENU_SFX_SENSITIVITY_FAST: "fast_sensitivity",
        }
        for sound, value in expected.items():
            assert sound.value == value, f"{sound.name} should be '{value}'"
