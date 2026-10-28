"""A stand-in for a grpcio ``StreamIO`` call: iterable and cancellable (#2266).

The HID adapters' reader cancels its call whenever it ends, so a fake call must
offer ``cancel()`` like the real one (a no-op once the call has finished).
"""

from collections.abc import Iterable


class FakeStreamCall:
    def __init__(self, frames: Iterable = ()):
        self._frames = iter(frames)
        self.cancelled = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._frames)

    def cancel(self) -> bool:
        self.cancelled = True
        return True
