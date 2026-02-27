# Implementation Plan: Prefork with SO_REUSEPORT (Option B)

## 1. Goals and Non-Goals

### Goals

- Run N independent worker processes, each with its own socket (SO_REUSEPORT), io_uring ring,
  Hub, ConnectionPool, and greenlet tree.
- Kernel-level connection distribution via hash of the 4-tuple (even load across workers).
- Parent process supervises workers: respawns crashes, forwards signals, detects crash loops.
- Single-worker mode (workers=1) preserves current behavior exactly — no fork, no SO_REUSEPORT.
- Zero changes to the Zig I/O layer (hub.zig, ring.zig, connection.zig, op_slot.zig,
  extension.zig).
- Production-grade signal handling that correctly wakes io_uring from blocking waits.

### Non-Goals

- Thread-per-core (Option C) — future work for free-threaded Python.
- Graceful connection draining on shutdown — initial implementation does abrupt shutdown; draining
  is a documented follow-up.
- Unix domain socket support — not yet in theframework.
- Hot code reload / rolling restart — future enhancement.
- Per-worker metrics aggregation — future enhancement.
- IPv6 support — orthogonal concern, not addressed here.

---

## 2. Architecture Overview

```
                    ┌─────────────────────┐
                    │   Parent Process     │
                    │                      │
                    │  1. fork() × N       │
                    │  2. Supervisor loop  │
                    │     - os.waitpid()   │
                    │     - signal fwd     │
                    │     - crash recovery │
                    └───────┬─────────────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
     ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
     │   Worker 0   │ │   Worker 1   │ │   Worker N-1 │
     │              │ │              │ │              │
     │ socket()     │ │ socket()     │ │ socket()     │
     │ SO_REUSEPORT │ │ SO_REUSEPORT │ │ SO_REUSEPORT │
     │ bind(:8000)  │ │ bind(:8000)  │ │ bind(:8000)  │
     │ listen(1024) │ │ listen(1024) │ │ listen(1024) │
     │              │ │              │ │              │
     │ Hub          │ │ Hub          │ │ Hub          │
     │ ├─ io_uring  │ │ ├─ io_uring  │ │ ├─ io_uring  │
     │ ├─ ConnPool  │ │ ├─ ConnPool  │ │ ├─ ConnPool  │
     │ ├─ OpSlots   │ │ ├─ OpSlots   │ │ ├─ OpSlots   │
     │ ├─ greenlets │ │ ├─ greenlets │ │ ├─ greenlets │
     │ └─ sig pipe  │ │ └─ sig pipe  │ │ └─ sig pipe  │
     │              │ │              │ │              │
     │ accept()  ─┐ │ │ accept()  ─┐ │ │ accept()  ─┐ │
     │   own queue▼ │ │   own queue▼ │ │   own queue▼ │
     └──────────────┘ └──────────────┘ └──────────────┘
              ▲             ▲             ▲
              └─────────────┼─────────────┘
                    kernel hash distributes
                    incoming connections
```

**Key invariant**: No listening socket, no io_uring ring, and no Hub exist in the parent process.
Each child creates everything it needs after fork(). This avoids all fork+io_uring pitfalls
documented in the prefork.md background section.

---

## 3. File Changes Summary

| File | Change Type | Description |
|---|---|---|
| `theframework/server.py` | **Major refactor** | Split into single-worker and multi-worker paths; add supervisor, signal handling, socket helper |
| `theframework/app.py` | **Minor** | Add `workers` parameter to `Framework.run()` |
| `stubs/_framework_core.pyi` | **No change** | No new Zig functions needed |
| `src/hub.zig` | **No change** | Hub, ring, pool, greenlets — all unchanged |
| `src/extension.zig` | **No change** | No new C extension methods |
| `src/ring.zig` | **No change** | |
| `src/connection.zig` | **No change** | |
| `src/op_slot.zig` | **No change** | |
| `tests/test_prefork.py` | **New file** | Integration tests for multi-worker mode |

---

## 4. Detailed Design

### 4.1 Socket Creation with SO_REUSEPORT

A new helper function creates the listening socket with the correct options.

```python
def _create_listen_socket(
    host: str,
    port: int,
    *,
    reuseport: bool = False,
    backlog: int = 1024,
) -> socket.socket:
    """Create a TCP listening socket.

    Args:
        reuseport: If True, set SO_REUSEPORT so multiple processes
                   can bind to the same address:port. Each gets its
                   own accept queue; the kernel distributes incoming
                   connections via a hash of the 4-tuple.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if reuseport:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind((host, port))
    sock.listen(backlog)
    return sock
```

