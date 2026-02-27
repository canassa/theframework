"""Tests for cooperative I/O primitives — green_sleep, green_connect, green_register_fd.
Real TCP sockets, real io_uring, no mocks."""

from __future__ import annotations

import json
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
from theframework.server import HandlerFunc, _handle_connection


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


def _run_acceptor(listen_fd: int, handler: HandlerFunc) -> None:
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
    sock.settimeout(5.0)
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


def test_green_sleep() -> None:
    """green_sleep cooperatively sleeps without blocking the event loop."""
    app = Framework()

    @app.route("/slow")
    def slow(request: Request, response: Response) -> None:
        _framework_core.green_sleep(0.2)
        response.write(b"slow done")

    @app.route("/fast")
    def fast(request: Request, response: Response) -> None:
        response.write(b"fast done")

    for listen_sock in _start_app_server(app):
        # Send slow request in a thread
        slow_result: bytes = b""
        slow_start = time.monotonic()
        slow_elapsed = 0.0

        def send_slow() -> None:
            nonlocal slow_result, slow_elapsed
            client = _connect(listen_sock)
            slow_result = _send_http_request(client, "GET", "/slow")
            slow_elapsed = time.monotonic() - slow_start
            client.close()

        slow_thread = threading.Thread(target=send_slow)
        slow_thread.start()

        # Give the slow request a moment to be received by the server
        time.sleep(0.05)

        # Send fast request — should respond immediately while slow is sleeping
        fast_client = _connect(listen_sock)
        fast_start = time.monotonic()
        fast_result = _send_http_request(fast_client, "GET", "/fast")
        fast_elapsed = time.monotonic() - fast_start
        fast_client.close()

        slow_thread.join(timeout=5)

        # Fast handler responded quickly (well under the 200ms sleep)
        assert fast_elapsed < 0.15
        assert fast_result.startswith(b"HTTP/1.1 200 OK\r\n")
        _, _, fast_body = fast_result.partition(b"\r\n\r\n")
        assert fast_body == b"fast done"

        # Slow handler took at least 200ms
        assert slow_elapsed >= 0.15
        assert slow_result.startswith(b"HTTP/1.1 200 OK\r\n")
        _, _, slow_body = slow_result.partition(b"\r\n\r\n")
        assert slow_body == b"slow done"


def test_green_connect() -> None:
    """green_connect makes an outbound TCP connection cooperatively."""
    # Start a simple TCP echo server as the connect target
    target_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    target_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    target_sock.bind(("127.0.0.1", 0))
    target_sock.listen(8)
    target_port: int = target_sock.getsockname()[1]

    def echo_target() -> None:
        try:
            while True:
                conn, _ = target_sock.accept()
                data = conn.recv(4096)
                if data:
                    conn.sendall(data)
                conn.close()
        except OSError:
            pass

    echo_thread = threading.Thread(target=echo_target, daemon=True)
    echo_thread.start()

    app = Framework()

    @app.route("/proxy")
    def proxy(request: Request, response: Response) -> None:
        # Connect to the echo target from inside the handler
        fd = _framework_core.green_connect("127.0.0.1", target_port)
        _framework_core.green_send(fd, b"hello from handler")
        data = _framework_core.green_recv(fd, 4096)
        _framework_core.green_close(fd)
        response.write(data)

    try:
        for listen_sock in _start_app_server(app):
            client = _connect(listen_sock)
            raw = _send_http_request(client, "GET", "/proxy")
            assert raw.startswith(b"HTTP/1.1 200 OK\r\n")
            _, _, resp_body = raw.partition(b"\r\n\r\n")
            assert resp_body == b"hello from handler"
            client.close()
    finally:
        target_sock.close()


def test_register_fd() -> None:
    """green_register_fd brings an external socket into the io_uring hub."""
    # Start a simple TCP echo server as the target
    target_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    target_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    target_sock.bind(("127.0.0.1", 0))
    target_sock.listen(8)
    target_port: int = target_sock.getsockname()[1]

    def echo_target() -> None:
        try:
            while True:
                conn, _ = target_sock.accept()
                data = conn.recv(4096)
                if data:
                    conn.sendall(data)
                conn.close()
        except OSError:
            pass

    echo_thread = threading.Thread(target=echo_target, daemon=True)
    echo_thread.start()

    app = Framework()

    @app.route("/register")
    def register_handler(request: Request, response: Response) -> None:
        # Create socket in Python, connect normally (blocking but fast for loopback)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("127.0.0.1", target_port))
        fd = sock.fileno()

        # Register it in the hub so green_send/green_recv work on it
        _framework_core.green_register_fd(fd)

        _framework_core.green_send(fd, b"registered fd works")
        data = _framework_core.green_recv(fd, 4096)

        _framework_core.green_unregister_fd(fd)
        sock.close()

        response.write(data)

    try:
        for listen_sock in _start_app_server(app):
            client = _connect(listen_sock)
            raw = _send_http_request(client, "GET", "/register")
            assert raw.startswith(b"HTTP/1.1 200 OK\r\n")
            _, _, resp_body = raw.partition(b"\r\n\r\n")
            assert resp_body == b"registered fd works"
            client.close()
    finally:
        target_sock.close()
