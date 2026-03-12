"""Reproduce: dead greenlet in ready queue crashes the hub.

The CQE-races-throw scenario:
1. Victim and killer are both blocked (e.g., green_sleep with same duration)
2. Both CQEs arrive in the same io_uring batch
3. CQE handler adds both greenlets to the ready queue
4. Drain loop switches to killer first
5. Killer calls victim.throw() — victim resumes, catches exception, dies
6. Drain loop continues to the next entry: victim's greenlet ref (from step 3)
7. Switch to dead victim — what happens?

Key finding: greenlet.switch() on a dead greenlet returns () (empty tuple),
it does NOT raise GreenletError. So if the C-level PyGreenlet_Switch behaves
the same way, the hub drain loop handles it fine.

These tests verify that:
- Dead greenlets in the ready queue don't crash the hub
- The CQE-races-throw scenario is safe
"""

from __future__ import annotations

import socket
import threading

import greenlet

import theframework  # noqa: F401
import _framework_core


def test_dead_greenlet_in_ready_queue() -> None:
    """Scheduling a dead greenlet must not crash the hub.

    Simulates the CQE handler having placed a greenlet ref in the ready
    queue after that greenlet was killed.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    listen_fd = sock.fileno()

    result: dict[str, object] = {"checker_ran": False}

    def acceptor() -> None:
        hub_g = _framework_core.get_hub_greenlet()

        # Create a greenlet, run it to completion so it's dead.
        # Parent must be the ACCEPTOR (default), NOT hub_g, so that
        # g.switch() returns to us when g finishes.
        def worker() -> None:
            pass

        g = greenlet.greenlet(worker)  # parent = acceptor (current greenlet)
        g.switch()  # run to completion — g.dead is now True
        assert g.dead, "greenlet should be dead after running to completion"

        # Schedule the dead greenlet — this simulates the CQE handler having
        # added a greenlet ref that was subsequently killed by a throw
        _framework_core.hub_schedule(g)

        # Schedule a checker that runs after the drain loop processes the
        # dead greenlet
        def checker() -> None:
            result["checker_ran"] = True
            _framework_core.hub_stop()

        gc = greenlet.greenlet(checker, parent=hub_g)
        _framework_core.hub_schedule(gc)

    ready = threading.Event()

    def run() -> None:
        ready.set()
        _framework_core.hub_run(listen_fd, acceptor)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(timeout=5)
    thread.join(timeout=5)
    sock.close()

    assert result["checker_ran"], (
        "Hub exited prematurely — switching to a dead greenlet in the "
        "ready queue caused the hub to crash instead of continuing"
    )


def test_kill_then_cqe_resumes_dead_greenlet() -> None:
    """Simulate the full CQE-races-throw sequence.

    The CQE handler adds the victim's greenlet to the ready queue BEFORE
    the killer throws into it. We simulate this by having the killer:
    1. hub_schedule(victim) — mimics CQE handler adding the ref
    2. victim.throw() — kills the victim

    The drain loop appends items dynamically (hub_schedule during drain),
    so the dead victim ref is at the tail of the ready queue when the
    drain loop reaches it.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    listen_fd = sock.fileno()

    victim_ref: list[greenlet.greenlet | None] = [None]
    result: dict[str, object] = {"checker_ran": False, "active_waits": None}

    def acceptor() -> None:
        hub_g = _framework_core.get_hub_greenlet()

        # -- victim: sleep for a long time --
        def victim_wrapper() -> None:
            victim_ref[0] = greenlet.getcurrent()
            try:
                _framework_core.green_sleep(60.0)
            except Exception:
                pass

        # -- killer: short sleep, then simulate the race --
        def killer() -> None:
            _framework_core.green_sleep(0.05)
            g = victim_ref[0]
            if g is not None and not g.dead:
                # Simulate the CQE handler having already placed victim
                # in the ready queue (this is the race condition)
                _framework_core.hub_schedule(g)
                # Now throw into victim — victim catches, finishes, dies.
                # Control returns to hub_g (victim's parent).
                # The drain loop then hits the scheduled (now dead) ref.
                g.throw(Exception("killed"))

        # -- checker: verify hub survived --
        def checker() -> None:
            _framework_core.green_sleep(0.2)
            result["checker_ran"] = True
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
    thread.join(timeout=5)
    sock.close()

    assert result["checker_ran"], (
        "Hub exited prematurely — the CQE-races-throw scenario caused "
        "a dead greenlet switch that crashed the hub drain loop"
    )


def test_multiple_dead_greenlets_in_ready_queue() -> None:
    """Multiple dead greenlets in the ready queue must all be skipped."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    listen_fd = sock.fileno()

    result: dict[str, object] = {"checker_ran": False}

    def acceptor() -> None:
        hub_g = _framework_core.get_hub_greenlet()

        # Create several dead greenlets (parent=acceptor so switch returns)
        for _ in range(5):
            def worker() -> None:
                pass
            g = greenlet.greenlet(worker)  # parent = acceptor
            g.switch()
            assert g.dead
            _framework_core.hub_schedule(g)

        # Schedule checker after all the dead ones
        def checker() -> None:
            result["checker_ran"] = True
            _framework_core.hub_stop()

        gc = greenlet.greenlet(checker, parent=hub_g)
        _framework_core.hub_schedule(gc)

    ready = threading.Event()

    def run() -> None:
        ready.set()
        _framework_core.hub_run(listen_fd, acceptor)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(timeout=5)
    thread.join(timeout=5)
    sock.close()

    assert result["checker_ran"], (
        "Hub exited prematurely — multiple dead greenlets in ready queue "
        "caused the hub to crash"
    )


def test_dead_greenlet_with_hub_parent_in_ready_queue() -> None:
    """Dead greenlet whose parent is hub_g — tests C-level PyGreenlet_Switch.

    When the drain loop switches to a dead greenlet whose parent is hub_g,
    the greenlet runtime may behave differently (switching to the parent
    instead of returning). This test verifies that the hub handles this
    case correctly.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    listen_fd = sock.fileno()

    result: dict[str, object] = {"checker_ran": False}

    def acceptor() -> None:
        hub_g = _framework_core.get_hub_greenlet()

        # Create a greenlet with parent=hub_g. We can't use g.switch()
        # because it wouldn't return to us. Instead, schedule it — when
        # the hub drains it, it runs, finishes, and its parent (hub_g)
        # gets control back. Then schedule it AGAIN (now dead) plus a
        # checker. The second drain should handle the dead greenlet.
        def worker() -> None:
            # After running, schedule the now-dead self + checker
            g = greenlet.getcurrent()

            def post_death() -> None:
                # This runs after worker dies, during the next hub iteration
                _framework_core.hub_schedule(g)  # schedule the dead worker

                def checker() -> None:
                    result["checker_ran"] = True
                    _framework_core.hub_stop()

                gc = greenlet.greenlet(checker, parent=_framework_core.get_hub_greenlet())
                _framework_core.hub_schedule(gc)

            gp = greenlet.greenlet(post_death, parent=_framework_core.get_hub_greenlet())
            _framework_core.hub_schedule(gp)

        gw = greenlet.greenlet(worker, parent=hub_g)
        _framework_core.hub_schedule(gw)

    ready = threading.Event()

    def run() -> None:
        ready.set()
        _framework_core.hub_run(listen_fd, acceptor)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(timeout=5)
    thread.join(timeout=5)
    sock.close()

    assert result["checker_ran"], (
        "Hub exited prematurely — dead greenlet with hub_g parent in "
        "ready queue caused the hub to crash"
    )