**Why SO_REUSEPORT instead of SO_REUSEADDR alone**: SO_REUSEADDR only prevents `EADDRINUSE` for
TIME_WAIT sockets. SO_REUSEPORT allows genuinely separate sockets (different processes) to bind
to the same address:port simultaneously. Each socket gets its own kernel accept queue. Without
SO_REUSEPORT, the second child's `bind()` would fail with `EADDRINUSE`.

**Backlog**: Raised from 128 (current) to 1024 for multi-worker. Each worker has its own accept
queue, so the effective total backlog is `N × 1024`. For single-worker mode, the original value
is preserved (we'll make it configurable but default to 1024).

**Error handling**: If SO_REUSEPORT is not supported (kernel < 3.9, which is unrealistic for
io_uring systems), `setsockopt` raises `OSError(ENOPROTOOPT)`. We let this propagate — it's a
fatal configuration error that means the kernel doesn't support the required feature.

### 4.2 Signal Handling Design

Signal handling in a prefork server is the trickiest part. The problem: the Hub's event loop
blocks in `io_uring_enter()` with the GIL released. Python signal handlers only run when the
GIL is held. A signal delivered during `io_uring_enter()` causes EINTR, but Zig's IoUring
retries automatically, so the Python handler never gets a chance to execute.

**Solution**: Use `signal.set_wakeup_fd()` combined with a signal watcher greenlet.

`signal.set_wakeup_fd(fd)` tells CPython's C-level signal handler to write the signal number
(as a single byte) to `fd` immediately when a signal is delivered — this happens at the C level,
before EINTR is returned to the caller. If `fd` is a pipe that io_uring is monitoring with a
pending recv SQE, the recv completes, producing a CQE that wakes the hub loop.

```
Signal arrives during io_uring_enter()
  │
  ├── 1. CPython C signal handler runs (before EINTR returned)
  │      └── Writes signal byte to sig_w (the wakeup fd)
  │
  ├── 2. io_uring_enter() returns EINTR
  │      └── Zig IoUring retries io_uring_enter()
  │
  └── 3. io_uring sees data on sig_r pipe (from step 1)
         └── Completes the pending recv SQE on sig_r
         └── io_uring_enter() returns with CQE
         └── GIL reacquired
         └── Hub processes CQE → signal watcher greenlet resumes
         └── Signal watcher calls hub_stop()
         └── Hub loop exits cleanly
```

**Implementation in the worker process**:

```python
def _signal_watcher(sig_read_fd: int) -> None:
    """Greenlet: wait for signal bytes on the pipe, then stop the hub."""
    _framework_core.green_register_fd(sig_read_fd)
    try:
        data = _framework_core.green_recv(sig_read_fd, 16)
        if data:
            _framework_core.hub_stop()
    except OSError:
        pass
    finally:
        _framework_core.green_unregister_fd(sig_read_fd)
```

The signal watcher is spawned as a greenlet parented to the hub, alongside the acceptor. It
registers the signal pipe's read end in the connection pool, then does a cooperative
`green_recv()` on it. When the signal byte arrives, it calls `hub_stop()`.

**Parent signal handling**:

```python
_shutting_down = False

def _parent_signal_handler(signum: int, frame: object) -> None:
    """SIGINT/SIGTERM handler for the parent process."""
    global _shutting_down
    if _shutting_down:
        return  # Prevent re-entry
    _shutting_down = True
    # Children are notified via SIGTERM in the supervisor loop
```

The parent doesn't directly kill children from the signal handler (signal handlers should be
minimal). Instead, it sets a flag that the supervisor loop checks.

**Child signal setup** (after fork, before hub_run):

```python
signal.signal(signal.SIGINT, signal.SIG_IGN)   # Parent handles Ctrl+C
signal.signal(signal.SIGTERM, lambda s, f: None) # Wakeup fd handles it
sig_r, sig_w = os.pipe()
os.set_blocking(sig_r, False)
os.set_blocking(sig_w, False)
signal.set_wakeup_fd(sig_w, warn_on_full_buffer=False)
```

SIGINT is ignored in children because the parent process group receives Ctrl+C and the parent
coordinates shutdown. SIGTERM gets a no-op Python handler (so it doesn't use the default
"terminate process" behavior), but the real work is done by `set_wakeup_fd` at the C level.

### 4.3 Worker Process Lifecycle

Each worker process, after fork(), follows this lifecycle:

