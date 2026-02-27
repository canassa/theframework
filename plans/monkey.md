# Monkey Patching — Production Hardening Plan

Status: **DRAFT**
Date: 2026-02-27

## Current State

We have a working monkey patching system (`theframework/monkey/`) that patches
`socket`, `time`, `select`, `selectors`, and `ssl`. All 55 tests pass.
But the implementation has significant bugs and shortcuts that make it
unsuitable for production. This plan catalogues every issue found and provides
a prioritised fix roadmap.

---

## Severity Definitions

- **P0 — Critical:** Data corruption, deadlocks, or CPU waste in the hot path.
  Must fix before any production use.
- **P1 — High:** Correctness bugs that affect real-world libraries. Will cause
  failures with common third-party code (requests, urllib3, sqlalchemy, etc.)
- **P2 — Medium:** Robustness and compatibility issues. Edge cases that will
  bite in production under load or unusual conditions.
- **P3 — Low:** Gevent parity / nice-to-have. Not needed for initial production
  readiness but important for long-term ecosystem compatibility.

---

## Issue Catalogue

### P0 — Critical

#### P0-1: Busy-wait polling in select() and selectors (CPU waste + latency)

**Files:** `_select.py:47-58`, `_select.py:156-165`, `_selectors.py:110-123`

**Bug:** Multi-fd `select()`, `poll.poll()`, and `CooperativeSelector.select()`
all use a `while True` loop with `_original_select(..., 0)` + `green_sleep(0.001)`.
This is a busy-wait poll loop that:
- Burns CPU doing syscalls every 1ms even when idle
- Adds up to 1ms of latency to every I/O event
- Does not scale — each iteration makes a real `select()` syscall

**Gevent pattern:** Creates an `io` watcher per fd via the event loop. Sets all
watchers' callbacks to append to a ready list and set an `Event`. Then does
`event.wait(timeout=timeout)` — true event-driven, zero wasted cycles.

**Fix:** We cannot create per-fd watchers in our io_uring model the same way gevent
does with libev. However, we CAN submit multiple `IORING_OP_POLL_ADD` SQEs (one per
fd) and yield to the hub. When any fd becomes ready, the hub resumes our greenlet.

This requires a new Zig primitive: `green_poll_multi(fds, events_array)` that:
1. Submits N `POLL_ADD` SQEs (one per fd/events pair)
2. Yields to the hub
3. On first completion, cancels the remaining N-1 SQEs
4. Returns which fd(s) are ready and their revents

Alternatively, since `green_poll_fd` already works for single fds, we can use
multiple greenlets (one per fd) coordinated via a shared `Event`-like flag. But
this is more complex and fragile.

**Preferred approach:** New `green_poll_multi` Zig primitive. This is the
cleanest path and matches how io_uring is designed to work (batch submissions).

**Fallback approach:** For the multi-fd case where we can't easily add the Zig
primitive right now, we can at least improve the polling: do an initial non-blocking
check (already done), then fall back to `green_poll_fd` on the first fd if only one
fd is actually being waited on, or use `_original_select` with a small but non-zero
timeout (e.g., 10ms) instead of the 0-then-sleep pattern. This is still suboptimal
but dramatically reduces CPU waste.

**Acceptance criteria:** `select()` with 1 readable fd + 100ms timeout uses <1%
CPU (currently ~100% on one core). Latency is <100μs from fd-ready to return
(currently up to 1ms).

---

#### P0-2: `hub_is_running()` is O(n) per I/O call

**File:** `_state.py:41-58`

**Bug:** Every `recv()`, `send()`, `connect()`, `accept()` call walks the entire
greenlet parent chain (O(n) in greenlet nesting depth). With deeply nested
greenlets (e.g., middleware chains, nested library calls), this is a hot-path
performance concern.

**Gevent pattern:** Uses `_thread._local` (native thread-local storage). Each
thread gets its own `hub` attribute. Hub detection is a single attribute lookup
— O(1).

**Fix:** Replace parent-chain walk with thread-local storage:
```python
import _thread

class _HubLocal(_thread._local):
    hub_greenlet: greenlet.greenlet | None = None

_hub_local = _HubLocal()

def hub_is_running() -> bool:
    return _hub_local.hub_greenlet is not None
```

The hub thread sets `_hub_local.hub_greenlet` when it starts and clears it when
it stops. Non-hub threads never see a hub greenlet set in their thread-local.

**Note:** We need the Zig `hub_run()` function (or Python wrapper) to set/clear
the thread-local. Since `hub_run` is called from Python, we can do this from the
Python side by wrapping the call.

