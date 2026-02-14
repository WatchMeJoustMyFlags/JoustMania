"""
Backend combination validation for MultiplexerBackend.

Multiple real backends (Bluetooth, HidAPI) conflict because they access the
same physical controllers via global APIs. Only mock + one real backend is safe.
"""

VALID_COMBINATIONS = {
    frozenset({"mock"}),
    frozenset({"bluetooth"}),
    frozenset({"hidapi"}),
    frozenset({"mock", "bluetooth"}),
    frozenset({"mock", "hidapi"}),
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
            "Multiple real backends conflict. "
            "Supported: mock, bluetooth, hidapi, mock+bluetooth, mock+hidapi"
        )
