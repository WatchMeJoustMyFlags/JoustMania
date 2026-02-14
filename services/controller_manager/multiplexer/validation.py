"""
Backend combination validation for MultiplexerBackend.

Bluetooth and HidAPI each scan for PS Move controllers using different
libraries.  Running both at the same time is supported via the multiplexer
— the adapters discover controllers independently and the multiplexer
routes per-serial operations to whichever adapter owns the controller.

Mock is always auto-injected (with 0 controllers) when the multiplexer is
enabled, so it appears in every combination automatically.
"""

VALID_COMBINATIONS = {
    frozenset({"mock"}),
    frozenset({"bluetooth"}),
    frozenset({"hidapi"}),
    frozenset({"mock", "bluetooth"}),
    frozenset({"mock", "hidapi"}),
    frozenset({"bluetooth", "hidapi"}),
    frozenset({"mock", "bluetooth", "hidapi"}),
}


def validate_backend_combination(names: list[str]) -> None:
    """Raise ValueError if the backend combination is unsupported.

    Args:
        names: List of backend name strings (e.g. ["mock", "bluetooth"]).

    Raises:
        ValueError: If the combination would cause hardware conflicts or
            contains duplicate backend names.
    """
    if len(names) != len(set(names)):
        raise ValueError(f"Unsupported backend combination: {names}. Duplicate backend names are not allowed.")
    combo = frozenset(names)
    if combo not in VALID_COMBINATIONS:
        raise ValueError(
            f"Unsupported backend combination: {names}. "
            "Supported: mock, bluetooth, hidapi, bluetooth+hidapi, "
            "and any of these with mock"
        )
