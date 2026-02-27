"""Tests for the io_uring + greenlet echo server — real TCP sockets, real io_uring."""

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


def _run_echo_server(listen_fd: int) -> None:
    """Acceptor function: accepts connections and spawns echo handlers."""

    def handle_connection(fd: int) -> None:
        hub_g = _framework_core.get_hub_greenlet()
        try:
            while True:
                data = _framework_core.green_recv(fd, 4096)
                if not data:
                    break
                _framework_core.green_send(fd, data)
        except OSError:
            pass

    while True:
        try:
            client_fd = _framework_core.green_accept(listen_fd)
        except OSError:
            break
        hub_g = _framework_core.get_hub_greenlet()
        g = greenlet.greenlet(lambda fd=client_fd: handle_connection(fd), parent=hub_g)
        _framework_core.hub_schedule(g)


@pytest.fixture()
def echo_server() -> Generator[tuple[socket.socket, threading.Thread]]:
    """Start an io_uring echo server on a random port in a background thread."""
    listen_sock = _create_listen_socket()
    listen_fd = listen_sock.fileno()

    ready = threading.Event()

    def run() -> None:
        ready.set()
        _framework_core.hub_run(listen_fd, lambda: _run_echo_server(listen_fd))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(timeout=5)
    # Small delay to ensure hub loop is running
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


def test_single_echo(echo_server: tuple[socket.socket, threading.Thread]) -> None:
    """Connect, send 'hello world', assert recv returns same."""
    listen_sock, _ = echo_server
    client = _connect(listen_sock)
    client.sendall(b"hello world")
    result = client.recv(4096)
    assert result == b"hello world"
    client.close()


def test_10_concurrent_connections(
    echo_server: tuple[socket.socket, threading.Thread],
) -> None:
    """10 clients with unique payloads, assert each echoes correctly."""
    listen_sock, _ = echo_server
    clients: list[socket.socket] = []
    payloads = [f"payload-{i}".encode() for i in range(10)]

    for payload in payloads:
        c = _connect(listen_sock)
        c.sendall(payload)
        clients.append(c)

    for i, c in enumerate(clients):
        result = c.recv(4096)
        assert result == payloads[i], f"client {i}: expected {payloads[i]!r}, got {result!r}"
        c.close()


def test_client_disconnect_resilience(
    echo_server: tuple[socket.socket, threading.Thread],
) -> None:
    """Abrupt close, then new connection works."""
    listen_sock, _ = echo_server

    # Abrupt close
    c1 = _connect(listen_sock)
    c1.close()

    # Small delay for server to process the disconnect
    time.sleep(0.15)

    # New connection should work
    c2 = _connect(listen_sock)
    c2.sendall(b"still alive")
    result = c2.recv(4096)
    assert result == b"still alive"
    c2.close()
