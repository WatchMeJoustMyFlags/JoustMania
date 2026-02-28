"""
Multiplexer package for per-controller backend routing.

Phase 1: MultiplexerBackend wraps a single child behind a feature flag.
Phase 2: Multiple children (mock + real) with validated combinations.
Phase 3: Centralized BT discovery for multi-adapter support.
Phase 4: ControllerIOAdapter + centralized state tracking.
"""

from services.controller_manager.multiplexer.adapter import ControllerIOAdapter
from services.controller_manager.multiplexer.bt_discovery import CentralizedBTDiscovery
from services.controller_manager.multiplexer.mobile_adapter import MobileAdapter
from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend
from services.controller_manager.multiplexer.validation import validate_backend_combination

__all__ = [
    "ControllerIOAdapter",
    "CentralizedBTDiscovery",
    "MobileAdapter",
    "MultiplexerBackend",
    "validate_backend_combination",
]
