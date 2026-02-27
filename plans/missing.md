# Missing Features: theframework vs Granian

Gap analysis for making theframework production-grade. Since theframework is designed to run **behind a reverse proxy** (nginx/caddy), many features are out of scope: HTTP/2, TLS/mTLS, ALPN, static file serving, slowloris protection, request size limits (nginx `client_max_body_size`), `Date` header (nginx adds it), `Expect: 100-continue` (nginx strips it), chunked request decoding (nginx de-chunks), `Connection: close` header management (nginx handles both sides), IPv6 (backend listens on localhost), and keep-alive idle timeout (nginx manages its upstream pool).

---

## Critical (Must-Have for Production)

### 1. Graceful Shutdown

**Granian**: Full two-phase shutdown chain. `SIGINT`/`SIGTERM` → stop accepting → `graceful_shutdown()` on all active connections → drain in-flight requests → wait for all handlers to complete → cleanup. Kill timeout with `SIGKILL` fallback. Connections are notified via `Notify::notify_waiters()`, and each connection finishes its current request before closing.

**theframework**: `hub_stop()` writes a byte to a pipe, which stops the hub loop. But there is **no draining of in-flight requests**. When the hub loop exits, active greenlets are simply abandoned — their connections are closed mid-request. The `_handle_connection` greenlets have no way to know shutdown is happening.

