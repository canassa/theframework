"""Tests for the greenlet hub pattern — cooperative scheduling fundamentals."""

from __future__ import annotations

import greenlet


def _run_hub(workers: list[greenlet.greenlet]) -> None:
    """Round-robin scheduler: switch to each worker until all are dead."""
    active = list(workers)
    while active:
        next_active: list[greenlet.greenlet] = []
        for w in active:
            if not w.dead:
                w.switch()
                if not w.dead:
                    next_active.append(w)
        active = next_active


def test_hub_interleaves_workers() -> None:
    """Spawn N workers that yield after each append. Assert round-robin order."""
    log: list[str] = []
    hub = greenlet.getcurrent()

    def worker_fn(name: str, count: int) -> None:
        parent = greenlet.getcurrent().parent
        assert parent is not None
        for i in range(count):
            log.append(f"{name}-{i}")
            parent.switch()

    workers = [greenlet.greenlet(lambda n=n: worker_fn(f"w{n}", 3), parent=hub) for n in range(3)]
    _run_hub(workers)

    assert log == [
        "w0-0",
        "w1-0",
        "w2-0",
        "w0-1",
        "w1-1",
        "w2-1",
        "w0-2",
        "w1-2",
        "w2-2",
    ]


def test_greenlet_exception_propagates_to_hub() -> None:
    """A greenlet that raises should propagate the exception to the hub."""
    hub = greenlet.getcurrent()

    def failing_worker() -> None:
        raise ValueError("boom")

    worker = greenlet.greenlet(failing_worker, parent=hub)

    caught: BaseException | None = None
    try:
        worker.switch()
    except ValueError as exc:
        caught = exc

    assert caught is not None
    assert str(caught) == "boom"
    assert worker.dead


def test_greenlet_finishes_and_becomes_dead() -> None:
    """A greenlet that returns normally should be marked dead."""
    hub = greenlet.getcurrent()
    result: list[int] = []

    def simple_worker() -> None:
        result.append(42)

    worker = greenlet.greenlet(simple_worker, parent=hub)
    assert not worker.dead
    worker.switch()
    assert worker.dead
    assert result == [42]
