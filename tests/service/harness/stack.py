"""ServiceStack: a composition of real services on a fresh flag plane.

A ``Composition`` names the Python services to run from source; the harness
always adds a fresh ``FlagPlane`` and a ``FakeAudio``. The stack answers the
compose fixture's ``get_service_host(name, port)`` / ``get_service_port(name,
port)`` with the compose service names and CONTAINER ports, so
``tests/integration/helpers.py`` and ``tests/integration/flag_service.py`` take
it in place of ``docker_compose`` unchanged.

Extending (S2+): add a service to ``SERVICES`` (its source dir, compose ports,
env wiring) and, for an in-process fake, a start/stop pair like FakeAudio's;
per-composition env overrides go in ``Composition.env``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .fake_audio import FakeAudio
from .flag_plane import FlagPlane
from .processes import PyService, free_port

# Compose name -> (source dir under services/, compose gRPC port, compose OTEL_SERVICE_NAME).
# Start order is this order: the coordinator needs the controller manager, the
# menu needs both.
SERVICES: dict[str, tuple[str, int, str]] = {
    "controller-manager": ("controller_manager", 50052, "controller-manager-service"),
    "game-coordinator": ("game_coordinator", 50053, "game-coordinator-service"),
    "menu": ("menu", 50054, "menu-service"),
}
MOCK_CONTROL_PORT = 50062  # controller-manager's mock control API in compose
AUDIO_PORT = 50056
FLAG_SERVICE_HTTP_PORT = 8070
# TCP port 1 (tcpmux) is never served here: a dial fails at once.
UNREACHABLE_PORT = 1


@dataclass(frozen=True)
class Composition:
    """Which services run (compose names, from SERVICES) plus env overrides."""

    services: tuple[str, ...]
    # Extra env per compose service name, applied last (e.g. a test-only knob).
    env: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def __post_init__(self):
        unknown = set(self.services) - set(SERVICES)
        if unknown:
            raise ValueError(f"unknown services {sorted(unknown)}; known: {sorted(SERVICES)}")


# Coordinator on mock controllers: headless games, interventions, flag service lifecycle.
GAME = Composition(("controller-manager", "game-coordinator"))
# Plus the menu: lobby, ready state, menu-started games, admin writes.
LOBBY = Composition(("controller-manager", "game-coordinator", "menu"))


class ServiceStack:
    def __init__(self, composition: Composition, workdir: Path):
        self.composition = composition
        self.workdir = workdir
        self.flag_plane = FlagPlane(workdir)
        self.fake_audio = FakeAudio()
        self.services: dict[str, PyService] = {}
        self._ports: dict[tuple[str, int], int] = {}

    # ----------------------------------------------- compose-like address API
    def get_service_host(self, name: str, port: int) -> str:
        self._lookup(name, port)
        return "127.0.0.1"

    def get_service_port(self, name: str, port: int) -> int:
        return self._lookup(name, port)

    def _lookup(self, name: str, port: int) -> int:
        try:
            return self._ports[(name, int(port))]
        except KeyError:
            raise KeyError(
                f"{name}:{port} is not in this service stack (composition {self.composition.services}); "
                f"available: {sorted(self._ports)}"
            ) from None

    # -------------------------------------------------------------- lifecycle
    def start(self) -> ServiceStack:
        try:
            self._start()
        except BaseException:
            self.stop()
            raise
        return self

    def _start(self) -> None:
        plane = self.flag_plane
        plane.start()
        self._ports[("flag-service", FLAG_SERVICE_HTTP_PORT)] = plane.http_port
        self._ports[("flagd", 8013)] = plane.flagd_eval_port
        self._ports[("flagd", 8015)] = plane.flagd_sync_port

        audio_port = self.fake_audio.start()
        self._ports[("audio", AUDIO_PORT)] = audio_port

        ports = {name: free_port() for name in self.composition.services}
        mock_port = free_port()
        for name in self.composition.services:
            self._ports[(name, SERVICES[name][1])] = ports[name]
        if "controller-manager" in ports:
            self._ports[("controller-manager", MOCK_CONTROL_PORT)] = mock_port

        envs = self.service_envs(ports, mock_port=mock_port, audio_port=audio_port)
        for name in self.composition.services:
            service = PyService(
                name,
                SERVICES[name][0],
                grpc_port=ports[name],
                env=envs[name],
                log_path=self.workdir / "logs" / f"{name}.log",
            )
            self.services[name] = service
            service.start()

    def service_envs(self, ports: Mapping[str, int], *, mock_port: int, audio_port: int) -> dict[str, dict[str, str]]:
        """The env of each composition service (on top of ``PyService``'s base env).

        Every address a service could dial points into this stack or at an
        unreachable ``127.0.0.1:1``, never at a default that a compose stack or
        real hardware on the same host answers on.
        """
        plane = self.flag_plane

        def addr(service: str) -> dict[str, str]:
            prefix = service.upper().replace("-", "_")
            # A service not in the composition keeps an address nothing answers
            # on: its callers fail fast instead of reaching a compose stack.
            port = ports.get(service, UNREACHABLE_PORT)
            return {f"{prefix}_HOST": "127.0.0.1", f"{prefix}_PORT": str(port)}

        common = {
            "FLAGD_HOST": "127.0.0.1",
            "FLAGD_PORT": str(plane.flagd_sync_port),
            "FLAG_SERVICE_URL": plane.flag_service_url,
            "AUDIO_HOST": "127.0.0.1",
            "AUDIO_PORT": str(audio_port),
            **addr("controller-manager"),
            **addr("game-coordinator"),
            # No collector in this tier: OTel SDK off (no exporter retries or noise),
            # and an unreachable endpoint in case a code path ignores the switch
            # (lib.telemetry defaults to localhost:4317, a compose collector's port).
            "OTEL_SDK_DISABLED": "true",
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{UNREACHABLE_PORT}",
            "OTEL_SERVICE_NAMESPACE": "joustmania",
        }
        per_service = {
            "controller-manager": {
                "OTEL_SERVICE_NAMESPACE": "infrastructure",
                "MOCK_CONTROL_PORT": str(mock_port),
                "CONTROLLER_NAMES_FILE": str(self.workdir / "controller_names.txt"),
                # Keep the host's BlueZ untouched: startup would rfkill-unblock
                # and enable every adapter it finds over D-Bus.
                "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/nonexistent/joustmania-service-tier",
                # Never real controllers: the rust backend (the default when the
                # controller.backend flag is unreadable) and the python backend
                # would dial localhost:50058/50059, where a compose stack on this
                # host publishes rust-hid.
                "RUST_HID_HOST": "127.0.0.1",
                "RUST_HID_PORT": str(UNREACHABLE_PORT),
                "PYTHON_HID_HOST": "127.0.0.1",
                "PYTHON_HID_PORT": str(UNREACHABLE_PORT),
            },
            "game-coordinator": {"FLAG_SERVICE_INSTANCE": "game-coordinator"},
            "menu": {},
        }
        return {
            name: {
                **common,
                "OTEL_SERVICE_NAME": SERVICES[name][2],
                **per_service[name],
                **self.composition.env.get(name, {}),
            }
            for name in self.composition.services
        }

    def stop(self) -> None:
        """Stop everything, in reverse start order; never raises."""
        for service in reversed(list(self.services.values())):
            service.stop()
        self.fake_audio.stop()
        self.flag_plane.stop()

    # ------------------------------------------------------------- debugging
    def log_tail(self) -> str:
        parts = [s.log_tail() for s in self.services.values()]
        parts.append(self.flag_plane.log_tail())
        return "\n".join(parts)
