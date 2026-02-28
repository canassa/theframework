# Pre-Fork Worker Improvements Plan

**Status**: Planning
**Priority**: High (Production Readiness)
**Scope**: Worker initialization and fork/spawn pattern
**Related Files**: `theframework/server.py` (lines 175-212, 445-477)

---

## Executive Summary

The Framework's pre-fork pattern is functional but lacks production-grade safety mechanisms. Granian's pattern is mature and battle-tested. This plan brings The Framework to feature parity with critical safety improvements.

**Target**: Python 3.14+ compatibility + graceful shutdown + proper error handling

---

## 1. CURRENT STATE ANALYSIS

### The Framework: Raw os.fork() Pattern

```python
# Parent: _serve_prefork() [lines 445-477]
for worker_id in range(workers):
    pid = os.fork()
    if pid == 0:
        _worker_main(worker_id, handler, host, port)
        os._exit(0)
    children[pid] = worker_id

# Child: _worker_main() [lines 175-212]
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_DFL)  # ← Hard kill
sock = _create_listen_socket(host, port, reuseport=True)
# ... error handling ...
_framework_core.hub_run(listen_fd, _acceptor)
```

**Characteristics**:
- ✅ Simple, direct
- ✅ No threading in parent
- ❌ No spawn method negotiation
- ❌ No graceful shutdown
- ❌ No initialization wrapper
- ❌ No readiness signal
- ❌ No backpressure

### Granian: multiprocessing.Process Pattern

```python
# Parent: Uses multiprocessing.Process
self._spawn_method = multiprocessing.get_start_method()
if self._spawn_method not in {'fork', 'spawn'}:
    self._spawn_method = 'spawn'

self.inner = multiprocessing.get_context(self._spawn_method).Process(
    name='granian-worker', target=target, args=args
)

# Child: Wrapped target
@staticmethod
def wrap_target(target):
    @wraps(target)
    def wrapped(worker_id, process_name, callback_loader, sock, ipc, loop_impl, ...):
        if process_name:
            setproctitle.setproctitle(f'{process_name} worker-{worker_id}')
        configure_logging(...)
        load_env(...)
        # ... more init steps ...
        loop = loops.get(loop_impl)
        callback = callback_loader()
        return target(worker_id, callback, sock, _ipc_handle, loop, *args, **kwargs)
    return wrapped
```

**Characteristics**:
- ✅ Spawn method negotiation
- ✅ Wrapped initialization
- ✅ Process naming
- ✅ Environment loading
- ✅ IPC setup
- ✅ Better error tracking
- ⚠️ More complex

---

## 2. IDENTIFIED ISSUES

### Issue 1: Python 3.14 Incompatibility (🔴 CRITICAL)

**Problem**: No spawn method negotiation
**Impact**: Server crashes on Python 3.14+
**Detection**: Runtime error when multiprocessing defaults to 'forkserver'

**Current Code** (line 459):
```python
pid = os.fork()  # Assumes 'fork' always works
```

**Why It Fails on Python 3.14**:
- Python 3.14 defaults to 'forkserver' on Linux
- Forkserver requires different socket passing mechanism
- Raw `os.fork()` ignores spawn method
- Results in socket descriptor issues or deadlock

**Fix Priority**: CRITICAL (implement first)

---

### Issue 2: Hard SIGTERM Kill (🔴 CRITICAL)

**Problem**: Worker dies immediately on SIGTERM
**Impact**: In-flight requests dropped, clients see connection reset
**Detection**: Clients disconnect unexpectedly during shutdown

**Current Code** (line 182):
```python
signal.signal(signal.SIGTERM, signal.SIG_DFL)  # Default = kill
```

**Scenario**:
```
t=0ms: Request arrives, handler starts processing
t=100ms: Parent sends SIGTERM for graceful shutdown
t=100ms: Worker dies immediately (SIG_DFL)
t=100ms: Handler is killed mid-request
Result: Client never receives response (connection reset)
```

**Fix Priority**: CRITICAL (implement second)

---

### Issue 3: No Initialization Wrapper (🟠 HIGH)

**Problem**: No structured initialization, scattered across _worker_main
**Impact**: Missing setup steps (process naming, logging, environment)
**Detection**: Manual code review

