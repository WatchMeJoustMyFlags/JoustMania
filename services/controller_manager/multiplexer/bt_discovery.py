"""
Centralized Bluetooth Discovery (stub for Phase 2/3).

In Phase 2, this module will provide a single BT discovery loop that scans
for controllers and routes them to the appropriate backend child. This avoids
multiple backends independently scanning the same Bluetooth adapter.

For Phase 1, MultiplexerBackend delegates discovery to each child backend's
own scan_controllers() method.
"""

import logging

logger = logging.getLogger(__name__)


class CentralizedBTDiscovery:
    """Stub for centralized Bluetooth discovery.

    Phase 2/3 will implement:
    - Single scan loop shared across all BT-based children
    - Controller-to-backend assignment based on adapter affinity
    - Hot-plug support for adding/removing adapters at runtime
    """

    def __init__(self):
        logger.info("CentralizedBTDiscovery stub initialized (Phase 1 — not active)")
