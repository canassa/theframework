"""Cooperative DNS resolution via a background thread pool.

Strategy: DNS lookups are inherently blocking (the C library's
``getaddrinfo`` etc. cannot be made non-blocking).  We run the
blocking call in a small thread pool and use an ``os.pipe`` to
notify the calling greenlet when the result is ready.  The greenlet
waits on the pipe's read end via ``green_poll_fd`` — zero busy-waiting.

The thread pool is lazily created on first use.
"""

from __future__ import annotations

import os as _os
from concurrent.futures import Future, ThreadPoolExecutor

import _socket

import _framework_core

from theframework.monkey._state import hub_is_running as _hub_is_running

# Poll event constant
_POLLIN: int = 0x001

# ---------------------------------------------------------------------------
# Thread pool (lazy singleton)
# ---------------------------------------------------------------------------

_dns_pool: ThreadPoolExecutor | None = None


def _get_pool() -> ThreadPoolExecutor:
    global _dns_pool
    if _dns_pool is None:
        _dns_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dns-resolver")
    return _dns_pool


# ---------------------------------------------------------------------------
# Core: run a blocking function in the DNS thread, wake via pipe
# ---------------------------------------------------------------------------


def _run_in_thread(fn: object, *args: object) -> object:
    """Run *fn(*args)* in the DNS thread pool.

    If the hub is not running, call *fn* directly (blocking fallback).
    Otherwise, submit the work to a thread, wait on a pipe for
    completion, and return the result (or re-raise the exception).
    """
    if not _hub_is_running():
        return fn(*args)  # type: ignore[operator]

    pool = _get_pool()

    # Create a pipe for notification.  The worker thread writes one
    # byte when it finishes; the greenlet waits on the read end via
    # green_poll_fd (IORING_OP_POLL_ADD — works on any fd).
    r_fd, w_fd = _os.pipe()

    # Set the read end non-blocking so io_uring POLL_ADD works correctly.
    _os.set_blocking(r_fd, False)

    result_box: list[object] = [None]
    error_box: list[BaseException | None] = [None]

    def _worker() -> None:
        try:
            result_box[0] = fn(*args)  # type: ignore[operator]
        except BaseException as exc:
            error_box[0] = exc
        finally:
            try:
                _os.write(w_fd, b"\x00")
            except OSError:
                pass

    future: Future[None] = pool.submit(_worker)  # noqa: F841

    try:
        # Wait cooperatively for the pipe notification.
        _framework_core.green_poll_fd(r_fd, _POLLIN)

        # Drain the notification byte.
        try:
            _os.read(r_fd, 1)
        except OSError:
            pass
    finally:
        _os.close(r_fd)
        _os.close(w_fd)

    if error_box[0] is not None:
        raise error_box[0]
    return result_box[0]


# ---------------------------------------------------------------------------
# Cooperative DNS functions (same signatures as _socket.*)
# ---------------------------------------------------------------------------


def getaddrinfo(
    host: str | bytes | None,
    port: str | bytes | int | None,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> list[tuple[int, int, int, str, tuple[str, int] | tuple[str, int, int, int]]]:
    """Cooperative version of :func:`socket.getaddrinfo`."""
    result = _run_in_thread(_socket.getaddrinfo, host, port, family, type, proto, flags)
    return result  # type: ignore[return-value]


def gethostbyname(hostname: str) -> str:
    """Cooperative version of :func:`socket.gethostbyname`."""
    result = _run_in_thread(_socket.gethostbyname, hostname)
    return result  # type: ignore[return-value]


def gethostbyname_ex(hostname: str) -> tuple[str, list[str], list[str]]:
    """Cooperative version of :func:`socket.gethostbyname_ex`."""
    result = _run_in_thread(_socket.gethostbyname_ex, hostname)
    return result  # type: ignore[return-value]


def gethostbyaddr(ip_address: str) -> tuple[str, list[str], list[str]]:
    """Cooperative version of :func:`socket.gethostbyaddr`."""
    result = _run_in_thread(_socket.gethostbyaddr, ip_address)
    return result  # type: ignore[return-value]


def getnameinfo(
    sockaddr: tuple[str, int] | tuple[str, int, int, int], flags: int
) -> tuple[str, str]:
    """Cooperative version of :func:`socket.getnameinfo`."""
    result = _run_in_thread(_socket.getnameinfo, sockaddr, flags)
    return result  # type: ignore[return-value]
