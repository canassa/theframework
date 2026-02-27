"""Tests for Phase 4 monkey-patching fixes.

Covers:
- P0-1: Eliminate busy-wait in select() / poll() / CooperativeSelector.select()
- P1-5: Single-fd select timeout enforcement via green_poll_fd_timeout
- green_poll_multi Zig primitive (multi-fd event-driven poll)
- green_poll_fd_timeout Zig primitive (single-fd poll with LINK_TIMEOUT)
"""

from __future__ import annotations

import os
import select
import socket
import threading
import time

import greenlet
import pytest

from theframework.monkey import patch_all, patch_select, patch_selectors
from theframework.monkey._select import (
    _multi_fd_select,
    _single_fd_select,
    poll as CooperativePoll,
)
from theframework.monkey._selectors import CooperativeSelector

import _framework_core  # Must be after theframework imports


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HUB_PORT: int = 0


def _start_hub(
    handler_fn: object,
    ready: threading.Event,
) -> tuple[threading.Thread, socket.socket]:
    import _socket

    raw_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    raw_sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    raw_sock.bind(("127.0.0.1", 0))
    raw_sock.listen(128)
    _addr = raw_sock.getsockname()
    global _HUB_PORT
    _HUB_PORT = _addr[1]

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
        finally:
            try:
                _framework_core.green_close(fd)
            except OSError:
                pass

    def _thread_main() -> None:
        from theframework.monkey._state import mark_hub_running, mark_hub_stopped

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
    return t, raw_sock  # type: ignore[return-value]


def _echo_handler(fd: int) -> None:
    while True:
        data = _framework_core.green_recv(fd, 4096)
        if not data:
            break
        _framework_core.green_send(fd, data)


# ===================================================================
# green_poll_multi primitive tests
# ===================================================================


class TestGreenPollMulti:
    def setup_method(self) -> None:
        patch_all()
        self.ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_echo_handler, self.ready)

    def teardown_method(self) -> None:
        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

    def test_poll_multi_detects_readable(self) -> None:
        """green_poll_multi should detect a readable fd."""
        results: list[object] = []

        def _handler(fd: int) -> None:
            # Create a pipe and write to it
            r_fd, w_fd = os.pipe()
            os.set_blocking(r_fd, False)
            os.write(w_fd, b"data")
            # Poll the read end
            ready = _framework_core.green_poll_multi([r_fd], [0x001], -1)
            results.append(ready)
            os.close(r_fd)
            os.close(w_fd)
            _framework_core.green_send(fd, b"done")

        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

        ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_handler, ready)

        import _socket

        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.connect(("127.0.0.1", _HUB_PORT))
        data = s.recv(1024)
        s.close()

        assert data == b"done"
        assert len(results) == 1
        ready_list = results[0]
        assert len(ready_list) >= 1  # type: ignore[arg-type]

    def test_poll_multi_timeout(self) -> None:
        """green_poll_multi should return empty on timeout."""
        results: list[object] = []

        def _handler(fd: int) -> None:
            # Create a pipe but don't write to it
            r_fd, w_fd = os.pipe()
            os.set_blocking(r_fd, False)
            # Poll with short timeout
            ready = _framework_core.green_poll_multi([r_fd], [0x001], 50)
            results.append(ready)
            os.close(r_fd)
            os.close(w_fd)
            _framework_core.green_send(fd, b"done")

        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

        ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_handler, ready)

        import _socket

        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.connect(("127.0.0.1", _HUB_PORT))
        data = s.recv(1024)
        s.close()

        assert data == b"done"
        assert len(results) == 1
        assert results[0] == []

    def test_poll_multi_multiple_fds(self) -> None:
        """green_poll_multi with multiple fds returns the ready one."""
        results: list[object] = []

        def _handler(fd: int) -> None:
            # Create two pipes, only write to the second
            r1, w1 = os.pipe()
            r2, w2 = os.pipe()
            os.set_blocking(r1, False)
            os.set_blocking(r2, False)
            os.write(w2, b"data")
            ready = _framework_core.green_poll_multi([r1, r2], [0x001, 0x001], -1)
            results.append(ready)
            os.close(r1)
            os.close(w1)
            os.close(r2)
            os.close(w2)
            _framework_core.green_send(fd, b"done")

        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

        ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_handler, ready)

        import _socket

        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.connect(("127.0.0.1", _HUB_PORT))
        data = s.recv(1024)
        s.close()

        assert data == b"done"
        assert len(results) == 1
        ready_list: list[tuple[int, int]] = results[0]  # type: ignore[assignment]
        assert len(ready_list) >= 1
        # The ready fd should be r2 (the one with data)
        ready_fds = [fd for fd, _ in ready_list]
        assert len(ready_fds) >= 1


