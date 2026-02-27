"""Monkey-patch ``ssl`` for cooperative TLS over io_uring sockets.

Strategy: SSL operations that would block return SSLWantReadError or
SSLWantWriteError.  We catch these and cooperatively wait for fd
readiness using ``green_poll_fd`` before retrying.

We do NOT replace ``ssl.SSLContext`` because the stdlib's
``verify_mode`` setter uses ``super(SSLContext, SSLContext)`` which
picks up the replacement via module globals and causes infinite
recursion.  Instead we replace ``ssl.SSLSocket`` and set
``SSLContext.sslsocket_class`` so that ``wrap_socket`` creates our
cooperative sockets.
"""

from __future__ import annotations

import socket as _socket_mod
import ssl as _orig_ssl_mod
import time as _time_mod
from collections.abc import Buffer

import _framework_core

from theframework.monkey._state import hub_is_running as _hub_is_running

_socket_timeout = _socket_mod.timeout

# Poll event constants
_POLLIN: int = 0x001
_POLLOUT: int = 0x004


def _ssl_retry(fd: int, func: object, *args: object, timeout: float | None = None) -> object:
    """Call *func*, retrying on SSLWantRead/WriteError with
    cooperative waiting.

    The fd is expected to already be registered with the hub (via the
    underlying cooperative socket).  We do NOT register/unregister here
    — that is the socket layer's responsibility.

    If *timeout* is a positive number, raises ``socket.timeout`` if the
    total time spent retrying exceeds *timeout* seconds.
    """
    deadline: float | None = None
    if timeout is not None and timeout > 0:
        deadline = _time_mod.monotonic() + timeout

    while True:
        try:
            return func(*args)  # type: ignore[operator]
        except _orig_ssl_mod.SSLWantReadError:
            if deadline is not None and _time_mod.monotonic() >= deadline:
                raise _socket_timeout("timed out")
            _framework_core.green_poll_fd(fd, _POLLIN)
        except _orig_ssl_mod.SSLWantWriteError:
            if deadline is not None and _time_mod.monotonic() >= deadline:
                raise _socket_timeout("timed out")
            _framework_core.green_poll_fd(fd, _POLLOUT)


class SSLSocket(_orig_ssl_mod.SSLSocket):
    """Cooperative SSLSocket that performs TLS I/O cooperatively."""

    def _get_ssl_timeout(self) -> float | None:
        """Return the timeout for SSL retry operations."""
        t = self.gettimeout()
        return t

    def read(self, len: int = 1024, buffer: bytearray | None = None) -> bytes:
        if not _hub_is_running():
            return super().read(len, buffer)
        t = self._get_ssl_timeout()
        if buffer is not None:
            result = _ssl_retry(self.fileno(), super().read, len, buffer, timeout=t)
        else:
            result = _ssl_retry(self.fileno(), super().read, len, timeout=t)
        return result  # type: ignore[return-value]

    def write(self, data: Buffer) -> int:
        if not _hub_is_running():
            return super().write(data)
        result = _ssl_retry(self.fileno(), super().write, data, timeout=self._get_ssl_timeout())
        return result  # type: ignore[return-value]

    def recv(self, bufsize: int = 1024, flags: int = 0) -> bytes:
        if not _hub_is_running():
            return super().recv(bufsize, flags)
        if flags != 0:
            raise ValueError("non-zero flags not allowed in calls to recv() on %s" % self.__class__)
        result = _ssl_retry(self.fileno(), super().recv, bufsize, timeout=self._get_ssl_timeout())
        return result  # type: ignore[return-value]

    def recv_into(
        self,
        buffer: Buffer,
        nbytes: int | None = None,
        flags: int = 0,
    ) -> int:
        if not _hub_is_running():
            if nbytes is not None:
                return super().recv_into(buffer, nbytes, flags)
            return super().recv_into(buffer)
        if flags != 0:
            raise ValueError(
                "non-zero flags not allowed in calls to recv_into() on %s" % self.__class__
            )
        t = self._get_ssl_timeout()
        if nbytes is not None:
            result = _ssl_retry(self.fileno(), super().recv_into, buffer, nbytes, timeout=t)
        else:
            result = _ssl_retry(self.fileno(), super().recv_into, buffer, timeout=t)
        return result  # type: ignore[return-value]

    def send(self, data: Buffer, flags: int = 0) -> int:
        if not _hub_is_running():
            return super().send(data, flags)
        if flags != 0:
            raise ValueError("non-zero flags not allowed in calls to send() on %s" % self.__class__)
        result = _ssl_retry(self.fileno(), super().send, data, timeout=self._get_ssl_timeout())
        return result  # type: ignore[return-value]

    def sendall(self, data: Buffer, flags: int = 0) -> None:
        if not _hub_is_running():
            super().sendall(data, flags)
            return
        if flags != 0:
            raise ValueError(
                "non-zero flags not allowed in calls to sendall() on %s" % self.__class__
            )
        t = self._get_ssl_timeout()
        deadline: float | None = None
        if t is not None and t > 0:
            deadline = _time_mod.monotonic() + t
        view = memoryview(bytes(data))
        total = len(view)
        sent = 0
        while sent < total:
            # Each send gets the remaining time
            remaining: float | None = None
            if deadline is not None:
                remaining = deadline - _time_mod.monotonic()
                if remaining <= 0:
                    raise _socket_timeout("timed out")
            n = _ssl_retry(self.fileno(), super().send, view[sent:], timeout=remaining)
            sent += n  # type: ignore[operator]

    def do_handshake(self) -> None:  # type: ignore[override]
        if not _hub_is_running():
            super().do_handshake()
            return
        _ssl_retry(self.fileno(), super().do_handshake, timeout=self._get_ssl_timeout())

    def unwrap(self) -> _socket_mod.socket:
        if not _hub_is_running():
            return super().unwrap()
        result = _ssl_retry(self.fileno(), super().unwrap, timeout=self._get_ssl_timeout())
        return result  # type: ignore[return-value]