```
fork()
  │
  ├── 1. Reset signal handlers
  │      - SIGINT → SIG_IGN
  │      - SIGTERM → no-op (wakeup fd handles it)
  │
  ├── 2. Create signal pipe (sig_r, sig_w)
  │      - os.pipe(), set both non-blocking
  │      - signal.set_wakeup_fd(sig_w)
  │
  ├── 3. Create listening socket with SO_REUSEPORT
  │      - socket(), SO_REUSEADDR, SO_REUSEPORT, bind(), listen()
  │
  ├── 4. Call _framework_core.hub_run(listen_fd, acceptor_fn)
  │      │
  │      │  Inside hub_run (Zig side — unchanged):
  │      │  ├── Allocate Hub on heap
  │      │  ├── Hub.init() → creates io_uring ring, ConnectionPool,
  │      │  │   OpSlotTable, stop_pipe
  │      │  ├── Set global_hub
  │      │  ├── Save hub_greenlet = current greenlet
  │      │  ├── Create acceptor greenlet from acceptor_fn
  │      │  ├── Schedule acceptor on ready queue
  │      │  └── Run hubLoop()
  │      │
  │      │  Inside acceptor_fn (Python side):
  │      │  ├── mark_hub_running(hub_g)
  │      │  ├── Spawn signal watcher greenlet
  │      │  │   └── green_register_fd(sig_r)
  │      │  │   └── green_recv(sig_r, 16) → blocks until signal
  │      │  │   └── hub_stop()
  │      │  └── _run_acceptor(listen_fd, handler)
  │      │       └── Loop: green_accept → spawn handler greenlet
  │      │
  │      └── hub_run returns (hub loop exited)
  │
  ├── 5. Cleanup
  │      - mark_hub_stopped()
  │      - signal.set_wakeup_fd(-1)
  │      - os.close(sig_r), os.close(sig_w)
  │      - sock.close()
  │
  └── 6. os._exit(0)
```

**Why `os._exit(0)` instead of `sys.exit()`**: `os._exit()` performs an immediate process exit
without running atexit handlers, flushing stdio buffers, or calling Python finalizers. This is
correct for forked child processes because:
- atexit handlers registered before fork are for the parent, not the child
- Python finalizers might try to clean up resources that belong to the parent
- The child's Hub cleanup already happened in step 5
- `sys.exit()` raises SystemExit, which could be caught and cause unexpected behavior

### 4.4 Parent Supervisor Loop

The parent process enters a supervisor loop after forking all workers.

```python
def _supervisor_loop(
    children: dict[int, int],  # pid → worker_id
    handler: HandlerFunc,
    host: str,
    port: int,
    num_workers: int,
    shutdown_timeout: float,
) -> None:
    """Monitor children, respawn on crash, coordinate shutdown."""
    crash_history: dict[int, list[float]] = defaultdict(list)

    while children and not _shutting_down:
        try:
            pid, status = os.waitpid(-1, 0)
        except ChildProcessError:
            break  # No children left
        except InterruptedError:
            # Signal interrupted waitpid — check shutdown flag
            if _shutting_down:
                break
            continue

        worker_id = children.pop(pid, None)
        if worker_id is None:
            continue

        if _shutting_down:
            continue  # Expected exit during shutdown

        # Worker crashed unexpectedly
        exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
        term_signal = os.WTERMSIG(status) if os.WIFSIGNALED(status) else 0

        _log_worker_exit(worker_id, pid, exit_code, term_signal)

        # Crash loop detection
        now = time.monotonic()
        crash_history[worker_id] = [
            t for t in crash_history[worker_id] if now - t < CRASH_WINDOW
        ]
        crash_history[worker_id].append(now)

        if len(crash_history[worker_id]) >= MAX_CRASHES_IN_WINDOW:
            _log(f"worker {worker_id}: crash loop detected "
                 f"({MAX_CRASHES_IN_WINDOW} crashes in {CRASH_WINDOW}s), "
                 f"not respawning")
            continue

        # Respawn
        new_pid = os.fork()
        if new_pid == 0:
            _worker_main(worker_id, handler, host, port)
            os._exit(0)
        children[new_pid] = worker_id
        _log(f"worker {worker_id}: respawned as pid {new_pid}")

    # Shutdown: send SIGTERM to remaining children
    _shutdown_children(children, shutdown_timeout)
```

**Crash loop detection**: Track the timestamps of crashes per worker within a sliding window
(e.g., 5 crashes in 60 seconds). If the threshold is exceeded, stop respawning that worker
and log a critical error. Other workers continue running.