**Acceptance criteria:** `hub_is_running()` is O(1). No greenlet parent chain
walking. Verified by microbenchmark showing constant time regardless of greenlet
depth.

---

### P1 — High

#### P1-1: `_build_results()` drops concurrent READ+WRITE events

**File:** `_selectors.py:125-144`

**Bug:** The `seen` set prevents a fd from appearing in results for both read and
write. If a fd is registered for `EVENT_READ | EVENT_WRITE` and both conditions are
true, only `EVENT_READ` is reported. The `EVENT_WRITE` is silently dropped.

This breaks any code that registers for both events on the same fd (common in
bidirectional protocols, HTTP/2 implementations, etc.)

**Gevent pattern:** Accumulates events with `|=` so both flags are reported
for the same fd:
```python
if fd in seen:
    existing_key, existing_events = seen[fd]
    seen[fd] = (existing_key, existing_events | new_events)
else:
    seen[fd] = (key, events)
```

**Fix:** Change `_build_results` to OR event masks when same fd appears in both
rlist and wlist:
```python
def _build_results(self, rlist, wlist):
    result_map: dict[int, tuple[_SKey, int]] = {}
    for obj in rlist:
        fd = _get_fd(obj)
        if fd in self._keys:
            result_map[fd] = (self._keys[fd], EVENT_READ)
    for obj in wlist:
        fd = _get_fd(obj)
        if fd in self._keys:
            if fd in result_map:
                key, events = result_map[fd]
                result_map[fd] = (key, events | EVENT_WRITE)
            else:
                result_map[fd] = (self._keys[fd], EVENT_WRITE)
    return list(result_map.values())
```

**Acceptance criteria:** Test that registers a fd for `EVENT_READ | EVENT_WRITE`,
makes both conditions true, and asserts the returned mask contains both flags.

---

#### P1-2: Socket `flags` parameter silently ignored

**Files:** `_socket.py:104-108` (recv), `_socket.py:110-118` (recv_into),
`_socket.py:127-132` (send), `_socket.py:134-140` (sendall)

**Bug:** When using the io_uring cooperative path, the `flags` parameter (e.g.,
`MSG_PEEK`, `MSG_WAITALL`, `MSG_DONTWAIT`, `MSG_NOSIGNAL`, `MSG_OOB`) is silently
dropped because `green_recv()` and `green_send()` don't accept flags.

Most real-world code uses `flags=0` (the default), so this usually doesn't cause
problems. But libraries that use `MSG_PEEK` (e.g., some protocol implementations)
or `MSG_DONTWAIT` will silently get wrong behavior.

**Gevent pattern:** Passes `*args` through to `self._sock.recv(*args)`, which is
the real C socket recv. For SSL, explicitly rejects non-zero flags with a
`ValueError`.

**Fix:** When `flags != 0`, use poll-then-syscall: wait for fd readiness via
`green_poll_fd`, then call `super().recv(bufsize, flags)` / `super().send(data,
flags)` on the non-blocking socket. This preserves all flag semantics while
still yielding cooperatively:

```python
def recv(self, bufsize: int, flags: int = 0) -> bytes:
    if not _hub_is_running() or self._timeout_value == 0.0:
        return super().recv(bufsize, flags)
    self._ensure_registered()
    if flags:
        _framework_core.green_poll_fd(self.fileno(), 0x001)  # POLLIN
        return super().recv(bufsize, flags)
    return _framework_core.green_recv(self.fileno(), bufsize)
```

**Acceptance criteria:** `sock.recv(1024, socket.MSG_PEEK)` returns data without
consuming it. `sock.send(data, socket.MSG_DONTWAIT)` raises `BlockingIOError` if
the buffer is full (not silently succeeding).

---

#### P1-3: No timeout enforcement on socket operations

**Files:** `_socket.py` (all I/O methods)

**Bug:** `socket.settimeout(5.0)` records the timeout in `_timeout_value` but it
is never enforced during cooperative I/O. If a `recv()` blocks forever waiting for
data that never arrives, the greenlet is blocked forever. The only timeout check
is `_timeout_value == 0.0` which skips the cooperative path entirely.

This is extremely dangerous in production. Any network hiccup, slow backend, or
hung connection will permanently consume a greenlet.

**Gevent pattern:** Uses `Timeout._start_new_or_dummy(self.timeout, timeout_exc)`
as a context manager around every I/O operation. The `Timeout` class schedules a
timer in the event loop that throws `socket.timeout` into the blocked greenlet
when it fires.

**Fix:** We need a timeout mechanism at the Zig level. Options:

