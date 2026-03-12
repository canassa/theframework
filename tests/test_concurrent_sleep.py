"""Regression test: concurrent green_sleep must respect individual durations.

Bug: greenSleep stores `kernel_timespec` as a stack-local variable and passes
its address to io_uring via prepTimeout.  When the greenlet switches out, its
C stack is saved to the heap and the stack memory is reused by the next
greenlet.  By the time submitAndWait submits the SQEs to the kernel, all
timespec pointers read from the same (reused) stack location — so every
concurrent sleep gets the duration of whichever greenlet ran last.

Symptom: a 50 ms sleep and a 200 ms sleep both wake at ~200 ms.
"""

from __future__ import annotations

import socket
import threading
import time

import greenlet

import theframework  # noqa: F401
import _framework_core


def test_concurrent_sleeps_respect_individual_durations() -> None:
    """A 50 ms sleep must wake before a 200 ms sleep, not at the same time."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    listen_fd = sock.fileno()

    wake_times: dict[str, float] = {}

    def acceptor() -> None:
        hub_g = _framework_core.get_hub_greenlet()
        t0 = time.monotonic()

        def short_sleeper() -> None:
            _framework_core.green_sleep(0.05)
            wake_times["short"] = time.monotonic() - t0

        def long_sleeper() -> None:
            _framework_core.green_sleep(0.2)
            wake_times["long"] = time.monotonic() - t0
            _framework_core.hub_stop()

        for fn in (short_sleeper, long_sleeper):
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

    assert "short" in wake_times, "short sleeper never woke"
    assert "long" in wake_times, "long sleeper never woke"

    # The 50 ms sleep must wake well before the 200 ms sleep.
    # Allow generous tolerance (100 ms gap minimum).
    gap = wake_times["long"] - wake_times["short"]
    assert gap > 0.1, (
        f"short woke at {wake_times['short']:.4f}s, "
        f"long woke at {wake_times['long']:.4f}s — "
        f"gap {gap:.4f}s < 0.1s (sleeps are coalescing)"
    )
