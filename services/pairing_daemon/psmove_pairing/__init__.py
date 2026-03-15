"""PS Move controller pairing daemon package."""

from lib.telemetry import init_telemetry

from .daemon import PairingDaemon

__all__ = ["PairingDaemon", "init_telemetry"]