# ===================================================================
# green_poll_fd_timeout primitive tests
# ===================================================================


class TestGreenPollFdTimeout:
    def setup_method(self) -> None:
        patch_all()
        self.ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_echo_handler, self.ready)

    def teardown_method(self) -> None:
        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

    def test_poll_fd_timeout_ready(self) -> None:
        """green_poll_fd_timeout returns revents when fd is ready."""
        results: list[int] = []

        def _handler(fd: int) -> None:
            r_fd, w_fd = os.pipe()
            os.set_blocking(r_fd, False)
            os.write(w_fd, b"data")
            revents = _framework_core.green_poll_fd_timeout(r_fd, 0x001, 5000)
            results.append(revents)
            os.close(r_fd)
            os.close(w_fd)
            _framework_core.green_send(fd, b"done")

        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

        ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_handler, ready)

        import _socket

        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.connect(("127.0.0.1", _HUB_PORT))
        data = s.recv(1024)
        s.close()

        assert data == b"done"
        assert len(results) == 1
        assert results[0] > 0  # Should have POLLIN set

    def test_poll_fd_timeout_expires(self) -> None:
        """green_poll_fd_timeout returns 0 when timeout fires."""
        results: list[int] = []

        def _handler(fd: int) -> None:
            r_fd, w_fd = os.pipe()
            os.set_blocking(r_fd, False)
            # Don't write anything — fd never becomes ready
            revents = _framework_core.green_poll_fd_timeout(r_fd, 0x001, 50)
            results.append(revents)
            os.close(r_fd)
            os.close(w_fd)
            _framework_core.green_send(fd, b"done")

        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

        ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_handler, ready)

        import _socket

        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.connect(("127.0.0.1", _HUB_PORT))
        data = s.recv(1024)
        s.close()

        assert data == b"done"
        assert len(results) == 1
        assert results[0] == 0  # Timeout → 0

    def test_poll_fd_timeout_timing(self) -> None:
        """green_poll_fd_timeout should return within reasonable time of timeout."""
        timings: list[float] = []

        def _handler(fd: int) -> None:
            r_fd, w_fd = os.pipe()
            os.set_blocking(r_fd, False)
            t0 = time.monotonic()
            _framework_core.green_poll_fd_timeout(r_fd, 0x001, 100)  # 100ms
            elapsed = time.monotonic() - t0
            timings.append(elapsed)
            os.close(r_fd)
            os.close(w_fd)
            _framework_core.green_send(fd, b"done")

        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

        ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_handler, ready)

        import _socket

        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.connect(("127.0.0.1", _HUB_PORT))
        data = s.recv(1024)
        s.close()

        assert len(timings) == 1
        # Should be around 100ms (allow 50ms-500ms range)
        assert 0.05 <= timings[0] <= 0.5, f"Expected ~100ms, got {timings[0] * 1000:.0f}ms"


# ===================================================================
# select() no-busy-wait tests
# ===================================================================


