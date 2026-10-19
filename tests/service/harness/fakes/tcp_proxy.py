"""TcpProxy: a loopback TCP forwarder whose connections a test can cut.

It stands between one service and another (e.g. the menu and the controller
manager: ``Composition(env={"menu": {"CONTROLLER_MANAGER_PORT": str(proxy.port)}})``)
so a test can drop the gRPC connection without restarting either process, the
way a network blip or a controller-manager hiccup does::

    proxy = TcpProxy()                      # binds now: the port goes into the composition (in a fixture)
    proxy.start(target_port)                # forwards from here on
    proxy.drop_connections()                # both ends see the connection reset
    proxy.wait_for_connections(n, timeout)  # the client dialled again

Connections that arrive before ``start`` wait in the listen backlog. Threads
only, no event loop: it works under any test's loop. ``wait_for_connections``
waits on a condition, never on a sleep. Create it in a fixture that also owns
``stop()`` (try/finally), never at import: a deselected test would leak it.
"""

from __future__ import annotations

import contextlib
import socket
import threading

_CHUNK = 65536


class TcpProxy:
    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.port: int = self._listener.getsockname()[1]
        self._target_port: int | None = None
        self._lock = threading.Condition()
        self._sockets: list[socket.socket] = []
        self._accepted = 0
        self._stopped = False
        self._thread: threading.Thread | None = None

    @property
    def accepted(self) -> int:
        """Connections accepted (and forwarded) so far."""
        with self._lock:
            return self._accepted

    def start(self, target_port: int) -> None:
        """Forward every connection to ``127.0.0.1:target_port``."""
        self._target_port = target_port
        self._thread = threading.Thread(target=self._accept_loop, name=f"tcp-proxy-{self.port}", daemon=True)
        self._thread.start()

    def drop_connections(self) -> int:
        """Reset every open connection (client and upstream side); returns how many sockets were closed."""
        with self._lock:
            sockets, self._sockets = self._sockets, []
        for sock in sockets:
            _close(sock)
        return len(sockets)

    def wait_for_connections(self, count: int, timeout: float) -> bool:
        """True once ``accepted`` reaches ``count`` within ``timeout`` seconds."""
        with self._lock:
            return self._lock.wait_for(lambda: self._accepted >= count, timeout=timeout)

    def stop(self) -> None:
        """Close the listener and every connection; never raises.

        ``shutdown()`` on the listener wakes a blocked ``accept()`` on Linux; on
        other systems the join below may wait out its timeout.
        """
        with self._lock:
            self._stopped = True
        _close(self._listener)
        self.drop_connections()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _accept_loop(self) -> None:
        while not self._stopped:
            try:
                client, _addr = self._listener.accept()
            except OSError:
                return
            try:
                upstream = socket.create_connection(("127.0.0.1", self._target_port), timeout=5)
                upstream.settimeout(None)
            except OSError:
                _close(client)
                continue
            with self._lock:
                if self._stopped:
                    # Accepted while stop() ran: its drop_connections() never saw this pair.
                    _close(client)
                    _close(upstream)
                    return
                self._sockets += [client, upstream]
                self._accepted += 1
                self._lock.notify_all()
            for source, sink in ((client, upstream), (upstream, client)):
                threading.Thread(target=_pump, args=(source, sink), daemon=True).start()


def _pump(source: socket.socket, sink: socket.socket) -> None:
    try:
        while data := source.recv(_CHUNK):
            sink.sendall(data)
    except OSError:
        pass
    finally:
        _close(source)
        _close(sink)


def _close(sock: socket.socket) -> None:
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()