**Constants**:
```python
CRASH_WINDOW = 60.0       # seconds
MAX_CRASHES_IN_WINDOW = 5  # maximum crashes before giving up
SHUTDOWN_TIMEOUT = 30.0    # seconds to wait for workers after SIGTERM
```

### 4.5 Shutdown Sequence

```
User hits Ctrl+C or sends SIGTERM to parent
  │
  ├── 1. Parent's signal handler sets _shutting_down = True
  │
  ├── 2. os.waitpid() returns InterruptedError (EINTR)
  │      └── Supervisor loop checks _shutting_down, breaks
  │
  ├── 3. _shutdown_children():
  │      │
  │      ├── a. Send SIGTERM to all living children
  │      │      └── for pid in children: os.kill(pid, SIGTERM)
  │      │
  │      ├── b. Wait for children to exit (with timeout)
  │      │      └── Loop: os.waitpid(WNOHANG) until all exited
  │      │          or SHUTDOWN_TIMEOUT exceeded
  │      │
  │      └── c. If timeout exceeded, send SIGKILL
  │             └── for pid in remaining: os.kill(pid, SIGKILL)
  │             └── os.waitpid() to reap zombies
  │
  └── 4. Parent exits
```

```python
def _shutdown_children(
    children: dict[int, int],
    timeout: float,
) -> None:
    """Send SIGTERM to all children, wait, then SIGKILL stragglers."""
    if not children:
        return

    # Send SIGTERM
    for pid in list(children):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            children.pop(pid, None)

    # Wait with timeout
    deadline = time.monotonic() + timeout
    while children and time.monotonic() < deadline:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                time.sleep(0.1)
                continue
            children.pop(pid, None)
        except ChildProcessError:
            break

    # SIGKILL stragglers
    for pid in list(children):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
```

### 4.6 Logging

Add minimal structured logging to the server module. Each message is prefixed with a role
identifier (parent or worker-N).

```python
import sys

def _log(msg: str, *, worker_id: int | None = None) -> None:
    """Write a log line to stderr."""
    if worker_id is not None:
        prefix = f"[worker-{worker_id}]"
    else:
        prefix = "[parent]"
    print(f"{prefix} {msg}", file=sys.stderr, flush=True)
```

This is intentionally simple — no dependency on the `logging` module. Users who want structured
logging can hook into it later.

### 4.7 Complete server.py Refactor

The new `server.py` structure:

```python
# theframework/server.py

from __future__ import annotations

import os
import signal
import socket
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable

import greenlet
import _framework_core

from theframework.request import Request
from theframework.response import Response

HandlerFunc = Callable[[Request, Response], None]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CRASH_WINDOW: float = 60.0
MAX_CRASHES_IN_WINDOW: int = 5
SHUTDOWN_TIMEOUT: float = 30.0

# ---------------------------------------------------------------------------
# Module-level state (for signal handler communication)
# ---------------------------------------------------------------------------

_shutting_down: bool = False

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(msg: str, *, worker_id: int | None = None) -> None: ...

# ---------------------------------------------------------------------------
# Socket creation
# ---------------------------------------------------------------------------

def _create_listen_socket(
    host: str, port: int, *, reuseport: bool = False, backlog: int = 1024
) -> socket.socket: ...

# ---------------------------------------------------------------------------
# Connection handling (unchanged from current code)
# ---------------------------------------------------------------------------

def _handle_connection(fd: int, handler: HandlerFunc) -> None: ...
    # Exactly the same as current implementation

def _run_acceptor(listen_fd: int, handler: HandlerFunc) -> None: ...
    # Exactly the same as current implementation

# ---------------------------------------------------------------------------
# Signal watcher greenlet
# ---------------------------------------------------------------------------

def _signal_watcher(sig_read_fd: int) -> None: ...

# ---------------------------------------------------------------------------
# Worker process
# ---------------------------------------------------------------------------

def _worker_main(
    worker_id: int, handler: HandlerFunc, host: str, port: int
) -> None: ...

# ---------------------------------------------------------------------------
# Single-worker mode (current behavior, no fork)
# ---------------------------------------------------------------------------

def _serve_single(
    handler: HandlerFunc,
    host: str,
    port: int,
    *,
    _ready: threading.Event | None = None,
) -> None: ...

# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------

def _shutdown_children(
    children: dict[int, int], timeout: float
) -> None: ...

def _supervisor_loop(
    children: dict[int, int],
    handler: HandlerFunc,
    host: str,
    port: int,
    num_workers: int,
    shutdown_timeout: float,
) -> None: ...

# ---------------------------------------------------------------------------
# Multi-worker mode
# ---------------------------------------------------------------------------

def _serve_prefork(
    handler: HandlerFunc, host: str, port: int, *, workers: int
) -> None: ...

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def serve(
    handler: HandlerFunc,
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    workers: int = 1,
    _ready: threading.Event | None = None,
) -> None:
    """Start the HTTP server.

    Args:
        handler: Function called for each HTTP request.
        host: Address to bind to.
        port: Port to bind to.
        workers: Number of worker processes. Defaults to 1 (single-process,
                 no fork). Set to 0 to auto-detect from CPU count.
        _ready: Event set when the server is ready (single-worker only).
    """
    if workers == 0:
        workers = os.cpu_count() or 1

    if workers == 1:
        _serve_single(handler, host, port, _ready=_ready)
    else:
        if _ready is not None:
            raise ValueError("_ready is not supported with workers > 1")
        _serve_prefork(handler, host, port, workers=workers)
```

