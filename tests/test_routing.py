"""Tests for the Framework router — real TCP sockets, real io_uring, real HTTP."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Generator

import greenlet
import pytest

import theframework  # noqa: F401 — triggers sys.path setup for _framework_core
import _framework_core

from theframework.app import Framework
from theframework.request import Request
from theframework.response import Response


def _start_app_server(app: Framework) -> Generator[socket.socket]:
    """Start a server running the given Framework app on an ephemeral port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    listen_fd = sock.fileno()

    handler = app._make_handler()

    ready = threading.Event()

    def run() -> None:
        ready.set()
        _framework_core.hub_run(
            listen_fd,
            lambda: _run_acceptor(listen_fd, handler),
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(timeout=5)
    time.sleep(0.05)

    yield sock

    _framework_core.hub_stop()
    thread.join(timeout=5)
    sock.close()


def _run_acceptor(listen_fd: int, handler: object) -> None:
    from theframework.server import _handle_connection, HandlerFunc

    typed_handler: HandlerFunc = handler  # type: ignore[assignment]
    while True:
        try:
            client_fd = _framework_core.green_accept(listen_fd)
        except OSError:
            break
        hub_g = _framework_core.get_hub_greenlet()
        g = greenlet.greenlet(
            lambda fd=client_fd: _handle_connection(fd, typed_handler),
            parent=hub_g,
        )
        _framework_core.hub_schedule(g)


def _connect(listen_sock: socket.socket) -> socket.socket:
    addr: tuple[str, int] = listen_sock.getsockname()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(3.0)
    sock.connect(addr)
    return sock


def _send_http_request(
    sock: socket.socket,
    method: str = "GET",
    path: str = "/",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> bytes:
    hdrs = headers or {}
    if body:
        hdrs.setdefault("Content-Length", str(len(body)))
    hdrs.setdefault("Host", "localhost")

    request = f"{method} {path} HTTP/1.1\r\n"
    for name, value in hdrs.items():
        request += f"{name}: {value}\r\n"
    request += "\r\n"
    sock.sendall(request.encode() + body)

    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(8192)
        if not chunk:
            break
        response += chunk

    header_end = response.index(b"\r\n\r\n") + 4
    headers_part = response[:header_end].decode()
    body_so_far = response[header_end:]

    content_length = 0
    for line in headers_part.split("\r\n"):
        if line.lower().startswith("content-length:"):
            content_length = int(line.split(":", 1)[1].strip())
            break

    while len(body_so_far) < content_length:
        chunk = sock.recv(8192)
        if not chunk:
            break
        body_so_far += chunk

    return response[:header_end] + body_so_far


def _make_app() -> Framework:
    app = Framework()

    @app.route("/a")
    def route_a(request: Request, response: Response) -> None:
        response.write(b"handler-a")

    @app.route("/b")
    def route_b(request: Request, response: Response) -> None:
        response.write(b"handler-b")

    @app.route("/c", methods=["GET", "POST"])
    def route_c(request: Request, response: Response) -> None:
        response.write(b"handler-c")

    @app.route("/users/{id}")
    def route_users(request: Request, response: Response) -> None:
        response.write(f"user={request.params['id']}".encode())

    return app


@pytest.fixture()
def app_server() -> Generator[socket.socket]:
    yield from _start_app_server(_make_app())


def test_basic_routes(app_server: socket.socket) -> None:
    """Three routes, each returns unique body."""
    for path, expected in [("/a", b"handler-a"), ("/b", b"handler-b"), ("/c", b"handler-c")]:
        client = _connect(app_server)
        raw = _send_http_request(client, "GET", path)
        assert raw.startswith(b"HTTP/1.1 200 OK\r\n")
        _, _, resp_body = raw.partition(b"\r\n\r\n")
        assert resp_body == expected
        client.close()


def test_404(app_server: socket.socket) -> None:
    """GET /nonexistent -> 404."""
    client = _connect(app_server)
    raw = _send_http_request(client, "GET", "/nonexistent")
    assert b"HTTP/1.1 404 Not Found\r\n" in raw
    _, _, resp_body = raw.partition(b"\r\n\r\n")
    assert resp_body == b"Not Found"
    client.close()


def test_405(app_server: socket.socket) -> None:
    """POST /a when /a only allows GET -> 405."""
    client = _connect(app_server)
    raw = _send_http_request(client, "POST", "/a")
    assert b"HTTP/1.1 405 Method Not Allowed\r\n" in raw
    _, _, resp_body = raw.partition(b"\r\n\r\n")
    assert resp_body == b"Method Not Allowed"
    client.close()


def test_path_params(app_server: socket.socket) -> None:
    """GET /users/42 -> handler gets request.params['id'] == '42'."""
    client = _connect(app_server)
    raw = _send_http_request(client, "GET", "/users/42")
    assert raw.startswith(b"HTTP/1.1 200 OK\r\n")
    _, _, resp_body = raw.partition(b"\r\n\r\n")
    assert resp_body == b"user=42"
    client.close()
