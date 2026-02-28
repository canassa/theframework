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
# Unit-level FFI tests for http_format_response (simple status+body API)
# ---------------------------------------------------------------------------


class TestHttpFormatResponse:
    """Direct tests of _framework_core.http_format_response (no headers variant)."""

    def test_basic_200_with_body(self) -> None:
        raw = _framework_core.http_format_response(200, b"hello")
        assert raw == b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello"

    def test_404_with_body(self) -> None:
        raw = _framework_core.http_format_response(404, b"not found")
        assert raw.startswith(b"HTTP/1.1 404 Not Found\r\n")
        assert b"Content-Length: 9\r\n" in raw
        assert raw.endswith(b"\r\n\r\nnot found")

    def test_204_no_content(self) -> None:
        raw = _framework_core.http_format_response(204, b"")
        assert raw == b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"

    def test_status_codes(self) -> None:
        """Verify all supported status codes produce correct status lines."""
        expected = {
            200: b"HTTP/1.1 200 OK\r\n",
            201: b"HTTP/1.1 201 Created\r\n",
            204: b"HTTP/1.1 204 No Content\r\n",
            301: b"HTTP/1.1 301 Moved Permanently\r\n",
            302: b"HTTP/1.1 302 Found\r\n",
            304: b"HTTP/1.1 304 Not Modified\r\n",
            400: b"HTTP/1.1 400 Bad Request\r\n",
            401: b"HTTP/1.1 401 Unauthorized\r\n",
            403: b"HTTP/1.1 403 Forbidden\r\n",
            404: b"HTTP/1.1 404 Not Found\r\n",
            405: b"HTTP/1.1 405 Method Not Allowed\r\n",
            413: b"HTTP/1.1 413 Payload Too Large\r\n",
            431: b"HTTP/1.1 431 Request Header Fields Too Large\r\n",
            500: b"HTTP/1.1 500 Internal Server Error\r\n",
            502: b"HTTP/1.1 502 Bad Gateway\r\n",
            503: b"HTTP/1.1 503 Service Unavailable\r\n",
            504: b"HTTP/1.1 504 Gateway Timeout\r\n",
        }
        for code, expected_line in expected.items():
            raw = _framework_core.http_format_response(code, b"")
            assert raw.startswith(expected_line), f"Status {code}: {raw!r}"

    def test_unsupported_status_code(self) -> None:
        with pytest.raises(ValueError, match="Unsupported HTTP status code"):
            _framework_core.http_format_response(999, b"")

    def test_auto_content_length(self) -> None:
        """Content-Length is auto-generated matching body size."""
        # http_format_response uses a 64 KB stack buffer, so body + headers
        # must fit within that. Keeping body sizes well below the limit.
        for size in [0, 1, 42, 1000, 60000]:
            body = b"A" * size
            raw = _framework_core.http_format_response(200, body)
            assert f"Content-Length: {size}\r\n".encode() in raw


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