### 4.8 app.py Changes

```python
# In Framework class:

def run(
    self,
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    workers: int = 1,
) -> None:
    """Start the server.

    Args:
        workers: Number of worker processes. 1 = single-process (default).
                 0 = auto-detect from CPU count.
    """
    serve(self._make_handler(), host, port, workers=workers)
```

---

## 5. Detailed Function Implementations

### 5.1 `_create_listen_socket`

```python
def _create_listen_socket(
    host: str,
    port: int,
    *,
    reuseport: bool = False,
    backlog: int = 1024,
) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if reuseport:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind((host, port))
        sock.listen(backlog)
    except BaseException:
        sock.close()
        raise
    return sock
```

Note the `try/except` to close the socket on bind/listen failure (avoids fd leak).

### 5.2 `_signal_watcher`

```python
def _signal_watcher(sig_read_fd: int) -> None:
    """Greenlet that monitors the signal wakeup pipe.

    When a signal byte arrives (written by CPython's C-level signal handler
    via set_wakeup_fd), this greenlet resumes and stops the hub, causing
    the event loop to exit cleanly.
    """
    _framework_core.green_register_fd(sig_read_fd)
    try:
        data = _framework_core.green_recv(sig_read_fd, 16)
        if data:
            _framework_core.hub_stop()
    except OSError:
        pass
    finally:
        try:
            _framework_core.green_unregister_fd(sig_read_fd)
        except RuntimeError:
            pass  # Hub may already be torn down
```

### 5.3 `_worker_main`

```python
def _worker_main(
    worker_id: int, handler: HandlerFunc, host: str, port: int
) -> None:
    """Entry point for a forked worker process."""
    # 1. Reset signals
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, lambda s, f: None)

    # 2. Create signal wakeup pipe
    sig_r, sig_w = os.pipe()
    os.set_blocking(sig_r, False)
    os.set_blocking(sig_w, False)
    old_wakeup_fd = signal.set_wakeup_fd(sig_w, warn_on_full_buffer=False)

    # 3. Create socket with SO_REUSEPORT
    try:
        sock = _create_listen_socket(host, port, reuseport=True)
    except OSError as e:
        _log(f"failed to bind {host}:{port}: {e}", worker_id=worker_id)
        os.close(sig_r)
        os.close(sig_w)
        os._exit(1)

    listen_fd = sock.fileno()
    _log(f"listening on {host}:{port} (fd={listen_fd})", worker_id=worker_id)

    # 4. Run hub
    from theframework.monkey._state import mark_hub_running, mark_hub_stopped

    try:
        def _acceptor() -> None:
            hub_g = _framework_core.get_hub_greenlet()
            mark_hub_running(hub_g)

            # Spawn signal watcher greenlet
            watcher_g = greenlet.greenlet(
                lambda: _signal_watcher(sig_r), parent=hub_g
            )
            _framework_core.hub_schedule(watcher_g)

            # Run the acceptor loop
            _run_acceptor(listen_fd, handler)

        _framework_core.hub_run(listen_fd, _acceptor)
    except Exception:
        _log(f"hub exited with error", worker_id=worker_id)
    finally:
        mark_hub_stopped()
        signal.set_wakeup_fd(old_wakeup_fd)
        os.close(sig_r)
        os.close(sig_w)
        sock.close()

    os._exit(0)
```

### 5.4 `_serve_single`

This is the current behavior, extracted into its own function.