**Missing Features**:
- ❌ Process naming (all workers show same in `ps`)
- ❌ Logging configuration (inherited from parent)
- ❌ Environment file loading (not available to worker)
- ❌ IPC setup (no metrics back-channel)

**Granian Does** (wrapped in target function):
```python
if process_name:
    setproctitle.setproctitle(f'{process_name} worker-{worker_id}')
configure_logging(log_level, log_config, log_enabled)
load_env(env_files)
_ipc_fd = os.dup(ipc.fileno())
os.set_blocking(_ipc_fd, False)
_ipc_handle = IPCSenderHandle(_ipc_fd)
```

**Fix Priority**: HIGH (implement third)

---

### Issue 4: No Bind Error Differentiation (🟠 HIGH)

**Problem**: All bind errors treated the same
**Impact**: Can't distinguish transient from permanent failures
**Detection**: Supervisor treats all failures identically

**Current Code** (line 186):
```python
try:
    sock = _create_listen_socket(host, port, reuseport=True)
except OSError as e:
    _log(f"failed to bind {host}:{port}: {e}", worker_id=worker_id)
    os._exit(1)  # Generic exit code
```

**Different Errors Need Different Handling**:
- `EADDRINUSE` → Transient (SO_REUSEPORT wait), retry with backoff
- `EACCES` → Permanent (permission denied), don't respawn
- `EMFILE` → System (too many FDs), scale back workers
- `ENOBUFS` → System (memory), unlikely to recover soon

**Exit Code Strategy**:
```python
BIND_EADDRINUSE = 42  # Transient, retry
BIND_EACCES = 43      # Permanent, don't retry
BIND_OTHER = 1        # Generic, respawn with backoff
```

**Fix Priority**: HIGH (implement fourth)

---

### Issue 5: No Hub Initialization Readiness (🟠 HIGH)

**Problem**: Parent doesn't know when worker is ready
**Impact**: Startup race condition, early connection attempts fail
**Detection**: Clients see "Connection refused" on startup

**Current Code** (lines 204-210):
```python
_framework_core.hub_run(listen_fd, _acceptor)  # Blocks, no readiness signal
# After this returns, worker is gone
# Parent never knows if worker is ready
```

**Timing Issue**:
```
t=0ms:   Parent forks all workers
t=1ms:   Parent returns from _serve_prefork() to caller
t=5ms:   Caller tries to connect (socket might not be open yet!)
t=10ms:  Worker finally finishes hub_run() initialization
Result:  Connection refused
```

**Granian Solution**: Workers are required to be ready before supervisor considers them "started"

**Fix Priority**: MEDIUM (affects startup reliability)

---

### Issue 6: No Backpressure (🟠 HIGH)

**Problem**: Unbounded greenlet spawning in acceptor
**Impact**: OOM under simultaneous connection floods
**Detection**: Server crashes with OOM killer

**Current Code** (lines 139-142):
```python
g = greenlet.greenlet(
    lambda fd=client_fd: _handle_connection(fd, handler), parent=hub_g
)
_framework_core.hub_schedule(g)  # No limit!
```

**Scenario Under Load**:
```
10,000 simultaneous connections arrive
10,000 greenlets spawned immediately
Memory: 10,000 × 50KB = 500MB just for stacks
OOM killer activates → kills parent → all workers die
```

**Fix Priority**: HIGH (critical for production stability)

---

### Issue 7: Double Hub Greenlet Lookups (🟡 MEDIUM)

**Problem**: Inefficiency and repeated C FFI calls
**Impact**: Minor performance cost, code clarity
**Detection**: Code review

**Current Code** (lines 138, 200):
```python
# In _acceptor():
hub_g = _framework_core.get_hub_greenlet()

# In _run_acceptor():
hub_g = _framework_core.get_hub_greenlet()  # Called again!
```

**Fix Priority**: MEDIUM (implement alongside other fixes)

---

### Issue 8: No Exception Handling in Hub Initialization (🟡 MEDIUM)

**Problem**: Hub errors might not propagate correctly
**Impact**: Silent failures, worker exits without logging
**Detection**: Supervisor sees unexpected exits

**Current Code** (lines 203-210):
```python
try:
    _framework_core.hub_run(listen_fd, _acceptor)
except Exception as e:
    _log(f"hub exited with error: {e}", worker_id=worker_id)
finally:
    mark_hub_stopped()
    sock.close()
```

