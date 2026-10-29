"""Inert defaults for optional backend / adapter features.

The MockAdapter implements the UC-046 motion patterns and the #926 death latch;
the #1837 trigger latch is real on hardware too (HidServiceAdapter.consume_trigger_press),
and the MultiplexerBackend overrides every hook to route it to the owning adapter.
Every backend and adapter inherits these defaults, so callers (the servicer, the
multiplexer) call them without probing for the attribute.
"""


class MockFeatureHooks:
    """No-op defaults; overridden where a backend implements the feature (see the module docstring)."""

    def consume_death_frame(self, serial: str) -> None:  # noqa: ARG002, B027
        """Account one delivered gameplay frame against a pending death latch (#926). Default: no latch."""

    def consume_trigger_press(self, serial: str) -> bool:  # noqa: ARG002
        """Whether the trigger was pressed since the last call, then clear (#1837). Default: no latch."""
        return False

    def pattern_frame(self, serial: str, subscriber_id: str, explicit: bool) -> tuple[dict, bool] | None:  # noqa: ARG002
        """Acceleration and trigger to deliver for a pattern controller, None without a pattern."""
        return None

    def pattern_stream_rate(self, serials) -> int | None:  # noqa: ARG002
        """Pinned stream rate when ``serials`` names a pattern controller, else None."""
        return None

    def record_pattern_overrun(self, subscriber_id: str) -> int:  # noqa: ARG002
        """Count a frame overrun for the pattern controllers a stream owns; returns how many."""
        return 0

    def release_pattern_stream(self, subscriber_id: str) -> None:  # noqa: ARG002, B027
        """A stream ended: its pattern controllers may be claimed by the next one. Default: none."""