**What's missing**:
- Signal handlers (`SIGINT`, `SIGTERM`, `SIGHUP`)
- Stop accepting new connections while finishing in-flight ones
- Per-connection shutdown notification (equivalent to granian's `connsig.notify_waiters()`)
- Configurable drain timeout before force-killing connections
- Clean greenlet teardown during shutdown

### 2. Error Handling in Request Handlers

**Granian**: Python exceptions in ASGI/WSGI callbacks are caught, logged with full tracebacks (`log_application_callable_exception`), and converted to 500 responses. The connection is never corrupted — the client always gets a valid HTTP response.

**theframework**: In `server.py:_handle_connection`, the handler is called bare — `handler(request, response)`. If the handler raises an exception, it propagates up to the `except OSError: pass` block which only catches OS errors, or crashes the greenlet entirely. The client gets **no response** (connection just drops), and the exception may be silently swallowed.

**What's missing**:
- `try/except` around handler invocation
- Return a proper `500 Internal Server Error` response on exception
- Log the exception with traceback
- Ensure the connection is always properly closed after an error

### 3. Logging

**Granian**: Structured logging via Python `logging`, configurable log levels, access logging with format string (`[time] addr - "method path protocol" status duration_ms`), application exception logging with tracebacks, `pyo3_log` bridge for Rust-side logging.

**theframework**: No logging at all. No access logging, no error logging, no startup messages.

**What's missing**:
- Access logging (method, path, status, duration)
- Error logging (handler exceptions, connection errors)
- Startup logging (listening address, configuration)
- Configurable log level and format
- Integration with Python `logging` module

---

## Important (Needed for Reliability)

### 4. Multi-Worker / Multi-Process

**Granian**: `MPServer` with configurable worker count, each running in its own process. Workers are forked/spawned, each with its own event loop. Automatic crash recovery with respawn. Crash loop detection (5.5s window). Configurable `workers_kill_timeout`. RSS memory monitoring with multi-sample threshold before respawn.

**theframework**: Single-process, single-threaded. One hub, one event loop. If the process crashes, the server is down. No way to utilize multiple CPU cores.

**What's missing**:
- Multi-process worker model (fork/spawn)
- Worker crash detection and automatic respawn
- Crash loop detection
- Worker lifetime TTL (periodic respawn for leak prevention)
- RSS memory monitoring with threshold-based respawn
- Configurable worker count

### 5. Connection Error Resilience

**Granian**: Failed TCP handshakes and TLS negotiations are logged and don't leak semaphore permits. Connection errors are caught at every level with proper cleanup. The `conn_handle_*` macros always release permits and notify guards even on error paths.

**theframework**: In `_handle_connection`, the only error handling is `except OSError: pass` in the outer try/except. But several failure modes aren't covered:
- What if `http_parse_request` returns `ValueError` (invalid HTTP)?
- What if `response._finalize()` raises?
- What if the greenlet is killed during a `green_recv` or `green_send`?

The `finally: green_close(fd)` block is good, but the connection pool slot may not be properly released in all error paths (the fd_to_conn mapping stays, the generation isn't bumped until the Zig-side close completes).

**What's missing**:
- Catch all exception types in connection handler, not just `OSError`
- Send `400 Bad Request` for malformed HTTP
- Ensure connection pool cleanup on all error paths
- Handle `ValueError` from HTTP parser gracefully

### 6. Response Streaming / Chunked Transfer Encoding

**Granian**: Full streaming support. ASGI `http.response.body` with `more_body=True` uses chunked transfer encoding via `mpsc::UnboundedSender<Bytes>`. SSE (Server-Sent Events) fast path detects `text/event-stream` and immediately starts streaming. Also supports `http.response.pathsend` for file responses.

**theframework**: The `Response` object buffers everything in memory (`_body_parts` list) and sends it all at once in `_finalize()`. The Zig layer has `writeHead`, `writeChunk`, and `writeEnd` functions for chunked encoding, but they're **not exposed to Python**. There's no streaming response API.

**What's missing**:
- Streaming response API (write headers, then stream body chunks)
- Chunked Transfer-Encoding support exposed to Python
- SSE (Server-Sent Events) support
- File response streaming (sendfile equivalent)

### 7. Backpressure / Connection Limiting

**Granian**: Semaphore-based connection limiting per worker (`backpressure = backlog / workers`). Permits are acquired before `accept()`, so the accept loop blocks when at capacity. This prevents unbounded memory growth under load. Also has TCP listen backlog (default 1024).

**theframework**: The accept loop in `_run_acceptor` accepts connections as fast as the kernel delivers them, immediately spawning a greenlet for each. The connection pool is 4096 slots, but there's no backpressure mechanism — when the pool is exhausted, `pool.acquire()` returns null and the accepted fd leaks (comment in code: "If pool exhausted, the fd still works but won't be tracked"). No limit on how many greenlets can be simultaneously active.

**Note**: nginx's `max_conns` per upstream is the primary gate here. This is defense-in-depth.

**What's missing**:
- Connection concurrency limit (semaphore or counter)
- Backpressure: stop accepting when at capacity
- Configurable max concurrent connections
- Proper handling when connection pool is exhausted (reject/close rather than leak)

### 8. Request Header Access in Zig Parser

**Granian**: Full header access — all headers are parsed and available to the protocol layer. Headers are used for connection management, content negotiation, WebSocket upgrades, etc.

**theframework**: The Zig HTTP parser (`http.zig`) parses headers into a 64-header array, but `pyHttpParseRequest` only returns `(method, path, body, consumed, keep_alive)`. Headers are then **re-parsed in Python** from the raw bytes (`request.py:_from_raw`). This means:
1. Headers are parsed twice (once in Zig, once in Python)
2. The Python re-parsing is fragile (splits on `b"\r\n\r\n"` then `b": "`)
3. Duplicate headers are silently dropped (dict overwrites)

**What's missing**:
- Pass parsed headers from Zig to Python directly (avoid double parsing)
- Support for duplicate headers (e.g., `Set-Cookie` can appear multiple times)
- Header value validation

---

## Nice-to-Have (Production Polish)

### 9. WebSocket Support

**Granian**: Full WebSocket RFC 6455 support with handshake validation, `Sec-WebSocket-Key`/`Version` checks, accept key derivation, message framing (text/binary/close/ping/pong), sub-protocol negotiation.

**theframework**: No WebSocket support.

**What's missing**:
- WebSocket upgrade detection
- Handshake negotiation
- Message framing (text, binary, close, ping, pong)
- Python API for WebSocket handlers

### 10. Metrics / Observability

**Granian**: 16 Prometheus metrics (connections active/handled/error, requests handled, blocking pool stats, GIL wait time, worker lifecycle). Lock-free atomics, separate HTTP exporter endpoint.

**theframework**: No metrics. No way to observe connection count, request rate, error rate, or pool utilization from outside.

**What's missing**:
- Connection pool utilization metric
- Request count / rate
- Error count
- Response time histogram or average
- Prometheus-compatible endpoint (or StatsD, etc.)

### 11. Hot Reload

**Granian**: File watching via `watchfiles` (Rust-based), configurable paths/patterns/filters, SIGHUP for rolling reload, environment files reloaded on change.

**theframework**: No reload support. Server must be restarted to pick up code changes.

**What's missing**:
- File watcher for development mode
- SIGHUP handler for graceful reload (in multi-worker mode)

### 12. Configuration System

**Granian**: 60+ configuration options via CLI (Click), environment variables, programmatic API. Validates configuration at startup (workers vs cores warnings, invalid combinations, etc.).

**theframework**: Hardcoded values. `serve()` takes `host` and `port` only. Connection pool is 4096 (hardcoded). Backlog is 128 (hardcoded). Recv buffer is 8192 (hardcoded). No CLI.

**What's missing**:
- Configurable backlog
- Configurable connection pool size
- Configurable recv buffer size
- Configurable timeouts
- CLI or configuration file support

### 13. Lifecycle Hooks

**Granian**: Three lifecycle hooks: `on_startup`, `on_reload`, `on_shutdown`. Allow users to run setup/teardown code.

**theframework**: No lifecycle hooks. No way to run code on startup (e.g., initialize DB connection pool) or shutdown (e.g., flush caches, close connections).

**What's missing**:
- `on_startup` hook (run before accepting connections)
- `on_shutdown` hook (run after draining connections)

### 14. PID File

**Granian**: PID file management with stale process detection, automatic cleanup.

**theframework**: No PID file support. Typically handled by systemd or similar process supervisor.

### 15. io_uring Zero-Copy Receive (ZC RX)

**Kernel status**: Merged in **Linux 6.15** (March 2025). Documented at [kernel.org/networking/iou-zcrx](https://docs.kernel.org/networking/iou-zcrx.html). Patchset by David Wei (Meta) and Pavel Begunkov.

**What it does**: DMA-direct network receive into userspace memory, bypassing the kernel copy entirely. Packet headers stay in kernel memory (processed by TCP stack normally), payloads land directly in user-registered pages. Demonstrated **saturating a 200Gbps link on a single CPU core** (188 Gbps measured).

**Performance**: On AMD EPYC + Broadcom 200G NIC: +41% throughput (82→116 Gbps different cores), +29% (62→80 Gbps same core).

**theframework**: Currently uses `IORING_OP_RECV` with provided buffer rings. The kernel still copies packet data into the provided buffers. No ZC RX support.

**What's needed to adopt**:
- **Hardware**: NIC must support header/data split, flow steering, and RSS (datacenter NICs: Broadcom P5, Mellanox ConnectX, Google gve)
- **Kernel**: Linux 6.15+
- **Ring setup**: Add `IORING_SETUP_SINGLE_ISSUER` + `IORING_SETUP_DEFER_TASKRUN` + `IORING_SETUP_CQE32` (or `IORING_SETUP_CQE_MIXED`)
- **New registration**: `io_uring_register_ifq()` via `IORING_REGISTER_ZCRX_IFQ` (opcode 32)
- **New opcode**: Replace `IORING_OP_RECV` with `IORING_OP_RECV_ZC` on the receive path
- **Refill queue**: Userspace must return consumed buffers to kernel via the refill mechanism
- **Out-of-band NIC config**: Flow steering rules and queue setup via ethtool (not yet kernel-API-controlled)

**Limitations**:
- Buffer sizes are fixed-length physically contiguous chunks — max buffer per CQE is constrained
- No strict guarantees on chunk sizes returned (varies by traffic pattern, HW offload, etc.)
- NIC configuration must be done out-of-band via ethtool
- Only supported on specific drivers (bnxt initially, mlx5/gve planned)

**Recommendation**: Implement as an **optional, feature-gated path**. Detect NIC capability at startup, use ZC RX when available, fall back to current `IORING_OP_RECV` + provided buffer rings otherwise. This is a datacenter/cloud optimization — the current approach is already efficient for typical deployments.

**Reference**: [lore.kernel.org thread](https://lore.kernel.org/io-uring/ZwW7_cRr_UpbEC-X@LQ3V64L9R2/T/), [Phoronix coverage](https://www.phoronix.com/news/Linux-6.15-IO_uring), [liburing patches](https://patchwork.kernel.org/project/io-uring/cover/20250215041857.2108684-1-dw@davidwei.uk/)

---

## Summary Priority Matrix

| Priority | Feature | Effort | Impact |
|----------|---------|--------|--------|
| **P0** | Graceful shutdown | Medium | Prevents data loss on deploy |
| **P0** | Handler error catching → 500 | Low | Prevents silent connection drops |
| **P0** | Logging | Low | Can't operate what you can't see |
| **P1** | Multi-worker | High | CPU utilization, crash resilience |
| **P1** | Connection error resilience | Low | Prevents connection leaks |
| **P1** | Response streaming | Medium | SSE, large responses |
| **P1** | Backpressure / connection limit | Medium | Defense-in-depth (nginx is primary gate) |
| **P1** | Pass headers from Zig (no double parse) | Medium | Performance, correctness |
| **P2** | WebSocket | High | Real-time features |
| **P2** | Metrics | Medium | Production observability |
| **P2** | Hot reload | Medium | Developer experience |
| **P2** | Configuration system | Medium | Operational flexibility |
| **P2** | Lifecycle hooks | Low | App initialization/teardown |
| **P2** | PID file | Trivial | Process management |
| **P3** | io_uring ZC RX | High | Eliminate last recv copy (datacenter NIC required, kernel 6.15+) |

---
---

# Implementation Quality: theframework vs Production-Grade Standards

Deep analysis of the **quality** of existing code — bugs, performance problems, and correctness
issues in what's already implemented. This is separate from missing features above.

Since theframework runs **behind a reverse proxy** (nginx/caddy), some issues are mitigated but
noted where they still matter.

---

## P0 — Bugs / Will Bite You in Production

### Q1. Request buffer accumulation is O(n²)

**File:** `server.py:19-26`

```python
buf = b""
while True:
    data = _framework_core.green_recv(fd, 8192)
    buf += data  # ← creates a NEW bytes object every iteration
```

Python `bytes` is immutable. `buf += data` copies the entire existing buffer plus the new data
into a fresh allocation every time. A 1MB POST body arriving in 128 recv calls copies ~64MB total.

**Granian** uses hyper's streaming body with `Arc<AsyncMutex<BodyStream>>` — zero accumulation
copies.

**Fix:** Use `bytearray` or `list[bytes]` with a single `b"".join()` at parse time.

### Q2. Pool exhaustion silently loses connections (fd leak)

**File:** `hub.zig:378-385`

```zig
if (self.pool.acquire()) |conn| {
    conn.fd = new_fd;
    conn.state = .idle;
    self.fd_to_conn[new_fd_usize] = conn.pool_index;
}
// If pool exhausted, the fd still works but won't be tracked
```

When all 4096 pool slots are used, accepted fds are **not tracked**. `greenClose` won't find
them, `greenRecv`/`greenSend` won't find them. The fd leaks until process exit.

**Granian** uses a semaphore to stop accepting before the pool can exhaust.

**Fix:** When pool is exhausted, close the accepted fd immediately and log a warning. Better:
implement backpressure (stop accepting when pool is near capacity).

### Q3. No request size limit

**File:** `server.py:19-26`

The `buf += data` loop accumulates indefinitely. Even behind nginx (`client_max_body_size`), a
misconfigured proxy or a pipelined sequence of large headers could exhaust memory. There's no cap
on the buffer size.

**Fix:** Add a configurable max request size (e.g., 1MB default). Return 413 and close if
exceeded.

### Q4. Double close on keep-alive=false path

**File:** `server.py:42-48`

```python
if not keep_alive:
    _framework_core.green_close(fd)  # ← close here
    return
# ...
finally:
    _framework_core.green_close(fd)  # ← and also close here
```

When `keep_alive` is False, `green_close` is called, then execution falls through to the `finally`
block which calls `green_close` **again** on the same fd. The Zig side handles an unknown fd
gracefully (falls through to `IGNORE_SENTINEL` close), but between the two closes the kernel might
have reused the fd number for a different connection — closing someone else's socket.

**Fix:** Remove the explicit close in the `if not keep_alive` branch and just `return` (the
`finally` block handles it). Or set a `closed` flag.

### Q5. Inconsistent connection state on greenlet switch failure

**File:** `hub.zig:441-468` (greenRecv)

```zig
conn.greenlet = current;
conn.state = .reading;
conn.pending_ops += 1;
self.active_waits += 1;
// ...
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) {
    // conn.state is still .reading, pending_ops still incremented
    // but greenlet is gone — the in-flight CQE will arrive later
    // and find conn.greenlet pointing to a dead/errored greenlet
    return null;
}
```

If the greenlet switch fails (e.g., exception in another greenlet), the connection is left in an
inconsistent state: `state = .reading`, `pending_ops = 1`, but the greenlet has errored out. When
the io_uring CQE eventually arrives, the hub will try to resume a greenlet that already failed.
The recv_buf cleanup is done but the state/pending_ops cleanup is not.

**Fix:** On switch failure, reset `conn.state = .idle`, decrement `conn.pending_ops` and
`self.active_waits`, and null out `conn.greenlet`. Same pattern needed in `greenSend`.

### Q6. fd_to_conn limit of 4096 is too low for production

**File:** `hub.zig:25, 378`

```zig
const MAX_FDS: usize = 4096;
// ...
if (new_fd_usize < MAX_FDS) { ... }
```

Linux fd numbers can exceed 4096 when the process has other fds open (database connections via
monkey-patched sockets, logging file handles, etc.). Connections with fd >= 4096 are silently
untracked — same as pool exhaustion but triggered by fd numbering.

**Fix:** Make MAX_FDS configurable, or use a HashMap instead of a fixed array. At minimum, close
and reject fds that can't be tracked.

---

## P1 — Performance / Correctness Issues

### Q7. Every recv allocates and frees a heap buffer

**File:** `hub.zig:415-497`

```zig
const buf = self.allocator.alloc(u8, max_bytes) catch { ... };
// ... submit recv, switch to hub, resume ...
const py_bytes = py.py_helper_bytes_from_string_and_size(...);
self.allocator.free(buf);
```

Every `green_recv` call: (1) heap-allocates max_bytes (8192), (2) io_uring fills it, (3) copies
into a Python bytes object, (4) frees the buffer. That's **2 allocations + 1 copy per recv**. For
a keep-alive connection handling 1000 requests, that's 2000+ malloc/free calls just for recv
buffers.

**Granian** avoids this with hyper's internal buffer management and zero-copy `Bytes` types.

**Fix:** Use a per-connection reusable recv buffer, or use io_uring buffer groups
(`initBufferGroup` is already implemented in `ring.zig` but unused).

### Q8. Response building is pure Python string concatenation

**File:** `response.py:55-75`

```python
parts: list[str] = [f"HTTP/1.1 {self.status} {reason}\r\n"]
for name, value in self._headers.items():
    parts.append(f"{name}: {value}\r\n")
parts.append("\r\n")
raw = "".join(parts).encode("latin-1") + body
```

This creates: N f-strings, a list, a joined string, a latin-1 encoded bytes, then concatenates
with body. That's **5+ intermediate allocations** per response. The Zig-side
`http_response.writeResponse` exists and writes directly into a buffer, but it's only used by
`pyHttpFormatResponse` which is **never called** by the Python response path.

**Granian** builds responses entirely in Rust with `Response::builder()`.

**Fix:** Use the Zig response writer from Python, or at minimum use `bytearray` to build the
response in-place.

### Q9. Headers parsed twice (Zig then Python)

**File:** `hub.zig:1808-1821` → `request.py:48-55`

Zig's `parseRequestFull` parses all headers into a 64-header array, resolves keep-alive, then
**throws away all headers** and only returns `(method, path, body, consumed, keep_alive)`. Python
then re-parses the raw bytes:

```python
header_section, _, _ = raw_bytes.partition(b"\r\n\r\n")
lines = header_section.split(b"\r\n")
for line in lines[1:]:
    if b": " in line:
        name, _, value = line.partition(b": ")
```

This Python parsing is both slower and less correct than the Zig parser:
- Splits on `b": "` (with space) not `b":"` — headers without a space after the colon are dropped
- Duplicate headers are silently overwritten (dict)
- No validation of header names or values

### Q10. No TCP_NODELAY on accepted connections

**File:** `server.py:71-74`

```python
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
# ← no TCP_NODELAY
```

Neither the listen socket nor accepted connections set `TCP_NODELAY`. Nagle's algorithm will delay
small responses (like API JSON responses) by up to 40ms waiting to batch with subsequent data.

**Granian** enables `TCP_NODELAY` on all connections.

**Fix:** Set `TCP_NODELAY` on accepted connections. Trivial one-liner.

### Q11. io_uring ring depth is only 256

**File:** `hub.zig:55`

```zig
const ring = try Ring.init(256);
```

With 4096 possible connections, each potentially having a recv or send in flight, plus poll
operations (up to 256 op slots), the 256-entry SQ can fill up. When the SQ is full,
`ring.prepRecv()` etc. will fail, and the green function will raise a RuntimeError to Python.

**Granian** runs on tokio which manages its I/O driver ring size automatically.

**Fix:** Increase ring depth to at least 1024, or 4096 to match pool size.

### Q12. recv_into does a needless double copy

**File:** `_socket.py:146-151`

```python
data = _framework_core.green_recv(self.fileno(), size)  # alloc + copy in Zig
memoryview(buffer)[:n] = data  # copy again into target buffer
```

`recv_into` is supposed to write directly into the caller's buffer to avoid copies. Instead,
`green_recv` allocates a Zig buffer → copies to Python bytes → Python copies into the target
buffer. Three memory copies total for what should be one.

**Fix:** Add a `green_recv_into` that takes a buffer object and writes directly into it.

### Q13. No SO_REUSEPORT

**File:** `server.py:72`

Only `SO_REUSEADDR` is set. When multi-worker support is added, `SO_REUSEPORT` will be needed for
kernel-level load balancing across workers.

**Granian** sets it on Linux/FreeBSD.

---

## P2 — Code Quality / Robustness

### Q14. Connection pool slot can leak permanently

**File:** `connection.zig:127-129`

```zig
self.free_list.append(self.allocator, conn.pool_index) catch {
    // If this fails, we leak a slot. Acceptable for now.
};
```

If the ArrayList growth fails, the pool slot is permanently lost. Same issue in
`op_slot.zig:124`. The free_list is pre-allocated to POOL_SIZE in `init()`, and since releases
can never exceed acquires, the list capacity is always sufficient.

**Fix:** Use `appendAssumeCapacity` instead of `append` — the capacity is guaranteed.

### Q15. Single-threaded accept — no multishot

**File:** `hub.zig:334-388`

Each `greenAccept` submits one `ACCEPT` SQE, switches to hub, gets one CQE, returns one fd. Under
burst conditions (many connections arriving simultaneously), this serializes acceptance to
one-at-a-time. `ring.zig` already has `prepAcceptMultishot` but it's unused.

**Granian** uses tokio's accept which batches efficiently.

**Fix:** Use multishot accept to handle bursts without per-connection SQE submission.

### Q16. Python header dict drops duplicate headers

**File:** `request.py:55`

```python
headers[name.decode("latin-1").lower()] = value.decode("latin-1")
```

HTTP allows multiple headers with the same name (e.g., `Set-Cookie`, `Via`, `X-Forwarded-For`).
Using a dict silently overwrites earlier values. This is a correctness issue.

**Fix:** Use a list of tuples or a multidict for headers.

### Q17. pyHttpFormatResponse has a 64KB response limit

**File:** `hub.zig:1903`

```zig
var buf: [65536]u8 = undefined;
```

Not currently used by the Python response path (which builds its own bytes), but if anyone calls
`http_format_response()` directly, responses > 64KB will fail with a RuntimeError.

**Fix:** Dynamic allocation, or document the limit clearly.

### Q18. Hub is a global singleton — one per process

**File:** `hub.zig:1353`

```zig
var global_hub: ?*Hub = null;
```

Only one hub per process. This prevents multi-threaded use (even for testing) and means
multi-worker requires multi-process.

**Granian** supports per-worker runtimes (either per-process or per-thread).

### Q19. No Connection header in HTTP responses

**File:** `response.py`

The response never sends a `Connection: keep-alive` or `Connection: close` header back. While
HTTP/1.1 defaults to keep-alive, explicit headers help proxies make correct decisions.

**Fix:** Set `Connection: keep-alive` or `Connection: close` based on the request.

### Q20. PollGroup stack allocation is fragile

**File:** `hub.zig:1108`

```zig
var group = op_slot.PollGroup{
    .resumed = false,
    .greenlet = current,
    .active_waits_n = @intCast(total_slots),
};
```

This is **correct** because greenlet preserves the C stack while suspended. But it's fragile — if
the greenlet implementation ever changes to stackless or heap-switching, this becomes a
use-after-free. Needs a clear comment explaining the safety invariant.

### Q21. CQE processing loop runs entirely with GIL held

**File:** `hub.zig:186-197`

```zig
const saved = py.py_helper_save_thread();
const sw_result = self.ring.submitAndWait(1);
py.py_helper_restore_thread(saved);
```

GIL is released only during `submitAndWait`. The entire CQE processing loop (up to 256 CQEs) and
the ready-queue draining (all greenlet switches) run with GIL held. This blocks DNS resolver
threads (from `_dns.py`) and any other Python threads.

**Granian** uses tokio's async runtime which never holds the GIL during I/O processing.

### Q22. Hardcoded listen backlog of 128

**File:** `server.py:74`

```python
sock.listen(128)
```

128 is the minimum reasonable value. Under burst traffic with connection queuing, this will cause
SYN drops at the kernel level.

**Granian** defaults to 1024 and makes it configurable.

**Fix:** Default to 1024, make configurable.

---

## What's Actually Good (Solid Implementation)

The existing implementation has several well-designed patterns worth preserving:

1. **Generation-based stale CQE detection** — Elegant solution for the async cancel/reuse race
   condition. Every connection and op slot has a generation counter; CQEs with mismatched
   generations are silently discarded. Well-tested with unit tests.

2. **Proper Python refcounting in Zig** — Every `incref` has a matching `decref`, with proper
   cleanup in error paths and `deinit`. The `hub.deinit()` walks all structures to decref
   remaining greenlets.

3. **GIL release during io_uring wait** — The most important optimization: `py_helper_save_thread`
   / `py_helper_restore_thread` around `submitAndWait`. Other Python threads can run while waiting
   for I/O.

4. **errdefer chains in Hub.init** — Proper partial-initialization cleanup if any step fails.
   Ring, pool, and op_slots are all cleaned up correctly.

5. **Cancel-before-close pattern** — `greenClose` correctly cancels all pending ops on a
   connection before submitting the close SQE. This prevents CQEs arriving for a closed fd.

6. **Comprehensive monkey patching** — Socket, select, selectors, DNS, SSL, and time.sleep are
   all cooperatively patched with proper hub-detection fallbacks. Non-hub code paths fall through
   to stdlib.

7. **Thread-safe stop signaling** — Pipe-based stop signal (`stop_pipe`) is correct and race-free,
   usable from any thread.

8. **Partial send loop** — `greenSend` correctly handles short writes by looping until all bytes
   are sent, with proper greenlet switching for each partial send.

9. **Connection state machine** — `idle → reading/writing → cancelling → closing` is a clean
   model with proper transitions and CQE handling for each state.

10. **io_uring buffer group support** — Already implemented in `ring.zig` (with tests) even though
    unused. Ready for the recv buffer optimization (Q7).

11. **parseRequestFull** — Avoids the dangling-header-slice problem by resolving keep-alive while
    the header array is still on the stack.

12. **Timeout enforcement** — `greenPollFdTimeout` uses io_uring's `IOSQE_IO_LINK` +
    `LINK_TIMEOUT` for kernel-enforced timeouts, avoiding userspace timer management.

---

## Quality Issues Priority Matrix

| # | Issue | Severity | Effort | Impact |
|---|-------|----------|--------|--------|
| Q1 | O(n²) request buffer accumulation | **P0** | Low | Memory blowup on large requests |
| Q2 | Pool exhaustion → fd leak | **P0** | Low | Connection leak under load |
| Q3 | No request size limit | **P0** | Low | OOM under attack/misconfiguration |
| Q4 | Double close on keep-alive=false | **P0** | Trivial | Potential fd reuse race |
| Q5 | Inconsistent state on switch failure | **P0** | Medium | Potential double-resume crash |
| Q6 | MAX_FDS=4096 fixed array | **P1** | Medium | Untracked fds with many open files |
| Q7 | Alloc/free per recv | **P1** | Medium | Allocation pressure under load |
| Q8 | Python-side response building | **P1** | Medium | Unnecessary allocations per response |
| Q9 | Headers parsed twice | **P1** | Medium | CPU waste, correctness gap |
| Q10 | No TCP_NODELAY | **P1** | Trivial | Up to 40ms latency on small responses |
| Q11 | Ring depth 256 | **P1** | Trivial | SQE failures under high concurrency |
| Q12 | recv_into double copy | **P1** | Medium | Extra copy on every recv_into call |
| Q13 | No SO_REUSEPORT | **P2** | Trivial | Needed for future multi-worker |
| Q14 | Pool slot leak on alloc failure | **P2** | Trivial | Use appendAssumeCapacity |
| Q15 | No multishot accept | **P2** | Medium | Accept throughput under burst |
| Q16 | Duplicate headers dropped | **P2** | Low | HTTP compliance |
| Q17 | 64KB response buffer limit | **P2** | Low | Affects Zig-side response path |
| Q18 | Global singleton hub | **P2** | High | Testing, multi-worker |
| Q19 | No Connection header in response | **P2** | Trivial | Proxy cooperation |
| Q20 | Stack-allocated PollGroup fragility | **P2** | Trivial | Comment for safety |
| Q21 | GIL held during CQE processing | **P2** | High | Blocks DNS/other threads |
| Q22 | Hardcoded backlog 128 | **P2** | Trivial | Burst handling |