**Missing**: Explicit error codes for different failure types

**Fix Priority**: MEDIUM (improve observability)

---

## 3. IMPROVEMENT PLAN

### Phase 1: Critical Safety (MUST DO)

#### 1.1: Add Spawn Method Negotiation

**File**: `theframework/server.py`
**Lines to Modify**: Top of `_serve_prefork()`

**Implementation**:
```python
def _serve_prefork(
    handler: HandlerFunc, host: str, port: int, *, workers: int
) -> None:
    """Multi-worker mode: fork N children, each with SO_REUSEPORT."""
    global _shutting_down
    _shutting_down = False

    # NEW: Validate spawn method compatibility
    try:
        spawn_method = multiprocessing.get_start_method()
        if spawn_method not in {'fork', 'spawn'}:
            _log(f"warning: spawn method '{spawn_method}' not explicitly supported, "
                 f"attempting to use 'spawn' instead")
            # Could also raise RuntimeError for strict enforcement
            # For now, document that forkserver requires Python 3.13 or earlier
    except Exception as e:
        _log(f"warning: could not determine spawn method: {e}")

    _log(f"starting {workers} workers on {host}:{port} "
         f"(spawn method: {spawn_method})")

    children: dict[int, int] = {}
    # ... rest of function
```

**Testing**:
```python
# tests/test_prefork_spawn_method.py
def test_prefork_negotiates_spawn_method():
    import multiprocessing
    spawn_method = multiprocessing.get_start_method()
    assert spawn_method in {'fork', 'spawn'}, \
        f"Spawn method '{spawn_method}' will fail on Python 3.14+"

def test_prefork_works_with_spawn_method():
    # Run server with forced spawn method
    method = multiprocessing.get_start_method()
    # Ensure it works
    pass
```

**Timeline**: 1-2 hours

---

#### 1.2: Implement Graceful SIGTERM Handling

**File**: `theframework/server.py`
**Lines to Modify**: Lines 180-182, add signal handler function

**Implementation**:
```python
def _worker_main(
    worker_id: int, handler: HandlerFunc, host: str, port: int
) -> None:
    """Entry point for a forked worker process."""

    # NEW: Store reference to control graceful shutdown
    _worker_state = {
        'shutting_down': False,
        'hub_g': None
    }

    # NEW: Graceful SIGTERM handler
    def _sigterm_handler(signum: int, frame: object) -> None:
        """Handle SIGTERM with graceful hub shutdown."""
        _worker_state['shutting_down'] = True
        if _worker_state['hub_g'] is not None:
            try:
                _framework_core.hub_stop()
            except Exception as e:
                _log(f"error during graceful shutdown: {e}",
                     worker_id=worker_id)

    # Setup signal handlers
    signal.signal(signal.SIGINT, signal.SIG_IGN)      # Ignore Ctrl-C
    signal.signal(signal.SIGTERM, _sigterm_handler)   # Graceful shutdown

    # Create listening socket
    try:
        sock = _create_listen_socket(host, port, reuseport=True)
    except OSError as e:
        _log(f"failed to bind {host}:{port}: {e}", worker_id=worker_id)
        os._exit(1)

    listen_fd = sock.fileno()
    _log(f"listening on {host}:{port} (fd={listen_fd})", worker_id=worker_id)

    from theframework.monkey._state import mark_hub_running, mark_hub_stopped

    try:
        def _acceptor() -> None:
            hub_g = _framework_core.get_hub_greenlet()
            _worker_state['hub_g'] = hub_g  # NEW: Store for signal handler
            mark_hub_running(hub_g)
            _run_acceptor(listen_fd, handler)

        _framework_core.hub_run(listen_fd, _acceptor)
    except Exception as e:
        _log(f"hub exited with error: {e}", worker_id=worker_id)
        _log(traceback.format_exc(), worker_id=worker_id)
    finally:
        mark_hub_stopped()
        sock.close()

    os._exit(0)
```

