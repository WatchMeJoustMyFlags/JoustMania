"""
Structural types for the game an intervention handler acts on (#2361).

Every mode in ``game_factory._GAME_CLASSES`` subclasses ``BaseGameMode`` and so
provides each member below, so a handler can use them directly instead of probing
with ``getattr`` / ``hasattr``. ``BaseGameMode`` does not inherit these Protocols:
conformance is checked at runtime by ``tests/test_intervention_target.py``.

Do not mark these ``@runtime_checkable`` and do not ``isinstance`` against them in
production code; that probes every member on every call. Types from ``games.base``
are imported under ``TYPE_CHECKING`` only, so importing this module does not pull
in the large ``games.base`` module.
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol

from openfeature.evaluation_context import EvaluationContext
from opentelemetry import trace

from lib.types import Sensitivity

if TYPE_CHECKING:
    from services.game_coordinator.games.base import RampDescriptor


class InterventionPlayer(Protocol):
    """The ``games.base.Player`` fields intervention handlers read or write."""

    serial: str
    alive: bool
    name: str
    span: trace.Span | None
    battery_pct: float | None
    sensitivity_factor: float
    handicap_factor: float


class InterventionTarget(Protocol):
    """What intervention handlers use on a live game (the shape of ``BaseGameMode``)."""

    # Mapping, not dict: dict is invariant in its value type.
    players: Mapping[str, InterventionPlayer]
    audio_client: Any | None
    gameplay_stream: Any | None
    game_kind: str
    experiment_id: str
    arm: str
    music_speed: float
    music_windows: Any
    init_music_windows: Any
    tempo_override: float | None
    tempo_ramp: "RampDescriptor | None"
    global_difficulty_factor: float

    @property
    def flag_eval_context(self) -> EvaluationContext: ...

    def get_game_name(self) -> str: ...

    def apply_sensitivity_override(self, value: Sensitivity) -> None: ...

    def clear_sensitivity_override(self) -> None: ...

    async def _play_sound(
        self,
        sound: Any,
        priority: int = 2,
        parent_context: Any | None = None,
        volume: float = 1.0,
    ) -> Any: ...

    async def _kill_player(self, serial: str, accel_mag: float, reason: str = "motion") -> bool: ...

    async def grant_shield(self, serial: str, seconds: float) -> bool: ...

    async def grant_toughness_boost(self, serial: str, seconds: float, boost: float | None = None) -> bool: ...

    async def warn_player(self, serial: str) -> bool: ...

    async def apply_soft_penalty(self, serial: str, seconds: float, factor: float | None = None) -> bool: ...

    async def apply_auto_rubberband(self, strength: str, seconds: float | None = None) -> int: ...

    async def revive_player(self, serial: str, spawn_grace: float | None = None) -> bool: ...
