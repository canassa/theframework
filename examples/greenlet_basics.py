"""Greenlet hub pattern demonstration.

A hub greenlet round-robins between worker greenlets. Each worker
cooperatively yields back to the hub after doing a unit of work.
This is the exact concurrency model the framework uses — the hub
will eventually be an io_uring event loop, and workers will be
HTTP connection handlers.
"""

from __future__ import annotations

import greenlet


def run_hub(workers: list[greenlet.greenlet]) -> None:
    """Round-robin between workers until all have finished."""
    active = list(workers)
    while active:
        next_active: list[greenlet.greenlet] = []
        for worker in active:
            if not worker.dead:
                worker.switch()
                if not worker.dead:
                    next_active.append(worker)
        active = next_active


def worker_fn(name: str, iterations: int, log: list[str]) -> None:
    """A worker that logs its progress and yields to the hub each iteration."""
    hub = greenlet.getcurrent().parent
    assert hub is not None
    for i in range(iterations):
        log.append(f"{name}-{i}")
        hub.switch()


def main() -> None:
    log: list[str] = []

    hub = greenlet.getcurrent()
    w1 = greenlet.greenlet(lambda: worker_fn("w1", 3, log), parent=hub)
    w2 = greenlet.greenlet(lambda: worker_fn("w2", 3, log), parent=hub)

    run_hub([w1, w2])

    print("Interleaved output:", log)
    # Expected: ['w1-0', 'w2-0', 'w1-1', 'w2-1', 'w1-2', 'w2-2']
    assert log == ["w1-0", "w2-0", "w1-1", "w2-1", "w1-2", "w2-2"]
    print("Hub pattern works correctly.")


if __name__ == "__main__":
    main()
