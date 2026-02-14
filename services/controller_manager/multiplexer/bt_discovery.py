"""
Centralized Bluetooth Discovery (stub for Phase 3).

Phase 2 added multi-backend support (mock + real) but each backend still
runs its own scan_controllers() loop independently. This works because
mock backends don't access real hardware.

Phase 3 will implement centralized BT discovery for multi-adapter setups:
- Single scan loop shared across all BT-based children
- Controller-to-backend assignment based on adapter affinity
- Hot-plug support for adding/removing adapters at runtime

For now, MultiplexerBackend delegates discovery to each child backend's
own scan_controllers() method.
"""

import logging

logger = logging.getLogger(__name__)


class CentralizedBTDiscovery:
    """Stub for centralized Bluetooth discovery.

    Phase 3 will implement:
    - Single scan loop shared across all BT-based children
    - Controller-to-backend assignment based on adapter affinity
    - Hot-plug support for adding/removing adapters at runtime
    """

    def __init__(self):
        logger.info("CentralizedBTDiscovery stub initialized (not active)")