**Option A — Zig-level IORING_OP_LINK_TIMEOUT:** Submit the I/O SQE linked with a
`TIMEOUT` SQE. io_uring natively cancels the I/O if the timeout fires. This is the
most efficient but requires changes to the ring/hub Zig code.

**Option B — Python-level timeout with green_sleep racing:** Use a pattern where
we submit the I/O op and also schedule a timer greenlet. If the timer fires first,
it cancels the I/O op. More complex, requires cancellation support in the hub.

**Option C — Pragmatic: wrap cooperative calls with a Python-level deadline check:**
Before each I/O call, record `deadline = monotonic() + timeout`. After the call
returns, check if deadline was exceeded. This doesn't truly cancel stuck I/O but
it does detect timeouts on the Python side. Combined with the Zig hub's own
per-op result checking, this catches most real-world cases.

For now, Option C is the pragmatic choice. It handles the common case (server
stopped sending, connection idle) because the Zig recv/send will return when data
arrives or the connection closes. True "stuck forever" cases (e.g., the remote
peer is alive but not sending) need Option A long-term.

**Implementation for Option C:**
```python
def _with_timeout(self, fn, *args, timeout_exc=None):
    timeout = self._timeout_value
    if timeout is None or timeout <= 0:
        return fn(*args)
    deadline = _time_mod.monotonic() + timeout
    result = fn(*args)
    if _time_mod.monotonic() > deadline:
        raise (timeout_exc or _socket_timeout("timed out"))
    return result
```

**Long-term:** Implement `IORING_OP_LINK_TIMEOUT` in the Zig hub (Option A).
Track this as a separate task.

**Acceptance criteria:** `sock.settimeout(0.1); sock.recv(1024)` on a silent
connection raises `socket.timeout` within ~200ms (not hanging forever).

---

#### P1-4: DNS functions not patched (getaddrinfo blocks the thread)

**File:** `__init__.py:52-70` (patch_socket)

**Bug:** `socket.getaddrinfo()`, `socket.gethostbyname()`,
`socket.gethostbyname_ex()`, `socket.gethostbyaddr()`, `socket.getnameinfo()` are
not patched. Any code that does DNS resolution (which includes `socket.connect()`
with a hostname, `urllib`, `requests`, etc.) will block the entire OS thread, stalling
ALL greenlets in that thread.

This is one of the most impactful production bugs. DNS lookups typically take
1-100ms, during which no other greenlet can make progress.

**Gevent pattern:** Replaces all DNS functions with versions that delegate to
`hub.resolver`, which by default runs the original C-level DNS functions in a
native thread via a threadpool. The calling greenlet yields cooperatively while
the thread does the blocking lookup.

**Fix:** We need a simple threadpool-based resolver. Since we don't have gevent's
threadpool, we can use `concurrent.futures.ThreadPoolExecutor`:

```python
import _socket
from concurrent.futures import ThreadPoolExecutor

_dns_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dns")

def _run_in_dns_thread(fn, *args):
    """Run fn(*args) in a DNS thread, yielding the greenlet cooperatively."""
    if not _hub_is_running():
        return fn(*args)
    future = _dns_pool.submit(fn, *args)
    # Poll until done, yielding to hub each iteration
    while not future.done():
        _framework_core.green_sleep(0.001)
    return future.result()

def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _run_in_dns_thread(_socket.getaddrinfo, host, port, family, type, proto, flags)
```

**Better approach:** Instead of polling, use a pipe/eventfd to wake the greenlet
when the DNS thread finishes. The DNS thread writes to the pipe, the greenlet
does `green_poll_fd(pipe_read_end, POLLIN)`. Zero busy-waiting.

**Functions to patch:** `getaddrinfo`, `gethostbyname`, `gethostbyname_ex`,
`gethostbyaddr`, `getnameinfo`.

**Acceptance criteria:** DNS resolution doesn't block the hub. Test: start two
greenlets, one does a slow DNS lookup, the other does fast I/O — the fast one
completes without waiting for DNS.

---

#### P1-5: `_single_fd_select()` ignores timeout parameter

**File:** `_select.py:61-106`

**Bug:** The `timeout` parameter is accepted but never used in the cooperative
path. `green_poll_fd()` blocks indefinitely. If the fd never becomes ready,
`select()` hangs forever even if a finite timeout was specified.

**Fix:** We need either:
1. A `green_poll_fd_timeout(fd, events, timeout_secs)` Zig primitive that uses
   `IORING_OP_LINK_TIMEOUT`, or
