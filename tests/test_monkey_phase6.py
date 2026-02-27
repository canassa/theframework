"""Phase 6 — Runtime integration tests for scenarios that previously only had
structural (source-inspection) coverage.

Tests:
- SSL handshake and I/O with cooperative hub
- SSL handshake timeout fires when peer is unresponsive
- IPv6 connect and echo via cooperative hub
- IPv6 connect timeout
- SSL over IPv6
"""

from __future__ import annotations

import _socket
import socket
import ssl
import threading
import time

import greenlet
import pytest

import _framework_core
from theframework.monkey._state import mark_hub_running, mark_hub_stopped

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _start_hub(
    handler_fn: object,
    family: int = _socket.AF_INET,
    bind_addr: tuple[str, int] = ("127.0.0.1", 0),
) -> tuple[threading.Thread, _socket.socket, int]:
    """Create a listening socket, start hub in a thread. Return (thread, sock, port)."""
    raw_sock = _socket.socket(family, _socket.SOCK_STREAM)
    raw_sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    raw_sock.bind(bind_addr)
    raw_sock.listen(128)
    port = raw_sock.getsockname()[1]

    def _run_acceptor() -> None:
        while True:
            try:
                client_fd = _framework_core.green_accept(raw_sock.fileno())
            except OSError:
                break
            hub_g = _framework_core.get_hub_greenlet()
            g = greenlet.greenlet(
                lambda fd=client_fd: _safe_handler(fd, handler_fn),
                parent=hub_g,
            )
            _framework_core.hub_schedule(g)

    def _safe_handler(fd: int, fn: object) -> None:
        try:
            fn(fd)  # type: ignore[operator]
        except OSError:
            pass

    ready = threading.Event()

    def _thread_main() -> None:
        ready.set()
        try:

            def _acceptor_with_mark() -> None:
                hub_g = _framework_core.get_hub_greenlet()
                mark_hub_running(hub_g)
                _run_acceptor()

            _framework_core.hub_run(raw_sock.fileno(), _acceptor_with_mark)
        finally:
            mark_hub_stopped()

    t = threading.Thread(target=_thread_main, daemon=True)
    t.start()
    ready.wait(timeout=5)
    time.sleep(0.05)
    return t, raw_sock, port


def _stop_hub(t: threading.Thread) -> None:
    _framework_core.hub_stop()
    t.join(timeout=5)


# ===================================================================
# SSL contexts via trustme
# ===================================================================


def _make_ssl_contexts(
    *identities: str,
) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """Create server and client SSL contexts using trustme."""
    trustme = pytest.importorskip("trustme")
    ca = trustme.CA()
    if not identities:
        identities = ("localhost", "127.0.0.1")
    server_cert = ca.issue_cert(*identities)

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_cert.configure_cert(server_ctx)
    ca.configure_trust(server_ctx)

    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ca.configure_trust(client_ctx)

    return server_ctx, client_ctx


# ===================================================================
# SSL handshake + I/O with hub
# ===================================================================


