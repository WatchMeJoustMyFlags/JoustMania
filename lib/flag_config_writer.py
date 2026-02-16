"""
Read-modify-write helper for flagd JSON flag files.

Enables services to persist setting changes by updating the defaultVariant
of a flag in a flagd JSON file. Writes directly to the file because
os.replace() fails with EBUSY on Docker bind mounts when flagd holds
an inotify watch on the file.
"""

import json
import logging

logger = logging.getLogger(__name__)


class FlagConfigWriter:
    """
    Read-modify-write for a single flagd flag file.

    Each instance manages one flag file (e.g., game_settings.json).
    Writes directly to the file (not via temp + rename) because
    os.replace() fails with EBUSY on Docker bind mounts.

    flagd watches the file via inotify and picks up changes in <100ms.
    """

    def __init__(self, file_path: str):
        """
        Initialize writer for a flag file.

        Args:
            file_path: Absolute path to the flagd JSON file
        """
        self.file_path = file_path

    def update_default_variant(self, flag_key: str, variant_name: str) -> bool:
        """
        Update the defaultVariant for a flag.

        Reads the JSON file, changes the defaultVariant for the specified flag,
        and writes it back.

        Args:
            flag_key: Flag name (e.g., "sensitivity")
            variant_name: Variant to set as default (e.g., "fast")

        Returns:
            True if successful, False on error
        """
        try:
            # Read current file
            with open(self.file_path) as f:
                data = json.load(f)

            flags = data.get("flags", {})
            if flag_key not in flags:
                logger.warning(f"Flag '{flag_key}' not found in {self.file_path}")
                return False

            flag = flags[flag_key]
            if variant_name not in flag.get("variants", {}):
                logger.warning(f"Variant '{variant_name}' not found for flag '{flag_key}' in {self.file_path}")
                return False

            # Update defaultVariant
            old_variant = flag.get("defaultVariant")
            flag["defaultVariant"] = variant_name

            # Write directly to the file
            with open(self.file_path, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")  # Trailing newline

            logger.info(
                f"Updated flag '{flag_key}' defaultVariant: '{old_variant}' -> '{variant_name}' in {self.file_path}"
            )
            return True

        except FileNotFoundError:
            logger.error(f"Flag file not found: {self.file_path}")
            return False
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in {self.file_path}: {e}")
            return False
        except Exception as e:
            logger.error(f"Error updating flag '{flag_key}': {e}")
            return False

    def get_variant_names(self, flag_key: str) -> list[str]:
        """
        Get the ordered list of variant names for a flag.

        Useful for cycling through variants in admin mode.

        Args:
            flag_key: Flag name

        Returns:
            List of variant names, or empty list on error
        """
        try:
            with open(self.file_path) as f:
                data = json.load(f)

            flags = data.get("flags", {})
            if flag_key not in flags:
                return []

            return list(flags[flag_key].get("variants", {}).keys())
        except Exception as e:
            logger.error(f"Error reading variants for '{flag_key}': {e}")
            return []

    def get_current_variant(self, flag_key: str) -> str | None:
        """
        Get the current defaultVariant for a flag.

        Args:
            flag_key: Flag name

        Returns:
            Current defaultVariant name, or None on error
        """
        try:
            with open(self.file_path) as f:
                data = json.load(f)

            flags = data.get("flags", {})
            if flag_key not in flags:
                return None

            return flags[flag_key].get("defaultVariant")
        except Exception as e:
            logger.error(f"Error reading current variant for '{flag_key}': {e}")
            return None

    def cycle_variant(self, flag_key: str, forward: bool = True) -> str | None:
        """
        Cycle the defaultVariant to the next (or previous) variant.

        Args:
            flag_key: Flag name
            forward: True for next variant, False for previous

        Returns:
            New variant name if successful, None on error
        """
        variants = self.get_variant_names(flag_key)
        if not variants:
            return None

        current = self.get_current_variant(flag_key)
        if current not in variants:
            # Default to first variant
            new_variant = variants[0]
        else:
            idx = variants.index(current)
            new_idx = (idx + 1) % len(variants) if forward else (idx - 1) % len(variants)
            new_variant = variants[new_idx]

        if self.update_default_variant(flag_key, new_variant):
            return new_variant
        return None