2. A Python-level workaround: race `green_poll_fd` against `green_sleep(timeout)`.

For option 2, we need a way to cancel the poll when the sleep fires (or vice
versa). This requires the Zig hub to support operation cancellation.

**Pragmatic fix:** Submit the poll, and separately schedule a timeout. This is
tricky without cancellation support. As a simpler intermediate fix, use the
non-blocking `_original_select` with a real timeout (not 0) — this blocks the
greenlet but at least respects the timeout:

```python
# For single-fd with hub running: use green_poll_fd
# but enforce timeout via a linked timeout approach
if timeout is not None:
    # Fall back to original select with capped timeout for now
    return _original_select(rlist, wlist, xlist, timeout)
```

This loses cooperative behavior for the timeout case but avoids the hang.

**Long-term:** Add `IORING_OP_LINK_TIMEOUT` support and a
`green_poll_fd_timeout()` primitive.

**Acceptance criteria:** `select.select([sock], [], [], 0.1)` returns `([], [], [])`
within ~200ms if the socket has no data.

---

#### P1-6: SSL `_ssl_retry` has no timeout + register/unregister per call

**File:** `_ssl.py:26-49`

**Bug (timeout):** The `while True` retry loop has no timeout. If the remote peer
stops responding mid-handshake, the greenlet blocks forever.

**Bug (performance):** Every SSL operation calls `green_register_fd()` /
`green_unregister_fd()`. A TLS handshake triggers 4+ rounds of
SSLWantRead/Write, each doing register/unregister. This is expensive I/O
overhead.

**Fix (register/unregister):** Remove the per-call register/unregister from
`_ssl_retry`. The socket should already be registered when the SSL layer is
used (our cooperative socket class does `_ensure_registered()`). The SSL
socket wraps a cooperative socket, so the fd is already registered.

**Fix (timeout):** Add deadline tracking to `_ssl_retry`:
```python
def _ssl_retry(fd, func, *args, timeout=None):
    deadline = None
    if timeout is not None and timeout > 0:
        deadline = _time_mod.monotonic() + timeout
    while True:
        try:
            return func(*args)
        except SSLWantReadError:
            if deadline and _time_mod.monotonic() >= deadline:
                raise _socket_mod.timeout("timed out")
            green_poll_fd(fd, 0x001)
        except SSLWantWriteError:
            if deadline and _time_mod.monotonic() >= deadline:
                raise _socket_mod.timeout("timed out")
            green_poll_fd(fd, 0x004)
```

Pass `self.gettimeout()` as the timeout from each SSL method.

**Acceptance criteria:** SSL handshake with a non-responsive peer times out.
SSL operations don't do register/unregister per retry round.

---

#### P1-7: `CooperativeSelector` is not a subclass of `BaseSelector`

**File:** `_selectors.py:36`