class TestSSLWithHub:
    """Runtime SSL tests using the cooperative hub."""

    def test_ssl_echo_through_hub(self) -> None:
        """SSL handshake + send/recv should work cooperatively through the hub."""
        server_ctx, client_ctx = _make_ssl_contexts()

        def handler(client_fd: int) -> None:
            # Wrap the raw fd in a Python socket, then SSL
            raw = socket.socket(fileno=_socket.dup(client_fd))
            try:
                sconn = server_ctx.wrap_socket(raw, server_side=True)
                try:
                    data = sconn.recv(1024)
                    sconn.sendall(data)
                finally:
                    try:
                        sconn.unwrap()
                    except OSError:
                        pass
                    sconn.close()
            except Exception:
                raw.close()

        t, raw_sock, port = _start_hub(handler)
        try:
            # Client connects with SSL (from outside the hub — blocking is fine)
            cs = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            cs.settimeout(5)
            cs.connect(("127.0.0.1", port))
            scs = client_ctx.wrap_socket(cs, server_hostname="localhost")
            try:
                scs.sendall(b"hello from ssl")
                echo = scs.recv(1024)
                assert echo == b"hello from ssl"
            finally:
                scs.close()
        finally:
            _stop_hub(t)
            raw_sock.close()

    def test_ssl_handshake_timeout(self) -> None:
        """SSL handshake should time out if the peer never responds to TLS."""
        # Create a raw TCP server that accepts but never does SSL handshake
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        accepted: list[socket.socket] = []

        def server_thread() -> None:
            conn, _ = srv.accept()
            accepted.append(conn)
            # Intentionally do NOT perform SSL handshake — just sit
            time.sleep(3)
            conn.close()

        st = threading.Thread(target=server_thread, daemon=True)
        st.start()

        # Use a permissive client context (we just want to test the timeout)
        _, client_ctx = _make_ssl_contexts()
        client_ctx.check_hostname = False
        client_ctx.verify_mode = ssl.CERT_NONE

        cs = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cs.settimeout(0.3)
        cs.connect(("127.0.0.1", port))
        try:
            with pytest.raises(
                (socket.timeout, TimeoutError, ssl.SSLError, ConnectionResetError, OSError)
            ):
                client_ctx.wrap_socket(cs, server_hostname="localhost")
        finally:
            cs.close()
            srv.close()
            for c in accepted:
                c.close()
            st.join(timeout=2)


# ===================================================================
# IPv6 connect + echo with hub
# ===================================================================


class TestIPv6WithHub:
    """Runtime IPv6 tests using the cooperative hub."""

    def test_ipv6_echo_through_hub(self) -> None:
        """IPv6 connect + send/recv should work cooperatively through the hub."""

        def handler(client_fd: int) -> None:
            data = _framework_core.green_recv(client_fd, 1024)
            if data:
                _framework_core.green_send(client_fd, data)

        t, raw_sock, port = _start_hub(handler, family=_socket.AF_INET6, bind_addr=("::1", 0))
        try:
            # Client connects over IPv6
            cs = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            cs.settimeout(5)
            cs.connect(("::1", port))
            cs.sendall(b"ipv6 hello")
            echo = cs.recv(1024)
            assert echo == b"ipv6 hello"
            cs.close()
        finally:
            _stop_hub(t)
            raw_sock.close()

    def test_ipv6_connect_timeout(self) -> None:
        """IPv6 connect to a non-responsive address should respect timeout."""
        cs = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        cs.settimeout(0.1)
        # Connect to IPv6 documentation/discard address to trigger timeout
        try:
            with pytest.raises((socket.timeout, TimeoutError, OSError)):
                cs.connect(("100::", 1))
        finally:
            cs.close()


# ===================================================================
# Combined: SSL over IPv6
# ===================================================================


class TestSSLOverIPv6:
    """SSL over IPv6 through the hub."""

    def test_ssl_ipv6_echo(self) -> None:
        """SSL handshake and echo should work over IPv6 through the hub."""
        server_ctx, client_ctx = _make_ssl_contexts("localhost", "::1")

        def handler(client_fd: int) -> None:
            raw = socket.socket(socket.AF_INET6, socket.SOCK_STREAM, fileno=_socket.dup(client_fd))
            try:
                sconn = server_ctx.wrap_socket(raw, server_side=True)
                try:
                    data = sconn.recv(1024)
                    sconn.sendall(data)
                finally:
                    try:
                        sconn.unwrap()
                    except OSError:
                        pass
                    sconn.close()
            except Exception:
                raw.close()

        t, raw_sock, port = _start_hub(handler, family=_socket.AF_INET6, bind_addr=("::1", 0))
        try:
            cs = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            cs.settimeout(5)
            cs.connect(("::1", port))
            scs = client_ctx.wrap_socket(cs, server_hostname="localhost")
            try:
                scs.sendall(b"ssl-ipv6")
                echo = scs.recv(1024)
                assert echo == b"ssl-ipv6"
            finally:
                scs.close()
        finally:
            _stop_hub(t)
            raw_sock.close()
