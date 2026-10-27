"""Spec classes for autospec'd gRPC stubs (the generated stubs bind their methods in __init__)."""

import asyncio


class MenuStubSpec:
    async def ProcessInput(self, request, timeout=None): ...
    async def GetMenuStatus(self, request, timeout=None): ...


class CoordinatorStubSpec:
    async def ForceEndGame(self, request, timeout=None): ...


class AudioStubSpec:
    async def PlaySound(self, request, timeout=None): ...
    async def StopSound(self, request, timeout=None): ...


class GatedSleep:
    """An injectable sleep that returns only when the test opens the gate."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        await self.gate.wait()