```python
def _serve_single(
    handler: HandlerFunc,
    host: str,
    port: int,
    *,
    _ready: threading.Event | None = None,
) -> None:
    """Single-worker mode: no fork, current behavior."""
    sock = _create_listen_socket(host, port, reuseport=False)
    listen_fd = sock.fileno()

    if _ready is not None:
        _ready.set()

    from theframework.monkey._state import mark_hub_running, mark_hub_stopped

    try:
        def _acceptor_with_mark() -> None:
            hub_g = _framework_core.get_hub_greenlet()
            mark_hub_running(hub_g)
            _run_acceptor(listen_fd, handler)

        _framework_core.hub_run(listen_fd, _acceptor_with_mark)
    finally:
        mark_hub_stopped()
        sock.close()
```

### 5.5 `_serve_prefork`

```python
def _serve_prefork(
    handler: HandlerFunc,
    host: str,
    port: int,
    *,
    workers: int,
) -> None:
    """Multi-worker mode: fork N children, each with SO_REUSEPORT."""
    global _shutting_down
    _shutting_down = False

    _log(f"starting {workers} workers on {host}:{port}")

    # Install parent signal handlers
    original_sigint = signal.getsignal(signal.SIGINT)
    original_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _parent_signal_handler)
    signal.signal(signal.SIGTERM, _parent_signal_handler)

    children: dict[int, int] = {}  # pid → worker_id

    try:
        # Fork workers
        for worker_id in range(workers):
            pid = os.fork()
            if pid == 0:
                # Child process — restore default signals before _worker_main
                # sets its own handlers (avoids inheriting parent handlers)
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                _worker_main(worker_id, handler, host, port)
                os._exit(0)  # Should not reach here
            children[pid] = worker_id
            _log(f"worker {worker_id}: forked as pid {pid}")

        # Enter supervisor loop
        _supervisor_loop(
            children, handler, host, port, workers, SHUTDOWN_TIMEOUT
        )
    finally:
        # Restore original signal handlers
        signal.signal(signal.SIGINT, original_sigint)
        signal.signal(signal.SIGTERM, original_sigterm)
        _shutting_down = False
```

### 5.6 `_supervisor_loop`

```python
def _supervisor_loop(
    children: dict[int, int],
    handler: HandlerFunc,
    host: str,
    port: int,
    num_workers: int,
    shutdown_timeout: float,
) -> None:
    crash_history: dict[int, list[float]] = defaultdict(list)

    while children and not _shutting_down:
        try:
            pid, status = os.waitpid(-1, 0)
        except ChildProcessError:
            break
        except InterruptedError:
            if _shutting_down:
                break
            continue

        worker_id = children.pop(pid, None)
        if worker_id is None:
            continue

        if _shutting_down:
            continue

        # Analyze exit
        if os.WIFEXITED(status):
            exit_code = os.WEXITSTATUS(status)
            _log(f"worker {worker_id} (pid {pid}) exited with code {exit_code}")
        elif os.WIFSIGNALED(status):
            sig = os.WTERMSIG(status)
            _log(f"worker {worker_id} (pid {pid}) killed by signal {sig}")
        else:
            _log(f"worker {worker_id} (pid {pid}) exited with status {status}")

        # Crash loop detection
        now = time.monotonic()
        history = crash_history[worker_id]
        history[:] = [t for t in history if now - t < CRASH_WINDOW]
        history.append(now)

        if len(history) >= MAX_CRASHES_IN_WINDOW:
            _log(
                f"worker {worker_id}: crash loop detected "
                f"({MAX_CRASHES_IN_WINDOW} crashes in {CRASH_WINDOW}s), "
                f"not respawning",
            )
            continue

        # Respawn
        new_pid = os.fork()
        if new_pid == 0:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            _worker_main(worker_id, handler, host, port)
            os._exit(0)
        children[new_pid] = worker_id
        _log(f"worker {worker_id}: respawned as pid {new_pid}")

    _shutdown_children(children, shutdown_timeout)
```

### 5.7 `_shutdown_children`

```python
def _shutdown_children(children: dict[int, int], timeout: float) -> None:
    if not children:
        return

    _log(f"shutting down {len(children)} workers")

    # Send SIGTERM
    for pid in list(children):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            children.pop(pid, None)

    # Wait with timeout
    deadline = time.monotonic() + timeout
    while children and time.monotonic() < deadline:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            time.sleep(0.1)
            continue
        children.pop(pid, None)

    # SIGKILL stragglers
    if children:
        _log(f"killing {len(children)} unresponsive workers")
        for pid in list(children):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        children.clear()

    _log("all workers stopped")
```

---

## 6. Why No Zig Changes Are Needed

The Zig I/O layer is entirely worker-local and stateless with respect to process identity:

1. **Hub is a global singleton per process** (`global_hub`). After fork(), each child has its own
   copy, initialized to `null`. `hub_run()` creates a fresh Hub in the child. No conflict.

