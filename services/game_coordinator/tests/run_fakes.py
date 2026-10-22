"""Shared fakes for the Werewolf and Zombies ``run()`` tests (UC-022 A1, UC-023 step 1).

Built on the queue stream of ``test_random_teams_formation`` (#1912).
"""

import grpc
from test_random_teams_formation import QueueControllerManager, QueueStream, RecordingLog


def name(event) -> str:
    return getattr(event, "value", event)


def event_order(log: RecordingLog) -> list[str]:
    return [name(value) for kind, value in log.entries if kind == "event"]


class StreamDeadAfterConfig:
    """A gameplay call that breaks right after its config write: later writes and reads raise."""

    producer = None

    def __init__(self, log: RecordingLog):
        self.log = log
        self.configured = False

    def _error(self) -> grpc.aio.AioRpcError:
        return grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, None, None, "stream broke")

    async def write(self, message):
        if self.configured:
            raise self._error()
        self.log.entries.append(("write", message))
        self.configured = message.HasField("config")

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise self._error()


class FirstStreamDeadControllerManager(QueueControllerManager):
    """The first gameplay call dies after its config write; every reopened call is healthy."""

    def StreamGameplayData(self):  # noqa: N802 - gRPC stub method name
        stream = StreamDeadAfterConfig(self.log) if not self.streams else QueueStream(self.log, **self.stream_kwargs)
        self.streams.append(stream)
        return stream
