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
        data = json.loads(request.body)
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
    """POST /echo with JSON body, assert handler reads json.loads(body) and echoes it back."""
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


# ---------------------------------------------------------------------------
# LazyRequest-specific tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def echo_all_server() -> Generator[socket.socket]:
    """Server that echoes all request properties as JSON."""

    def handler(request: Request, response: Response) -> None:
        data = {
            "method": request.method,
            "path": request.path,
            "full_path": request.full_path,
            "query_params": request.query_params,
            "headers": request.headers,
            "body": request.body.decode("latin-1"),
        }
        response.set_header("Content-Type", "application/json")
        response.write(json.dumps(data).encode())

    yield from _start_server(handler)


def test_property_values_match(echo_all_server: socket.socket) -> None:
    """Send a request with known fields, verify every property returns expected values."""
    client = _connect(echo_all_server)
    body = b"test body content"
    raw = _send_http_request(
        client,
        "POST",
        "/hello/world?foo=bar&baz=42",
        headers={
            "Content-Type": "text/plain",
            "X-Custom": "myvalue",
        },
        body=body,
    )
    assert raw.startswith(b"HTTP/1.1 200 OK\r\n")
    _, _, resp_body = raw.partition(b"\r\n\r\n")
    data = json.loads(resp_body)
    assert data["method"] == "POST"
    assert data["path"] == "/hello/world"
    assert data["full_path"] == "/hello/world?foo=bar&baz=42"
    assert data["query_params"]["foo"] == "bar"
    assert data["query_params"]["baz"] == "42"
    assert data["headers"]["content-type"] == "text/plain"
    assert data["headers"]["x-custom"] == "myvalue"
    assert data["body"] == "test body content"
    client.close()


def test_duplicate_headers_comma_joined(echo_all_server: socket.socket) -> None:
    """Duplicate Cache-Control headers should be comma-joined per RFC 9110."""
    client = _connect(echo_all_server)
    raw_req = (
        b"GET / HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Cache-Control: no-cache\r\n"
        b"Cache-Control: no-store\r\n"
        b"\r\n"
    )
    client.sendall(raw_req)
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = client.recv(8192)
        if not chunk:
            break
        response += chunk
    header_end = response.index(b"\r\n\r\n") + 4
    headers_part = response[:header_end].decode()
    content_length = 0
    for line in headers_part.split("\r\n"):
        if line.lower().startswith("content-length:"):
            content_length = int(line.split(":", 1)[1].strip())
            break
    body_so_far = response[header_end:]
    while len(body_so_far) < content_length:
        chunk = client.recv(8192)
        if not chunk:
            break
        body_so_far += chunk
    data = json.loads(body_so_far)
    assert data["headers"]["cache-control"] == "no-cache, no-store"
    client.close()


def test_header_keys_lowercased(echo_all_server: socket.socket) -> None:
    """Headers with mixed casing should have lowercased keys."""
    client = _connect(echo_all_server)
    raw_req = (
        b"GET / HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Type: text/html\r\n"
        b"X-Request-ID: abc123\r\n"
        b"\r\n"
    )
    client.sendall(raw_req)
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = client.recv(8192)
        if not chunk:
            break
        response += chunk
    header_end = response.index(b"\r\n\r\n") + 4
    headers_part = response[:header_end].decode()
    content_length = 0
    for line in headers_part.split("\r\n"):
        if line.lower().startswith("content-length:"):
            content_length = int(line.split(":", 1)[1].strip())
            break
    body_so_far = response[header_end:]
    while len(body_so_far) < content_length:
        chunk = client.recv(8192)
        if not chunk:
            break
        body_so_far += chunk
    data = json.loads(body_so_far)
    assert "content-type" in data["headers"]
    assert "x-request-id" in data["headers"]
    # Verify no uppercase keys exist
    for key in data["headers"]:
        assert key == key.lower()
    client.close()