2. **io_uring ring is created inside Hub.init()**, which is called from `hub_run()`. Since
   `hub_run()` is called only in child processes (after fork), each child gets its own ring.
   No ring exists before fork. No ring inheritance issues.

3. **ConnectionPool, OpSlotTable** are fields of Hub. Created fresh in each child.

4. **fd_to_conn mapping** is an array inside Hub. Fresh per child.

5. **The stop_pipe** is created in Hub.init(). Each child gets its own stop pipe.

6. **greenlet state** is thread-local (per-process after fork). Each child has its own greenlet
   tree rooted at the hub greenlet.

7. **Monkey patching state** (`_saved`, `_hub_local`) is per-process after fork. The patched
   module references survive fork correctly — they point to the same cooperative wrappers that
   delegate to the child's own hub.

The signal wakeup mechanism uses only existing Zig APIs: `green_register_fd()`,
`green_recv()`, `green_unregister_fd()`, and `hub_stop()`. No new Zig functions are needed.

---

## 7. Edge Cases and Error Handling

### 7.1 SO_REUSEPORT Not Available

Kernel < 3.9. Not possible if io_uring works (requires ≥ 5.1). If it somehow happens,
`setsockopt` raises `OSError(ENOPROTOOPT)`, which propagates and is visible in the worker's
stderr. The worker exits, parent respawns it, it fails again, crash loop detection fires.

### 7.2 Port Already In Use

If another process holds the port (without SO_REUSEPORT), each child's `bind()` fails with
`EADDRINUSE`. Workers exit immediately. Parent sees all workers crash, crash loop detection
fires, parent exits.

### 7.3 fork() Failure

`os.fork()` raises `OSError` on failure (e.g., `ENOMEM`, `EAGAIN`). In the initial fork loop,
this propagates to `_serve_prefork`, which logs the error and shuts down any already-forked
children. In the respawn path, it's caught and logged; the worker is not respawned.

### 7.4 Worker Exits Cleanly (exit code 0)

Hub loop exit conditions: `active_waits == 0 and ready queue empty`. This normally shouldn't
happen in a running server (the acceptor greenlet keeps the loop alive). But if it does, it's
treated as a crash and the worker is respawned.

### 7.5 Monkey Patching and fork()

`patch_all()` must be called before `serve()` / `app.run()`. After fork, each child inherits
the patched modules. This is correct:
- Patched `socket.socket` → cooperative wrapper → delegates to child's Hub
- Patched `time.sleep` → cooperative sleep → delegates to child's Hub
- Hub detection (`hub_is_running()`) → thread-local → per-process after fork

If `patch_all()` is called after fork (in the child), it works too — each child patches its own
interpreter state.

### 7.6 Application State After fork()

Application-level resources (database connections, file handles, caches) are duplicated on
fork. The user's handler function gets a **copy** of any global state. This is a known
characteristic of prefork servers.

For resources that don't survive fork (database connections with server-side state, open
cursors), the user must re-initialize them in the handler or use a lazy initialization
pattern. This is the same as gunicorn's `post_fork` hook.

For the initial implementation, we don't add a `post_fork` hook. It's documented as a future
enhancement if user demand arises.

---

## 8. Testing Strategy

### 8.1 Unit Tests

**Test: `_create_listen_socket` with SO_REUSEPORT**
- Create two sockets on the same port with SO_REUSEPORT.
- Verify both bind successfully.
- Verify connections are distributed (connect N times, check both sockets get connections).
- Cleanup: close both sockets.

**Test: crash loop detection logic**
- Simulate crash timestamps within the window.
- Verify detection fires at the threshold.
- Verify old crashes outside the window are discarded.

### 8.2 Integration Tests

These tests run on Linux only (io_uring requirement).

**Test: multi-worker basic functionality**
```python
def test_multiworker_serves_requests():
    """Start 4 workers, send 100 requests, verify all succeed."""
    # Start server in subprocess with workers=4
    # Send 100 concurrent HTTP requests
    # Verify all get 200 OK responses
    # Send SIGTERM to parent, verify clean exit
```

**Test: worker crash and respawn**
```python
def test_worker_respawn_on_crash():
    """Kill one worker, verify it's respawned."""
    # Start server with workers=2
    # Identify worker PIDs (from ps or by querying)
    # Kill one worker with SIGKILL
    # Wait briefly
    # Send requests — verify they still succeed (other worker + respawn)
    # Cleanup
```