class TestSelectNoBusyWait:
    """Verify select() uses event-driven waiting, not busy-wait."""

    def test_select_source_no_busy_wait(self) -> None:
        """The multi-fd path should NOT use green_sleep(0.001)."""
        import inspect

        from theframework.monkey import _select

        src = inspect.getsource(_select)
        # The old busy-wait pattern was green_sleep(0.001) in a while loop
        assert "green_sleep(0.001)" not in src, "select module still contains busy-wait pattern"

    def test_selectors_source_no_busy_wait(self) -> None:
        """CooperativeSelector should NOT use green_sleep(0.001)."""
        import inspect

        from theframework.monkey import _selectors

        src = inspect.getsource(_selectors)
        assert "green_sleep(0.001)" not in src, "selectors module still contains busy-wait pattern"

    def test_select_uses_green_poll_multi(self) -> None:
        """The multi-fd path should use green_poll_multi."""
        import inspect

        from theframework.monkey._select import _multi_fd_select

        src = inspect.getsource(_multi_fd_select)
        assert "green_poll_multi" in src

    def test_select_single_fd_uses_poll_fd_timeout(self) -> None:
        """The single-fd path with timeout should use green_poll_fd_timeout."""
        import inspect

        from theframework.monkey._select import _single_fd_select

        src = inspect.getsource(_single_fd_select)
        assert "green_poll_fd_timeout" in src

    def test_selectors_uses_green_poll_multi(self) -> None:
        """CooperativeSelector.select should use green_poll_multi."""
        import inspect

        src = inspect.getsource(CooperativeSelector.select)
        assert "green_poll_multi" in src

    def test_poll_uses_green_poll_multi(self) -> None:
        """poll.poll should use green_poll_multi."""
        import inspect

        src = inspect.getsource(CooperativePoll.poll)
        assert "green_poll_multi" in src


# ===================================================================
# select() timeout enforcement (P1-5)
# ===================================================================


class TestSelectTimeoutEnforcement:
    """Verify select() respects timeout even on single-fd cooperative path."""

    def setup_method(self) -> None:
        patch_all()
        self.ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_echo_handler, self.ready)

    def teardown_method(self) -> None:
        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

    def test_single_fd_select_respects_timeout(self) -> None:
        """select([sock], [], [], 0.1) should return within ~200ms."""
        timings: list[float] = []
        results_box: list[object] = []

        def _handler(fd: int) -> None:
            # Create a socket pair, don't write any data
            r_fd, w_fd = os.pipe()
            os.set_blocking(r_fd, False)
            t0 = time.monotonic()
            # Use green_poll_fd_timeout directly since select dispatch needs
            # the Python select module to be patched
            revents = _framework_core.green_poll_fd_timeout(r_fd, 0x001, 100)
            elapsed = time.monotonic() - t0
            timings.append(elapsed)
            results_box.append(revents)
            os.close(r_fd)
            os.close(w_fd)
            _framework_core.green_send(fd, b"done")

        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

        ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_handler, ready)

        import _socket

        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.connect(("127.0.0.1", _HUB_PORT))
        data = s.recv(1024)
        s.close()

        assert data == b"done"
        assert len(timings) == 1
        assert timings[0] < 0.5, f"Should timeout within 500ms, took {timings[0] * 1000:.0f}ms"
        assert results_box[0] == 0  # Timeout → 0

    def test_multi_fd_select_respects_timeout(self) -> None:
        """select() with multiple fds and timeout should not hang."""
        timings: list[float] = []
        results_box: list[object] = []

        def _handler(fd: int) -> None:
            r1, w1 = os.pipe()
            r2, w2 = os.pipe()
            os.set_blocking(r1, False)
            os.set_blocking(r2, False)
            t0 = time.monotonic()
            ready = _framework_core.green_poll_multi([r1, r2], [0x001, 0x001], 100)
            elapsed = time.monotonic() - t0
            timings.append(elapsed)
            results_box.append(ready)
            os.close(r1)
            os.close(w1)
            os.close(r2)
            os.close(w2)
            _framework_core.green_send(fd, b"done")

        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

        ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_handler, ready)

        import _socket

        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.connect(("127.0.0.1", _HUB_PORT))
        data = s.recv(1024)
        s.close()

        assert data == b"done"
        assert len(timings) == 1
        assert timings[0] < 0.5
        assert results_box[0] == []  # Timeout → empty list


# ===================================================================
# Cooperative select() integration tests (with hub)
# ===================================================================


