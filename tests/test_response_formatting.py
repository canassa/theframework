"""Tests for response formatting via http_send_response (zero-copy writev)."""

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


# ---------------------------------------------------------------------------
# Integration tests (with real HTTP server)
# ---------------------------------------------------------------------------


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
    headers_part = response[:header_end].decode("latin-1")
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
def response_server() -> Generator[socket.socket]:
    """Server that echoes back custom headers and body."""
    app = Framework()

    @app.route("/plain")
    def plain(request: Request, response: Response) -> None:
        response.set_header("Content-Type", "text/plain")
        response.write(b"hello world")

    @app.route("/json")
    def json_route(request: Request, response: Response) -> None:
        response.set_header("Content-Type", "application/json")
        response.write(b'{"status":"ok"}')

    @app.route("/multi-header")
    def multi_header(request: Request, response: Response) -> None:
        response.set_header("X-First", "one")
        response.set_header("X-Second", "two")
        response.set_header("X-Third", "three")
        response.write(b"ok")

    @app.route("/empty")
    def empty(request: Request, response: Response) -> None:
        pass

    @app.route("/custom-status")
    def custom_status(request: Request, response: Response) -> None:
        response.set_status(201)
        response.set_header("Content-Type", "application/json")
        response.write(b'{"id":42}')

    @app.route("/large-body")
    def large_body(request: Request, response: Response) -> None:
        response.set_header("Content-Type", "application/octet-stream")
        response.write(b"X" * 100_000)

    yield from _start_app_server(app)


def test_response_with_content_type(response_server: socket.socket) -> None:
    """Response includes Content-Type header set by handler."""
    client = _connect(response_server)
    raw = _send_http_request(client, "GET", "/plain")
    assert raw.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"Content-Type: text/plain\r\n" in raw
    assert b"Content-Length: 11\r\n" in raw
    _, _, body = raw.partition(b"\r\n\r\n")
    assert body == b"hello world"
    client.close()


def test_response_json(response_server: socket.socket) -> None:
    """JSON response has correct Content-Type and body."""
    client = _connect(response_server)
    raw = _send_http_request(client, "GET", "/json")
    assert b"Content-Type: application/json\r\n" in raw
    _, _, body = raw.partition(b"\r\n\r\n")
    assert body == b'{"status":"ok"}'
    client.close()


def test_response_multiple_headers(response_server: socket.socket) -> None:
    """Multiple custom headers are all present in the response."""
    client = _connect(response_server)
    raw = _send_http_request(client, "GET", "/multi-header")
    assert b"X-First: one\r\n" in raw
    assert b"X-Second: two\r\n" in raw
    assert b"X-Third: three\r\n" in raw
    _, _, body = raw.partition(b"\r\n\r\n")
    assert body == b"ok"
    client.close()


def test_response_empty_body(response_server: socket.socket) -> None:
    """Response with no body has Content-Length: 0."""
    client = _connect(response_server)
    raw = _send_http_request(client, "GET", "/empty")
    assert raw.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"Content-Length: 0\r\n" in raw
    _, _, body = raw.partition(b"\r\n\r\n")
    assert body == b""
    client.close()


def test_response_custom_status(response_server: socket.socket) -> None:
    """Response with custom status code."""
    client = _connect(response_server)
    raw = _send_http_request(client, "GET", "/custom-status")
    assert raw.startswith(b"HTTP/1.1 201 Created\r\n")
    assert b"Content-Type: application/json\r\n" in raw
    _, _, body = raw.partition(b"\r\n\r\n")
    assert body == b'{"id":42}'
    client.close()


def test_response_large_body(response_server: socket.socket) -> None:
    """Response > 64 KB body works correctly via writev."""
    client = _connect(response_server)
    raw = _send_http_request(client, "GET", "/large-body")
    assert raw.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"Content-Length: 100000\r\n" in raw
    _, _, body = raw.partition(b"\r\n\r\n")
    assert body == b"X" * 100_000
    client.close()


def test_response_keep_alive_multiple(response_server: socket.socket) -> None:
    """Multiple responses on the same keep-alive connection are all formatted correctly."""
    client = _connect(response_server)

    for path in ["/plain", "/json", "/multi-header"]:
        raw = _send_http_request(client, "GET", path)
        assert raw.startswith(b"HTTP/1.1 200 OK\r\n")
        assert b"Content-Length:" in raw

    client.close()
