"""Typed outcomes a game handler raises when it applies nothing (#1837).

Kept in its own module so ``games/base.py`` can raise it without importing
``interventions.py`` (which imports the games indirectly).
"""


class InterventionNotApplied(Exception):  # noqa: N818 - an outcome, not an error
    """The handler ran but applied nothing on purpose.

    The intervention manager records it as ``blocked=true`` with ``reason`` as the
    ``block_reason`` and refunds the backstop reservation (UC-040 A22, A31).
    """

    def __init__(self, reason: str, detail: str = "", *, refused_serial: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        # The player whose check refused a group shield (UC-040 A48); "" otherwise.
        self.refused_serial = refused_serial
