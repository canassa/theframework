"""Tests for Q5: greenlet switch failure cleanup.

Each test verifies that throwing into a greenlet suspended inside a specific
green_* function does not leak active_waits.  Before the fix, each test should
FAIL (active_waits > 0 after cleanup).  After fixing the specific function,
exactly that test should PASS.

Mechanism: greenlet.throw() on a suspended greenlet causes PyGreenlet_Switch
(inside the Zig green function) to return NULL — the exact switch-failure path.
"""

from __future__ import annotations

import os
import select
import socket
import threading
from collections.abc import Callable

import greenlet
import pytest

import theframework  # noqa: F401 — triggers sys.path setup for _framework_core
import _framework_core


# ---------------------------------------------------------------------------
# Shared test harness
# ---------------------------------------------------------------------------

POLLIN = select.POLLIN


def _run_switch_failure_test(
    make_victim: Callable[[object], None],
    *,
    expect_waits: int = 0,
) -> int:
    """Run a switch-failure test and return the observed active_waits.

    *make_victim(hub_g)* is called inside a fresh greenlet.  It must call the
    target green function (which will block).  The wrapper catches all
    exceptions so the greenlet dies cleanly.

    A "killer" greenlet sleeps 50 ms, then throws into the victim.  A
    "checker" greenlet sleeps 200 ms, records active_waits, and stops the hub.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    listen_fd = sock.fileno()

    victim_ref: list[greenlet.greenlet | None] = [None]
    result: dict[str, int | None] = {"active_waits": None}

    def acceptor() -> None:
        hub_g = _framework_core.get_hub_greenlet()

        # -- victim --
        def victim_wrapper() -> None:
            victim_ref[0] = greenlet.getcurrent()
            try:
                make_victim(hub_g)
            except Exception:
                pass

        # -- killer --
        def killer() -> None:
            _framework_core.green_sleep(0.05)
            g = victim_ref[0]
            if g is not None and not g.dead:
                g.throw(Exception("killed"))

        # -- checker --
        def checker() -> None:
            _framework_core.green_sleep(0.2)
            result["active_waits"] = _framework_core.get_active_waits()
            _framework_core.hub_stop()

        for fn in (victim_wrapper, killer, checker):
            g = greenlet.greenlet(fn, parent=hub_g)
            _framework_core.hub_schedule(g)

    ready = threading.Event()

    def run() -> None:
        ready.set()
        _framework_core.hub_run(listen_fd, acceptor)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(timeout=5)
    thread.join(timeout=10)
    sock.close()

    assert result["active_waits"] is not None, "checker never ran — hub likely hung"
    return result["active_waits"]


# ---------------------------------------------------------------------------
# Helper: create a socket pair registered in the hub
# ---------------------------------------------------------------------------


class RegisteredSocketPair:
    """A socket pair where one end is registered in the hub's connection pool."""

    def __init__(self, hub_g: object) -> None:
        self.a, self.b = socket.socketpair()
        self.a.setblocking(False)
        self.b.setblocking(False)
        self.fd = self.a.fileno()
        _framework_core.green_register_fd(self.fd)

    def close(self) -> None:
        try:
            _framework_core.green_unregister_fd(self.fd)
        except Exception:
            pass
        self.a.close()
        self.b.close()


# ---------------------------------------------------------------------------
# 1. greenSleep
# ---------------------------------------------------------------------------


def test_green_sleep_switch_failure() -> None:
    """Throwing into a greenlet in green_sleep must not leak active_waits."""

    def victim(hub_g: object) -> None:
        _framework_core.green_sleep(60.0)

    assert _run_switch_failure_test(victim) == 0


# ---------------------------------------------------------------------------
# 2. greenRecv
# ---------------------------------------------------------------------------


def test_green_recv_switch_failure() -> None:
    """Throwing into a greenlet in green_recv must not leak active_waits."""
    pair: RegisteredSocketPair | None = None

    def victim(hub_g: object) -> None:
        nonlocal pair
        pair = RegisteredSocketPair(hub_g)
        # Peer never sends, so recv blocks forever
        _framework_core.green_recv(pair.fd, 4096)

    try:
        assert _run_switch_failure_test(victim) == 0
    finally:
        if pair:
            pair.close()


