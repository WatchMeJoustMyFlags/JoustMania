"""A per-frame error in StreamGameplayData ends the call non-OK; a normal end stays OK (#1930, UC-030 BR-006)."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import grpc
import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from test_identify_controller import SERIAL, _game_config, servicer  # noqa: F401  (pytest fixture)

from proto import controller_manager_pb2


class _AbortedError(Exception):
    pass


class _Context:
    def __init__(self):
        self.cancelled_flag = False
        self.abort_args = None

    def cancelled(self):
        return self.cancelled_flag

    async def abort(self, code, details):
        self.abort_args = (code, details)
        raise _AbortedError(details)


async def _requests():
    yield controller_manager_pb2.GameplayStreamControl(config=_game_config(SERIAL))
    await asyncio.Event().wait()


@pytest.fixture
def stream_servicer(servicer):  # noqa: F811
    servicer.state_builder.build_state = MagicMock(
        side_effect=lambda serial, _info: controller_manager_pb2.ControllerState(serial=serial)
    )
    servicer.discovery_loop.drain_health_counters.return_value = (0, 0, 0)
    return servicer


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", flow="main", br=["BR-006"])
async def test_frame_error_ends_the_call_with_internal(stream_servicer):
    """UC-030 BR-006: an unexpected per-frame exception aborts INTERNAL, never ends OK."""
    stream_servicer._build_gameplay_update = MagicMock(side_effect=RuntimeError("boom"))
    context = _Context()

    with pytest.raises(_AbortedError):
        async for _ in stream_servicer.StreamGameplayData(_requests(), context):
            pass

    code, details = context.abort_args
    assert code == grpc.StatusCode.INTERNAL
    assert "boom" in details


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", flow="main", br=["BR-006"])
async def test_client_cancel_ends_ok(stream_servicer):
    """UC-030 BR-006: a client cancel ends the stream normally, without abort."""
    context = _Context()
    frames = 0
    async for _ in stream_servicer.StreamGameplayData(_requests(), context):
        frames += 1
        context.cancelled_flag = True

    assert frames >= 1
    assert context.abort_args is None
