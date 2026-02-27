"""Tests for Request and Response objects — real TCP sockets, real io_uring, real HTTP."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Generator

import pytest

import theframework  # noqa: F401 — triggers sys.path setup for _framework_core
import _framework_core

from theframework.request import Request
from theframework.response import Response
from theframework.server import serve, HandlerFunc


def _start_server(handler: HandlerFunc) -> Generator[socket.socket]:
    """Start a server with the given handler on an ephemeral port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    listen_fd = sock.fileno()
    port: int = sock.getsockname()[1]

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
    """Accept connections and spawn handler greenlets."""
    import greenlet

    from theframework.server import _handle_connection

    while True:
        try:
            client_fd = _framework_core.green_accept(listen_fd)
        except OSError:
            break
        hub_g = _framework_core.get_hub_greenlet()
        g = greenlet.greenlet(lambda fd=client_fd: _handle_connection(fd, handler), parent=hub_g)
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
    """Send a raw HTTP request and receive the full response."""
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
def json_echo_server() -> Generator[socket.socket]:
    """Server that echoes JSON body back."""

    def handler(request: Request, response: Response) -> None:
        data = request.json()
        response.set_header("Content-Type", "application/json")
        response.write(json.dumps(data).encode())

    yield from _start_server(handler)


@pytest.fixture()
def query_params_server() -> Generator[socket.socket]:
    """Server that returns query params as JSON."""

    def handler(request: Request, response: Response) -> None:
        response.set_header("Content-Type", "application/json")
        response.write(json.dumps(request.query_params).encode())

    yield from _start_server(handler)


@pytest.fixture()
def custom_status_server() -> Generator[socket.socket]:
    """Server that returns a custom status and header."""

    def handler(request: Request, response: Response) -> None:
        response.set_status(201)
        response.set_header("X-Custom", "test")
        response.write(b"created")

    yield from _start_server(handler)


def test_json_echo(json_echo_server: socket.socket) -> None:
    """POST /echo with JSON body, assert handler reads request.json() and echoes it back."""
    client = _connect(json_echo_server)
    body = json.dumps({"key": "value"}).encode()
    raw = _send_http_request(
        client,
        "POST",
        "/echo",
        headers={"Content-Type": "application/json"},
        body=body,
    )
    assert b"HTTP/1.1 200 OK\r\n" in raw
    assert b"Content-Type: application/json\r\n" in raw
    # Parse the response body
    _, _, resp_body = raw.partition(b"\r\n\r\n")
    assert json.loads(resp_body) == {"key": "value"}
    client.close()


def test_query_params(query_params_server: socket.socket) -> None:
    """GET /path?foo=bar&baz=1, assert handler sees query params."""
    client = _connect(query_params_server)
    raw = _send_http_request(client, "GET", "/path?foo=bar&baz=1")
    assert raw.startswith(b"HTTP/1.1 200 OK\r\n")
    _, _, resp_body = raw.partition(b"\r\n\r\n")
    params = json.loads(resp_body)
    assert params["foo"] == "bar"
    assert params["baz"] == "1"
    client.close()


def test_custom_status_and_header(custom_status_server: socket.socket) -> None:
    """Handler sets status 201 and header X-Custom: test."""
    client = _connect(custom_status_server)
    raw = _send_http_request(client, "GET", "/")
    assert raw.startswith(b"HTTP/1.1 201 Created\r\n")
    assert b"X-Custom: test\r\n" in raw
    _, _, resp_body = raw.partition(b"\r\n\r\n")
    assert resp_body == b"created"
    client.close()