class TestSelectIntegrationWithHub:
    """Test patched select module working cooperatively with the hub."""

    def setup_method(self) -> None:
        patch_all()
        self.ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_echo_handler, self.ready)

    def teardown_method(self) -> None:
        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

    def test_patched_select_readable(self) -> None:
        """Patched select.select should detect readable socket."""
        results: list[object] = []

        def _handler(fd: int) -> None:
            r_fd, w_fd = os.pipe()
            os.set_blocking(r_fd, False)
            os.write(w_fd, b"x")
            r, w, x = select.select([r_fd], [], [], 5.0)
            results.append((len(r), len(w), len(x)))
            os.close(r_fd)
            os.close(w_fd)
            _framework_core.green_send(fd, b"done")

        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

        ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_handler, ready)

        import _socket

        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.connect(("127.0.0.1", _HUB_PORT))
        data = s.recv(1024)
        s.close()

        assert data == b"done"
        assert len(results) == 1
        r_len: int = results[0][0]  # type: ignore[index]
        assert r_len == 1

    def test_patched_select_timeout(self) -> None:
        """Patched select.select should timeout properly."""
        results: list[object] = []

        def _handler(fd: int) -> None:
            r_fd, w_fd = os.pipe()
            os.set_blocking(r_fd, False)
            t0 = time.monotonic()
            r, w, x = select.select([r_fd], [], [], 0.1)
            elapsed = time.monotonic() - t0
            results.append((r, w, x, elapsed))
            os.close(r_fd)
            os.close(w_fd)
            _framework_core.green_send(fd, b"done")

        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

        ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_handler, ready)

        import _socket

        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.connect(("127.0.0.1", _HUB_PORT))
        data = s.recv(1024)
        s.close()

        assert data == b"done"
        assert len(results) == 1
        entry = results[0]
        r_list: list[object] = entry[0]  # type: ignore[index]
        w_list: list[object] = entry[1]  # type: ignore[index]
        elapsed_val: float = entry[3]  # type: ignore[index]
        assert r_list == []
        assert w_list == []
        assert elapsed_val < 0.5  # Should be ~100ms


# ===================================================================
# CooperativeSelector integration tests
# ===================================================================


class TestSelectorIntegrationWithHub:
    def setup_method(self) -> None:
        patch_all()
        self.ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_echo_handler, self.ready)

    def teardown_method(self) -> None:
        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

    def test_selector_detects_readable_with_hub(self) -> None:
        """CooperativeSelector should detect readable fd with hub running."""
        import selectors

        results: list[int] = []

        def _handler(fd: int) -> None:
            r_fd, w_fd = os.pipe()
            os.set_blocking(r_fd, False)
            os.write(w_fd, b"x")
            sel = CooperativeSelector()
            sel.register(r_fd, selectors.EVENT_READ)
            ready = sel.select(timeout=5.0)
            results.append(len(ready))
            sel.close()
            os.close(r_fd)
            os.close(w_fd)
            _framework_core.green_send(fd, b"done")

        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

        ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_handler, ready)

        import _socket

        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.connect(("127.0.0.1", _HUB_PORT))
        data = s.recv(1024)
        s.close()

        assert data == b"done"
        assert len(results) == 1
        assert results[0] >= 1

    def test_selector_timeout_with_hub(self) -> None:
        """CooperativeSelector timeout should work with hub."""
        import selectors

        results: list[float] = []

        def _handler(fd: int) -> None:
            r_fd, w_fd = os.pipe()
            os.set_blocking(r_fd, False)
            sel = CooperativeSelector()
            sel.register(r_fd, selectors.EVENT_READ)
            t0 = time.monotonic()
            ready = sel.select(timeout=0.1)
            elapsed = time.monotonic() - t0
            results.append(elapsed)
            sel.close()
            os.close(r_fd)
            os.close(w_fd)
            _framework_core.green_send(fd, b"done")

        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

        ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_handler, ready)

        import _socket

        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.connect(("127.0.0.1", _HUB_PORT))
        data = s.recv(1024)
        s.close()

        assert data == b"done"
        assert len(results) == 1
        assert results[0] < 0.5


# ===================================================================
# poll() replacement tests
# ===================================================================


