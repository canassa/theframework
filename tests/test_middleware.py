"""Tests for middleware — real TCP sockets, real io_uring, real HTTP."""

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
from theframework.server import HandlerFunc


def _start_app_server(app: Framework) -> Generator[socket.socket]:
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


def _run_acceptor(listen_fd: int, handler: HandlerFunc) -> None:
    from theframework.server import _handle_connection

    while True:
        try:
            client_fd = _framework_core.green_accept(listen_fd)
        except OSError:
            break
        hub_g = _framework_core.get_hub_greenlet()
        g = greenlet.greenlet(
            lambda fd=client_fd: _handle_connection(fd, handler),
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


@pytest.fixture()
def header_middleware_server() -> Generator[socket.socket]:
    """Server with middleware that adds X-Middleware: hit."""
    app = Framework()

    def add_header_mw(request: Request, response: Response, next_fn: HandlerFunc) -> None:
        response.set_header("X-Middleware", "hit")
        next_fn(request, response)

    app.use(add_header_mw)

    @app.route("/hello")
    def hello(request: Request, response: Response) -> None:
        response.write(b"hello")

    yield from _start_app_server(app)


@pytest.fixture()
def auth_middleware_server() -> Generator[socket.socket]:
    """Server with auth middleware that returns 401 if X-Token header is missing."""
    app = Framework()

    def auth_mw(request: Request, response: Response, next_fn: HandlerFunc) -> None:
        if "x-token" not in request.headers:
            response.set_status(401)
            response.write(b"Unauthorized")
            return
        next_fn(request, response)

    app.use(auth_mw)

    @app.route("/protected")
    def protected(request: Request, response: Response) -> None:
        response.write(b"secret data")

    yield from _start_app_server(app)


@pytest.fixture()
def order_middleware_server() -> Generator[socket.socket]:
    """Server with two middlewares that each append to X-Order header."""
    app = Framework()

    def first_mw(request: Request, response: Response, next_fn: HandlerFunc) -> None:
        existing = response._headers.get("X-Order", "")
        response.set_header("X-Order", (existing + ",first") if existing else "first")
        next_fn(request, response)

    def second_mw(request: Request, response: Response, next_fn: HandlerFunc) -> None:
        existing = response._headers.get("X-Order", "")
        response.set_header("X-Order", (existing + ",second") if existing else "second")
        next_fn(request, response)

    app.use(first_mw)
    app.use(second_mw)

    @app.route("/order")
    def order(request: Request, response: Response) -> None:
        response.write(b"ok")

    yield from _start_app_server(app)


def test_middleware_adds_header(header_middleware_server: socket.socket) -> None:
    """Middleware sets X-Middleware: hit on response, assert header present."""
    client = _connect(header_middleware_server)
    raw = _send_http_request(client, "GET", "/hello")
    assert raw.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"X-Middleware: hit\r\n" in raw
    _, _, resp_body = raw.partition(b"\r\n\r\n")
    assert resp_body == b"hello"
    client.close()


def test_middleware_auth_shortcircuit(auth_middleware_server: socket.socket) -> None:
    """Auth middleware returns 401 if X-Token header missing, 200 if present."""
    # Without token — 401
    client = _connect(auth_middleware_server)
    raw = _send_http_request(client, "GET", "/protected")
    assert b"HTTP/1.1 401 Unauthorized\r\n" in raw
    _, _, resp_body = raw.partition(b"\r\n\r\n")
    assert resp_body == b"Unauthorized"
    client.close()

    # With token — 200
    client = _connect(auth_middleware_server)
    raw = _send_http_request(client, "GET", "/protected", headers={"X-Token": "valid"})
    assert raw.startswith(b"HTTP/1.1 200 OK\r\n")
    _, _, resp_body = raw.partition(b"\r\n\r\n")
    assert resp_body == b"secret data"
    client.close()


def test_middleware_execution_order(order_middleware_server: socket.socket) -> None:
    """Two middlewares each append to X-Order header, assert first,second order."""
    client = _connect(order_middleware_server)
    raw = _send_http_request(client, "GET", "/order")
    assert raw.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"X-Order: first,second\r\n" in raw
    client.close()