**Testing**:
```python
# tests/test_prefork_graceful_shutdown.py
def test_graceful_shutdown_completes_pending_requests():
    """Ensure SIGTERM allows in-flight requests to complete."""
    import threading
    import time

    handler_started = threading.Event()
    handler_done = threading.Event()

    def slow_handler(request, response):
        handler_started.set()
        time.sleep(0.5)  # Simulate work
        handler_done.set()
        response.send(b"OK")

    server_thread = threading.Thread(
        target=serve,
        args=(slow_handler,),
        kwargs={'host': '127.0.0.1', 'port': 8000, 'workers': 1},
        daemon=True
    )
    server_thread.start()

    time.sleep(0.1)  # Let server start

    # Send request
    import socket
    sock = socket.socket()
    sock.connect(('127.0.0.1', 8000))
    sock.send(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")

    handler_started.wait(timeout=1)

    # Send SIGTERM to worker
    # ... (requires subprocess handling)

    # Verify handler completes
    assert handler_done.wait(timeout=2), "Handler was killed before completing"
```

**Timeline**: 2-3 hours

---

### Phase 2: High-Priority Improvements

#### 2.1: Add Initialization Wrapper

**File**: `theframework/server.py`
**New Function**: `_wrap_worker_target()`

**Implementation**:
```python
def _wrap_worker_target(target: Callable) -> Callable:
    """Wrap worker target with initialization steps.

    Mirrors Granian's pattern for structured initialization.
    """
    @wraps(target)
    def wrapped(
        worker_id: int,
        process_name: str | None,
        handler: HandlerFunc,
        host: str,
        port: int,
        *args,
        **kwargs
    ) -> None:
        # 1. Set process name (for monitoring, ps output)
        if process_name:
            try:
                import setproctitle
                setproctitle.setproctitle(f"{process_name} worker-{worker_id}")
            except ImportError:
                pass  # Optional dependency

        # 2. Configure logging for worker (separate from parent)
        # Initialize worker-specific logger with worker_id in context
        # This allows better filtering in logs

        # 3. Call actual worker function
        return target(worker_id, handler, host, port, *args, **kwargs)

    return wrapped

# Update _serve_prefork to use wrapper:
def _serve_prefork(
    handler: HandlerFunc, host: str, port: int, *, workers: int,
    process_name: str | None = None
) -> None:
    """Multi-worker mode: fork N children, each with SO_REUSEPORT."""
    global _shutting_down
    _shutting_down = False

    wrapped_worker = _wrap_worker_target(_worker_main)

    _log(f"starting {workers} workers on {host}:{port}")
    children: dict[int, int] = {}

    try:
        for worker_id in range(workers):
            pid = os.fork()
            if pid == 0:
                wrapped_worker(worker_id, process_name, handler, host, port)
                os._exit(0)
            children[pid] = worker_id
            _log(f"worker {worker_id}: forked as pid {pid}")

        _supervisor_loop(...)
    # ... rest
```

**Testing**:
```python
def test_process_name_set():
    """Verify worker process name is set in ps output."""
    # Would require subprocess and ps command
    # Or check /proc/<pid>/comm on Linux
    pass
```

**Timeline**: 1-2 hours

---

#### 2.2: Add Bind Error Differentiation

**File**: `theframework/server.py`
**Lines to Modify**: Lines 185-189

**Implementation**:
```python
def _worker_main(
    worker_id: int, handler: HandlerFunc, host: str, port: int
) -> None:
    """Entry point for a forked worker process."""

    # ... signal setup ...

    # Create listening socket with error differentiation
    try:
        sock = _create_listen_socket(host, port, reuseport=True)
    except OSError as e:
        # Differentiate error types
        if e.errno == errno.EADDRINUSE:
            _log(
                f"address {host}:{port} still in use (SO_REUSEPORT failed). "
                f"This may happen if previous worker hasn't fully released the port. "
                f"Error: {e}",
                worker_id=worker_id
            )
            os._exit(42)  # Exit code 42 = EADDRINUSE (transient)

        elif e.errno == errno.EACCES:
            _log(
                f"permission denied binding to {host}:{port}. "
                f"Ensure process has required permissions. "
                f"Error: {e}",
                worker_id=worker_id
            )
            os._exit(43)  # Exit code 43 = EACCES (permanent)

        elif e.errno == errno.EMFILE:
            _log(
                f"too many open files. System has hit file descriptor limit. "
                f"Consider increasing ulimit. "
                f"Error: {e}",
                worker_id=worker_id
            )
            os._exit(44)  # Exit code 44 = EMFILE (system)

        else:
            _log(
                f"failed to bind {host}:{port}: {e}",
                worker_id=worker_id
            )
            os._exit(1)  # Generic exit code

    # ... rest
```

