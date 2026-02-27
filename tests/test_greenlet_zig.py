import sys
import pathlib

_ext_dir = str(pathlib.Path(__file__).resolve().parent.parent / "zig-out" / "lib")
if _ext_dir not in sys.path:
    sys.path.insert(0, _ext_dir)

import greenlet
import _framework_core


def test_greenlet_switch_to_returns_value() -> None:
    """Zig creates a greenlet, switches to it, gets the return value."""
    result = _framework_core.greenlet_switch_to(lambda: "from greenlet")
    assert result == "from greenlet"


def test_greenlet_switch_to_with_exception() -> None:
    """Exception in the greenlet propagates back through the switch."""

    def raise_error() -> None:
        raise ValueError("boom")

    try:
        _framework_core.greenlet_switch_to(raise_error)
        assert False, "Should have raised"
    except ValueError as e:
        assert str(e) == "boom"


def test_greenlet_ping_pong() -> None:
    """Zig switches to a greenlet multiple times, collecting results."""
    hub = greenlet.getcurrent()

    def worker(*_args: object) -> object:
        # First time we receive the counter via switch args
        val = 0
        while True:
            # Send transformed value back to parent, receive next counter
            result = hub.switch(val * 10)
            val = result
        return None  # unreachable but satisfies type checker

    results = _framework_core.greenlet_ping_pong(worker, 5)
    assert results == [0, 10, 20, 30, 40]


def test_many_rapid_switches() -> None:
    """1000+ rapid greenlet switches to check for refcount leaks or crashes."""
    for _ in range(1000):
        result = _framework_core.greenlet_switch_to(lambda: 1)
        assert result == 1