**Test: graceful shutdown**
```python
def test_graceful_shutdown():
    """Send SIGTERM to parent, verify all workers exit."""
    # Start server with workers=4
    # Verify it's accepting connections
    # Send SIGTERM to parent
    # Wait for parent to exit
    # Verify exit code 0
    # Verify no zombie processes
```

**Test: SIGINT (Ctrl+C) shutdown**
```python
def test_sigint_shutdown():
    """Send SIGINT to parent process group."""
    # Start server with workers=2
    # Send SIGINT to the process group
    # Verify all processes exit
```

**Test: single-worker backward compatibility**
```python
def test_single_worker_unchanged():
    """workers=1 uses current code path (no fork)."""
    # Start server with workers=1 (default)
    # Verify requests work
    # Verify only 1 process (no children)
```

**Test: SO_REUSEPORT distribution**
```python
def test_connections_distributed_across_workers():
    """Verify kernel distributes connections across workers."""
    # Start server with workers=4
    # Send 1000 requests from diverse source ports
    # (Each worker could log its PID in a response header for verification)
    # Verify multiple workers handled requests
```

### 8.3 Stress Tests (Manual)

- **Sustained load**: `wrk -t8 -c256 -d60s http://127.0.0.1:8000/hello` with workers=4.
  Monitor for errors, measure throughput vs. single-worker.
- **Worker kill under load**: While load test runs, `kill -9` a random worker every 5s.
  Verify no sustained errors beyond the killed connections.
- **Shutdown under load**: Send SIGTERM while load test runs. Verify parent exits within
  SHUTDOWN_TIMEOUT.

---

## 9. Implementation Order

### Phase 1: Core Multi-Worker (MVP)

1. Add `_create_listen_socket()` with SO_REUSEPORT support
2. Extract `_serve_single()` from current `serve()` (preserves backward compat)
3. Implement `_worker_main()` with signal pipe and signal watcher greenlet
4. Implement `_serve_prefork()` with fork loop
5. Implement basic `_supervisor_loop()` with waitpid and crash respawn
6. Implement `_shutdown_children()` with SIGTERM → timeout → SIGKILL
7. Wire up signal handlers (`_parent_signal_handler`)
8. Update `serve()` to dispatch based on workers count
9. Update `Framework.run()` to accept `workers` parameter
10. Add basic logging (`_log()`)

### Phase 2: Testing

11. Write integration tests for multi-worker mode
12. Write integration tests for signal handling
13. Write integration tests for crash recovery
14. Verify single-worker backward compatibility with existing test suite
15. Manual stress testing

### Phase 3: Polish

16. Add `workers=0` auto-detection (os.cpu_count())
17. Add startup logging (PID, worker count, bind address)
18. Review error messages and edge case handling
19. Update examples to show multi-worker usage

---

## 10. Future Enhancements (Not In This Plan)

- **Graceful connection draining**: On shutdown, stop accepting but let in-flight requests
  finish before exiting. Requires a "stop accepting" mechanism in the hub (close listen socket
  from greenlet, let active connections drain).
- **Rolling restart**: Start new workers before stopping old ones, for zero-downtime deploys.
- **post_fork hook**: User callback that runs in each child after fork, for re-initializing
  resources (database connections, etc.).
- **Per-worker metrics**: Each worker tracks request count, latency, error rate. Parent
  aggregates via pipes or shared memory.
- **Worker process title**: `setproctitle` to show worker ID in `ps` output.
- **Configurable shutdown timeout**: Expose SHUTDOWN_TIMEOUT as a parameter.
- **Configurable crash loop thresholds**: Expose CRASH_WINDOW and MAX_CRASHES_IN_WINDOW.
- **PID file**: Write parent PID to a file for process management.
- **Unix domain socket support**: SO_REUSEPORT works with Unix sockets too.
- **Preload / application factory**: Load app module before fork for copy-on-write memory
  sharing. Provide a factory function for post-fork initialization.
- **Load imbalance detection**: SO_REUSEPORT distributes new connections fairly via 4-tuple
  hash, but long-lived connections (websockets, streaming, slow clients) can create persistent
  imbalance across workers. Monitor per-worker connection counts and expose metrics; eventually
  consider a mechanism to shed load from overloaded workers.
- **CPU affinity**: Pin each worker to a specific core via `os.sched_setaffinity(0, {worker_id})`
  in `_worker_main` for improved cache locality — keeps io_uring CQEs, connection state, and
  Python objects in the same L1/L2 cache. Inspired by thread-per-core architectures (Seastar,
  Apache Iggy) where core pinning reduces cross-core cache thrashing under load.