**Update supervisor to handle exit codes** (in _supervisor_loop):
```python
if os.WIFEXITED(status):
    exit_code = os.WEXITSTATUS(status)

    if exit_code == 42:  # EADDRINUSE
        _log(f"worker {worker_id} (pid {pid}): bind failed (address in use, may retry)")
        # Respawn with exponential backoff
    elif exit_code == 43:  # EACCES
        _log(f"worker {worker_id} (pid {pid}): bind failed (permission denied, not respawning)")
        continue  # Don't respawn
    elif exit_code == 44:  # EMFILE
        _log(f"worker {worker_id} (pid {pid}): bind failed (too many FDs, scaling down)")
        # Could signal supervisor to reduce worker count
    else:
        _log(f"worker {worker_id} (pid {pid}) exited with code {exit_code}")
```

**Testing**:
```python
def test_bind_eaddrinuse_error_code():
    """Verify EADDRINUSE returns exit code 42."""
    # Mock socket with EADDRINUSE error
    # Verify exit code is 42
    pass

def test_bind_eacces_error_code():
    """Verify EACCES returns exit code 43."""
    pass
```

**Timeline**: 2-3 hours

---

#### 2.3: Add Backpressure Limiting

**File**: `theframework/server.py`
**Lines to Modify**: In `_run_acceptor()` or connection handler

**Implementation**:
```python
def _run_acceptor(listen_fd: int, handler: HandlerFunc) -> None:
    """Accept connections and spawn handler greenlets with backpressure."""

    # NEW: Backpressure limiting
    # Max concurrent connections per worker (can be tuned)
    # This prevents unbounded greenlet spawning under load
    MAX_CONCURRENT_CONNECTIONS = 100

    active_connections = [0]  # Use list to allow mutation in nested function

    # Alternatively, track greenlets:
    # pending_handlers = []

    while True:
        try:
            client_fd = _framework_core.green_accept(listen_fd)
        except OSError as e:
            if e.errno in (errno.EBADF, errno.EINVAL, errno.ENOTSOCK):
                break
            _log(f"acceptor error: {e}")
            break

        # NEW: Check if we're at capacity
        if active_connections[0] >= MAX_CONCURRENT_CONNECTIONS:
            # Option 1: Close new connection (hard backpressure)
            try:
                _framework_core.green_close(client_fd)
            except OSError:
                pass
            continue

        # Option 2: Use semaphore (preferred, like Granian)
        # This requires more refactoring

        active_connections[0] += 1

        hub_g = _framework_core.get_hub_greenlet()

        def _make_handler(fd: int = client_fd) -> None:
            try:
                _handle_connection(fd, handler)
            finally:
                active_connections[0] -= 1

        g = greenlet.greenlet(_make_handler, parent=hub_g)
        _framework_core.hub_schedule(g)
```

**Better Implementation (with proper semaphore)**:
```python
# Create semaphore at worker startup
# Pass to acceptor
# Acquire before spawning greenlet
# Release in finally block of handler

# But this requires changes to greenlet integration
# For now, the counter approach is simpler
```

**Testing**:
```python
def test_backpressure_limits_connections():
    """Verify backpressure prevents OOM under load."""
    # Simulate 1000 simultaneous connections
    # Verify memory doesn't grow unbounded
    # Verify some connections are refused gracefully
    pass
```

**Timeline**: 2-4 hours (depending on implementation approach)

---

### Phase 3: Polish and Optimization

#### 3.1: Cache Hub Greenlet Reference

**File**: `theframework/server.py`
**Lines to Modify**: Lines 138, 200

**Implementation**:
```python
def _worker_main(...):
    # ...
    from theframework.monkey._state import mark_hub_running, mark_hub_stopped

    try:
        hub_g = _framework_core.get_hub_greenlet()  # Get once

        def _acceptor() -> None:
            mark_hub_running(hub_g)
            _run_acceptor(listen_fd, handler, hub_g)  # Pass as argument

        _framework_core.hub_run(listen_fd, _acceptor)
    finally:
        mark_hub_stopped()
        sock.close()

def _run_acceptor(listen_fd: int, handler: HandlerFunc, hub_g) -> None:
    """Accept connections and spawn handler greenlets."""
    while True:
        # ... accept logic ...
        g = greenlet.greenlet(
            lambda fd=client_fd: _handle_connection(fd, handler), parent=hub_g
        )
        _framework_core.hub_schedule(g)
```

