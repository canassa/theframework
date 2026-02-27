"""Tests for the greenlet + selectors echo server — real sockets, no mocks."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Generator
from typing import TYPE_CHECKING

import pytest

from examples.greenlet_echo import GreenletEchoServer

if TYPE_CHECKING:
    pass


@pytest.fixture()
def echo_server() -> Generator[GreenletEchoServer]:
    """Start a GreenletEchoServer on a random port in a background thread."""
    server = GreenletEchoServer()
    server.listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.listen_sock.setblocking(False)
    server.listen_sock.bind(("127.0.0.1", 0))
    server.listen_sock.listen(128)

    ready = threading.Event()

    def run() -> None:
        ready.set()
        server.run()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(timeout=5)
    # Small delay to ensure hub loop is running
    time.sleep(0.05)

    yield server

    server.running = False
    thread.join(timeout=5)


def _connect(server: GreenletEchoServer) -> socket.socket:
    assert server.listen_sock is not None
    addr: tuple[str, int] = server.listen_sock.getsockname()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(addr)
    return sock


def test_single_echo(echo_server: GreenletEchoServer) -> None:
    """Send data on one connection, assert it comes back."""
    client = _connect(echo_server)
    client.sendall(b"hello")
    result = client.recv(4096)
    assert result == b"hello"
    client.close()


def test_multiple_concurrent_connections(echo_server: GreenletEchoServer) -> None:
    """Open several connections, send different data on each, assert independence."""
    clients: list[socket.socket] = []
    payloads = [f"msg-{i}".encode() for i in range(5)]

    for payload in payloads:
        c = _connect(echo_server)
        c.sendall(payload)
        clients.append(c)

    for i, c in enumerate(clients):
        c.settimeout(2.0)
        result = c.recv(4096)
        assert result == payloads[i], f"client {i}: expected {payloads[i]!r}, got {result!r}"
        c.close()


def test_client_disconnect_does_not_crash_server(echo_server: GreenletEchoServer) -> None:
    """Close a client abruptly, then verify the server still works."""
    c1 = _connect(echo_server)
    c1.close()

    # Small delay for server to process the disconnect
    time.sleep(0.15)

    c2 = _connect(echo_server)
    c2.sendall(b"still alive")
    c2.settimeout(2.0)
    result = c2.recv(4096)
    assert result == b"still alive"
    c2.close()
