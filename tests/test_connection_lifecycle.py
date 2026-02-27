"""Tests for connection lifecycle — partial sends, fd reuse, pool reuse."""

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
    time.sleep(0.05)

    yield listen_sock, thread

    _framework_core.hub_stop()
    thread.join(timeout=5)
    listen_sock.close()


def _connect(listen_sock: socket.socket) -> socket.socket:
    addr: tuple[str, int] = listen_sock.getsockname()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    sock.connect(addr)
    return sock


def _recv_all(sock: socket.socket, expected_len: int) -> bytes:
    """Receive exactly expected_len bytes."""
    data = b""
    while len(data) < expected_len:
        chunk = sock.recv(expected_len - len(data))
        if not chunk:
            break
        data += chunk
    return data


def test_1mb_payload_echo(
    echo_server: tuple[socket.socket, threading.Thread],
) -> None:
    """Exercises partial send/recv reassembly with a large payload."""
    listen_sock, _ = echo_server
    client = _connect(listen_sock)

    # 1MB payload
    payload = b"X" * (1024 * 1024)
    client.sendall(payload)

    result = _recv_all(client, len(payload))
    assert len(result) == len(payload), f"Expected {len(payload)} bytes, got {len(result)}"
    assert result == payload
    client.close()


def test_fd_reuse(
    echo_server: tuple[socket.socket, threading.Thread],
) -> None:
    """Half-send + close, then new connection works."""
    listen_sock, _ = echo_server

    # Open connection, send partial data, close abruptly
    c1 = _connect(listen_sock)
    c1.sendall(b"partial")
    c1.close()

    time.sleep(0.15)

    # New connection should work fine
    c2 = _connect(listen_sock)
    c2.sendall(b"after reuse")
    result = c2.recv(4096)
    assert result == b"after reuse"
    c2.close()


def test_50_connection_pool_reuse(
    echo_server: tuple[socket.socket, threading.Thread],
) -> None:
    """Open 50, close all, open 50 more — exercises connection pool reuse."""
    listen_sock, _ = echo_server

    # Open 50 connections
    clients: list[socket.socket] = []
    for i in range(50):
        c = _connect(listen_sock)
        c.sendall(f"batch1-{i}".encode())
        clients.append(c)

    # Verify all echo correctly
    for i, c in enumerate(clients):
        expected = f"batch1-{i}".encode()
        result = c.recv(4096)
        assert result == expected, f"client {i}: expected {expected!r}, got {result!r}"
        c.close()

    time.sleep(0.2)

    # Open 50 more — should reuse pool slots
    clients2: list[socket.socket] = []
    for i in range(50):
        c = _connect(listen_sock)
        c.sendall(f"batch2-{i}".encode())
        clients2.append(c)

    for i, c in enumerate(clients2):
        expected = f"batch2-{i}".encode()
        result = c.recv(4096)
        assert result == expected, f"client {i}: expected {expected!r}, got {result!r}"
        c.close()
