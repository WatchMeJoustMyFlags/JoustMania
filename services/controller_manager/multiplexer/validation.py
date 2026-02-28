"""
Backend combination validation for MultiplexerBackend.

Mock is always auto-injected (with 0 controllers) when the multiplexer is
enabled, so it appears in every combination automatically.
"""

VALID_COMBINATIONS = {
    frozenset({"mock"}),
    frozenset({"hidapi"}),
    frozenset({"mock", "hidapi"}),
}


def validate_backend_combination(names: list[str]) -> None:
    """Raise ValueError if the backend combination is unsupported.

    Args:
        names: List of backend name strings (e.g. ["mock", "hidapi"]).

    Raises:
        ValueError: If the combination would cause hardware conflicts or
            contains duplicate backend names.
    """
    if len(names) != len(set(names)):
        raise ValueError(f"Unsupported backend combination: {names}. Duplicate backend names are not allowed.")
    combo = frozenset(names)
    if combo not in VALID_COMBINATIONS:
        raise ValueError(
            f"Unsupported backend combination: {names}. Supported: mock, hidapi, and any of these with mock"
        )
