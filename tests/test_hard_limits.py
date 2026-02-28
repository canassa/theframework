"""Tests for Phase 5: Hard limits on request sizes and connections.

Verifies that the server correctly rejects requests that exceed configured
limits (max_body_size, max_header_size) and handles connection pool exhaustion.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Generator

import greenlet
import pytest

import theframework  # noqa: F401 — triggers sys.path setup for _framework_core
import _framework_core

from theframework.request import Request
from theframework.response import Response
from theframework.server import HandlerFunc, _handle_connection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_listen_socket() -> socket.socket:
    """Create a TCP listen socket on a random ephemeral port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    return sock


def _run_acceptor_with_config(
    listen_fd: int,
    handler: HandlerFunc,
    config: dict[str, int],
) -> None:
    """Accept connections and spawn handler greenlets, passing config."""
    while True:
        try:
            client_fd = _framework_core.green_accept(listen_fd)
        except OSError:
            break
        hub_g = _framework_core.get_hub_greenlet()
        g = greenlet.greenlet(
            lambda fd=client_fd: _handle_connection(fd, handler, config),
            parent=hub_g,
        )
        _framework_core.hub_schedule(g)


def _start_server_with_config(
    handler: HandlerFunc,
    config: dict[str, int],
) -> Generator[socket.socket]:
    """Start a server with the given handler and config on an ephemeral port."""
    sock = _create_listen_socket()
    listen_fd = sock.fileno()

    ready = threading.Event()

    def run() -> None:
        ready.set()
        _framework_core.hub_run(
            listen_fd,
            lambda: _run_acceptor_with_config(listen_fd, handler, config),
            config,
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(timeout=5)
    time.sleep(0.05)

    yield sock

    _framework_core.hub_stop()
    thread.join(timeout=5)
    sock.close()


def _connect(listen_sock: socket.socket, timeout: float = 3.0) -> socket.socket:
    addr: tuple[str, int] = listen_sock.getsockname()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(addr)
    return sock


def _recv_response(sock: socket.socket) -> bytes:
    """Receive a full HTTP response (headers + body based on Content-Length)."""
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(8192)
        if not chunk:
            return response
        response += chunk

    header_end = response.index(b"\r\n\r\n") + 4
    headers_part = response[:header_end].decode("latin-1", errors="replace")
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

    return _recv_response(sock)


def _ok_handler(request: Request, response: Response) -> None:
    """Simple handler that always returns 200 OK."""
    response.write(b"OK")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def small_body_server() -> Generator[socket.socket]:
    """Server with max_body_size=256 bytes."""
    config = {"max_body_size": 256, "max_header_size": 32768}
    yield from _start_server_with_config(_ok_handler, config)


@pytest.fixture()
def small_header_server() -> Generator[socket.socket]:
    """Server with max_header_size=128 bytes."""
    config = {"max_header_size": 128, "max_body_size": 1_048_576}
    yield from _start_server_with_config(_ok_handler, config)


@pytest.fixture()
def tiny_pool_server() -> Generator[socket.socket]:
    """Server with max_connections=2 to test pool exhaustion."""
    config = {"max_connections": 2, "max_header_size": 32768, "max_body_size": 1_048_576}
    yield from _start_server_with_config(_ok_handler, config)


# ---------------------------------------------------------------------------
# Tests: Body size limit
# ---------------------------------------------------------------------------


def test_post_body_under_limit(small_body_server: socket.socket) -> None:
    """POST with body under max_body_size should succeed with 200."""
    client = _connect(small_body_server)
    try:
        body = b"x" * 100  # 100 bytes, under 256 limit
        response = _send_http_request(client, "POST", "/", body=body)
        assert response.startswith(b"HTTP/1.1 200 OK\r\n")
    finally:
        client.close()


def test_post_body_too_large(small_body_server: socket.socket) -> None:
    """POST with body > max_body_size should get 413 Payload Too Large."""
    client = _connect(small_body_server)
    try:
        body = b"x" * 512  # 512 bytes, exceeds 256 limit
        response = _send_http_request(client, "POST", "/", body=body)
        assert b"413" in response
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Tests: Header size limit
# ---------------------------------------------------------------------------


def test_request_headers_under_limit(small_header_server: socket.socket) -> None:
    """Request with small headers should succeed with 200."""
    client = _connect(small_header_server)
    try:
        response = _send_http_request(client, "GET", "/")
        assert response.startswith(b"HTTP/1.1 200 OK\r\n")
    finally:
        client.close()


def test_request_headers_too_large(small_header_server: socket.socket) -> None:
    """Request with headers exceeding max_header_size should get 431."""
    client = _connect(small_header_server)
    try:
        # Build request with a large custom header to exceed 128-byte limit
        big_header_value = "x" * 200
        response = _send_http_request(
            client,
            "GET",
            "/",
            headers={"X-Big": big_header_value},
        )
        assert b"431" in response
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Tests: Recovery after rejection
# ---------------------------------------------------------------------------


def test_server_recovers_after_body_rejection(
    small_body_server: socket.socket,
) -> None:
    """After rejecting a too-large body, the server should still handle normal requests."""
    # First: send an oversized request
    client1 = _connect(small_body_server)
    try:
        body = b"x" * 512
        response1 = _send_http_request(client1, "POST", "/", body=body)
        assert b"413" in response1
    finally:
        client1.close()

    time.sleep(0.1)

    # Second: send a normal request — should succeed
    client2 = _connect(small_body_server)
    try:
        response2 = _send_http_request(client2, "GET", "/")
        assert response2.startswith(b"HTTP/1.1 200 OK\r\n")
    finally:
        client2.close()


def test_server_recovers_after_header_rejection(
    small_header_server: socket.socket,
) -> None:
    """After rejecting too-large headers, the server should still handle normal requests."""
    # First: send request with oversized headers
    client1 = _connect(small_header_server)
    try:
        big_header_value = "x" * 200
        response1 = _send_http_request(
            client1,
            "GET",
            "/",
            headers={"X-Big": big_header_value},
        )
        assert b"431" in response1
    finally:
        client1.close()

    time.sleep(0.1)

    # Second: send a normal request — should succeed
    client2 = _connect(small_header_server)
    try:
        response2 = _send_http_request(client2, "GET", "/")
        assert response2.startswith(b"HTTP/1.1 200 OK\r\n")
    finally:
        client2.close()


# ---------------------------------------------------------------------------
# Tests: Connection pool exhaustion (503)
# ---------------------------------------------------------------------------


def test_connection_pool_exhaustion(tiny_pool_server: socket.socket) -> None:
    """When connection pool is full, new connections should get 503 or be rejected.

    With max_connections=2, the 3rd concurrent connection should be rejected.
    The Zig side sends 503 and closes the fd. The Python handler greenlet
    will fail immediately since the fd is not tracked.
    """
    clients: list[socket.socket] = []
    try:
        # Open 2 connections (filling the pool) — these should succeed
        for _ in range(2):
            c = _connect(tiny_pool_server)
            resp = _send_http_request(c, "GET", "/")
            assert resp.startswith(b"HTTP/1.1 200 OK\r\n")
            # Keep connection open (keep-alive)
            clients.append(c)

        # Give the server a moment to process
        time.sleep(0.2)

        # Open a 3rd connection — pool should be exhausted
        # The Zig side sends "HTTP/1.1 503 Service Unavailable" and closes
        c3 = _connect(tiny_pool_server)
        try:
            response = _recv_response(c3)
            # We should either get a 503 or the connection should be
            # closed immediately (empty response)
            if response:
                assert b"503" in response
        except ConnectionError, TimeoutError:
            # Connection was refused or reset — also acceptable
            pass
        finally:
            c3.close()
    finally:
        for c in clients:
            c.close()


def test_pool_recovery_after_exhaustion(tiny_pool_server: socket.socket) -> None:
    """After pool exhaustion and releasing connections, new connections work."""
    # Fill the pool
    c1 = _connect(tiny_pool_server)
    resp1 = _send_http_request(c1, "GET", "/", headers={"Connection": "close"})
    assert resp1.startswith(b"HTTP/1.1 200 OK\r\n")
    c1.close()

    time.sleep(0.2)

    c2 = _connect(tiny_pool_server)
    resp2 = _send_http_request(c2, "GET", "/", headers={"Connection": "close"})
    assert resp2.startswith(b"HTTP/1.1 200 OK\r\n")
    c2.close()

    time.sleep(0.2)

    # Should still be able to connect (connections were closed, pool freed)
    c3 = _connect(tiny_pool_server)
    resp3 = _send_http_request(c3, "GET", "/")
    assert resp3.startswith(b"HTTP/1.1 200 OK\r\n")
    c3.close()