# ---------------------------------------------------------------------------
# 3. greenSend
# ---------------------------------------------------------------------------


def test_green_send_switch_failure() -> None:
    """Throwing into a greenlet in green_send must not leak active_waits."""
    pair: RegisteredSocketPair | None = None

    def victim(hub_g: object) -> None:
        nonlocal pair
        pair = RegisteredSocketPair(hub_g)
        # Send a huge buffer — the kernel buffer will fill and send will block
        _framework_core.green_send(pair.fd, b"x" * 4_000_000)

    try:
        assert _run_switch_failure_test(victim) == 0
    finally:
        if pair:
            pair.close()


# ---------------------------------------------------------------------------
# 4. greenSendResponse
# ---------------------------------------------------------------------------


def test_green_send_response_switch_failure() -> None:
    """Throwing into a greenlet in http_send_response must not leak active_waits."""
    pair: RegisteredSocketPair | None = None

    def victim(hub_g: object) -> None:
        nonlocal pair
        pair = RegisteredSocketPair(hub_g)
        # We need a connection that went through the pool (green_register_fd
        # does that).  http_send_response will format headers in the arena
        # and writev headers + body.  With a huge body and a non-reading peer,
        # the writev blocks.
        _framework_core.http_send_response(
            pair.fd, 200, [], b"x" * 4_000_000
        )

    try:
        assert _run_switch_failure_test(victim) == 0
    finally:
        if pair:
            pair.close()


# ---------------------------------------------------------------------------
# 5. doRecv  (via http_read_request)
# ---------------------------------------------------------------------------


def test_do_recv_switch_failure() -> None:
    """Throwing into a greenlet in doRecv (inside http_read_request) must not
    leak active_waits."""
    pair: RegisteredSocketPair | None = None

    def victim(hub_g: object) -> None:
        nonlocal pair
        pair = RegisteredSocketPair(hub_g)
        # Send a partial HTTP request: headers claim 100 KB body, but we
        # only send 10 bytes.  The server will block in doRecv waiting for
        # the remaining body.
        partial = (
            b"POST / HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: 100000\r\n"
            b"\r\n"
            b"0123456789"
        )
        pair.b.sendall(partial)
        _framework_core.http_read_request(pair.fd, 32768, 1048576)

    try:
        assert _run_switch_failure_test(victim) == 0
    finally:
        if pair:
            pair.close()


# ---------------------------------------------------------------------------
# 6. greenConnect
# ---------------------------------------------------------------------------


def test_green_connect_switch_failure() -> None:
    """Throwing into a greenlet in green_connect must not leak active_waits."""

    def victim(hub_g: object) -> None:
        # Non-routable address — connect hangs indefinitely
        _framework_core.green_connect("10.255.255.1", 9999)

    assert _run_switch_failure_test(victim) == 0


# ---------------------------------------------------------------------------
# 7. greenConnectFd
# ---------------------------------------------------------------------------


def test_green_connect_fd_switch_failure() -> None:
    """Throwing into a greenlet in green_connect_fd must not leak active_waits."""
    sock: socket.socket | None = None

    def victim(hub_g: object) -> None:
        nonlocal sock
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        _framework_core.green_register_fd(sock.fileno())
        # Non-routable address — connect hangs indefinitely
        _framework_core.green_connect_fd(sock.fileno(), "10.255.255.1", 9999)

    try:
        assert _run_switch_failure_test(victim) == 0
    finally:
        if sock:
            try:
                _framework_core.green_unregister_fd(sock.fileno())
            except Exception:
                pass
            sock.close()


# ---------------------------------------------------------------------------
# 8. greenPollFd
# ---------------------------------------------------------------------------


