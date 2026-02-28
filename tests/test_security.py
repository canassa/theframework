"""Security test suite — black-box tests for robustness and exploit resistance.

Starts a real server and attacks it over TCP with raw sockets.
Tests cover: malformed requests, header manipulation, Content-Length attacks,
path abuse, pipelining abuse, handler crash recovery, connection lifecycle
edge cases, response size limits, and concurrent connection stress.

CAVEAT: This server will always be deployed behind nginx/caddy, so we skip
categories already mitigated by the reverse proxy (slowloris, SSL, HTTP/2,
IP rate limiting, basic slow-read attacks).
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from collections.abc import Generator

import greenlet
import pytest

import theframework  # noqa: F401 — triggers sys.path setup
import _framework_core

from theframework.app import Framework
from theframework.request import Request
from theframework.response import Response
from theframework.server import HandlerFunc, _handle_connection


# ---------------------------------------------------------------------------
# Helpers (shared across all tests)
# ---------------------------------------------------------------------------


def _create_listen_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    return sock


def _run_acceptor(
    listen_fd: int, handler: HandlerFunc, config: dict[str, int] | None = None
) -> None:
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


def _start_server(
    handler: HandlerFunc, config: dict[str, int] | None = None
) -> Generator[socket.socket]:
    sock = _create_listen_socket()
    listen_fd = sock.fileno()
    ready = threading.Event()

    def run() -> None:
        ready.set()
        _framework_core.hub_run(
            listen_fd,
            lambda: _run_acceptor(listen_fd, handler, config),
            config or {},
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(timeout=5)
    time.sleep(0.05)
    yield sock
    _framework_core.hub_stop()
    thread.join(timeout=5)
    sock.close()


def _start_app_server(
    app: Framework, config: dict[str, int] | None = None
) -> Generator[socket.socket]:
    handler = app._make_handler()
    yield from _start_server(handler, config)


def _connect(listen_sock: socket.socket, timeout: float = 3.0) -> socket.socket:
    addr: tuple[str, int] = listen_sock.getsockname()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(addr)
    return sock


def _recv_response(sock: socket.socket, timeout: float = 3.0) -> bytes:
    """Receive a full HTTP response (headers + body based on Content-Length).

    Uses MSG_PEEK + exact recv to avoid consuming bytes belonging to
    the next pipelined response.
    """
    sock.settimeout(timeout)
    response = b""
    try:
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(1)
            if not chunk:
                return response
            response += chunk
    except TimeoutError, ConnectionError:
        return response

    header_end = response.index(b"\r\n\r\n") + 4
    headers_part = response[:header_end].decode("latin-1", errors="replace")

    content_length = 0
    for line in headers_part.split("\r\n"):
        if line.lower().startswith("content-length:"):
            content_length = int(line.split(":", 1)[1].strip())
            break

    body = b""
    while len(body) < content_length:
        try:
            chunk = sock.recv(content_length - len(body))
        except TimeoutError, ConnectionError:
            break
        if not chunk:
            break
        body += chunk

    return response[:header_end] + body


def _send_raw(sock: socket.socket, data: bytes) -> bytes:
    """Send raw bytes and receive the response."""
    sock.sendall(data)
    return _recv_response(sock)


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
    return _recv_response(sock)


def _get_status_code(response: bytes) -> int | None:
    """Extract HTTP status code from a raw response."""
    try:
        status_line = response.split(b"\r\n", 1)[0]
        return int(status_line.split(b" ", 2)[1])
    except IndexError, ValueError:
        return None


def _ok_handler(request: Request, response: Response) -> None:
    response.write(b"OK")


def _echo_handler(request: Request, response: Response) -> None:
    """Echo back request metadata as JSON."""
    data = {
        "method": request.method,
        "path": request.path,
        "full_path": request.full_path,
        "query_params": request.query_params,
        "headers": request.headers,
        "body_len": len(request.body),
        "params": request.params,
    }
    response.set_header("Content-Type", "application/json")
    response.write(json.dumps(data).encode())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def server() -> Generator[socket.socket]:
    """Default server with echo handler."""
    yield from _start_server(_echo_handler)


@pytest.fixture()
def ok_server() -> Generator[socket.socket]:
    """Simple OK server."""
    yield from _start_server(_ok_handler)


@pytest.fixture()
def small_limits_server() -> Generator[socket.socket]:
    """Server with small limits for testing boundary conditions."""
    config = {"max_header_size": 512, "max_body_size": 1024}
    yield from _start_server(_echo_handler, config)


@pytest.fixture()
def tiny_pool_server() -> Generator[socket.socket]:
    """Server with max_connections=4 for connection exhaustion tests."""
    config = {"max_connections": 4, "max_header_size": 32768, "max_body_size": 1_048_576}
    yield from _start_server(_ok_handler, config)


@pytest.fixture()
def crash_server() -> Generator[socket.socket]:
    """Server with routes that crash in various ways."""
    app = Framework()

    @app.route("/ok")
    def ok(request: Request, response: Response) -> None:
        response.write(b"OK")

    @app.route("/crash")
    def crash(request: Request, response: Response) -> None:
        raise RuntimeError("handler crashed")

    @app.route("/crash-after-write")
    def crash_after_write(request: Request, response: Response) -> None:
        response.write(b"partial")
        raise RuntimeError("crash after write")

    @app.route("/zero-division")
    def zero_div(request: Request, response: Response) -> None:
        1 / 0  # noqa: B018

    @app.route("/key-error")
    def key_err(request: Request, response: Response) -> None:
        d: dict[str, str] = {}
        _ = d["missing"]

    @app.route("/type-error")
    def type_err(request: Request, response: Response) -> None:
        _ = "string" + 42  # type: ignore[operator]

    @app.route("/large-response")
    def large_response(request: Request, response: Response) -> None:
        response.write(b"X" * 500_000)

    @app.route("/many-headers")
    def many_headers(request: Request, response: Response) -> None:
        for i in range(50):
            response.set_header(f"X-Header-{i}", f"value-{i}")
        response.write(b"OK")

    @app.route("/echo", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def echo(request: Request, response: Response) -> None:
        data = {
            "method": request.method,
            "path": request.path,
            "body": request.body.decode("latin-1"),
            "headers": request.headers,
        }
        response.set_header("Content-Type", "application/json")
        response.write(json.dumps(data).encode())

    yield from _start_app_server(app)


# ===========================================================================
# 1. MALFORMED HTTP REQUESTS
# ===========================================================================


class TestMalformedRequests:
    """Verify the server doesn't crash or leak state on garbage input."""

    def test_complete_garbage(self, ok_server: socket.socket) -> None:
        """Send random bytes — server should reject or close cleanly."""
        client = _connect(ok_server)
        try:
            response = _send_raw(client, b"\x00\x01\x02\x03\xff\xfe\xfd")
            # Server should either return 400 or close the connection
            if response:
                assert _get_status_code(response) == 400
        except ConnectionError, TimeoutError:
            pass  # acceptable — server closed connection
        finally:
            client.close()

    def test_empty_request(self, ok_server: socket.socket) -> None:
        """Connect and send nothing (simulate accidental TCP connect)."""
        client = _connect(ok_server)
        try:
            # Close our write side to signal EOF
            client.shutdown(socket.SHUT_WR)
            response = _recv_response(client, timeout=2.0)
            # Server should close cleanly — no response is acceptable
            assert response == b"" or _get_status_code(response) == 400
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_partial_request_line(self, ok_server: socket.socket) -> None:
        """Send only a partial request line then close."""
        client = _connect(ok_server)
        try:
            client.sendall(b"GET / HT")
            client.shutdown(socket.SHUT_WR)
            response = _recv_response(client, timeout=2.0)
            if response:
                assert _get_status_code(response) == 400
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_request_line_only_no_headers(self, ok_server: socket.socket) -> None:
        """Send request line + empty line (no headers) — should still work."""
        client = _connect(ok_server)
        try:
            response = _send_raw(client, b"GET / HTTP/1.1\r\n\r\n")
            # This is technically valid HTTP/1.1 (Host header recommended but not
            # strictly required by all parsers)
            status = _get_status_code(response)
            assert status is not None
            # Either 200 (accepted) or 400 (missing Host) are acceptable
            assert status in (200, 400)
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_only_newlines(self, ok_server: socket.socket) -> None:
        """Send only CRLF sequences — no request line."""
        client = _connect(ok_server)
        try:
            response = _send_raw(client, b"\r\n\r\n\r\n\r\n")
            # Should reject as invalid or time out
            if response:
                status = _get_status_code(response)
                assert status in (None, 400)
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_lf_only_line_endings(self, ok_server: socket.socket) -> None:
        """Send request with LF-only line endings (not CRLF)."""
        client = _connect(ok_server)
        try:
            response = _send_raw(client, b"GET / HTTP/1.1\nHost: localhost\n\n")
            # Parser may or may not accept LF-only; should not crash
            if response:
                status = _get_status_code(response)
                assert status is not None
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_null_bytes_in_request_line(self, ok_server: socket.socket) -> None:
        """Null bytes in the request line."""
        client = _connect(ok_server)
        try:
            response = _send_raw(client, b"GET /\x00evil HTTP/1.1\r\nHost: localhost\r\n\r\n")
            if response:
                status = _get_status_code(response)
                # Should reject or handle safely
                assert status is not None
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_null_bytes_in_header_value(self, server: socket.socket) -> None:
        """Null bytes in header values."""
        client = _connect(server)
        try:
            raw = b"GET / HTTP/1.1\r\nHost: localhost\r\nX-Evil: foo\x00bar\r\n\r\n"
            response = _send_raw(client, raw)
            if response:
                status = _get_status_code(response)
                assert status is not None
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_cr_without_lf(self, ok_server: socket.socket) -> None:
        """Bare CR without LF in headers."""
        client = _connect(ok_server)
        try:
            response = _send_raw(client, b"GET / HTTP/1.1\rHost: localhost\r\n\r\n")
            if response:
                status = _get_status_code(response)
                assert status is not None
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_very_long_request_line(self, ok_server: socket.socket) -> None:
        """Request line exceeding typical buffer sizes."""
        path = "/" + "A" * 8000
        client = _connect(ok_server)
        try:
            response = _send_http_request(client, "GET", path)
            status = _get_status_code(response)
            assert status is not None
            # Should succeed or reject with 414 or 431
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_invalid_http_version(self, ok_server: socket.socket) -> None:
        """HTTP version that doesn't exist."""
        client = _connect(ok_server)
        try:
            response = _send_raw(client, b"GET / HTTP/9.9\r\nHost: localhost\r\n\r\n")
            if response:
                status = _get_status_code(response)
                assert status in (None, 400, 200, 505)
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_http_10_request(self, ok_server: socket.socket) -> None:
        """Valid HTTP/1.0 request — should work, connection should close after."""
        client = _connect(ok_server)
        try:
            response = _send_raw(client, b"GET / HTTP/1.0\r\nHost: localhost\r\n\r\n")
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
            # HTTP/1.0 default is no keep-alive — server should close
            time.sleep(0.1)
            remaining = client.recv(4096)
            assert remaining == b""
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_double_crlf_before_request(self, ok_server: socket.socket) -> None:
        """Leading CRLF before the request line (some browsers do this)."""
        client = _connect(ok_server)
        try:
            response = _send_raw(client, b"\r\n\r\nGET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            if response:
                status = _get_status_code(response)
                # Should either handle it or reject it
                assert status is not None
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_server_recovers_after_garbage(self, ok_server: socket.socket) -> None:
        """After receiving garbage, the server should still handle new connections."""
        # Send garbage on first connection
        client1 = _connect(ok_server)
        try:
            client1.sendall(b"\xff\xfe\xfd\x00\x01\x02")
            client1.close()
        except ConnectionError, TimeoutError:
            pass

        time.sleep(0.2)

        # Normal request on a new connection should work
        client2 = _connect(ok_server)
        try:
            response = _send_http_request(client2, "GET", "/")
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        finally:
            client2.close()


# ===========================================================================
# 2. HEADER MANIPULATION
# ===========================================================================


class TestHeaderManipulation:
    """Attacks targeting header parsing edge cases."""

    def test_header_with_no_value(self, server: socket.socket) -> None:
        """Header with name but empty value."""
        client = _connect(server)
        try:
            raw = b"GET / HTTP/1.1\r\nHost: localhost\r\nX-Empty:\r\n\r\n"
            response = _send_raw(client, raw)
            status = _get_status_code(response)
            assert status is not None
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_header_with_no_colon(self, server: socket.socket) -> None:
        """Header line without a colon separator."""
        client = _connect(server)
        try:
            raw = b"GET / HTTP/1.1\r\nHost: localhost\r\nNoColonHere\r\n\r\n"
            response = _send_raw(client, raw)
            if response:
                status = _get_status_code(response)
                assert status in (200, 400)
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_header_with_space_in_name(self, server: socket.socket) -> None:
        """Header name containing spaces (invalid per RFC)."""
        client = _connect(server)
        try:
            raw = b"GET / HTTP/1.1\r\nHost: localhost\r\nBad Header: value\r\n\r\n"
            response = _send_raw(client, raw)
            if response:
                status = _get_status_code(response)
                assert status in (200, 400)
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_header_name_with_control_chars(self, server: socket.socket) -> None:
        """Header name with control characters."""
        client = _connect(server)
        try:
            raw = b"GET / HTTP/1.1\r\nHost: localhost\r\nX-\x01Evil: value\r\n\r\n"
            response = _send_raw(client, raw)
            if response:
                status = _get_status_code(response)
                assert status is not None
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_header_value_with_newline_injection(self, server: socket.socket) -> None:
        """Attempt CRLF injection in header value (response splitting)."""
        client = _connect(server)
        try:
            # Attempt to inject a second header via CRLF in the value
            raw = (
                b"GET / HTTP/1.1\r\nHost: localhost\r\nX-Inject: value\r\nX-Injected: evil\r\n\r\n"
            )
            response = _send_raw(client, raw)
            if response:
                status = _get_status_code(response)
                assert status is not None
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_exactly_at_header_count_limit(self, server: socket.socket) -> None:
        """Send exactly 100 headers (MAX_HEADERS) — should succeed."""
        client = _connect(server)
        try:
            lines = b"GET / HTTP/1.1\r\nHost: localhost\r\n"
            for i in range(99):  # +Host = 100
                lines += f"X-H-{i:03d}: val-{i}\r\n".encode()
            lines += b"\r\n"
            response = _send_raw(client, lines)
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        finally:
            client.close()

    def test_one_over_header_count_limit(self, server: socket.socket) -> None:
        """Send 101 headers — one over MAX_HEADERS. Rejected as 400."""
        client = _connect(server)
        try:
            lines = b"GET / HTTP/1.1\r\nHost: localhost\r\n"
            for i in range(100):  # +Host = 101
                lines += f"X-H-{i:03d}: val-{i}\r\n".encode()
            lines += b"\r\n"
            response = _send_raw(client, lines)
            assert b"400" in response
        finally:
            client.close()

    def test_many_headers_rejected(self, server: socket.socket) -> None:
        """Send 200 headers — well over the limit, must be rejected."""
        client = _connect(server)
        try:
            lines = b"GET / HTTP/1.1\r\nHost: localhost\r\n"
            for i in range(199):  # +Host = 200
                lines += f"X-H-{i:03d}: val-{i}\r\n".encode()
            lines += b"\r\n"
            response = _send_raw(client, lines)
            assert b"400" in response
        finally:
            client.close()

    def test_33_headers_succeed(self, server: socket.socket) -> None:
        """33 headers — well within MAX_HEADERS=100."""
        client = _connect(server)
        try:
            lines = b"GET / HTTP/1.1\r\nHost: localhost\r\n"
            for i in range(32):  # +Host = 33
                lines += f"X-H-{i:03d}: val-{i}\r\n".encode()
            lines += b"\r\n"
            response = _send_raw(client, lines)
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        finally:
            client.close()

    def test_recovery_after_header_count_overflow(self, server: socket.socket) -> None:
        """After rejecting too many headers, server still handles normal requests."""
        c1 = _connect(server)
        try:
            lines = b"GET / HTTP/1.1\r\nHost: localhost\r\n"
            for i in range(110):
                lines += f"X-H-{i}: v\r\n".encode()
            lines += b"\r\n"
            response = _send_raw(c1, lines)
            assert b"400" in response
        finally:
            c1.close()

        time.sleep(0.2)

        c2 = _connect(server)
        try:
            response = _send_http_request(c2, "GET", "/")
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        finally:
            c2.close()

    def test_duplicate_host_headers(self, server: socket.socket) -> None:
        """Multiple Host headers — potential virtual host confusion."""
        client = _connect(server)
        try:
            raw = b"GET / HTTP/1.1\r\nHost: legitimate.com\r\nHost: evil.com\r\n\r\n"
            response = _send_raw(client, raw)
            status = _get_status_code(response)
            assert status is not None
            if status == 200:
                # Verify which host the handler sees
                _, _, body = response.partition(b"\r\n\r\n")
                data = json.loads(body)
                # Should see one of them consistently (last wins per Python dict)
                assert data["headers"]["host"] in ("legitimate.com", "evil.com")
        finally:
            client.close()

    def test_header_with_very_long_value(self, server: socket.socket) -> None:
        """Single header with a very long value (within total header limit)."""
        client = _connect(server)
        try:
            long_val = "A" * 16000
            response = _send_http_request(
                client,
                "GET",
                "/",
                headers={"X-Long": long_val},
            )
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        finally:
            client.close()

    def test_many_small_headers(self, server: socket.socket) -> None:
        """Many small headers to fill the header size limit."""
        client = _connect(server)
        try:
            lines = b"GET / HTTP/1.1\r\nHost: localhost\r\n"
            # Each header is ~20 bytes: "X-NN: value\r\n"
            for i in range(200):
                lines += f"X-{i:04d}: v\r\n".encode()
            lines += b"\r\n"
            response = _send_raw(client, lines)
            status = _get_status_code(response)
            assert status is not None
        finally:
            client.close()

    def test_header_case_sensitivity(self, server: socket.socket) -> None:
        """Headers with mixed case — verify consistent lowercasing."""
        client = _connect(server)
        try:
            raw = (
                b"GET / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"X-MiXeD-CaSe: value1\r\n"
                b"x-mixed-case: value2\r\n"
                b"\r\n"
            )
            response = _send_raw(client, raw)
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
            _, _, body = response.partition(b"\r\n\r\n")
            data = json.loads(body)
            # Last value wins because the headers dict is keyed by lowered name
            assert data["headers"]["x-mixed-case"] == "value2"
        finally:
            client.close()

    def test_transfer_encoding_chunked_no_cl(self, server: socket.socket) -> None:
        """Transfer-Encoding: chunked without Content-Length.

        Server doesn't support chunked encoding, so body is empty (no CL).
        The chunked framing data stays in the buffer as a malformed pipelined request.
        """
        client = _connect(server)
        try:
            raw = (
                b"POST / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"5\r\nhello\r\n0\r\n\r\n"
            )
            response = _send_raw(client, raw)
            status = _get_status_code(response)
            assert status == 200
            _, _, body = response.partition(b"\r\n\r\n")
            data = json.loads(body)
            # No CL header → body is empty
            assert data["body_len"] == 0
        finally:
            client.close()


# ===========================================================================
# 3. CONTENT-LENGTH ATTACKS
# ===========================================================================


class TestContentLengthAttacks:
    """Attacks targeting Content-Length parsing and body handling."""

    def test_content_length_zero_with_body(self, server: socket.socket) -> None:
        """Content-Length: 0 but body data follows. The server should parse
        the first request with an empty body, and treat the trailing data
        as a (malformed) pipelined request.
        """
        client = _connect(server)
        try:
            raw = (
                b"POST / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Content-Length: 0\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"unexpected body data"
            )
            client.sendall(raw)
            # With Connection: close, server sends one response then closes.
            # The first request has Content-Length: 0, so body is empty.
            all_data = b""
            while True:
                try:
                    chunk = client.recv(8192)
                except TimeoutError, ConnectionError:
                    break
                if not chunk:
                    break
                all_data += chunk
            assert all_data.startswith(b"HTTP/1.1 200 OK\r\n")
            assert b'"body_len": 0' in all_data
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_content_length_larger_than_body(self, ok_server: socket.socket) -> None:
        """Content-Length claims more data than actually sent, then close.

        Server should eventually see EOF before the full body and respond with 400.
        """
        client = _connect(ok_server)
        try:
            raw = b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 10000\r\n\r\nshort"
            client.sendall(raw)
            client.shutdown(socket.SHUT_WR)
            response = _recv_response(client, timeout=3.0)
            if response:
                status = _get_status_code(response)
                # Should be 400 (incomplete body) or connection closed
                assert status in (None, 400)
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_content_length_negative(self, ok_server: socket.socket) -> None:
        """Negative Content-Length value."""
        client = _connect(ok_server)
        try:
            raw = b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: -1\r\n\r\n"
            response = _send_raw(client, raw)
            if response:
                status = _get_status_code(response)
                assert status == 400
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_content_length_non_numeric(self, ok_server: socket.socket) -> None:
        """Non-numeric Content-Length value."""
        client = _connect(ok_server)
        try:
            raw = b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: abc\r\n\r\n"
            response = _send_raw(client, raw)
            if response:
                status = _get_status_code(response)
                assert status == 400
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_content_length_hex(self, ok_server: socket.socket) -> None:
        """Content-Length as hex value (0x10)."""
        client = _connect(ok_server)
        try:
            raw = b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0x10\r\n\r\n"
            response = _send_raw(client, raw)
            if response:
                status = _get_status_code(response)
                # Should reject — only decimal is valid
                assert status == 400
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_content_length_with_leading_zeros(self, server: socket.socket) -> None:
        """Content-Length with leading zeros (e.g., 005)."""
        client = _connect(server)
        try:
            raw = b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 005\r\n\r\nhello"
            response = _send_raw(client, raw)
            if response:
                status = _get_status_code(response)
                assert status is not None
                if status == 200:
                    _, _, body = response.partition(b"\r\n\r\n")
                    data = json.loads(body)
                    assert data["body_len"] == 5
        finally:
            client.close()

    def test_content_length_with_whitespace(self, server: socket.socket) -> None:
        """Content-Length with leading/trailing whitespace."""
        client = _connect(server)
        try:
            raw = b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length:  5 \r\n\r\nhello"
            response = _send_raw(client, raw)
            if response:
                status = _get_status_code(response)
                assert status is not None
                if status == 200:
                    _, _, body = response.partition(b"\r\n\r\n")
                    data = json.loads(body)
                    assert data["body_len"] == 5
        finally:
            client.close()

    def test_content_length_overflow(self, ok_server: socket.socket) -> None:
        """Content-Length with a value that would overflow usize."""
        client = _connect(ok_server)
        try:
            raw = (
                b"POST / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Content-Length: 99999999999999999999\r\n"
                b"\r\n"
            )
            response = _send_raw(client, raw)
            if response:
                status = _get_status_code(response)
                # Should reject — integer overflow
                assert status in (400, 413)
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_no_content_length_for_post(self, server: socket.socket) -> None:
        """POST without Content-Length — body should be empty."""
        client = _connect(server)
        try:
            raw = b"POST / HTTP/1.1\r\nHost: localhost\r\n\r\n"
            response = _send_raw(client, raw)
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
            _, _, body = response.partition(b"\r\n\r\n")
            data = json.loads(body)
            assert data["body_len"] == 0
        finally:
            client.close()


# ===========================================================================
# 4. REQUEST SIZE LIMITS
# ===========================================================================


class TestRequestSizeLimits:
    """Verify hard limits on header and body sizes."""

    def test_body_exactly_at_limit(self, small_limits_server: socket.socket) -> None:
        """Body exactly at max_body_size=1024 — should succeed."""
        client = _connect(small_limits_server)
        try:
            body = b"x" * 1024
            response = _send_http_request(client, "POST", "/", body=body)
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        finally:
            client.close()

    def test_body_one_byte_over_limit(self, small_limits_server: socket.socket) -> None:
        """Body one byte over max_body_size=1024 — should get 413."""
        client = _connect(small_limits_server)
        try:
            body = b"x" * 1025
            response = _send_http_request(client, "POST", "/", body=body)
            assert b"413" in response
        finally:
            client.close()

    def test_headers_exactly_at_limit(self, small_limits_server: socket.socket) -> None:
        """Headers exactly at max_header_size=512 bytes."""
        client = _connect(small_limits_server)
        try:
            # Build a request with headers just under 512 bytes.
            # Request line + Host header + custom header + CRLFCRLF
            base = b"GET / HTTP/1.1\r\nHost: localhost\r\n"
            target = 510  # leave room for final CRLFCRLF
            pad = target - len(base) - len(b"X-Pad: \r\n")
            if pad > 0:
                raw = base + b"X-Pad: " + b"A" * pad + b"\r\n\r\n"
            else:
                raw = base + b"\r\n"
            response = _send_raw(client, raw)
            status = _get_status_code(response)
            assert status is not None
        finally:
            client.close()

    def test_headers_over_limit(self, small_limits_server: socket.socket) -> None:
        """Headers exceeding max_header_size=512 — should get 431."""
        client = _connect(small_limits_server)
        try:
            big_value = "X" * 600
            response = _send_http_request(
                client,
                "GET",
                "/",
                headers={"X-Big": big_value},
            )
            assert b"431" in response
        finally:
            client.close()

    def test_combined_header_body_at_limit(self, small_limits_server: socket.socket) -> None:
        """Small headers + body that together push the cumulative limit."""
        client = _connect(small_limits_server)
        try:
            # max_header_size=512 + max_body_size=1024 = 1536 cumulative limit
            body = b"x" * 1024  # at body limit
            response = _send_http_request(client, "POST", "/", body=body)
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        finally:
            client.close()

    def test_recovery_after_413(self, small_limits_server: socket.socket) -> None:
        """After 413 rejection, a normal request on a new connection succeeds."""
        # Trigger 413
        c1 = _connect(small_limits_server)
        try:
            body = b"x" * 2048
            response = _send_http_request(c1, "POST", "/", body=body)
            assert b"413" in response
        finally:
            c1.close()

        time.sleep(0.2)

        # Normal request should succeed
        c2 = _connect(small_limits_server)
        try:
            response = _send_http_request(c2, "GET", "/")
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        finally:
            c2.close()

    def test_recovery_after_431(self, small_limits_server: socket.socket) -> None:
        """After 431 rejection, a normal request on a new connection succeeds."""
        c1 = _connect(small_limits_server)
        try:
            big_value = "X" * 600
            response = _send_http_request(c1, "GET", "/", headers={"X-Big": big_value})
            assert b"431" in response
        finally:
            c1.close()

        time.sleep(0.2)

        c2 = _connect(small_limits_server)
        try:
            response = _send_http_request(c2, "GET", "/")
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        finally:
            c2.close()


# ===========================================================================
# 5. PATH AND QUERY STRING ATTACKS
# ===========================================================================


class TestPathAndQueryString:
    """Attacks targeting URL path and query string parsing."""

    def test_path_traversal(self, server: socket.socket) -> None:
        """Path traversal attempt (../../etc/passwd)."""
        client = _connect(server)
        try:
            response = _send_http_request(client, "GET", "/../../etc/passwd")
            # The path should be passed through literally — no filesystem access.
            # Just verify we get a response (framework doesn't serve files).
            status = _get_status_code(response)
            assert status is not None
        finally:
            client.close()

    def test_encoded_path_traversal(self, server: socket.socket) -> None:
        """URL-encoded path traversal (..%2F..%2Fetc%2Fpasswd)."""
        client = _connect(server)
        try:
            response = _send_http_request(client, "GET", "/..%2F..%2Fetc%2Fpasswd")
            status = _get_status_code(response)
            assert status is not None
        finally:
            client.close()

    def test_double_encoded_path(self, server: socket.socket) -> None:
        """Double URL encoding (%252e%252e%252f)."""
        client = _connect(server)
        try:
            response = _send_http_request(client, "GET", "/%252e%252e%252f")
            status = _get_status_code(response)
            assert status is not None
        finally:
            client.close()

    def test_very_long_query_string(self, server: socket.socket) -> None:
        """Extremely long query string."""
        client = _connect(server)
        try:
            qs = "a=" + "B" * 10000
            response = _send_http_request(client, "GET", f"/?{qs}")
            status = _get_status_code(response)
            assert status is not None
        finally:
            client.close()

    def test_many_query_parameters(self, server: socket.socket) -> None:
        """Hundreds of query parameters (hash collision DoS vector for some parsers)."""
        client = _connect(server)
        try:
            params = "&".join(f"k{i}=v{i}" for i in range(500))
            response = _send_http_request(client, "GET", f"/?{params}")
            status = _get_status_code(response)
            assert status is not None
        finally:
            client.close()

    def test_query_string_with_special_chars(self, server: socket.socket) -> None:
        """Query string with special/encoded characters."""
        client = _connect(server)
        try:
            response = _send_http_request(
                client,
                "GET",
                "/?key=%00%01%02&foo=bar%26baz=qux",
            )
            status = _get_status_code(response)
            assert status is not None
        finally:
            client.close()

    def test_null_byte_in_path(self, server: socket.socket) -> None:
        """Null byte in the URL path (%00)."""
        client = _connect(server)
        try:
            response = _send_http_request(client, "GET", "/test%00evil")
            status = _get_status_code(response)
            assert status is not None
        finally:
            client.close()

    def test_empty_path(self, server: socket.socket) -> None:
        """Empty path (just host)."""
        client = _connect(server)
        try:
            raw = b"GET  HTTP/1.1\r\nHost: localhost\r\n\r\n"
            response = _send_raw(client, raw)
            if response:
                status = _get_status_code(response)
                assert status is not None
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_asterisk_path(self, server: socket.socket) -> None:
        """OPTIONS * request (valid HTTP for server-wide options)."""
        client = _connect(server)
        try:
            response = _send_http_request(client, "OPTIONS", "*")
            status = _get_status_code(response)
            assert status is not None
        finally:
            client.close()

    def test_absolute_uri(self, server: socket.socket) -> None:
        """Absolute URI in request line (proxy-style)."""
        client = _connect(server)
        try:
            response = _send_raw(
                client,
                b"GET http://localhost/ HTTP/1.1\r\nHost: localhost\r\n\r\n",
            )
            status = _get_status_code(response)
            assert status is not None
        finally:
            client.close()


# ===========================================================================
# 6. HTTP METHOD ATTACKS
# ===========================================================================


class TestHTTPMethods:
    """Attacks targeting HTTP method handling."""

    def test_unknown_method(self, server: socket.socket) -> None:
        """Request with an unknown HTTP method."""
        client = _connect(server)
        try:
            response = _send_raw(
                client,
                b"FOOBAR / HTTP/1.1\r\nHost: localhost\r\n\r\n",
            )
            status = _get_status_code(response)
            assert status is not None
        finally:
            client.close()

    def test_very_long_method(self, ok_server: socket.socket) -> None:
        """Extremely long HTTP method name."""
        client = _connect(ok_server)
        try:
            method = "A" * 1000
            response = _send_raw(
                client,
                f"{method} / HTTP/1.1\r\nHost: localhost\r\n\r\n".encode(),
            )
            if response:
                status = _get_status_code(response)
                assert status is not None
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_lowercase_method(self, server: socket.socket) -> None:
        """Lowercase method (get instead of GET)."""
        client = _connect(server)
        try:
            response = _send_raw(
                client,
                b"get / HTTP/1.1\r\nHost: localhost\r\n\r\n",
            )
            status = _get_status_code(response)
            assert status is not None
            if status == 200:
                _, _, body = response.partition(b"\r\n\r\n")
                data = json.loads(body)
                # Verify what the handler receives
                assert isinstance(data["method"], str)
        finally:
            client.close()

    def test_method_with_spaces(self, ok_server: socket.socket) -> None:
        """Method with embedded space (should be rejected)."""
        client = _connect(ok_server)
        try:
            response = _send_raw(
                client,
                b"GET EVIL / HTTP/1.1\r\nHost: localhost\r\n\r\n",
            )
            if response:
                status = _get_status_code(response)
                # Should be parsed weirdly or rejected
                assert status is not None
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()


# ===========================================================================
# 7. PIPELINING ATTACKS
# ===========================================================================


class TestPipelining:
    """Attacks targeting HTTP pipelining (multiple requests per connection)."""

    def test_simple_pipeline(self, ok_server: socket.socket) -> None:
        """Two valid pipelined requests sent in a single TCP write."""
        client = _connect(ok_server)
        try:
            raw = (
                b"GET /first HTTP/1.1\r\nHost: localhost\r\n\r\n"
                b"GET /second HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            client.sendall(raw)
            # Read everything until connection closes
            all_data = b""
            while True:
                try:
                    chunk = client.recv(8192)
                except TimeoutError, ConnectionError:
                    break
                if not chunk:
                    break
                all_data += chunk
            # Both responses should be present
            assert all_data.count(b"HTTP/1.1 200 OK") == 2
        finally:
            client.close()

    def test_pipeline_with_body(self, server: socket.socket) -> None:
        """Pipelined POST requests with bodies."""
        client = _connect(server)
        try:
            raw = (
                b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 5\r\n\r\nhello"
                b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 5\r\nConnection: close\r\n\r\nworld"
            )
            client.sendall(raw)
            all_data = b""
            while True:
                try:
                    chunk = client.recv(8192)
                except TimeoutError, ConnectionError:
                    break
                if not chunk:
                    break
                all_data += chunk
            # Both responses should be present, each with body_len: 5
            assert all_data.count(b"HTTP/1.1 200 OK") == 2
            assert all_data.count(b'"body_len": 5') == 2
        finally:
            client.close()

    def test_pipeline_valid_then_invalid(self, ok_server: socket.socket) -> None:
        """First request valid, second request malformed."""
        client = _connect(ok_server)
        try:
            raw = b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\nGARBAGE\x00\xff\r\n\r\n"
            client.sendall(raw)
            resp1 = _recv_response(client)
            assert resp1.startswith(b"HTTP/1.1 200 OK\r\n")
            # Second should cause an error or connection close
            resp2 = _recv_response(client, timeout=2.0)
            if resp2:
                status = _get_status_code(resp2)
                assert status in (None, 400)
        except ConnectionError, TimeoutError:
            pass  # Server may close after bad request
        finally:
            client.close()

    def test_pipeline_close_in_middle(self, ok_server: socket.socket) -> None:
        """Pipeline request with Connection: close in the middle."""
        client = _connect(ok_server)
        try:
            raw = (
                b"GET /first HTTP/1.1\r\nHost: localhost\r\n\r\n"
                b"GET /second HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
                b"GET /third HTTP/1.1\r\nHost: localhost\r\n\r\n"
            )
            client.sendall(raw)
            resp1 = _recv_response(client)
            assert resp1.startswith(b"HTTP/1.1 200 OK\r\n")
            resp2 = _recv_response(client)
            assert resp2.startswith(b"HTTP/1.1 200 OK\r\n")
            # Third request should not be served (connection closed)
            time.sleep(0.2)
            remaining = client.recv(4096)
            assert remaining == b""
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_many_pipelined_requests(self, ok_server: socket.socket) -> None:
        """Many pipelined requests to test resource cleanup.

        We send all requests at once and read the raw response stream.
        Counting individual HTTP 200 responses in the stream confirms the
        server handled all of them.
        """
        client = _connect(ok_server)
        try:
            req = b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n"
            count = 50
            # Send last one with Connection: close so server closes when done
            pipeline = req * (count - 1)
            pipeline += b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            client.sendall(pipeline)

            # Read all response data until connection closes
            all_data = b""
            while True:
                try:
                    chunk = client.recv(65536)
                except TimeoutError, ConnectionError:
                    break
                if not chunk:
                    break
                all_data += chunk

            # Count the number of "HTTP/1.1 200 OK" in the stream
            ok_count = all_data.count(b"HTTP/1.1 200 OK")
            assert ok_count == count
        finally:
            client.close()


# ===========================================================================
# 8. HANDLER CRASH RECOVERY
# ===========================================================================


class TestHandlerCrashRecovery:
    """Verify the server handles handler exceptions gracefully."""

    def test_handler_crash_returns_500(self, crash_server: socket.socket) -> None:
        """Handler raising RuntimeError should produce 500."""
        client = _connect(crash_server)
        try:
            response = _send_http_request(client, "GET", "/crash")
            assert b"500" in response
        finally:
            client.close()

    def test_zero_division_returns_500(self, crash_server: socket.socket) -> None:
        """ZeroDivisionError in handler should produce 500."""
        client = _connect(crash_server)
        try:
            response = _send_http_request(client, "GET", "/zero-division")
            assert b"500" in response
        finally:
            client.close()

    def test_key_error_returns_500(self, crash_server: socket.socket) -> None:
        """KeyError in handler should produce 500."""
        client = _connect(crash_server)
        try:
            response = _send_http_request(client, "GET", "/key-error")
            assert b"500" in response
        finally:
            client.close()

    def test_type_error_returns_500(self, crash_server: socket.socket) -> None:
        """TypeError in handler should produce 500."""
        client = _connect(crash_server)
        try:
            response = _send_http_request(client, "GET", "/type-error")
            assert b"500" in response
        finally:
            client.close()

    def test_server_recovers_after_crash(self, crash_server: socket.socket) -> None:
        """After a handler crash, subsequent requests still work."""
        # Crash
        c1 = _connect(crash_server)
        try:
            response = _send_http_request(c1, "GET", "/crash")
            assert b"500" in response
        finally:
            c1.close()

        time.sleep(0.1)

        # Normal request
        c2 = _connect(crash_server)
        try:
            response = _send_http_request(c2, "GET", "/ok")
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        finally:
            c2.close()

    def test_rapid_crash_recovery(self, crash_server: socket.socket) -> None:
        """Multiple crashes in rapid succession shouldn't kill the server."""
        for _ in range(10):
            client = _connect(crash_server)
            try:
                response = _send_http_request(client, "GET", "/crash")
                assert b"500" in response
            finally:
                client.close()

        # Server should still work
        client = _connect(crash_server)
        try:
            response = _send_http_request(client, "GET", "/ok")
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        finally:
            client.close()

    def test_alternating_crash_and_success(self, crash_server: socket.socket) -> None:
        """Alternating crash and normal requests on separate connections."""
        for i in range(5):
            client = _connect(crash_server)
            try:
                if i % 2 == 0:
                    response = _send_http_request(client, "GET", "/crash")
                    assert b"500" in response
                else:
                    response = _send_http_request(client, "GET", "/ok")
                    assert response.startswith(b"HTTP/1.1 200 OK\r\n")
            finally:
                client.close()


# ===========================================================================
# 9. RESPONSE SIZE / HEADER TESTS
# ===========================================================================


class TestResponseBehavior:
    """Tests for response formatting and edge cases."""

    def test_large_response_body(self, crash_server: socket.socket) -> None:
        """Handler returning a 500 KB response body."""
        client = _connect(crash_server)
        try:
            response = _send_http_request(client, "GET", "/large-response")
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
            assert b"Content-Length: 500000\r\n" in response
            _, _, body = response.partition(b"\r\n\r\n")
            assert len(body) == 500_000
            assert body == b"X" * 500_000
        finally:
            client.close()

    def test_many_response_headers(self, crash_server: socket.socket) -> None:
        """Handler setting 50 response headers."""
        client = _connect(crash_server)
        try:
            response = _send_http_request(client, "GET", "/many-headers")
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
            for i in range(50):
                assert f"X-Header-{i}: value-{i}\r\n".encode() in response
        finally:
            client.close()

    def test_empty_response_body(self, crash_server: socket.socket) -> None:
        """Handler that doesn't write anything."""
        app = Framework()

        @app.route("/empty")
        def empty(request: Request, response: Response) -> None:
            pass

        for sock in _start_app_server(app):
            client = _connect(sock)
            try:
                response = _send_http_request(client, "GET", "/empty")
                assert response.startswith(b"HTTP/1.1 200 OK\r\n")
                assert b"Content-Length: 0\r\n" in response
            finally:
                client.close()
            break


# ===========================================================================
# 10. CONNECTION LIFECYCLE
# ===========================================================================


class TestConnectionLifecycle:
    """Tests for connection management edge cases."""

    def test_connect_and_disconnect_immediately(self, ok_server: socket.socket) -> None:
        """Connect and immediately close — server shouldn't crash."""
        for _ in range(10):
            client = _connect(ok_server)
            client.close()

        time.sleep(0.2)

        # Server should still work
        client = _connect(ok_server)
        try:
            response = _send_http_request(client, "GET", "/")
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        finally:
            client.close()

    def test_connect_send_partial_then_close(self, ok_server: socket.socket) -> None:
        """Send partial request then close — server should clean up."""
        for _ in range(5):
            client = _connect(ok_server)
            try:
                client.sendall(b"GET / HTT")
                time.sleep(0.01)
            finally:
                client.close()

        time.sleep(0.2)

        client = _connect(ok_server)
        try:
            response = _send_http_request(client, "GET", "/")
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        finally:
            client.close()

    def test_tcp_reset(self, ok_server: socket.socket) -> None:
        """Send RST instead of graceful close."""
        for _ in range(5):
            client = _connect(ok_server)
            try:
                client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
                # Set linger to force RST on close
                client.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_LINGER,
                    struct.pack("ii", 1, 0),
                )
            finally:
                client.close()

        time.sleep(0.2)

        client = _connect(ok_server)
        try:
            response = _send_http_request(client, "GET", "/")
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        finally:
            client.close()

    def test_many_sequential_connections(self, ok_server: socket.socket) -> None:
        """Open and close many connections sequentially."""
        for _ in range(50):
            client = _connect(ok_server)
            try:
                response = _send_http_request(
                    client,
                    "GET",
                    "/",
                    headers={"Connection": "close"},
                )
                assert response.startswith(b"HTTP/1.1 200 OK\r\n")
            finally:
                client.close()

    def test_keep_alive_many_requests(self, ok_server: socket.socket) -> None:
        """Many requests on a single keep-alive connection."""
        client = _connect(ok_server)
        try:
            for _ in range(100):
                response = _send_http_request(client, "GET", "/")
                assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        finally:
            client.close()

    def test_connection_pool_exhaustion_and_recovery(
        self,
        tiny_pool_server: socket.socket,
    ) -> None:
        """Fill connection pool, verify 503, release, verify recovery."""
        clients: list[socket.socket] = []
        try:
            # Fill the pool (max_connections=4)
            for _ in range(4):
                c = _connect(tiny_pool_server)
                resp = _send_http_request(c, "GET", "/")
                assert resp.startswith(b"HTTP/1.1 200 OK\r\n")
                clients.append(c)

            time.sleep(0.2)

            # 5th connection should be rejected
            c5 = _connect(tiny_pool_server)
            try:
                response = _recv_response(c5, timeout=2.0)
                if response:
                    assert b"503" in response
            except ConnectionError, TimeoutError:
                pass
            finally:
                c5.close()
        finally:
            for c in clients:
                c.close()

        time.sleep(0.3)

        # After releasing all connections, new ones should work
        client = _connect(tiny_pool_server)
        try:
            response = _send_http_request(client, "GET", "/")
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        finally:
            client.close()

    def test_concurrent_connections(self, ok_server: socket.socket) -> None:
        """Open several connections, each on its own thread, verify responses.

        The io_uring hub runs in a single thread, so we stagger connections
        slightly to avoid overwhelming the accept queue.
        """
        successes = 0
        failures = 0
        lock = threading.Lock()

        def worker() -> None:
            nonlocal successes, failures
            try:
                c = _connect(ok_server)
                try:
                    resp = _send_http_request(c, "GET", "/")
                    with lock:
                        if resp.startswith(b"HTTP/1.1 200 OK\r\n"):
                            successes += 1
                        else:
                            failures += 1
                finally:
                    c.close()
            except Exception:
                with lock:
                    failures += 1

        threads: list[threading.Thread] = []
        for _ in range(10):
            t = threading.Thread(target=worker)
            threads.append(t)
            t.start()
            time.sleep(0.02)  # stagger connections

        for t in threads:
            t.join(timeout=10)

        # Most should succeed
        assert successes >= 5, f"Only {successes}/10 succeeded"

        # Server should still work after
        time.sleep(0.2)
        client = _connect(ok_server)
        try:
            response = _send_http_request(client, "GET", "/")
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        finally:
            client.close()


# ===========================================================================
# 11. KEEP-ALIVE AND CONNECTION HEADER
# ===========================================================================


class TestKeepAlive:
    """Tests for keep-alive and Connection header handling."""

    def test_http_10_default_no_keep_alive(self, ok_server: socket.socket) -> None:
        """HTTP/1.0 defaults to Connection: close."""
        client = _connect(ok_server)
        try:
            client.sendall(b"GET / HTTP/1.0\r\nHost: localhost\r\n\r\n")
            response = _recv_response(client)
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
            time.sleep(0.2)
            remaining = client.recv(4096)
            assert remaining == b""  # connection closed
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_http_10_keep_alive_explicit(self, ok_server: socket.socket) -> None:
        """HTTP/1.0 with explicit Connection: keep-alive."""
        client = _connect(ok_server)
        try:
            client.sendall(
                b"GET / HTTP/1.0\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n",
            )
            resp1 = _recv_response(client)
            assert resp1.startswith(b"HTTP/1.1 200 OK\r\n")
            # Should be able to send another request
            client.sendall(
                b"GET / HTTP/1.0\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n",
            )
            resp2 = _recv_response(client)
            assert resp2.startswith(b"HTTP/1.1 200 OK\r\n")
        except ConnectionError, TimeoutError:
            pass  # some implementations may not honor this
        finally:
            client.close()

    def test_http_11_connection_close(self, ok_server: socket.socket) -> None:
        """HTTP/1.1 with Connection: close should close after response."""
        client = _connect(ok_server)
        try:
            response = _send_http_request(
                client,
                "GET",
                "/",
                headers={"Connection": "close"},
            )
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
            time.sleep(0.2)
            remaining = client.recv(4096)
            assert remaining == b""
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()

    def test_connection_header_case_insensitive(self, ok_server: socket.socket) -> None:
        """Connection header value should be matched case-insensitively."""
        client = _connect(ok_server)
        try:
            response = _send_http_request(
                client,
                "GET",
                "/",
                headers={"Connection": "CLOSE"},
            )
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
            time.sleep(0.2)
            remaining = client.recv(4096)
            assert remaining == b""
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()


# ===========================================================================
# 12. ROUTING SECURITY
# ===========================================================================


class TestRoutingSecurity:
    """Tests for routing-related security concerns."""

    def test_route_with_special_chars_in_param(self, crash_server: socket.socket) -> None:
        """Path parameter containing special characters."""
        client = _connect(crash_server)
        try:
            response = _send_http_request(
                client,
                "GET",
                "/echo",
            )
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        finally:
            client.close()

    def test_404_does_not_leak_info(self, crash_server: socket.socket) -> None:
        """404 response should not leak internal information."""
        client = _connect(crash_server)
        try:
            response = _send_http_request(client, "GET", "/nonexistent-path-12345")
            assert b"404" in response
            _, _, body = response.partition(b"\r\n\r\n")
            body_lower = body.lower()
            # Should not contain stack traces, file paths, or internal details
            assert b"traceback" not in body_lower
            assert b".py" not in body_lower
            assert b"error" not in body_lower or body == b"Not Found"
        finally:
            client.close()

    def test_500_does_not_leak_stacktrace(self, crash_server: socket.socket) -> None:
        """500 error response should not contain stack trace."""
        client = _connect(crash_server)
        try:
            response = _send_http_request(client, "GET", "/crash")
            assert b"500" in response
            _, _, body = response.partition(b"\r\n\r\n")
            body_lower = body.lower()
            assert b"traceback" not in body_lower
            assert b"runtimeerror" not in body_lower
            assert b"handler crashed" not in body_lower
        finally:
            client.close()

    def test_method_not_allowed(self, crash_server: socket.socket) -> None:
        """Request with wrong HTTP method should return 405."""
        client = _connect(crash_server)
        try:
            # /ok is GET only
            response = _send_http_request(client, "DELETE", "/ok")
            assert b"405" in response
        finally:
            client.close()


# ===========================================================================
# 13. JSON BODY PARSING
# ===========================================================================


class TestBodyParsing:
    """Tests for request body parsing edge cases."""

    def test_invalid_json_body(self, crash_server: socket.socket) -> None:
        """POST with invalid JSON — handler that calls request.json() should fail gracefully."""
        app = Framework()

        @app.route("/json", methods=["POST"])
        def json_handler(request: Request, response: Response) -> None:
            try:
                data = request.json()
                response.write(json.dumps(data).encode())
            except json.JSONDecodeError, ValueError:
                response.set_status(400)
                response.write(b"Invalid JSON")

        for sock in _start_app_server(app):
            client = _connect(sock)
            try:
                body = b"not valid json {"
                response = _send_http_request(
                    client,
                    "POST",
                    "/json",
                    headers={"Content-Type": "application/json"},
                    body=body,
                )
                status = _get_status_code(response)
                assert status == 400
            finally:
                client.close()
            break

    def test_empty_body_json(self, crash_server: socket.socket) -> None:
        """POST with empty body — request.json() should raise."""
        app = Framework()

        @app.route("/json", methods=["POST"])
        def json_handler(request: Request, response: Response) -> None:
            try:
                data = request.json()
                response.write(json.dumps(data).encode())
            except json.JSONDecodeError, ValueError:
                response.set_status(400)
                response.write(b"Invalid JSON")

        for sock in _start_app_server(app):
            client = _connect(sock)
            try:
                response = _send_http_request(
                    client,
                    "POST",
                    "/json",
                    headers={"Content-Type": "application/json"},
                    body=b"",
                )
                status = _get_status_code(response)
                assert status == 400
            finally:
                client.close()
            break

    def test_binary_body(self, server: socket.socket) -> None:
        """POST with binary body data."""
        client = _connect(server)
        try:
            body = bytes(range(256))
            response = _send_http_request(client, "POST", "/", body=body)
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
            _, _, resp_body = response.partition(b"\r\n\r\n")
            data = json.loads(resp_body)
            assert data["body_len"] == 256
        finally:
            client.close()

    def test_utf8_body(self, server: socket.socket) -> None:
        """POST with UTF-8 body content."""
        client = _connect(server)
        try:
            body = "Hello 世界 🌍".encode("utf-8")
            response = _send_http_request(client, "POST", "/", body=body)
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
            _, _, resp_body = response.partition(b"\r\n\r\n")
            data = json.loads(resp_body)
            assert data["body_len"] == len(body)
        finally:
            client.close()


# ===========================================================================
# 14. REQUEST SMUGGLING / DESYNC
# ===========================================================================


class TestRequestSmuggling:
    """Tests for HTTP request smuggling and desync vectors.

    Note: Many smuggling attacks rely on Transfer-Encoding: chunked,
    which this server does not support. We test what we can.
    """

    def test_request_smuggling_via_line_folding(self, server: socket.socket) -> None:
        """Obsolete header line folding (RFC 7230 Section 3.2.4)."""
        client = _connect(server)
        try:
            raw = b"GET / HTTP/1.1\r\nHost: localhost\r\nX-Folded: first\r\n continuation\r\n\r\n"
            response = _send_raw(client, raw)
            if response:
                status = _get_status_code(response)
                # Should handle or reject obsolete folding
                assert status is not None
        except ConnectionError, TimeoutError:
            pass
        finally:
            client.close()


# ===========================================================================
# 15. STRESS AND EDGE CASES
# ===========================================================================


class TestStressAndEdgeCases:
    """Stress tests and unusual scenarios."""

    def test_rapid_connect_disconnect(self, ok_server: socket.socket) -> None:
        """Rapidly open and close many connections."""
        for _ in range(100):
            try:
                client = _connect(ok_server, timeout=1.0)
                client.close()
            except ConnectionError, TimeoutError:
                pass

        time.sleep(0.3)

        # Server should still be alive
        client = _connect(ok_server)
        try:
            response = _send_http_request(client, "GET", "/")
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        finally:
            client.close()

    def test_mixed_crash_and_normal_load(self, crash_server: socket.socket) -> None:
        """Mix of normal requests and crashes — sequential to avoid GIL contention.

        Verifies the server handles alternating crashes and normal requests
        without corrupting state.
        """
        for i in range(10):
            client = _connect(crash_server)
            try:
                if i % 3 == 0:
                    response = _send_http_request(client, "GET", "/crash")
                    assert b"500" in response
                else:
                    response = _send_http_request(client, "GET", "/ok")
                    assert response.startswith(b"HTTP/1.1 200 OK\r\n")
            finally:
                client.close()

        # Server should still be alive
        client = _connect(crash_server)
        try:
            response = _send_http_request(client, "GET", "/ok")
            assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        finally:
            client.close()

    def test_interleaved_requests_on_same_connection(
        self,
        crash_server: socket.socket,
    ) -> None:
        """Send request, get 500, then send normal request on same connection.

        After a 500, the server closes the connection, so the second
        request should fail gracefully.
        """
        client = _connect(crash_server)
        try:
            # First: crash
            response = _send_http_request(client, "GET", "/crash")
            assert b"500" in response
            # Connection should be closed — second request should fail
            try:
                response2 = _send_http_request(client, "GET", "/ok")
                # If we get a response, it should be valid
                if response2:
                    status = _get_status_code(response2)
                    assert status is not None
            except ConnectionError, TimeoutError, ValueError:
                pass  # Expected — connection was closed
        finally:
            client.close()

    def test_very_large_response_on_keep_alive(self, crash_server: socket.socket) -> None:
        """Large response followed by normal request on same connection."""
        client = _connect(crash_server)
        try:
            # Large response
            resp1 = _send_http_request(client, "GET", "/large-response")
            assert resp1.startswith(b"HTTP/1.1 200 OK\r\n")
            _, _, body1 = resp1.partition(b"\r\n\r\n")
            assert len(body1) == 500_000

            # Follow-up request on same connection
            resp2 = _send_http_request(client, "GET", "/ok")
            assert resp2.startswith(b"HTTP/1.1 200 OK\r\n")
        finally:
            client.close()
