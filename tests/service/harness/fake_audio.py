"""FakeAudio: an in-process AudioService that records every call.

The real audio service needs ALSA and assets; the service tier replaces it with
this fake on an ephemeral port (``AUDIO_HOST``/``AUDIO_PORT`` of the callers). A
test reads ``calls`` (or ``calls_of("PlaySound")``) to assert what the
coordinator or menu asked for. Every RPC answers ``success=True``.
"""

from __future__ import annotations

import threading
from concurrent import futures
from dataclasses import dataclass, field

import grpc
from google.protobuf.json_format import MessageToDict

from proto import audio_pb2, audio_pb2_grpc


@dataclass
class AudioCall:
    method: str
    request: dict = field(default_factory=dict)


class _Servicer(audio_pb2_grpc.AudioServiceServicer):
    def __init__(self, record):
        self._record = record

    def PlaySound(self, request, context):
        self._record("PlaySound", request)
        return audio_pb2.PlaySoundResponse(success=True)

    def PlayMusic(self, request, context):
        self._record("PlayMusic", request)
        return audio_pb2.PlayMusicResponse(success=True)

    def StopMusic(self, request, context):
        self._record("StopMusic", request)
        return audio_pb2.StopMusicResponse(success=True)

    def ChangeTempo(self, request, context):
        self._record("ChangeTempo", request)
        return audio_pb2.ChangeTempoResponse(success=True)

    def SetVolume(self, request, context):
        self._record("SetVolume", request)
        return audio_pb2.SetVolumeResponse(success=True)


class FakeAudio:
    def __init__(self):
        self._lock = threading.Lock()
        self._calls: list[AudioCall] = []
        self._server: grpc.Server | None = None
        self.port: int | None = None

    def _record(self, method: str, request) -> None:
        with self._lock:
            self._calls.append(AudioCall(method, MessageToDict(request, preserving_proto_field_name=True)))

    @property
    def calls(self) -> list[AudioCall]:
        with self._lock:
            return list(self._calls)

    def calls_of(self, method: str) -> list[AudioCall]:
        return [c for c in self.calls if c.method == method]

    def clear(self) -> None:
        with self._lock:
            self._calls.clear()

    def start(self) -> int:
        self._server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
        audio_pb2_grpc.add_AudioServiceServicer_to_server(_Servicer(self._record), self._server)
        self.port = self._server.add_insecure_port("127.0.0.1:0")
        self._server.start()
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.stop(grace=1).wait()
            self._server = None
