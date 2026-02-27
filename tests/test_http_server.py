"""Tests for the HTTP server integration — real TCP sockets, real io_uring, real HTTP."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Generator

import greenlet
import pytest

import sys
import pathlib

_ext_dir = str(pathlib.Path(__file__).resolve().parent.parent / "zig-out" / "lib")
if _ext_dir not in sys.path:
    sys.path.insert(0, _ext_dir)

import _framework_core


def _create_listen_socket() -> socket.socket:
    """Create a TCP listen socket on a random ephemeral port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    return sock


def _run_http_server(listen_fd: int) -> None:
    """Acceptor function: accepts connections and spawns HTTP handlers."""

    def handle_connection(fd: int) -> None:
        buf = b""
        try:
            while True:
                data = _framework_core.green_recv(fd, 8192)
                if not data:
                    break
                buf += data

                # Try to parse a complete HTTP request
                while True:
                    result = _framework_core.http_parse_request(buf)
                    if result is None:
                        break  # Need more data

                    method, path, body, consumed, keep_alive = result
                    buf = buf[consumed:]

                    # Generate HTTP response
                    response_body = b"hello world"
                    response = _framework_core.http_format_response(200, response_body)
                    _framework_core.green_send(fd, response)

                    if not keep_alive:
                        _framework_core.green_close(fd)
                        return
        except OSError:
            pass
        finally:
            _framework_core.green_close(fd)

    while True:
        try:
            client_fd = _framework_core.green_accept(listen_fd)
        except OSError:
            break
        hub_g = _framework_core.get_hub_greenlet()
        g = greenlet.greenlet(lambda fd=client_fd: handle_connection(fd), parent=hub_g)
        _framework_core.hub_schedule(g)


@pytest.fixture()
def http_server() -> Generator[tuple[socket.socket, threading.Thread]]:
    """Start an io_uring HTTP server on a random port in a background thread."""
    listen_sock = _create_listen_socket()
    listen_fd = listen_sock.fileno()

    ready = threading.Event()

    def run() -> None:
        ready.set()
        _framework_core.hub_run(listen_fd, lambda: _run_http_server(listen_fd))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(timeout=5)
    time.sleep(0.05)

    yield listen_sock, thread

    _framework_core.hub_stop()
    thread.join(timeout=5)
    listen_sock.close()


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

    # Receive response — read until we have headers + full body
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(8192)
        if not chunk:
            break
        response += chunk

    # Parse Content-Length from response headers to read the body
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


def test_single_http_request(
    http_server: tuple[socket.socket, threading.Thread],
) -> None:
    """Send a GET / request, assert valid HTTP response with status 200 and body 'hello world'."""
    listen_sock, _ = http_server
    client = _connect(listen_sock)
    response = _send_http_request(client, "GET", "/")

    # Parse the full response
    assert response.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"Content-Length: 11\r\n" in response
    assert response.endswith(b"\r\n\r\nhello world")
    client.close()


def test_keep_alive_two_requests(
    http_server: tuple[socket.socket, threading.Thread],
) -> None:
    """Send two requests on the same connection (keep-alive), assert both get correct responses."""
    listen_sock, _ = http_server
    client = _connect(listen_sock)

    # First request
    response1 = _send_http_request(client, "GET", "/first")
    assert response1.startswith(b"HTTP/1.1 200 OK\r\n")
    assert response1.endswith(b"\r\n\r\nhello world")

    # Second request on the same connection
    response2 = _send_http_request(client, "GET", "/second")
    assert response2.startswith(b"HTTP/1.1 200 OK\r\n")
    assert response2.endswith(b"\r\n\r\nhello world")

    client.close()


def test_connection_close(
    http_server: tuple[socket.socket, threading.Thread],
) -> None:
    """Send a request with Connection: close, assert the server closes the connection after responding."""
    listen_sock, _ = http_server
    client = _connect(listen_sock)

    response = _send_http_request(client, "GET", "/", headers={"Connection": "close"})
    assert response.startswith(b"HTTP/1.1 200 OK\r\n")
    assert response.endswith(b"\r\n\r\nhello world")

    # Server should close the connection — further recv should return empty
    time.sleep(0.1)
    remaining = client.recv(4096)
    assert remaining == b""
    client.close()