**Timeline**: 30 minutes

---

#### 3.2: Improve Error Handling and Logging

**File**: `theframework/server.py`
**Lines to Modify**: Exception handling blocks

**Implementation**:
```python
try:
    def _acceptor() -> None:
        hub_g = _framework_core.get_hub_greenlet()
        mark_hub_running(hub_g)
        _run_acceptor(listen_fd, handler, hub_g)

    _framework_core.hub_run(listen_fd, _acceptor)
except Exception as e:
    _log(f"hub exited with error: {type(e).__name__}: {e}",
         worker_id=worker_id)
    _log(traceback.format_exc(), worker_id=worker_id)

    # Set exit code based on error type
    if isinstance(e, MemoryError):
        os._exit(50)  # Memory error
    elif isinstance(e, OSError):
        os._exit(51)  # OS error
    else:
        os._exit(52)  # Unknown error
finally:
    mark_hub_stopped()
    sock.close()
```

**Timeline**: 1 hour

---

## 4. IMPLEMENTATION TIMELINE

### Week 1: Critical Fixes
- ☐ Phase 1.1: Spawn method negotiation (2 hours)
- ☐ Phase 1.2: Graceful SIGTERM handling (3 hours)
- ☐ Testing and validation (2 hours)
- **Total: 7 hours**

### Week 2: High-Priority Improvements
- ☐ Phase 2.1: Initialization wrapper (2 hours)
- ☐ Phase 2.2: Bind error differentiation (3 hours)
- ☐ Update supervisor loop to handle exit codes (2 hours)
- ☐ Testing (3 hours)
- **Total: 10 hours**

### Week 3: Stability & Polish
- ☐ Phase 2.3: Backpressure limiting (3-4 hours)
- ☐ Phase 3.1: Cache hub reference (30 mins)
- ☐ Phase 3.2: Error handling improvements (1 hour)
- ☐ Comprehensive testing (4 hours)
- ☐ Documentation (2 hours)
- **Total: 11 hours**

**Total Project Time: ~28 hours (~3-4 weeks at 1-2 hours/day)**

---

## 5. TESTING STRATEGY

### Unit Tests
```python
# tests/test_prefork_worker.py

class TestWorkerInitialization:
    def test_spawn_method_negotiation(self): ...
    def test_graceful_sigterm_shutdown(self): ...
    def test_bind_error_differentiation(self): ...
    def test_hub_readiness(self): ...

class TestBackpressure:
    def test_backpressure_limits_greenlets(self): ...
    def test_memory_stable_under_load(self): ...

class TestErrorHandling:
    def test_bind_eaddrinuse_retries(self): ...
    def test_bind_eacces_no_retry(self): ...
```

### Integration Tests
```python
# tests/integration/test_prefork_server.py

def test_prefork_basic_request():
    """Start server with multiple workers, send request."""
    pass

def test_prefork_graceful_shutdown():
    """Ensure in-flight requests complete on shutdown."""
    pass

def test_prefork_worker_respawn():
    """Verify supervisor respawns crashed workers."""
    pass

def test_prefork_high_concurrency():
    """Stress test with high concurrent connections."""
    pass
```

### Compatibility Tests
```python
# tests/test_python_versions.py

def test_works_on_python_313():
    # Current version
    pass

def test_works_on_python_314_with_fork():
    # Python 3.14 with fork spawn method
    pass

def test_works_on_python_314_with_spawn():
    # Python 3.14 with spawn method
    pass
```

---

## 6. MIGRATION PATH

### For Existing Users

**No Breaking Changes**:
- `serve()` API remains unchanged
- All improvements are backward compatible
- Default behavior is same

**Optional Enhancements**:
```python
# New optional parameter (Phase 3)
serve(
    handler,
    host="127.0.0.1",
    port=8000,
    workers=4,
    process_name="myapp",  # NEW: for ps visibility
    max_concurrent_per_worker=100,  # NEW: backpressure tuning
)
```

