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

Telemetry is off by default (``OTEL_SDK_DISABLED``). ``Composition(...,
otlp=True)`` starts an ``OtlpSink`` (``stack.otlp_sink``) and points every
service's OTLP exporter at it instead.

``Composition(..., agent=True)`` (needs ``otlp=True``; the ``AGENT``
composition) adds the Go agent built at HEAD (``stack.agent``) and a
scriptable fake OpenAI-compatible backend (``stack.fake_openai``): the sink tees
the services' traces and metrics to the agent, as the collector does in compose,
and the agent exports its own telemetry to the sink.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .agent import AgentProcess
from .fake_audio import FakeAudio
from .fakes.claude import API_KEY as FAKE_CLAUDE_API_KEY
from .fakes.claude import MODEL as FAKE_CLAUDE_MODEL
from .fakes.claude import FakeClaude
from .fakes.github import FakeGitHub
from .fakes.jev import API_KEY as FAKE_JEV_API_KEY
from .fakes.jev import FakeJev
from .fakes.openai import API_KEY as FAKE_OPENAI_API_KEY
from .fakes.openai import FakeOpenAI
from .fakes.otlp import OtlpSink
from .flag_plane import FlagPlane
from .processes import PyService, ServiceStartError, free_port

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
OTLP_GRPC_PORT = 4317  # otel-collector's OTLP/gRPC port in compose
FLAG_SERVICE_HTTP_PORT = 8070
AGENT_OTLP_PORT = 4317  # the agent's OTLP/gRPC receiver in compose
FAKE_OPENAI_PORT = 8080  # fake-openai in docker-compose.decide-e2e.yml
# The ci/agent.json variant of decision.openai.base_url the agent is pointed at the fake with.
FAKE_OPENAI_VARIANT = ("agent", "decision.openai.base_url", "fake")
# Same for the Jev decide backend (#1404): the ci/agent.json `fake` variant of decision.jev.base_url.
FAKE_JEV_VARIANT = ("agent", "decision.jev.base_url", "fake")
# TCP port 1 (tcpmux) is never served here: a dial fails at once.
UNREACHABLE_PORT = 1


@dataclass(frozen=True)
class Composition:
    """Which services run (compose names, from SERVICES) plus env overrides."""

    services: tuple[str, ...]
    # Extra env per compose service name, applied last (e.g. a test-only knob).
    env: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    # Telemetry on, exported to an in-process OtlpSink (``stack.otlp_sink``).
    otlp: bool = False
    # The Go agent (``stack.agent``) plus a fake OpenAI backend (``stack.fake_openai``).
    agent: bool = False
    # Also a fake Claude (``stack.fake_claude``) the agent's retro and proposer text backend is
    # pointed at (AGENT_INFERENCE_BACKEND=claude + AGENT_INFERENCE_BASE_URL; needs ``agent``).
    fake_claude: bool = False
    # Also a fake Jev (``stack.fake_jev``) behind the ``fake`` variant of ``decision.jev.base_url``;
    # the agent gets the fake's dummy ``JEV_API_KEY`` (without this flag it has none: UC-044 A1).
    fake_jev: bool = False
    # Also a fake GitHub REST API (``stack.fake_github``) the agent's promoter opens issues and
    # PRs on (the promotion env gate + GITHUB_API_URL; needs ``agent``). Never github.com.
    fake_github: bool = False
    # Extra env for the agent process, applied last (e.g. the experiment seed of TC-011).
    agent_env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if self.fake_claude and not self.agent:
            raise ValueError("fake_claude=True needs agent=True")
        if self.fake_jev and not self.agent:
            raise ValueError("fake_jev=True needs agent=True")
        if self.fake_github and not self.agent:
            raise ValueError("fake_github=True needs agent=True")
        unknown = set(self.services) - set(SERVICES)
        if unknown:
            raise ValueError(f"unknown services {sorted(unknown)}; known: {sorted(SERVICES)}")
        if self.agent and not (self.otlp and "game-coordinator" in self.services):
            raise ValueError("agent=True needs otlp=True (the agent observes through OTLP) and the game coordinator")


# The menu reports healthy before its boot lobby entry (menu_auto_start, services/menu/server.py);
# a StopMenu/StartMenu restart that lands in that window sees STOPPED, then the boot flips the menu to
# RUNNING and StartMenu fails "Menu already running" (#1798). The stack is ready after the entry.
READY_LOG_MARKERS = {"menu": "Menu auto-started"}

