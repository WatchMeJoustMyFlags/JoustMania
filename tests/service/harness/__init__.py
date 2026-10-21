"""Service tier (T2) harness: see tests/service/README.md."""

from .fake_audio import AudioCall, FakeAudio
from .flag_plane import FlagPlane
from .processes import ManagedProcess, PyService, ServiceStartError, free_port
from .stack import AGENT, AGENT_CLAUDE, AGENT_JEV, GAME, LOBBY, SERVICES, Composition, ServiceStack

__all__ = [
    "AGENT",
    "AGENT_CLAUDE",
    "AGENT_JEV",
    "GAME",
    "LOBBY",
    "SERVICES",
    "AudioCall",
    "Composition",
    "FakeAudio",
    "FlagPlane",
    "ManagedProcess",
    "PyService",
    "ServiceStack",
    "ServiceStartError",
    "free_port",
]
