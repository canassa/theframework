from __future__ import annotations

import socket
import threading
from collections.abc import Callable

import greenlet

import _framework_core

from theframework.request import Request
from theframework.response import Response

HandlerFunc = Callable[[Request, Response], None]


def _handle_connection(fd: int, handler: HandlerFunc) -> None:
    """Per-connection greenlet: recv -> parse -> Request/Response -> handler -> finalize."""
    buf = b""
    try:
        while True:
            data = _framework_core.green_recv(fd, 8192)
            if not data:
                break
            buf += data

            while True:
                result = _framework_core.http_parse_request(buf)
                if result is None:
                    break  # Need more data

                method, path, body, consumed, keep_alive = result
                raw_bytes = buf[:consumed]
                buf = buf[consumed:]

                request = Request._from_raw(method, path, body, raw_bytes)
                response = Response(fd)

                handler(request, response)
                response._finalize()

                if not keep_alive:
                    _framework_core.green_close(fd)
                    return
    except OSError:
        pass
    finally:
        _framework_core.green_close(fd)


def _run_acceptor(listen_fd: int, handler: HandlerFunc) -> None:
    """Accept connections and spawn handler greenlets."""
    while True:
        try:
            client_fd = _framework_core.green_accept(listen_fd)
        except OSError:
            break
        hub_g = _framework_core.get_hub_greenlet()
        g = greenlet.greenlet(lambda fd=client_fd: _handle_connection(fd, handler), parent=hub_g)
        _framework_core.hub_schedule(g)


def serve(
    handler: HandlerFunc,
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    _ready: threading.Event | None = None,
) -> None:
    """Bind, listen, run hub with connection handler."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    listen_fd = sock.fileno()

    if _ready is not None:
        _ready.set()

    from theframework.monkey._state import mark_hub_running, mark_hub_stopped

    try:

        def _acceptor_with_mark() -> None:
            hub_g = _framework_core.get_hub_greenlet()
            mark_hub_running(hub_g)
            _run_acceptor(listen_fd, handler)

        _framework_core.hub_run(listen_fd, _acceptor_with_mark)
    finally:
        mark_hub_stopped()
        sock.close()
