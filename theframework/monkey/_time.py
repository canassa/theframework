"""Monkey-patch ``time.sleep`` to yield cooperatively."""

from __future__ import annotations

import time as _time_mod

import _framework_core

from theframework.monkey._state import hub_is_running

# Save original before it gets patched (this module is imported during
# patch_time() which happens before the attribute replacement).
_original_sleep = _time_mod.sleep


def sleep(seconds: float) -> None:
    """Cooperative sleep — yields to the io_uring hub."""
    if seconds < 0:
        raise ValueError("sleep length must be non-negative")
    if not hub_is_running():
        _original_sleep(seconds)
        return
    if seconds == 0:
        _framework_core.green_sleep(0.0)
        return
    _framework_core.green_sleep(seconds)
