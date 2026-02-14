"""
Multiplexer package for per-controller backend routing.

Phase 1: MultiplexerBackend wraps a single child behind a feature flag.
Phase 2/3: Multiple children with centralized BT discovery.
"""

from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend

__all__ = ["MultiplexerBackend"]