def test_direct_construction_blocked() -> None:
    """_framework_core.Request() should raise TypeError."""
    with pytest.raises(TypeError, match="cannot be created directly"):
        _framework_core.Request()


def test_eager_fields_survive_invalidation() -> None:
    """After _invalidate(), method, path, full_path still return correct values."""
    captured: dict[str, object] = {}

    def handler(request: Request, response: Response) -> None:
        # Access eager fields before invalidation
        captured["method"] = request.method
        captured["path"] = request.path
        captured["full_path"] = request.full_path
        # Also access headers to cache them
        captured["headers_before"] = dict(request.headers)
        response.write(b"ok")

    gen = _start_server(handler)
    listen_sock = next(gen)
    try:
        client = _connect(listen_sock)
        _send_http_request(client, "GET", "/test?q=1", headers={"X-Test": "val"})
        client.close()
        # Verify the handler saw correct values
        assert captured["method"] == "GET"
        assert captured["path"] == "/test"
        assert captured["full_path"] == "/test?q=1"
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def test_invalidation_blocks_uncached_lazy_fields() -> None:
    """After _invalidate(), accessing uncached body/headers raises RuntimeError."""
    errors: list[str] = []

    def handler(request: Request, response: Response) -> None:
        # Do NOT access body or headers (keep them uncached)
        _ = request.method  # only access eager field
        # Manually invalidate
        request._invalidate()
        # Now try accessing lazy fields that were never cached
        try:
            _ = request.body
            errors.append("body did not raise")
        except RuntimeError as e:
            if "after handler returned" not in str(e):
                errors.append(f"body wrong error: {e}")
        try:
            _ = request.headers
            errors.append("headers did not raise")
        except RuntimeError as e:
            if "after handler returned" not in str(e):
                errors.append(f"headers wrong error: {e}")
        try:
            _ = request.query_params
            errors.append("query_params did not raise")
        except RuntimeError as e:
            if "after handler returned" not in str(e):
                errors.append(f"query_params wrong error: {e}")
        # Eager fields should still work after invalidation
        assert request.method == "POST"
        assert request.path == "/test"
        assert request.full_path == "/test?x=1"
        response.write(b"ok")

    gen = _start_server(handler)
    listen_sock = next(gen)
    try:
        client = _connect(listen_sock)
        raw = _send_http_request(client, "POST", "/test?x=1", body=b"hello")
        assert raw.startswith(b"HTTP/1.1 200 OK\r\n")
        client.close()
        assert errors == [], f"Errors: {errors}"
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def test_invalidation_allows_cached_fields() -> None:
    """After _invalidate(), cached body/headers still return correct values."""
    results: dict[str, object] = {}

    def handler(request: Request, response: Response) -> None:
        # Access lazy fields to trigger materialization (caching)
        body_before = request.body
        headers_before = dict(request.headers)
        qp_before = dict(request.query_params)
        # Now invalidate
        request._invalidate()
        # Access them again — should succeed from cache
        results["body_after"] = request.body
        results["headers_after"] = dict(request.headers)
        results["qp_after"] = dict(request.query_params)
        results["body_match"] = (request.body == body_before)
        results["headers_match"] = (dict(request.headers) == headers_before)
        results["qp_match"] = (dict(request.query_params) == qp_before)
        response.write(b"ok")

    gen = _start_server(handler)
    listen_sock = next(gen)
    try:
        client = _connect(listen_sock)
        raw = _send_http_request(
            client, "POST", "/path?key=val",
            headers={"X-Test": "value"},
            body=b"body content",
        )
        assert raw.startswith(b"HTTP/1.1 200 OK\r\n")
        client.close()
        assert results["body_match"] is True
        assert results["headers_match"] is True
        assert results["qp_match"] is True
        assert results["body_after"] == b"body content"
        assert results["qp_after"] == {"key": "val"}
    finally:
        try:
            next(gen)
        except StopIteration:
            pass
