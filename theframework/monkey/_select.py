"""Monkey-patch ``select`` for cooperative I/O."""

from __future__ import annotations

import select as _orig_select_mod
import time as _time_mod

import _framework_core

from theframework.monkey._state import get_fd as _get_fd, hub_is_running as _hub_is_running

# Save originals before patching
_original_select = _orig_select_mod.select
_original_poll_cls = _orig_select_mod.poll

_POLLIN = 0x001
_POLLOUT = 0x004


def select(
    rlist: list[object],
    wlist: list[object],
    xlist: list[object],
    timeout: float | None = None,
) -> tuple[list[object], list[object], list[object]]:
    """Cooperative ``select.select`` — yields to the hub while waiting."""
    # Fast path: timeout == 0 means non-blocking poll
    if timeout is not None and timeout == 0:
        return _original_select(rlist, wlist, xlist, 0)

    # When no hub is running, fall back to the real select
    if not _hub_is_running():
        return _original_select(rlist, wlist, xlist, timeout)

    # Empty lists: just sleep for timeout
    if not rlist and not wlist and not xlist:
        if timeout is not None and timeout > 0:
            _framework_core.green_sleep(timeout)
        return [], [], []

    # Single-fd optimisation: use green_poll_fd / green_poll_fd_timeout
    if len(rlist) + len(wlist) <= 1 and not xlist:
        return _single_fd_select(rlist, wlist, timeout)

    # Multi-fd: use green_poll_multi (event-driven, no busy-wait)
    return _multi_fd_select(rlist, wlist, xlist, timeout)


def _single_fd_select(
    rlist: list[object],
    wlist: list[object],
    timeout: float | None,
) -> tuple[list[object], list[object], list[object]]:
    """Optimised path for single-fd select using green_poll_fd_timeout."""
    fd_obj: object = None
    events = 0
    is_read = False

    if rlist:
        fd_obj = rlist[0]
        events = _POLLIN
        is_read = True
    elif wlist:
        fd_obj = wlist[0]
        events = _POLLOUT
        is_read = False

    if fd_obj is None:
        if timeout is not None:
            _framework_core.green_sleep(timeout)
        return [], [], []

    fd = _get_fd(fd_obj)

    if timeout is not None:
        # Use green_poll_fd_timeout for timeout enforcement
        timeout_ms = max(1, int(timeout * 1000))
        revents = _framework_core.green_poll_fd_timeout(fd, events, timeout_ms)
    else:
        # No timeout — block indefinitely
        revents = _framework_core.green_poll_fd(fd, events)

    if revents > 0:
        if is_read:
            return [fd_obj], [], []
        return [], [fd_obj], []
    return [], [], []


def _multi_fd_select(
    rlist: list[object],
    wlist: list[object],
    xlist: list[object],
    timeout: float | None,
) -> tuple[list[object], list[object], list[object]]:
    """Multi-fd select using green_poll_multi (event-driven, no busy-wait)."""
    # Build deduplicated (fd -> events) mapping
    fd_events: dict[int, int] = {}
    for obj in rlist:
        fd = _get_fd(obj)
        fd_events[fd] = fd_events.get(fd, 0) | _POLLIN
    for obj in wlist:
        fd = _get_fd(obj)
        fd_events[fd] = fd_events.get(fd, 0) | _POLLOUT
    # xlist: use POLLPRI for exceptional conditions
    for obj in xlist:
        fd = _get_fd(obj)
        fd_events[fd] = fd_events.get(fd, 0) | 0x002  # POLLPRI

    fds_list = list(fd_events.keys())
    events_list = list(fd_events.values())

    timeout_ms = -1
    if timeout is not None:
        timeout_ms = max(1, int(timeout * 1000))

    ready = _framework_core.green_poll_multi(fds_list, events_list, timeout_ms)

    # Map ready fds back to the original objects
    fd_to_robj: dict[int, object] = {_get_fd(obj): obj for obj in rlist}
    fd_to_wobj: dict[int, object] = {_get_fd(obj): obj for obj in wlist}
    fd_to_xobj: dict[int, object] = {_get_fd(obj): obj for obj in xlist}

    r_result: list[object] = []
    w_result: list[object] = []
    x_result: list[object] = []

    for fd, revents in ready:
        if revents & _POLLIN and fd in fd_to_robj:
            r_result.append(fd_to_robj[fd])
        if revents & _POLLOUT and fd in fd_to_wobj:
            w_result.append(fd_to_wobj[fd])
        if revents & 0x002 and fd in fd_to_xobj:  # POLLPRI
            x_result.append(fd_to_xobj[fd])
        # POLLERR/POLLHUP also signal readability/writability
        if revents & (0x008 | 0x010):  # POLLERR | POLLHUP
            if fd in fd_to_robj and fd_to_robj[fd] not in r_result:
                r_result.append(fd_to_robj[fd])

    return r_result, w_result, x_result


# ---------------------------------------------------------------------------
# poll() replacement
# ---------------------------------------------------------------------------


class poll:
    """Cooperative ``select.poll`` replacement."""

    def __init__(self) -> None:
        self._fds: dict[int, int] = {}  # fd -> eventmask

    def register(self, fd: object, eventmask: int = _POLLIN | 0x002 | _POLLOUT) -> None:
        self._fds[_get_fd(fd)] = eventmask

    def modify(self, fd: object, eventmask: int) -> None:
        fileno = _get_fd(fd)
        if fileno not in self._fds:
            raise KeyError(fileno)
        self._fds[fileno] = eventmask

    def unregister(self, fd: object) -> None:
        del self._fds[_get_fd(fd)]

    def poll(self, timeout: float | None = None) -> list[tuple[int, int]]:
        """Wait for events. *timeout* is in milliseconds (``poll()``
        convention), or ``None`` for indefinite."""
        # Fast path: non-blocking check
        if timeout is not None and timeout == 0:
            real_poll = _original_poll_cls()
            for fd, mask in self._fds.items():
                real_poll.register(fd, mask)
            return real_poll.poll(0)

        if not _hub_is_running():
            real_poll = _original_poll_cls()
            for fd, mask in self._fds.items():
                real_poll.register(fd, mask)
            return real_poll.poll(timeout)

        if not self._fds:
            if timeout is not None and timeout > 0:
                _framework_core.green_sleep(timeout / 1000.0)
            return []

        fds_list = list(self._fds.keys())
        events_list = list(self._fds.values())

        timeout_ms = -1
        if timeout is not None:
            timeout_ms = max(1, int(timeout))

        ready = _framework_core.green_poll_multi(fds_list, events_list, timeout_ms)
        return ready
