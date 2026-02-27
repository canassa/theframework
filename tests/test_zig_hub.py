import sys
import pathlib

_ext_dir = str(pathlib.Path(__file__).resolve().parent.parent / "zig-out" / "lib")
if _ext_dir not in sys.path:
    sys.path.insert(0, _ext_dir)

from collections.abc import Callable

import greenlet
import _framework_core


def test_hub_round_robin_order() -> None:
    """Three workers each yield 3 times. Assert round-robin interleaving."""
    hub = greenlet.getcurrent()
    log: list[str] = []

    def make_worker(wid: int) -> Callable[[], None]:
        def worker() -> None:
            for i in range(3):
                log.append(f"w{wid}-{i}")
                hub.switch(f"w{wid}-{i}")

        return worker

    workers = [greenlet.greenlet(make_worker(i), parent=hub) for i in range(3)]
    _framework_core.run_hub(workers)

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


def test_hub_exception_propagates() -> None:
    """A worker that raises propagates the exception to the hub caller."""
    hub = greenlet.getcurrent()

    def bad_worker() -> None:
        raise RuntimeError("worker exploded")

    workers = [greenlet.greenlet(bad_worker, parent=hub)]
    try:
        _framework_core.run_hub(workers)
        assert False, "Should have raised"
    except RuntimeError as e:
        assert str(e) == "worker exploded"


def test_hub_surviving_workers_after_exception() -> None:
    """If one worker raises, the hub propagates it. Other workers stop."""
    hub = greenlet.getcurrent()
    log: list[str] = []

    def good_worker() -> None:
        log.append("good-0")
        hub.switch()
        log.append("good-1")
        hub.switch()

    def bad_worker() -> None:
        log.append("bad-0")
        hub.switch()
        raise ValueError("oops")

    workers = [
        greenlet.greenlet(good_worker, parent=hub),
        greenlet.greenlet(bad_worker, parent=hub),
    ]
    try:
        _framework_core.run_hub(workers)
        assert False, "Should have raised"
    except ValueError:
        pass

    # Both workers ran their first iteration, then bad_worker raised on second
    assert "good-0" in log
    assert "bad-0" in log