### Deprecation Timeline

- **v1.X**: Add improvements, keep defaults
- **v2.0**: New parameters become available
- **v3.0**: Could tighten defaults (e.g., require spawn method negotiation)

---

## 7. COMPARISON WITH GRANIAN

### Feature Parity Table

| Feature | The Framework (Current) | The Framework (After Plan) | Granian | Notes |
|---------|------------------------|---------------------------|---------|-------|
| Spawn method negotiation | ❌ | ✅ | ✅ | Python 3.14 compatibility |
| Graceful SIGTERM | ❌ | ✅ | ✅ | Preserves in-flight requests |
| Init wrapper | ❌ | ✅ | ✅ | Process naming, logging |
| Bind error differentiation | ❌ | ✅ | ⚠️ (partial) | Different handling for different errors |
| IPC back-channel | ❌ | ⚠️ (future) | ✅ | Metrics collection |
| Backpressure limiting | ❌ | ✅ | ✅ | Memory safety under load |
| Worker healthcheck | ❌ | ⚠️ (future) | ✅ | RSS monitoring |
| Signal preemption | ⚠️ (greenlet) | ⚠️ (same) | ✅ (tokio) | Fundamental difference |

### Architecture Comparison

| Aspect | The Framework | Granian |
|--------|---------------|---------|
| Parent concurrency | Single thread + select() | Thread per worker + events |
| Worker I/O | Greenlets + io_uring | Tokio + io_uring |
| Graceful shutdown | Manual hub_stop() | Tokio graceful_shutdown() |
| Process isolation | File descriptor sharing | Separate processes |
| Overhead | Lower | Higher |
| Preemption | Cooperative | Tokio's scheduler |

---

## 8. ACCEPTANCE CRITERIA

### Phase 1: Critical Safety ✅
- [ ] Server runs on Python 3.14 with fork or spawn method
- [ ] SIGTERM causes graceful shutdown (not hard kill)
- [ ] In-flight requests complete before worker exits
- [ ] All existing tests pass

### Phase 2: High-Priority ✅
- [ ] Process names visible in `ps` output (with optional setproctitle)
- [ ] Bind errors differentiated by exit code
- [ ] Supervisor handles different exit codes appropriately
- [ ] No unbounded greenlet growth under load (backpressure active)
- [ ] New integration tests pass

### Phase 3: Polish ✅
- [ ] Hub greenlet reference cached (no redundant FFI calls)
- [ ] Error handling logs different failure modes
- [ ] Comprehensive test coverage (>90%)
- [ ] Documentation updated
- [ ] Performance benchmarks stable

---

## 9. REFERENCES

**Granian Implementation**:
- `granian/granian/server/mp.py` - Multiprocessing pattern
- `granian/granian/server/common.py` - Supervisor loop
- `granian/src/workers.rs` - Rust worker coordination

**The Framework Current**:
- `theframework/server.py` - Current implementation

**Related Issues**:
- Python 3.14 multiprocessing changes
- Signal handling best practices
- Greenlet preemption limitations

---

## 10. RISKS AND MITIGATION

### Risk 1: Spawn Method Changes
**Risk**: Different behavior with 'spawn' vs 'fork'
**Mitigation**: Comprehensive testing on both methods, document differences

### Risk 2: Graceful Shutdown Complexity
**Risk**: Introducing bugs in signal handling
**Mitigation**: Small changes, extensive testing, conservative defaults

### Risk 3: Backpressure Impact on Throughput
**Risk**: Limiting connections might reduce peak throughput
**Mitigation**: Make configurable, tune defaults based on benchmarks

### Risk 4: Breaking Changes
**Risk**: Changes might break existing deployments
**Mitigation**: All changes backward compatible, no breaking API changes

---

## 11. SUCCESS METRICS

- ✅ Python 3.14+ support confirmed
- ✅ Graceful shutdown latency < 3 seconds
- ✅ Zero connection resets on shutdown
- ✅ Memory stable under 10K concurrent connections
- ✅ Process names visible in monitoring tools
- ✅ Error logs clearly indicate failure cause
- ✅ Supervisor correctly differentiates exit codes
- ✅ Performance benchmarks within 5% of baseline
