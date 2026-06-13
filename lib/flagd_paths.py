"""Single source of truth for the active flagd flag directory (issue #959).

Which flag file is active for a given domain used to be decided implicitly and
in scattered places: ``docker-compose.ci.yml`` overlaid ``<domain>.ci.json`` over
the base ``<domain>.json`` in-container path, so in CI flagd served DIFFERENT
content at the same path -- invisibly. Code/tests that needed the *active* host
flag file had no single source to consult, so they hardcoded
``services/flagd/<domain>.json`` or branched on ``os.getenv("CI")``. That desynced
from what flagd actually served and bit us in #957 (a test wrote ``game.json``
while CI flagd served ``game.ci.json``).

The fix is one knob: ``FLAGD_FLAG_DIR`` names the active flag directory, and the
active flag file for a domain is a PLAIN join ``$FLAGD_FLAG_DIR/<domain>.json``.
flagd, the agent writers, and the integration tests all resolve the same way, so
the file a test mutates IS the file flagd serves -- the #957 mismatch is
structurally impossible. CI selects a complete CI flag directory (``services/flagd/ci``)
via a single directory mount + ``FLAGD_FLAG_DIR``; there is no ``.ci.json`` naming
convention, no per-domain overlay mapping, and no manifest.
"""

import os
from pathlib import Path

# The single knob: the directory holding the active flagd flag files.
FLAG_DIR_ENV = "FLAGD_FLAG_DIR"

# Repo-root-relative default flag dir: lib/flagd_paths.py -> lib/ -> repo root.
_DEFAULT_FLAG_DIR = Path(__file__).resolve().parent.parent / "services" / "flagd"


def flag_dir() -> Path:
    """Return the directory holding the active flagd flag files.

    Override with ``FLAGD_FLAG_DIR``; defaults to the repo's ``services/flagd``
    (the host-tooling default). Services run in-container with
    ``FLAGD_FLAG_DIR=/etc/flagd``.
    """
    override = os.environ.get(FLAG_DIR_ENV)
    return Path(override).resolve() if override else _DEFAULT_FLAG_DIR


def active_flag_file(domain: str, *, directory: Path | None = None) -> Path:
    """Return the active host flag-file path for ``domain``: ``<dir>/<domain>.json``.

    This is the SINGLE source of truth for "which host file does flagd serve for
    this domain". A plain join of the flag directory and ``<domain>.json`` -- no
    ``.ci.json`` convention, no overlay mapping. Callers must never hardcode
    ``services/flagd/<domain>.json`` or branch on ``os.getenv("CI")``.

    Args:
        domain: flagd domain / flag-set id (e.g. ``"game"``, ``"interventions"``).
        directory: override the flag directory (default: ``flag_dir()``).

    Returns:
        Absolute path to the active host flag file ``<dir>/<domain>.json``.
    """
    base = directory if directory is not None else flag_dir()
    return base / f"{domain}.json"
