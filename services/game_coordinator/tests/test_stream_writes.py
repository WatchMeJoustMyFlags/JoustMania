"""One write in flight per gameplay call, and a started write is never cancelled (#2096; UC-030 BR-006)."""

import ast
import asyncio
import sys
from pathlib import Path

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))

from services.game_coordinator.stream_writes import serialized_write

SERVICE_DIR = test_dir.parent


class GatedStream:
    """A write that stays in flight until released; records order and overlaps."""

    def __init__(self):
        self.gate = asyncio.Event()
        self.started: list[str] = []
        self.finished: list[str] = []
        self.in_flight = 0
        self.overlaps = 0

    async def write(self, message):
        self.in_flight += 1
        self.overlaps += self.in_flight > 1
        self.started.append(message)
        try:
            await self.gate.wait()
            self.finished.append(message)
        finally:
            self.in_flight -= 1


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", br=["BR-006"])
async def test_a_cancelled_writer_leaves_its_write_running_and_holds_the_slot():
    """A cancel propagates at once, the started write is not cancelled, and the next write waits for it."""
    stream = GatedStream()
    first = asyncio.create_task(serialized_write(stream, "first"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert stream.started == ["first"]
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(serialized_write(stream, "second"))
    await asyncio.sleep(0.01)
    assert stream.started == ["first"]  # still waiting for the first write
    stream.gate.set()
    await asyncio.wait_for(second, timeout=1)
    assert stream.finished == ["first", "second"]
    assert stream.overlaps == 0


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", br=["BR-006"])
async def test_a_failed_write_raises_to_its_caller_and_frees_the_slot():
    class Failing:
        calls = 0

        async def write(self, _message):
            Failing.calls += 1
            if Failing.calls == 1:
                raise asyncio.InvalidStateError("RPC already finished")

    stream = Failing()
    with pytest.raises(asyncio.InvalidStateError):
        await serialized_write(stream, "a")
    await asyncio.wait_for(serialized_write(stream, "b"), timeout=1)
    assert Failing.calls == 2


def _raw_stream_writes(path: Path) -> list[str]:
    """``<x>.write(...)`` calls whose receiver names a stream, outside stream_writes.py."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "write"
            and "stream" in ast.unparse(node.func.value).lower()
        ):
            found.append(f"{path.relative_to(SERVICE_DIR)}:{node.lineno} {ast.unparse(node.func)}")
    return found


@pytest.mark.uc("UC-030", br=["BR-006"])
def test_no_gameplay_stream_write_bypasses_serialized_write():
    """Every gameplay-stream write in the coordinator goes through serialized_write (#2096 review)."""
    offenders = []
    for path in sorted(SERVICE_DIR.rglob("*.py")):
        relative = path.relative_to(SERVICE_DIR)
        if relative.parts[0] in ("tests", ".venv") or path.name == "stream_writes.py":
            continue
        offenders += _raw_stream_writes(path)
    assert offenders == []


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", br=["BR-006"])
async def test_a_failed_write_left_by_a_cancelled_caller_is_logged(caplog):
    """The orphaned write's failure is not lost: it is logged at debug level."""
    gate = asyncio.Event()

    class FailsLater:
        async def write(self, _message):
            await gate.wait()
            raise asyncio.InvalidStateError("RPC already finished")

    caller = asyncio.create_task(serialized_write(FailsLater(), "m"))
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    with caplog.at_level("DEBUG", logger="services.game_coordinator.stream_writes"):
        gate.set()
        for _ in range(5):
            await asyncio.sleep(0)
    assert "left running by a cancelled caller failed" in caplog.text