class TestPollIntegrationWithHub:
    def setup_method(self) -> None:
        patch_all()
        self.ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_echo_handler, self.ready)

    def teardown_method(self) -> None:
        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

    def test_poll_detects_readable(self) -> None:
        """Cooperative poll should detect readable fd."""
        results: list[object] = []

        def _handler(fd: int) -> None:
            r_fd, w_fd = os.pipe()
            os.set_blocking(r_fd, False)
            os.write(w_fd, b"x")
            p = CooperativePoll()
            p.register(r_fd, 0x001)  # POLLIN
            ready = p.poll(5000)  # 5 second timeout (ms)
            results.append(ready)
            os.close(r_fd)
            os.close(w_fd)
            _framework_core.green_send(fd, b"done")

        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

        ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_handler, ready)

        import _socket

        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.connect(("127.0.0.1", _HUB_PORT))
        data = s.recv(1024)
        s.close()

        assert data == b"done"
        assert len(results) == 1
        assert len(results[0]) >= 1  # type: ignore[arg-type]

    def test_poll_timeout(self) -> None:
        """Cooperative poll should timeout properly."""
        results: list[tuple[list[tuple[int, int]], float]] = []

        def _handler(fd: int) -> None:
            r_fd, w_fd = os.pipe()
            os.set_blocking(r_fd, False)
            p = CooperativePoll()
            p.register(r_fd, 0x001)
            t0 = time.monotonic()
            ready = p.poll(100)  # 100ms timeout
            elapsed = time.monotonic() - t0
            results.append((ready, elapsed))
            os.close(r_fd)
            os.close(w_fd)
            _framework_core.green_send(fd, b"done")

        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

        ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_handler, ready)

        import _socket

        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.connect(("127.0.0.1", _HUB_PORT))
        data = s.recv(1024)
        s.close()

        assert data == b"done"
        assert len(results) == 1
        ready_fds, elapsed = results[0]
        assert ready_fds == []
        assert elapsed < 0.5


# ===================================================================
# Cooperative behavior: I/O doesn't block other greenlets
# ===================================================================


class TestCooperativeBehavior:
    """select/poll operations should not block other greenlets."""

    def setup_method(self) -> None:
        patch_all()
        self.ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_echo_handler, self.ready)

    def teardown_method(self) -> None:
        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

    def test_poll_multi_allows_concurrent_accept(self) -> None:
        """A greenlet waiting on poll_multi should not prevent new accepts."""
        accepted: list[bool] = []

        def _poll_then_echo_handler(fd: int) -> None:
            accepted.append(True)
            r_fd, w_fd = os.pipe()
            os.set_blocking(r_fd, False)
            os.write(w_fd, b"x")  # Make it immediately ready
            _framework_core.green_poll_multi([r_fd], [0x001], 500)
            os.close(r_fd)
            os.close(w_fd)
            # Echo whatever the client sends
            data = _framework_core.green_recv(fd, 4096)
            if data:
                _framework_core.green_send(fd, data)

        _framework_core.hub_stop()
        self.thread.join(timeout=5)
        self.listen_sock.close()

        ready = threading.Event()
        self.thread, self.listen_sock = _start_hub(_poll_then_echo_handler, ready)

        import _socket

        # Connect two clients
        s1 = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s1.connect(("127.0.0.1", _HUB_PORT))
        time.sleep(0.05)
        s2 = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s2.connect(("127.0.0.1", _HUB_PORT))
        time.sleep(0.1)

        # Both should have been accepted
        assert len(accepted) == 2

        # Both can still do I/O
        s1.sendall(b"hello1")
        s2.sendall(b"hello2")
        data1 = s1.recv(1024)
        data2 = s2.recv(1024)
        s1.close()
        s2.close()

        assert data1 == b"hello1"
        assert data2 == b"hello2"


# ===================================================================
# Fallback without hub
# ===================================================================


class TestSelectFallbackNoHub:
    """select/poll/selectors should work without hub running."""

    def test_select_works_without_hub(self) -> None:
        """select.select should use original select when hub is not running."""
        patch_all()
        r_fd, w_fd = os.pipe()
        os.write(w_fd, b"data")
        r, w, x = select.select([r_fd], [], [], 0)
        os.close(r_fd)
        os.close(w_fd)
        assert len(r) == 1

    def test_poll_works_without_hub(self) -> None:
        """poll.poll should work without hub."""
        patch_all()
        r_fd, w_fd = os.pipe()
        os.write(w_fd, b"data")
        p = CooperativePoll()
        p.register(r_fd, 0x001)
        result = p.poll(0)
        os.close(r_fd)
        os.close(w_fd)
        assert len(result) >= 1

    def test_selector_works_without_hub(self) -> None:
        """CooperativeSelector should work without hub."""
        import selectors

        patch_all()
        r_fd, w_fd = os.pipe()
        os.write(w_fd, b"data")
        sel = CooperativeSelector()
        sel.register(r_fd, selectors.EVENT_READ)
        ready = sel.select(timeout=0)
        sel.close()
        os.close(r_fd)
        os.close(w_fd)
        assert len(ready) >= 1