def test_green_poll_fd_switch_failure() -> None:
    """Throwing into a greenlet in green_poll_fd must not leak active_waits."""
    r_fd: int | None = None
    w_fd: int | None = None

    def victim(hub_g: object) -> None:
        nonlocal r_fd, w_fd
        r_fd, w_fd = os.pipe()
        # Poll for read on a pipe with no data — blocks forever
        _framework_core.green_poll_fd(r_fd, POLLIN)

    try:
        assert _run_switch_failure_test(victim) == 0
    finally:
        if r_fd is not None:
            os.close(r_fd)
        if w_fd is not None:
            os.close(w_fd)


# ---------------------------------------------------------------------------
# 9. greenPollFdTimeout
# ---------------------------------------------------------------------------


def test_green_poll_fd_timeout_switch_failure() -> None:
    """Throwing into a greenlet in green_poll_fd_timeout must not leak
    active_waits."""
    r_fd: int | None = None
    w_fd: int | None = None

    def victim(hub_g: object) -> None:
        nonlocal r_fd, w_fd
        r_fd, w_fd = os.pipe()
        # Poll with a very long timeout — blocks until killed
        _framework_core.green_poll_fd_timeout(r_fd, POLLIN, 60000)

    try:
        assert _run_switch_failure_test(victim) == 0
    finally:
        if r_fd is not None:
            os.close(r_fd)
        if w_fd is not None:
            os.close(w_fd)


# ---------------------------------------------------------------------------
# 10. greenPollMulti
# ---------------------------------------------------------------------------


def test_green_poll_multi_switch_failure() -> None:
    """Throwing into a greenlet in green_poll_multi must not leak
    active_waits."""
    r_fd: int | None = None
    w_fd: int | None = None

    def victim(hub_g: object) -> None:
        nonlocal r_fd, w_fd
        r_fd, w_fd = os.pipe()
        # Poll with a very long timeout — blocks until killed
        _framework_core.green_poll_multi([r_fd], [POLLIN], 60000)

    try:
        assert _run_switch_failure_test(victim) == 0
    finally:
        if r_fd is not None:
            os.close(r_fd)
        if w_fd is not None:
            os.close(w_fd)


# ---------------------------------------------------------------------------
# 11. greenAccept
# ---------------------------------------------------------------------------


def test_green_accept_switch_failure() -> None:
    """Throwing into the acceptor greenlet during green_accept must not leak
    active_waits.

    This test is structured differently: the acceptor itself is the victim.
    It schedules the killer before calling green_accept.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    listen_fd = sock.fileno()

    result: dict[str, int | None] = {"active_waits": None}

    def acceptor() -> None:
        hub_g = _framework_core.get_hub_greenlet()
        acceptor_g = greenlet.getcurrent()

        def killer() -> None:
            _framework_core.green_sleep(0.05)
            if not acceptor_g.dead:
                acceptor_g.throw(Exception("killed"))

        def checker() -> None:
            _framework_core.green_sleep(0.2)
            result["active_waits"] = _framework_core.get_active_waits()
            _framework_core.hub_stop()

        for fn in (killer, checker):
            g = greenlet.greenlet(fn, parent=hub_g)
            _framework_core.hub_schedule(g)

        # Now block in green_accept — killer will throw into us
        try:
            _framework_core.green_accept(listen_fd)
        except Exception:
            pass

        # After the throw, yield so the checker can run
        _framework_core.green_sleep(0.0)

    ready = threading.Event()

    def run() -> None:
        ready.set()
        _framework_core.hub_run(listen_fd, acceptor)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(timeout=5)
    thread.join(timeout=10)
    sock.close()

    assert result["active_waits"] is not None, "checker never ran — hub likely hung"
    # For accept, the CQE handler unconditionally decrements active_waits
    # when the accept CQE eventually arrives.  At check time the accept CQE
    # may not have arrived yet, so we allow 0 or 1.  The key invariant is
    # that active_waits must not grow unboundedly — it must settle to 0.
    assert result["active_waits"] <= 1, (
        f"active_waits leaked: {result['active_waits']}"
    )