# Coordinator on mock controllers: headless games, interventions, flag service lifecycle.
GAME = Composition(("controller-manager", "game-coordinator"))
# Plus the menu: lobby, ready state, menu-started games, admin writes.
LOBBY = Composition(("controller-manager", "game-coordinator", "menu"))
# Plus the agent at HEAD observing over OTLP and a fake OpenAI backend: in-game decisions.
AGENT = Composition(("controller-manager", "game-coordinator", "menu"), otlp=True, agent=True)
# Plus a fake Claude behind the agent's retro and proposer text backend (TC-010).
AGENT_CLAUDE = Composition(("controller-manager", "game-coordinator", "menu"), otlp=True, agent=True, fake_claude=True)
# Plus a fake Jev behind the agent's `jev` decide backend, with its dummy key (TC-008).
AGENT_JEV = Composition(("controller-manager", "game-coordinator", "menu"), otlp=True, agent=True, fake_jev=True)


class ServiceStack:
    def __init__(self, composition: Composition, workdir: Path):
        self.composition = composition
        self.workdir = workdir
        self.fake_openai: FakeOpenAI | None = FakeOpenAI(workdir) if composition.agent else None
        self.fake_claude: FakeClaude | None = FakeClaude() if composition.fake_claude else None
        self.fake_jev: FakeJev | None = FakeJev() if composition.fake_jev else None
        self.fake_github: FakeGitHub | None = FakeGitHub() if composition.fake_github else None
        # Picked up front: the sink tees to it, and the agent must not be found by a stale address.
        self.agent_port: int | None = free_port() if composition.agent else None
        self.agent: AgentProcess | None = None
        variant_values = {FAKE_OPENAI_VARIANT: self.fake_openai.base_url} if self.fake_openai else {}
        if self.fake_jev is not None:
            variant_values[FAKE_JEV_VARIANT] = self.fake_jev.base_url
        self.flag_plane = FlagPlane(workdir, variant_values=variant_values or None)
        self.fake_audio = FakeAudio()
        forward_to = f"127.0.0.1:{self.agent_port}" if self.agent_port else None
        self.otlp_sink: OtlpSink | None = OtlpSink(forward_to=forward_to) if composition.otlp else None
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
        if self.fake_openai is not None:
            self._ports[("fake-openai", FAKE_OPENAI_PORT)] = self.fake_openai.start()
        if self.fake_claude is not None:
            self.fake_claude.start()
        if self.fake_jev is not None:
            self.fake_jev.start()
        if self.fake_github is not None:
            self.fake_github.start()
        plane = self.flag_plane
        plane.start()
        self._ports[("flag-service", FLAG_SERVICE_HTTP_PORT)] = plane.http_port
        self._ports[("flagd", 8013)] = plane.flagd_eval_port
        self._ports[("flagd", 8015)] = plane.flagd_sync_port

        audio_port = self.fake_audio.start()
        self._ports[("audio", AUDIO_PORT)] = audio_port

        otlp_port = None
        if self.otlp_sink is not None:
            otlp_port = self.otlp_sink.start()
            self._ports[("otel-collector", OTLP_GRPC_PORT)] = otlp_port

        ports = {name: free_port() for name in self.composition.services}
        mock_port = free_port()
        for name in self.composition.services:
            self._ports[(name, SERVICES[name][1])] = ports[name]
        if "controller-manager" in ports:
            self._ports[("controller-manager", MOCK_CONTROL_PORT)] = mock_port

        if self.composition.agent:
            # Before the services: their first exports (game start) reach a live agent.
            self.agent = AgentProcess(
                self.workdir,
                listen_port=self.agent_port,
                flagd_sync_port=plane.flagd_sync_port,
                flag_service_url=plane.flag_service_url,
                otlp_endpoint=self.otlp_sink.endpoint,
                coordinator_port=ports["game-coordinator"],
                mock_control_port=mock_port,
                openai_api_key=FAKE_OPENAI_API_KEY,
                extra_env={
                    **self._inference_env(),
                    **self._jev_env(),
                    **(self.fake_github.agent_env() if self.fake_github else {}),
                    **self.composition.agent_env,
                },
            )
            self._ports[("agent", AGENT_OTLP_PORT)] = self.agent_port
            self.agent.start()

        envs = self.service_envs(ports, mock_port=mock_port, audio_port=audio_port, otlp_port=otlp_port)
        for name in self.composition.services:
            service = PyService(
                name,
                SERVICES[name][0],
                grpc_port=ports[name],
                env=envs[name],
                log_path=self.workdir / "logs" / f"{name}.log",
                ready_log_marker=READY_LOG_MARKERS.get(name),
            )
            self.services[name] = service
            service.start()

    def _jev_env(self) -> dict[str, str]:
        """The Jev decide backend's key: the fake's dummy key, or none (UC-044 A1)."""
        return {"JEV_API_KEY": FAKE_JEV_API_KEY} if self.fake_jev is not None else {}

    def _inference_env(self) -> dict[str, str]:
        """The agent's retro/proposer text backend: the fake Claude, or the stub when there is none."""
        if self.fake_claude is None:
            return {}
        return {
            "AGENT_INFERENCE_BACKEND": "claude",
            "ANTHROPIC_API_KEY": FAKE_CLAUDE_API_KEY,
            "AGENT_INFERENCE_BASE_URL": self.fake_claude.base_url,
            "AGENT_INFERENCE_MODEL": FAKE_CLAUDE_MODEL,
            # Bootstrap default only: the live experiments_enabled flag decides (FR-066).
            "AGENT_EXPERIMENTS_ENABLED": "true",
            # The proposer gate's operator ceiling (#1810, C-034): the M7-4 proposer runs
            # only when it is true AND experiment_proposer_enabled is on (ci default on).
            "AGENT_EXPERIMENT_PROPOSER_ENABLED": "true",
            # The same gate opens the retro-seeded thesis proposer; a candidate list
            # naming no game flag keeps it (and the dynamic declarer) from declaring a
            # second experiment next to the M7-4 one TC-010 asserts on.
            "AGENT_EXPERIMENT_DYNAMIC_CANDIDATES": "tc010_no_such_flag",
            # The experiment journal (default /var/lib/joustmania/...) lives in the stack's workdir.
            "AGENT_EXPERIMENT_DIR": str(self.workdir / "experiments"),
        }

    def service_envs(
        self, ports: Mapping[str, int], *, mock_port: int, audio_port: int, otlp_port: int | None = None
    ) -> dict[str, dict[str, str]]:
        """The env of each composition service (on top of ``PyService``'s base env).

        Every address a service could dial points into this stack or at an
        unreachable ``127.0.0.1:1``, never at a default that a compose stack or
        real hardware on the same host answers on. With ``otlp_port`` the OTel
        SDK is on and exports to that port (the stack's ``OtlpSink``).
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
        if otlp_port is not None:
            del common["OTEL_SDK_DISABLED"]
            common["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://127.0.0.1:{otlp_port}"
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
        # After the services (their last exports), before the sink (its own last export).
        if self.agent is not None:
            self.agent.stop()
        self.fake_audio.stop()
        # After the services: their exporters flush to the sink on shutdown.
        if self.otlp_sink is not None:
            self.otlp_sink.stop()
        self.flag_plane.stop()
        if self.fake_openai is not None:
            self.fake_openai.stop()
        if self.fake_claude is not None:
            self.fake_claude.stop()
        if self.fake_jev is not None:
            self.fake_jev.stop()
        if self.fake_github is not None:
            self.fake_github.stop()

    # ------------------------------------------------------------- debugging
    def log_tail(self) -> str:
        parts = [s.log_tail() for s in self.services.values()]
        if self.agent is not None:
            parts.append(self.agent.log_tail())
        if self.fake_openai is not None:
            parts.append(self.fake_openai.log_tail())
        if self.fake_claude is not None:
            parts.append(self.fake_claude.log_tail())
        if self.fake_jev is not None:
            parts.append(self.fake_jev.log_tail())
        if self.fake_github is not None:
            parts.append(self.fake_github.log_tail())
        parts.append(self.flag_plane.log_tail())
        return "\n".join(parts)


logger = logging.getLogger(__name__)

START_ATTEMPTS = 3
_PORT_CONFLICT_MARKERS = ("address already in use", "failed to bind to address")


def start_stack(
    composition: Composition,
    make_workdir: Callable[[], Path],
    *,
    attempts: int = START_ATTEMPTS,
) -> ServiceStack:
    """A started stack; retried on fresh ports when a service lost a port race.

    ``free_port()`` cannot reserve a port: another process (an outgoing
    connection's source port, a parallel job) may take it before the service
    binds it (#1763). Such a start fails with "address already in use" in the
    log tail; the stack is then rebuilt, with a new workdir and new ports. Any
    other start failure is raised at once.
    """
    for attempt in range(1, attempts + 1):
        stack = ServiceStack(composition, make_workdir())
        try:
            return stack.start()
        except ServiceStartError as exc:
            message = str(exc)
            if not any(marker in message.lower() for marker in _PORT_CONFLICT_MARKERS) or attempt == attempts:
                raise
            logger.warning(
                "service-tier stack start %d/%d lost a port race (#1763), retrying on fresh ports: %s",
                attempt,
                attempts,
                message.splitlines()[0] if message else "",
            )
    raise AssertionError("unreachable")  # pragma: no cover