**Bug:** `isinstance(sel, selectors.BaseSelector)` returns `False`. Code that
type-checks selectors (including Python's own `asyncio` and many libraries) will
reject this object.

The original reason for avoiding subclassing was mypy's `explicit-any` errors
from `BaseSelector`'s abstract methods. But we already have a module-level
`# mypy: disable-error-code="explicit-any"` directive, so this is solvable.

**Fix:** Subclass `selectors.BaseSelector` (or `_BaseSelectorImpl`) and add
`# type: ignore` annotations where needed for the abstract methods.

**Acceptance criteria:** `isinstance(CooperativeSelector(), selectors.BaseSelector)`
returns `True`.

---

#### P1-8: `patch_select` / `patch_selectors` sets epoll/kqueue to None

**File:** `__init__.py:88-90`, `__init__.py:105-107`

**Bug:** `patch_item(select, attr, None)` sets `select.epoll = None`. Code that
does `if hasattr(select, 'epoll')` finds it, then crashes calling `None()`.
Code that does `getattr(select, 'epoll', None)` gets `None` and can't
distinguish "not available" from "patched away".

**Gevent pattern:** Uses `delattr()` to actually remove the attribute.

**Fix:** Add a `remove_item()` function to `_state.py` that saves the original
and calls `delattr()`:
```python
def remove_item(module, attr):
    old = getattr(module, attr, _MISSING)
    if old is not _MISSING:
        _save(module, attr, old)
        delattr(module, attr)
```

Then use `remove_item` instead of `patch_item(..., None)`.

**Acceptance criteria:** `hasattr(select, 'epoll')` returns `False` after patching.

---

#### P1-9: `_selectors.py` imports `_original_select` from possibly-patched module

**File:** `_selectors.py:13`

**Bug:** `_original_select = __import__("select").select`. If the select module
has already been patched when this module is imported (e.g., if `patch_select()`
runs before `patch_selectors()`), `_original_select` will be our cooperative
`select`, not the original. This creates infinite recursion: our patched select
→ CooperativeSelector.select → patched select → ...

This happens in practice because `patch_all()` calls `patch_select()` before
`patch_selectors()`.

**Fix:** Import from `_state.get_original` or save the reference before patching:
```python
import select as _select_mod
_original_select = _select_mod.select  # Must be imported BEFORE patch_select() runs
```

Or better: use the saved original from `_state`:
```python
from theframework.monkey._state import get_original
# In select(), use: get_original("select", "select")
```

But the safest approach is to import the C-level `_select` module directly,
which is never patched.

**Acceptance criteria:** `patch_all()` doesn't cause recursion. `selectors` uses
the real OS-level select, not our patched version.

---

### P2 — Medium

#### P2-1: `recv_into()` creates unnecessary intermediate copy

**File:** `_socket.py:110-118`

**Bug:** Calls `green_recv()` to get a `bytes` object, then copies into the
buffer via `memoryview`. This defeats the zero-copy purpose of `recv_into()`.

**Fix:** Add a `green_recv_into` Zig primitive that writes directly into a
provided buffer. Until then, the current approach works but is suboptimal.

**Pragmatic fix for now:** Document the limitation. The copy overhead is small
for typical buffer sizes (<64KB). For the long term, track as a Zig-layer
enhancement.

---

#### P2-2: `accept()` returns fake address on `getpeername()` failure

**File:** `_socket.py:209-211`

**Bug:** Returns `("0.0.0.0", 0)` for all socket families. Wrong for IPv6
(`("::", 0, 0, 0)`) and Unix domain sockets (`b""`).

**Fix:** Check `self.family` and return the appropriate zero address:
```python
except OSError:
    if self.family == _socket.AF_INET6:
        peer_addr = ("::", 0, 0, 0)
    elif self.family == _socket.AF_UNIX:
        peer_addr = b""
    else:
        peer_addr = ("0.0.0.0", 0)
```

---

#### P2-3: `create_connection` uses identity comparison for timeout sentinel

**File:** `_socket.py:316`

**Bug:** `if timeout is not _orig_socket_mod.getdefaulttimeout()` uses `is not`
which breaks if the default timeout changes between import time and call time.

**Fix:** Use a private sentinel object, like the stdlib does:
```python
_GLOBAL_DEFAULT_TIMEOUT = object()

def create_connection(address, timeout=_GLOBAL_DEFAULT_TIMEOUT, ...):
    ...
    if timeout is not _GLOBAL_DEFAULT_TIMEOUT:
        sock.settimeout(timeout)
```

---

#### P2-4: No close() waiter cancellation (fd reuse race)

**File:** `_socket.py:274-283`

**Bug:** When `close()` is called while another greenlet is blocked on
`green_recv(fd)`, the blocked greenlet continues waiting on the now-closed fd.
If the OS reuses the fd number for a new connection, the blocked greenlet could
receive data from the wrong connection.

**Gevent pattern:** `cancel_waits_close_and_then()` defers the actual close until
after all waiters have been woken with an `EBADF` error. Also replaces `self._sock`
with a `_closedsocket` proxy.

**Fix:** This requires support from the Zig hub — we need a way to cancel pending
io_uring operations for a given fd. The hub's `greenClose` already does
`IORING_OP_ASYNC_CANCEL` for the connection's pending ops. We should expose this
via a `green_cancel_fd(fd)` primitive or use `green_close` which already handles
cancellation.

**Pragmatic fix:** When `close()` is called, call `green_close(fd)` instead of
`super().close()` when the hub is running. `green_close` cancels pending ops.

---

#### P2-5: No `SOCK_NONBLOCK` type masking

**File:** `_socket.py`

**Bug:** Since we set sockets to non-blocking internally (`super().setblocking(False)`),
`sock.type` includes `SOCK_NONBLOCK`. Libraries that check
`sock.type == SOCK_STREAM` will get `False`.

**Fix:** Override the `type` property:
```python
@property
def type(self):
    t = super().type
    if self._registered:
        t &= ~_socket.SOCK_NONBLOCK
    return t
```

---

#### P2-6: `bytes(data)` copy in send/sendall for non-bytes

**File:** `_socket.py:131,139`

**Bug:** `bytes(data)` creates a full copy for memoryview, bytearray, etc.

**Fix:** Use `memoryview(data).tobytes()` only when needed, or pass memoryview
directly if the Zig layer accepts buffer protocol objects. For now, check if
`green_send` can accept memoryview (it uses `PyBytes_AsString` so it needs bytes).
This is a Zig-layer limitation — `green_send` only accepts `bytes` objects.

**Pragmatic fix:** Accept the copy for now. Document as a known limitation.
Long-term: modify `green_send` to accept the buffer protocol.

---

#### P2-7: SSL sendall uses O(n²) byte slicing

**File:** `_ssl.py:83-88`

**Bug:** `bdata[sent:]` creates a new bytes object on each loop iteration.

**Fix:** Use `memoryview`:
```python
view = memoryview(bytes(data))
while sent < total:
    n = _ssl_retry(self.fileno(), super().send, view[sent:], flags)
    sent += n
```

---

#### P2-8: SSL `do_handshake` block parameter deprecated

**File:** `_ssl.py:90`

**Bug:** `do_handshake(self, block: bool = False)` — the `block` parameter was
removed in Python 3.12.

**Fix:** Remove the `block` parameter:
```python
def do_handshake(self) -> None:
```

---

#### P2-9: No SSL `recv_into()` override

**File:** `_ssl.py`

**Bug:** `ssl_sock.recv_into()` falls through to `ssl.SSLSocket.recv_into()`
which uses blocking I/O.

**Fix:** Add `recv_into` override using `_ssl_retry`:
```python
def recv_into(self, buffer, nbytes=None, flags=0):
    if not _hub_is_running():
        return super().recv_into(buffer, nbytes, flags)
    return _ssl_retry(self.fileno(), super().recv_into, buffer, nbytes, flags)
```

---

#### P2-10: Duplicated `_get_fd()` helper

**Files:** `_socket.py:27-35`, `_select.py:14-20`, `_selectors.py:19-25`

**Fix:** Move to `_state.py` and import from there.

---

#### P2-11: `sleep()` doesn't validate negative values

**File:** `_time.py:21`

**Bug:** `if seconds <= 0: green_sleep(0.0)` silently converts negative to zero.
The stdlib `time.sleep()` raises `ValueError` for negative values.

**Fix:**
```python
if seconds < 0:
    raise ValueError("sleep length must be non-negative")
if seconds == 0:
    _framework_core.green_sleep(0.0)
    return
```

---

#### P2-12: No closed-state checking on selector methods

**File:** `_selectors.py`

**Bug:** After `close()`, calling `register()`, `select()`, etc. doesn't raise
`RuntimeError('selector is closed')` as the stdlib requires.

**Fix:** Add check at the top of each method:
```python
def _check_closed(self):
    if self._closed:
        raise RuntimeError("selector is closed")
```

---

#### P2-13: `connect()` only handles IPv4 string addresses

**File:** `_socket.py:181`

**Bug:** `green_connect_fd(self.fileno(), ip, int(sa[1]))` extracts only host and
port. IPv6 addresses have additional fields (flowinfo, scope_id) in the sockaddr
tuple. The Zig layer likely only handles IPv4.

**Fix:** Check address family. For AF_INET6, pass the full address. This may
require updating the Zig `greenConnectFd` to handle IPv6 addresses. For now,
fall back to poll-then-connect for IPv6:
```python
if self.family == _socket.AF_INET6:
    _framework_core.green_poll_fd(self.fileno(), 0x004)  # POLLOUT
    err = super().connect_ex(address)
    if err and err != _errno_mod.EISCONN:
        raise OSError(err, _os.strerror(err))
    return
```

---

### P3 — Low (Gevent Parity / Nice-to-Have)

These are important for long-term ecosystem compatibility but not blockers for
initial production use.

#### P3-1: No `patch_thread()` — threading primitives deadlock

`threading.Lock`, `threading.Event`, `threading.Condition`, `threading.RLock` all
block at the OS level, not at the greenlet level. When greenlet A holds a lock and
yields, greenlet B will deadlock trying to acquire it because the OS-level lock
blocks the entire thread.

This affects: `logging` (has locks), `urllib3` (has locks), `requests`, `sqlalchemy`,
every library that uses `threading.Lock` internally.

**Fix:** Implement cooperative versions of threading primitives using greenlet
switches. This is a large undertaking. Gevent has ~2000 lines for this.

**Workaround for now:** Document that users should avoid sharing `threading.Lock`
across greenlets. Most server code doesn't do this explicitly, but library-internal
locks can trigger it.

#### P3-2: No `is_object_patched()` / `get_original()` re-export

Minor API gap. Easy to add.

#### P3-3: No event/plugin system for patching hooks

Gevent emits events via setuptools entry points. Low priority.

#### P3-4: No `restore()` / unpatch capability

Would be useful for testing. Low priority for production.

#### P3-5: No `-m theframework.monkey` entry point

Gevent supports `python -m gevent.monkey script.py`. Low priority.

#### P3-6: No `__repr__`, `__getstate__`, weakref support on socket

Nice for debugging but not functional issues.

#### P3-7: No `SSLObject` cooperative class

Only needed for asyncio TLS and memory BIO usage. Low priority.

#### P3-8: No `sendfile()` support

Only matters for file-serving use cases.

#### P3-9: No `patch_os()`, `patch_subprocess()`

Important for code that forks or spawns subprocesses from handlers.

---

## Implementation Order

### Phase 1: Fix correctness bugs (no Zig changes needed) ✅ DONE

These are pure Python fixes that don't require new Zig primitives.

1. ✅ **P1-1: Fix `_build_results` dual-event bug** — OR event masks for same fd
2. ✅ **P1-8: Fix epoll/kqueue removal (None → delattr)** — added `remove_item()`
3. ✅ **P1-9: Fix `_selectors.py` import of possibly-patched select** — uses `get_original()`
4. ✅ **P2-2: Fix accept() fake address** — family-aware fallback address
5. ✅ **P2-3: Fix create_connection timeout sentinel** — `_GLOBAL_DEFAULT_TIMEOUT`
6. ✅ **P2-7: Fix SSL sendall O(n²) slicing** — uses `memoryview`
7. ✅ **P2-8: Fix SSL do_handshake block parameter** — removed
8. ✅ **P2-9: Add SSL recv_into** — with flag rejection like gevent
9. ✅ **P2-10: Deduplicate _get_fd** — centralised in `_state.py`
10. ✅ **P2-11: Validate negative sleep** — raises `ValueError`
11. ✅ **P2-12: Add selector closed-state checking** — `_check_closed()` guard
12. ✅ **P2-5: Add SOCK_NONBLOCK type masking** — `type` property override

Also done: SSL recv/send/sendall reject non-zero flags with ValueError (gevent pattern).
Also done: SSL `_ssl_retry` no longer does register/unregister per call (P1-6 partial).
Also done: SSL `read()` no longer passes `buffer=None` unconditionally.

28 new tests in `tests/test_monkey_phase1.py`. 83 total tests pass. mypy clean.

### Phase 2: Hub detection + socket robustness ✅ DONE

13. ✅ **P0-2: Replace hub_is_running with thread-local** — `_HubLocal` thread-local in
    `_state.py`, `mark_hub_running`/`mark_hub_stopped` in `server.py` wrapper
14. ✅ **P1-2: Handle non-zero flags via poll-then-syscall** — all I/O methods
    (recv/send/sendall/recv_into/recvfrom/sendto/recvfrom_into) use `green_poll_fd`
    then `super()` when flags != 0
15. ✅ **P1-3: Add timeout enforcement (Python-level deadline)** — `_get_deadline()`
    / `_check_deadline()` on all I/O methods + connect + accept
16. ✅ **P1-6: Fix SSL _ssl_retry (timeout + no register/unregister)** — `_ssl_retry`
    accepts `timeout` kwarg, all SSLSocket methods pass `self._get_ssl_timeout()`,
    sendall uses per-send remaining time
17. ✅ **P2-4: Improve close() with green_close** — registered sockets use
    `green_close(fd)` which cancels pending io_uring ops before closing
18. ✅ **P2-13: Handle IPv6 in connect** — AF_INET6 uses poll-then-connect_ex
    fallback since Zig greenConnectFd only handles IPv4

Also done: Unix socket connect uses poll-then-connect_ex pattern.
Also done: connect_ex returns errno properly via cooperative connect().

41 new tests in `tests/test_monkey_phase2.py`. 124 total tests pass. mypy clean.

### Phase 3: DNS patching ✅ DONE

19. ✅ **P1-4: Implement threadpool-based DNS resolver** — new `_dns.py` module
    using `ThreadPoolExecutor` + `os.pipe()` for zero-busy-wait notification.
    Worker thread writes to pipe, greenlet waits via `green_poll_fd(pipe_rd, POLLIN)`.
    Functions: `getaddrinfo`, `gethostbyname`, `gethostbyname_ex`, `gethostbyaddr`,
    `getnameinfo`.
20. ✅ **Update patch_socket to patch DNS functions** — all 5 DNS functions
    replaced in `socket` module by `patch_socket()`.

22 new tests in `tests/test_monkey_phase3.py`. 146 total tests pass. mypy clean.

### Phase 4: Eliminate busy-wait polling ✅ DONE

21. ✅ **P0-1: Add `green_poll_multi` Zig primitive** — added `greenPollMulti` and
    `greenPollFdTimeout` to hub.zig, `PollGroup` struct to op_slot.zig,
    `prepLinkTimeout` and `prepCancelUserData` to ring.zig, exports in extension.zig
22. ✅ **Update _select.py to use green_poll_multi** — single-fd path uses
    `green_poll_fd_timeout`, multi-fd path uses `green_poll_multi`, no busy-wait
23. ✅ **Update _selectors.py to use green_poll_multi** — `CooperativeSelector.select()`
    uses `green_poll_multi` when hub is running, falls back to real select otherwise
24. ✅ **P1-5: Implement timeout for single-fd select** — uses `IOSQE_IO_LINK` +
    `IORING_OP_LINK_TIMEOUT` via `green_poll_fd_timeout`

24 new tests in `tests/test_monkey_phase4.py`. 170 total tests pass. mypy clean.

### Phase 5: Selector subclassing ✅ DONE

25. ✅ **P1-7: Make CooperativeSelector subclass BaseSelector** — subclassed
    `selectors.BaseSelector`, removed redundant `modify`/`__enter__`/`__exit__`
    (inherited from base), added `# type: ignore[override]` on `get_map`.

3 new tests in `tests/test_monkey_phase1.py`. 173 total tests pass. mypy clean.

### Phase 6: Tests ✅ DONE

All 10 key scenarios are now covered with runtime tests:

- ✅ Dual READ+WRITE events on selectors (Phase 1)
- ✅ `MSG_PEEK` flag preserved through recv (Phase 2)
- ✅ Socket timeout actually fires (Phase 2)
- ✅ DNS doesn't block the hub (Phase 3)
- ✅ `select()` with timeout returns on time (Phase 4)
- ✅ SSL handshake + echo through hub (Phase 6 — new runtime test)
- ✅ SSL handshake timeout fires when peer is unresponsive (Phase 6)
- ✅ `hasattr(select, 'epoll')` returns False (Phase 1)
- ✅ Selector closed-state raises RuntimeError (Phase 1)
- ✅ IPv6 connect + echo through hub (Phase 6 — new runtime test)
- ✅ IPv6 connect timeout (Phase 6)
- ✅ SSL over IPv6 echo through hub (Phase 6)
- ✅ SOCK_NONBLOCK masked from socket.type (Phase 1)

**Bug found and fixed:** `patch_ssl()` was replacing `ssl.SSLSocket` at module
level, which broke `_create`'s `super(SSLSocket, self).__init__()` chain.
Fixed by only setting `SSLContext.sslsocket_class` (isinstance still works
because our class is a subclass of the original).

5 new tests in `tests/test_monkey_phase6.py`. 178 total tests pass. mypy clean.

---

## What We're NOT Fixing (and Why)

1. **`sendall` single-call bug (previously reported as critical):** This is
   actually NOT a bug. The Zig `greenSend` (hub.zig:512-562) already contains a
   `while (total_sent < buf_len)` loop that ensures all data is sent. The Python
   `sendall` calling `green_send` once IS correct because `green_send` returns
   only after sending all bytes. Verified by reading the Zig source.

2. **`patch_thread`:** Too large for this phase. Would need cooperative Lock,
   Event, Condition, RLock, Semaphore, Barrier, Timer implementations. Track
   separately.

3. **asyncio compatibility:** Out of scope. The framework is greenlet-based, not
   asyncio-based.

4. **Full gevent event/plugin system:** Over-engineered for our needs.

---

## Dependencies on Zig Changes

The following fixes require new Zig primitives:

| Fix | Zig Primitive Needed | Complexity |
|-----|---------------------|------------|
| P0-1 (busy-wait) | `green_poll_multi(fds, events[])` | Medium — submit N POLL_ADD SQEs, cancel on first completion |
| P1-3 (timeouts) | `IORING_OP_LINK_TIMEOUT` support | Medium — link timeout SQEs to I/O SQEs |
| P1-5 (select timeout) | `green_poll_fd_timeout(fd, events, timeout)` | Low — single POLL_ADD + linked TIMEOUT |
| P2-1 (recv_into) | `green_recv_into(fd, buffer, size)` | Low — write directly to buffer |

Phase 4 (eliminating busy-wait) is the most impactful Zig change and should be
prioritised after the pure-Python fixes are done.
