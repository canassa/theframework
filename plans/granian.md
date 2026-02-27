# Granian: Deep Technical Analysis

## What is Granian?

Granian is a **high-performance HTTP server written in Rust** designed to serve Python web applications. Created by Giovanni Barillari (emmett-framework), it is a production-ready (v2.7.2) replacement for the traditional Gunicorn + Uvicorn stack. The core HTTP engine is built on **Tokio** (async runtime) and **Hyper** (HTTP implementation), with **PyO3** bridging Rust and Python.

**License:** BSD-3-Clause
**Production users:** Paperless-ngx, Reflex, SearXNG, Weblate, Microsoft, Mozilla, Sentry

---

## Project Structure

```
granian/
├── granian/                  # Python package (47 .py files)
│   ├── __init__.py           # Public API: __version__, loops, Granian
│   ├── __main__.py           # `python -m granian` entry point
│   ├── cli.py                # Click-based CLI with 100+ configuration options
│   ├── constants.py          # Enums: Interfaces, HTTPModes, RuntimeModes, Loops, TaskImpl, SSLProtocols
│   ├── http.py               # HTTP1Settings / HTTP2Settings dataclasses
│   ├── log.py                # LogLevels, configure_logging(), access log formatting
│   ├── errors.py             # FatalError, ConfigurationError, PidFileError
│   ├── net.py                # SocketSpec, SocketHolder, UnixSocketSpec (pickling support)
│   ├── asgi.py               # ASGI 3.0 lifespan + callback wrapper
│   ├── wsgi.py               # WSGI PEP 3333 wrapper (Response, ResponseIterWrap)
│   ├── rsgi.py               # RSGI protocol wrapper (custom Rust-optimized protocol)
│   ├── _types.py             # WebsocketMessage, SSLCtx type alias
│   ├── _compat.py            # Python version detection
│   ├── _futures.py           # _CBScheduler, _CBSchedulerTask (Rust↔asyncio bridge)
│   ├── _signals.py           # SIGINT/SIGTERM/SIGHUP signal handlers
│   ├── _loops.py             # Event loop registry (asyncio, uvloop, rloop, winloop)
│   ├── _imports.py           # Optional dependency imports (anyio, dotenv, etc.)
│   ├── _internal.py          # Module loading, target resolution, env file loading
│   ├── _granian.pyi          # Type stubs for the Rust-compiled _granian module
│   ├── server/
│   │   ├── __init__.py       # Conditional: MPServer (GIL) or MTServer (free-threaded)
│   │   ├── common.py         # AbstractServer + AbstractWorker base classes
│   │   ├── mp.py             # Multi-process server (GIL Python) – WorkerProcess
│   │   ├── mt.py             # Multi-thread server (free-threaded Python 3.13+)
│   │   └── embed.py          # Embeddable server (experimental async interface)
│   └── utils/
│       └── proxies.py        # Proxy header middleware (X-Forwarded-For/Proto)
├── src/                      # Rust source (41 .rs files)
│   ├── lib.rs                # Module root, global allocator, PyO3 module init
│   ├── serve.rs              # Worker serve macros (mt/st/fut × TCP/UDS)
│   ├── workers.rs            # Connection acceptance, HTTP builders, service dispatch
│   ├── runtime.rs            # Tokio runtime wrappers, future_into_py bridges
│   ├── callbacks.rs          # CallbackScheduler (Python coroutine execution engine)
│   ├── blocking.rs           # BlockingRunner (Python thread pool with GIL management)
│   ├── http.rs               # HTTPRequest/HTTPResponse type aliases, 404/500 builders
│   ├── files.rs              # Static file serving (path canonicalization, MIME, streaming)
│   ├── tls.rs                # TLS config (rustls, cert/key/CRL loading, ALPN)
│   ├── ws.rs                 # WebSocket upgrade (HyperWebsocket, UpgradeData)
│   ├── metrics.rs            # WorkerMetrics (atomics), MetricsAggregator, MetricsExporter
│   ├── ipc.rs                # Inter-process communication (unnamed pipes for metrics)
│   ├── net.rs                # SocketHolder, SockAddr (TCP/UDS), ListenerSpec
│   ├── asyncio.rs            # copy_context (Python context variable support)
│   ├── conversion.rs         # Rust↔Python type conversions, FutureResultToPy
│   ├── sys.rs                # ProcInfoCollector (RSS memory sampling)
│   ├── utils.rs              # header_contains_value, log_application_callable_exception
│   ├── asgi/                 # ASGI protocol implementation
│   │   ├── mod.rs            # Module definition + init_pymodule
│   │   ├── serve.rs          # ASGIWorker (PyO3 class, serve_mtr/serve_str methods)
│   │   ├── http.rs           # ASGI HTTP request handler (scope creation, callback invocation)
│   │   ├── io.rs             # ASGIHTTPProtocol + ASGIWebsocketProtocol
│   │   ├── callbacks.rs      # CallbackWatcher* (HTTP/WebSocket lifecycle managers)
│   │   ├── conversion.rs     # ASGI scope dict construction, message conversion
│   │   ├── types.rs          # ASGIMessageType enum
│   │   └── errors.rs         # UnsupportedASGIMessage, error_flow!, error_message! macros
│   ├── rsgi/                 # RSGI protocol (similar structure to ASGI)
│   └── wsgi/                 # WSGI protocol (callbacks, http, io, serve, types)
├── tests/                    # Comprehensive test suite
│   ├── test_asgi.py, test_wsgi.py, test_rsgi.py
│   ├── test_cli.py, test_embed.py, test_https.py
│   ├── test_static_files.py, test_ws.py, test_uds.py, test_sysmon.py
│   ├── apps/                 # Test ASGI/WSGI/RSGI applications
│   └── fixtures/             # TLS certs, static files
├── benchmarks/               # Extensive benchmarking suite vs Uvicorn/Gunicorn
├── Cargo.toml                # Rust manifest (hyper 1.8, tokio, pyo3, rustls, etc.)
├── pyproject.toml            # Python metadata (maturin build, Python 3.10+)
├── build.rs                  # PyO3 build configuration
└── docs/spec/RSGI.md         # RSGI protocol specification
```

---

## Core Architecture

### Language Split

The architecture is a **Rust core with a Python orchestration layer**:

```
┌─────────────────────────────────────────────────────────┐
│                  Python Layer                            │
│  AbstractServer → MPServer (processes) / MTServer (threads) │
│  Worker lifecycle, signal handling, configuration, CLI  │
├─────────────────────────────────────────────────────────┤
│                  PyO3 Bridge                             │
│  CallbackScheduler, ASGIWorker, RSGIWorker, WSGIWorker │
│  WorkerSignal, SocketHolder, MetricsAggregator          │
├─────────────────────────────────────────────────────────┤
│                  Rust Core                               │
│  Tokio runtime, Hyper HTTP, rustls TLS, tungstenite WS │
│  BlockingRunner, metrics collection, static files       │
└─────────────────────────────────────────────────────────┘
```

- **Rust** handles all I/O: TCP accept, TLS handshake, HTTP parsing, WebSocket framing, static file serving, response streaming. This is where performance comes from.
- **Python** handles orchestration: worker process management, signal handling, configuration, application callback loading, and the ASGI/WSGI/RSGI application itself.

### Code Organization

#### Rust Layer (`src/`)

| Module | File(s) | Responsibility |
|--------|---------|---------------|
| **Entry** | `lib.rs` | Global allocator, PyO3 module registration, BUILD_GIL flag |
| **Serve** | `serve.rs` | `serve_fn!` macro: generates MT/ST/FUT × TCP/UDS server functions |
| **Workers** | `workers.rs` | `Worker<C,A,H,F,M>` struct, connection acceptance loops, HTTP builders, `WorkerSvc` hyper service |
| **Runtime** | `runtime.rs` | `RuntimeWrapper` (tokio + blocking runner), `RuntimeRef` (cloneable handle), `future_into_py_*` bridges |
| **Callbacks** | `callbacks.rs` | `CallbackScheduler` (coroutine stepper), `PyIterAwaitable`, `PyFutureAwaitable`, `PyEmptyAwaitable` |
| **Blocking** | `blocking.rs` | `BlockingRunner` (Empty/Mono/Pool), Python thread pool with GIL release |
| **HTTP** | `http.rs` | Type aliases (`HTTPRequest`, `HTTPResponse`, `HTTPResponseBody`), response builders |
| **TLS** | `tls.rs` | Certificate/key/CRL loading, `ServerConfig` construction, ALPN configuration |
| **WebSocket** | `ws.rs` | `HyperWebsocket` future, `UpgradeData`, `is_upgrade_request()`, `upgrade_intent()` |
| **Files** | `files.rs` | `match_static_file()` (path canonicalization), `serve_static_file()` (streaming) |
| **Metrics** | `metrics.rs` | `WorkerMetrics` (atomics), `MetricsAggregator`, `MetricsExporter` (HTTP endpoint) |
| **IPC** | `ipc.rs` | `IPCSenderHandle`/`IPCReceiverHandle`, unnamed pipe message protocol |
| **Net** | `net.rs` | `SocketHolder`, `SockAddr` enum (TCP/UDS), listener construction |
| **Async** | `asyncio.rs` | `copy_context()` — Python context variable bridge |
| **Conversion** | `conversion.rs` | `FutureResultToPy`, HTTP config converters (Python dicts → Rust structs) |
| **System** | `sys.rs` | `ProcInfoCollector` — RSS memory sampling via `/proc` |
| **Utils** | `utils.rs` | `header_contains_value()`, `log_application_callable_exception()` |
| **ASGI** | `asgi/*.rs` | `ASGIHTTPProtocol`, `ASGIWebsocketProtocol`, scope construction, message conversion |
| **RSGI** | `rsgi/*.rs` | Direct Rust object protocol (lower overhead than ASGI) |
| **WSGI** | `wsgi/*.rs` | Synchronous protocol with blocking thread dispatch |

#### Python Layer (`granian/`)

| Module | Responsibility |
|--------|---------------|
| `server/common.py` | `AbstractServer` — startup/shutdown, worker spawn/respawn, reload, lifetime/RSS monitoring, hooks, PID file |
| `server/mp.py` | `MPServer` — multiprocessing-based workers, IPC pipe setup, RSS monitoring |
| `server/mt.py` | `MTServer` — threading-based workers for free-threaded Python |
| `server/embed.py` | Embeddable server returning asyncio Future |
| `cli.py` | Click CLI with all configuration options + environment variable support |
| `asgi.py` | ASGI lifespan protocol, callback wrapper with state/logging |
| `wsgi.py` | WSGI `start_response` handler, iterable response wrapper |
| `rsgi.py` | RSGI callback wrapper with logging |
| `_futures.py` | `_CBScheduler` — bridges Rust callbacks to Python asyncio |
| `_signals.py` | OS signal handlers (SIGINT, SIGTERM, SIGHUP, SIGBREAK on Windows) |
| `_loops.py` | Event loop registry (asyncio, uvloop, rloop, winloop) |
| `_internal.py` | Module/target loading, path resolution, env file loading |

### Worker Models

Granian supports three worker execution modes:

| Mode | Runtime | Use Case |
|------|---------|----------|
| **MT (Multi-threaded)** | Single process, tokio multi-threaded runtime | Default for ASGI/WSGI, HTTP/2, multi-thread configs |
| **ST (Single-threaded)** | N OS threads each with a single-threaded tokio runtime + `LocalSet` | Default for RSGI with 1 runtime thread |
| **FUT (Future)** | Returns a Python `asyncio.Future`, no blocking main thread | Embeddable server |

Worker selection happens automatically (`RuntimeModes.auto`) or can be forced. On GIL-enabled Python, workers are **processes** (`MPServer`). On free-threaded Python 3.13+, workers are **threads** (`MTServer`).

### Worker Spawn Architecture

#### Multi-Process Mode (GIL Python)

```
Main Process (MPServer)
├── Signal handlers (SIGINT, SIGTERM, SIGHUP)
├── Shared socket (SocketSpec → SocketHolder)
├── IPC pipes (one per worker, unnamed pipes)
├── Metrics exporter (separate TCP listener)
├── Watcher threads (one per worker, detect unexpected exit)
├── Lifetime watcher thread (optional)
├── RSS watcher thread (optional)
└── Worker Processes
    ├── Worker 1 (fork/spawn)
    │   ├── Configure logging
    │   ├── Load environment files
    │   ├── Load application target
    │   ├── Create event loop
    │   ├── Create Rust worker (ASGIWorker/RSGIWorker/WSGIWorker)
    │   ├── Create CallbackScheduler
    │   └── Enter serve loop (serve_mtr/serve_str)
    ├── Worker 2 ...
    └── Worker N ...
```

#### Single-Threaded Runtime Mode (ST)

Each worker process spawns N OS threads, each with its own tokio `current_thread` runtime and `LocalSet`:

```
Worker Process
├── Signal listener thread (watches for shutdown)
├── Metrics collector (IPC or local)
└── Runtime Threads
    ├── Thread 1: tokio LocalSet + Worker.listen()
    ├── Thread 2: tokio LocalSet + Worker.listen()
    └── Thread N: tokio LocalSet + Worker.listen()
```

#### Multi-Threaded Runtime Mode (MT)

Each worker process has a single tokio `multi_thread` runtime:

```
Worker Process
├── Tokio multi-thread runtime (N worker threads)
├── Blocking runner (Python thread pool)
├── Metrics collector
└── Worker.listen() on runtime
```

### Request Flow (End-to-End)

```
1. TCP/UDS Accept
   ├── Semaphore acquire (backpressure)
   ├── TLS handshake (if enabled, via rustls)
   └── Connection spawned as tokio task

2. HTTP Handling (hyper)
   ├── HTTP/1.1: conn_builder_h1 (keep-alive, header timeout, pipeline flush)
   ├── HTTP/2:   conn_builder_h2 (adaptive window, stream limits, keep-alive ping)
   └── Auto:     hyper_util::auto::Builder (ALPN negotiation)

3. Service Dispatch (WorkerSvc)
   ├── Static file check → serve_static_file() [if mounted]
   ├── Metrics increment (if enabled, atomic fetch_add)
   └── Protocol handler invocation

4. Python Callback (ASGI/RSGI/WSGI)
   ├── CallbackScheduler.schedule() → loop.call_soon_threadsafe
   ├── Coroutine execution via PyIter_Send (CPython 3.10+) or .send()
   ├── Future bridging: PyIterAwaitable (~55% faster) or PyFutureAwaitable (~38% faster)
   └── Response collected via oneshot::channel

5. Response Streaming
   ├── Single body: Full<Bytes> (non-chunked, ~4% faster)
   ├── Chunked/SSE: StreamBody over unbounded mpsc channel
   └── File: ReaderStream with 131KB buffer

6. Connection Teardown
   ├── Graceful shutdown via biased tokio::select!
   ├── Notify guard → disconnect signal to Python app
   └── Semaphore permit release (backpressure)
```

### Connection Acceptance Pipeline

The accept loop (`workers.rs:1044-1083`) is the core hot path:

```rust
while accept_loop {
    tokio::select! {
        biased;
        (permit, event) = async {
            let permit = semaphore.acquire_owned().await;  // ← backpressure
            (permit, listener.accept().await)               // ← accept connection
        } => {
            // Spawn connection handler as tokio task
            let disconnect_guard = Arc::new(Notify::new());
            let handle = self.handle(disconnect_guard.clone());
            let svc = WorkerSvc { ... };
            tasks.spawn(handle.call(svc, stream, permit, connsig));
        },
        _ = sig.changed() => {
            accept_loop = false;
            connsig.notify_waiters();  // ← signal all active connections
        }
    }
}
```

Key design: `biased` select means connections are processed before checking the signal. The semaphore ensures backpressure.

### Connection Handler Flow

Each connection handler (`conn_handle_h1_impl!`, `conn_handle_h2_impl!`, `conn_handle_ha_impl!`):

```rust
let conn = hyper::server::conn::http1::Builder::new()
    .header_read_timeout(opts.header_read_timeout)
    .keep_alive(opts.keep_alive)
    .serve_connection(stream, svc);
tokio::pin!(conn);

tokio::select! {
    biased;
    _ = conn.as_mut() => { done = true; },    // ← normal completion
    _ = sig.notified() => {                     // ← shutdown signal
        conn.as_mut().graceful_shutdown();       // ← drain in-flight requests
    }
}
if !done { _ = conn.as_mut().await; }          // ← wait for drain

self.guard.notify_one();                        // ← notify disconnect
drop(permit);                                   // ← release backpressure
```

---

## Protocol Implementations

### ASGI 3.0

The ASGI implementation lives in `src/asgi/`. Key structures:

**`ASGIHTTPProtocol`** (`src/asgi/io.rs:38-51`) manages the full HTTP lifecycle:
- `tx: oneshot::Sender<HTTPResponse>` — single-shot channel for the final response
- `request_body: Arc<AsyncMutex<BodyStream<Incoming>>>` — request body stream
- `response_started / response_chunked` — atomic flags tracking response state
- `body_tx: mpsc::UnboundedSender<Bytes>` — chunked response body channel
- `flow_rx_exhausted / flow_rx_closed` — flow control flags for disconnect detection
- `disconnect_guard: Arc<Notify>` — notified when client disconnects

**SSE (Server-Sent Events) optimization** (`src/asgi/io.rs:193-197`):
When `Content-Type: text/event-stream` is detected in `http.response.start`, the protocol immediately starts a chunked response instead of buffering. This avoids the deferred intent pattern and sends headers to the client instantly.

**Deferred Response Intent** (`src/asgi/io.rs:191-213`):
When an ASGI app sends `http.response.start`, the response headers are buffered. If the next message is `http.response.body` with `more_body=False`, a single `Full<Bytes>` response is sent. This avoids the overhead of chunked encoding for simple responses (~4% improvement).

**`ASGIWebsocketProtocol`** (`src/asgi/io.rs:344-358`) handles WebSocket lifecycle:
- Upgrade negotiation via `UpgradeData`
- Stream split into `WSRxStream` / `WSTxStream` (futures split)
- Atomic state tracking for accept/close/disconnect
- Sub-protocol negotiation via `Sec-WebSocket-Protocol` header

**Protocol compliance:**
- Lifespan protocol (startup/shutdown events)
- HTTP scope with method, path, query_string, headers, server, client
- WebSocket scope with upgrade negotiation
- `http.response.pathsend` extension for file responses
- State management between lifespan and request scopes

### RSGI (Rust Server Gateway Interface)

RSGI is a custom protocol (documented in `docs/spec/RSGI.md`) designed for tighter Rust-Python integration. It avoids the ASGI dictionary-based message passing, instead providing direct Rust objects to Python. This means less serialization overhead and more control over response streaming.

### WSGI

The WSGI implementation uses a synchronous model. The `BlockingRunner` manages a Python thread pool where WSGI callbacks execute. The `WorkerSignalSync` uses `crossbeam_channel::bounded(1)` for synchronous shutdown signaling instead of tokio watch channels.

**Protocol compliance (PEP 3333):**
- Standard CGI environment construction
- `start_response` callable with status and headers
- Iterable response body support with `close()` cleanup
- Synchronous execution via blocking thread pool

### WebSocket (RFC 6455)

- Proper handshake validation (key, version)
- Accept key derivation via `derive_accept_key()`
- Message types: text, binary, close, ping/pong
- Sub-protocol negotiation
- Close frame with reason code
- `Sec-WebSocket-Key` presence check, `Sec-WebSocket-Version` must be `"13"`
- Upgrade only on matching `Connection: Upgrade` + `Upgrade: websocket` headers

---

## Callback Scheduling Engine

The `CallbackScheduler` (`src/callbacks.rs`) is the heart of the Python integration. It implements a custom coroutine execution engine that is **~38-55% faster than pyo3_asyncio**:

### Two Awaitable Strategies

1. **`PyIterAwaitable`** (bare yield pattern):
   - Uses Python's iterator protocol (`__next__` returning `None` = "not ready")
   - ~55% faster than pyo3_asyncio for short operations
   - Higher CPU usage (busy-polling)

2. **`PyFutureAwaitable`** (asyncio.Future-like):
   - Implements `_asyncio_future_blocking`, `add_done_callback`, `cancel`, etc.
   - ~38% faster than pyo3_asyncio for long operations
   - Lower CPU overhead, proper event loop integration
   - Supports cancellation with `CancelledError`

### Coroutine Stepping

On CPython 3.10+, `CallbackScheduler::send()` uses the raw C API:
```rust
pyo3::ffi::PyIter_Send(state.coro.as_ptr(), self.pynone.as_ptr(), &raw mut pres);
```

This is faster than calling Python's `.send()` method. The scheduler handles:
- **Bare yield** (result is `None`): reschedule via `loop.call_soon`
- **Future blocking** (`_asyncio_future_blocking = True`): add waker callback
- **StopIteration**: coroutine completed
- **Exception**: propagate via `.throw()`

Context variables are preserved via `PyContext_Enter` / `PyContext_Exit` and `copy_context`.

### Freelist Optimization

```rust
#[pyclass(frozen, freelist = 128, module = "granian._granian")]
pub(crate) struct PyEmptyAwaitable;
```
PyO3 freelist of 128 pre-allocated `PyEmptyAwaitable` objects reduces allocation pressure.

---

## Blocking Thread Pool

The `BlockingRunner` (`src/blocking.rs`) manages Python threads separately from tokio's thread pool:

| Variant | Behavior |
|---------|----------|
| `Empty` | 0 threads, tasks silently dropped |
| `Mono` | 1 permanent thread, unbounded crossbeam queue |
| `Pool` | N threads with dynamic scaling and idle timeout |

### Dynamic Scaling Logic

```rust
let overload = self.queue.len() - threads;
if (overload > 0) && (threads < self.tmax) {
    self.spawn_thread(current_count); // compare_exchange for thread-safety
}
```

Threads exit after `idle_timeout` (default 30s, configurable 5-600s). Thread count uses `AtomicUsize` with `compare_exchange` to prevent races.

### GIL Management

Workers call `py.detach()` before blocking on queue receive, releasing the GIL. On GIL-disabled Python 3.13+, separate code paths skip GIL management entirely (compile-time `#[cfg(Py_GIL_DISABLED)]`).

### Metrics Instrumentation

With metrics enabled, every blocking worker tracks:
- `blocking_idle_cumul`: microseconds spent waiting for tasks
- `blocking_busy_cumul`: microseconds spent executing tasks
- `py_wait_cumul`: microseconds waiting for GIL acquisition (GIL builds only)

---

## Notification System

Granian uses a layered notification system built on tokio's synchronization primitives.

### 1. Shutdown Notification Chain

```
OS Signal (SIGINT/SIGTERM)
  → Python signal_handler_interrupt()
    → main_loop_interrupt.set() (threading.Event)
      → Worker.terminate() → multiprocessing terminate/kill
        → WorkerSignal.set() → tokio watch::channel tx.send(true)
          → sig.changed() in accept loop → stop accepting
            → connsig.notify_waiters() → all connections notified
              → conn.graceful_shutdown() → drain in-flight requests
                → tasks.wait() → wait for all handlers to complete
                  → mc_notify.notified() → wait for metrics collector
                    → drop(wrk) → cleanup
```

### 2. Connection Disconnect Notification

Each connection has a `disconnect_guard: Arc<tokio::sync::Notify>`:

```rust
// Created per-connection in acceptor_impl_stream!
let disconnect_guard = Arc::new(tokio::sync::Notify::new());

// Stored in WorkerSvc, passed to ASGI/RSGI protocol
let svc = WorkerSvc {
    disconnect_guard: disconnect_guard.clone(),
    ...
};

// Notified when connection handler exits
self.guard.notify_one(); // in conn_handle_*_impl!
```

In `ASGIHTTPProtocol::receive()`, the disconnect guard detects client disconnection:

```rust
tokio::select! {
    biased;
    frame = body.next() => { ... },                    // ← request body chunk
    () = guard_disconnect.notified() => {               // ← client disconnected
        disconnected.store(true, atomic::Ordering::Release);
        None
    }
}
```

This allows the Python ASGI application to receive `{"type": "http.disconnect"}` when the client drops the connection, even mid-request.

### 3. WebSocket Initialization Notification

WebSocket connections use a multi-phase notification:

```rust
// ASGIWebsocketProtocol fields
init_tx: Arc<AtomicBool>,        // set to true when WS accepted
init_event: Arc<Notify>,          // notified after accept
closed: Arc<AtomicBool>,          // set to true on close
```

Flow:
```
1. receive() called → returns {"type": "websocket.connect"}
2. send({"type": "websocket.accept"}) called
   → upgrade.send() → HTTP 101 response
   → websocket.await → WebSocketStream
   → split into tx/rx
   → init_tx.store(true)
   → init_event.notify_one()
3. receive() called again
   → if !accepted: init_event.notified().await  ← blocks until accept
   → ws.next() → receive WebSocket frame
```

### 4. Response Flow Notification

The `ASGIHTTPProtocol` uses several notification channels for the response lifecycle:

```rust
flow_rx_exhausted: Arc<AtomicBool>,  // request body fully consumed
flow_rx_closed: Arc<AtomicBool>,     // connection closed during read
flow_tx_waiter: Arc<Notify>,         // response body completed
```

The `flow_tx_waiter` is critical: it's notified when the final response body chunk is sent, allowing `receive()` to return `http.disconnect` to the application.

### 5. Cancellation Notification

`PyFutureAwaitable` supports Python asyncio cancellation:

```rust
fn cancel(pyself: PyRef<Self>, msg: Option<Py<PyAny>>) -> bool {
    // CAS: Pending → Cancelled
    if compare_exchange fails { return false; }
    // Store cancel message
    pyself.cancel_msg.set(cancel_msg);
    // Notify the Rust future to abort
    pyself.cancel_tx.notify_one();
    // Notify Python done callbacks
    event_loop.call_soon(cb, pyself);
    true
}
```

In `future_into_py_futlike`:
```rust
tokio::select! {
    biased;
    result = fut => { set_result(aw, py, result) },
    () = cancel_tx.notified() => { aw.drop_ref(py) },  // ← cancelled
}
```

### 6. Runtime Close Notification

`RuntimeRef` has a `sig: Arc<Notify>` used for runtime-wide shutdown:

```rust
pub fn close(&self) {
    self.sig.notify_waiters();  // ← notify all spawned cancellable tasks
}
```

In `spawn_cancellable`:
```rust
tokio::select! {
    biased;
    () = fut => {},
    () = sig.notified() => {
        on_cancel.notify_one();  // ← propagate to task-specific handler
    }
}
```

### 7. Metrics Collection Notification

Metrics collectors run on a timer with shutdown awareness:

```rust
loop {
    tokio::select! {
        biased;
        () = tokio::time::sleep(collector.interval) => {
            collector.poll(sender);  // ← collect and send metrics
        },
        _ = sig.changed() => break,  // ← shutdown signal
    }
}
notify.notify_one();  // ← tell serve loop we're done
```

The `mc_notify` pattern ensures the serve function waits for metrics to flush before cleanup:

```rust
// In serve_fn! (mt variant)
wrk.listen(srx, listener, backpressure).await;
wrk.rt.close();
tasks.close();
tasks.wait().await;
mc_notify.notified().await;  // ← wait for metrics collector to finish
```

### Signal Type Summary

| Signal Type | Channel | Direction | Purpose |
|-------------|---------|-----------|---------|
| `WorkerSignal` | `tokio::watch::channel<bool>` | Python → Rust | Async shutdown signal |
| `WorkerSignalSync` | `crossbeam_channel::bounded(1)` | Python → Rust | Sync shutdown (WSGI) |
| `connsig` | `Arc<tokio::sync::Notify>` | Accept loop → connections | Shutdown all connections |
| `disconnect_guard` | `Arc<tokio::sync::Notify>` | Connection → protocol | Client disconnection |
| `flow_tx_waiter` | `Arc<tokio::sync::Notify>` | Send path → receive path | Response completed |
| `init_event` | `Arc<tokio::sync::Notify>` | Accept path → receive path | WebSocket accepted |
| `cancel_tx` | `Arc<tokio::sync::Notify>` | Python → Rust future | Asyncio cancellation |
| `runtime.sig` | `Arc<tokio::sync::Notify>` | Runtime → all tasks | Runtime closing |
| `mc_notify` | `Arc<tokio::sync::Notify>` | Metrics → serve loop | Metrics collector done |
| `main_loop_interrupt` | `threading.Event` | Signal/watcher → main | Python main loop wakeup |

---

## Security

### TLS/HTTPS (`src/tls.rs`)

TLS uses **rustls** (memory-safe, no OpenSSL dependency):

- **Protocol versions**: TLS 1.2 and 1.3 configurable via `ssl_protocol_min`
- **ALPN negotiation**: Advertises `h2` + `http/1.1` in auto mode, single protocol otherwise
- **Client certificates**: Full mTLS support via `WebPkiClientVerifier`
- **CRL validation**: Certificate revocation list support via `tls_load_crls`
- **Encrypted keys**: PKCS#8 encrypted private key decryption with password

Configuration flows through `WorkerTlsConfig` → `tls_cfg()` → `rustls::ServerConfig` → `TlsAcceptor`.

### Mutual TLS (`src/workers.rs:193-236`)

- **Client certificate verification** via `WebPkiClientVerifier`
- **CA certificate store**: Custom root store for client cert validation
- Configurable: required (`client_verify=true`) or optional (`allow_unauthenticated()`)

### Path Traversal Prevention (`src/files.rs:28-46`)

```rust
match Path::new(&fpath).canonicalize() {
    Ok(full_path) => {
        if full_path.starts_with(mount_point) {  // ← jail check
            return full_path;
        }
        return Err("outside mount path");  // ← blocked
    }
}
```
URI paths are percent-decoded first, then canonicalized (resolving symlinks), then checked against mount boundaries.

### Input Validation

- HTTP/1 buffer size limits (configurable, default ~417KB)
- HTTP/2 max header list size (16MB)
- HTTP/2 max frame size (configurable)
- Header read timeout (prevents slowloris attacks)
- Proper error responses for malformed ASGI messages

### Server Identity

- All responses include `Server: granian` header
- Version exposed via `__version__` (not in HTTP headers)

### Proxy Header Support (`granian/utils/proxies.py`)

- Middleware for `X-Forwarded-For` and `X-Forwarded-Proto`
- Trusted host validation (IP addresses, CIDR ranges, wildcards)
- Available for both ASGI and WSGI

---

## Metrics System

### Collection (Lock-Free)

`WorkerMetrics` (`src/metrics.rs:34-46`) uses atomics exclusively:
```rust
pub conn_active: AtomicUsize,      // gauge
pub conn_handled: AtomicUsize,     // counter
pub conn_err: AtomicUsize,         // counter
pub req_handled: AtomicUsize,      // counter
pub req_static_handled: AtomicUsize,
pub req_static_err: AtomicUsize,
pub blocking_threads: AtomicUsize, // gauge
pub blocking_queue: AtomicIsize,   // gauge (can go negative)
pub blocking_idle_cumul: AtomicUsize,
pub blocking_busy_cumul: AtomicUsize,
pub py_wait_cumul: AtomicUsize,
```

### Transport

- **GIL builds**: Metrics sent via IPC unnamed pipes (`interprocess` crate) to a receiver thread in the main process
- **GIL-disabled builds**: Direct in-memory collection via `MetricsAggregator.collect()`

### Export

`MetricsExporter` runs its own HTTP server on a separate socket. It serves Prometheus-format text (`text/plain`) with per-worker labels:
```
# TYPE granian_connections_active gauge
granian_connections_active{worker="1"} 42
granian_connections_active{worker="2"} 38
```

### Prometheus Metrics Catalog

**Per-worker (12 metrics):**
- `granian_worker_lifetime` — seconds since worker started
- `granian_connections_active` — current active connections
- `granian_connections_handled` — total connections accepted
- `granian_connections_err` — total failed connections
- `granian_requests_handled` — total requests processed
- `granian_static_requests_handled` — total static file requests
- `granian_static_requests_err` — total static file errors
- `granian_blocking_threads` — current Python thread count
- `granian_blocking_queue` — current task queue depth
- `granian_blocking_idle_cumulative` — microseconds threads idle
- `granian_blocking_busy_cumulative` — microseconds threads busy
- `granian_py_wait_cumulative` — microseconds waiting for GIL

**Main process (4 metrics):**
- `granian_workers_spawns` — total workers started
- `granian_workers_respawns_for_err` — respawns due to crashes
- `granian_workers_respawns_for_lifetime` — respawns due to TTL
- `granian_workers_respawns_for_rss` — respawns due to memory

---

## Backpressure

### Semaphore-Based Connection Limiting

```rust
let semaphore = Arc::new(tokio::sync::Semaphore::new(backpressure));
// In accept loop:
let permit = semaphore.acquire_owned().await.unwrap();
// ... spawn connection handler ...
// In connection handler teardown:
drop(permit); // releases semaphore slot
```

- Default: `backlog / workers` (e.g., 1024 backlog / 4 workers = 256 concurrent per worker)
- Configurable via `--backpressure` (minimum 1)
- Permit acquired before `accept()`, released after connection fully handled
- Prevents unbounded memory growth under load

### TCP Listen Backlog

```python
self.backlog = max(128, backlog)  # Default: 1024
```
Kernel-level connection queue. When full, new SYN packets are silently dropped (or rejected with RST depending on OS).

### HTTP/2 Flow Control

- `initial_connection_window_size`: Bytes allowed before ACK (default 1MB)
- `initial_stream_window_size`: Per-stream flow control (default 1MB)
- `max_concurrent_streams`: Max simultaneous streams (default 200)
- `max_send_buffer_size`: Output buffer limit (default 400KB)
- `max_frame_size`: Single frame size limit (default 16KB)
- Adaptive window scaling based on RTT

### Blocking Thread Pool Backpressure

- Thread pool dynamically scales based on queue depth vs active threads
- `compare_exchange` prevents race conditions in thread spawning
- Idle timeout (default 30s, configurable 5-600s) shrinks pool under low load

---

## Static File Serving

`src/files.rs` serves files directly from Rust, bypassing Python entirely:

1. **Path matching**: URI percent-decoded, matched against mount prefix
2. **Security**: `Path::canonicalize()` + prefix check prevents directory traversal
3. **Directory rewrite**: Optional redirect from `/dir/` → `/dir/index.html`
4. **Streaming**: `ReaderStream::with_capacity(file, 131_072)` — 128KB chunks
5. **Headers**: MIME type via `mime_guess`, `Cache-Control: max-age=N`, `Server: granian`
6. **Error handling**: 404 for not found, logged at info level

---

## Memory & Allocation

### Global Allocator Selection (`src/lib.rs:1-7`)

Compile-time feature flags select the allocator:
```rust
#[cfg(feature = "jemalloc")]
static GLOBAL: tikv_jemallocator::Jemalloc = tikv_jemallocator::Jemalloc;

#[cfg(feature = "mimalloc")]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;
```
- **jemalloc**: Better multi-threaded fragmentation resistance, thread-local caching
- **mimalloc**: Lower metadata overhead, faster small allocations
- **Default**: System malloc
- Selected at compile time, zero runtime cost

### Memory Patterns

| Pattern | Where | Purpose |
|---------|-------|---------|
| `Arc<T>` | Connection guards, callback schedulers, metrics | Shared ownership across async tasks |
| `OnceLock<T>` | `CallbackScheduler.schedule_fn`, `PyIterAwaitable.result` | Lazy, lock-free one-time initialization |
| `AtomicU8/U16/Usize/Bool` | Response state, metrics counters | Lock-free updates on hot paths |
| `Mutex<Option<T>>` | `oneshot::Sender`, `body_tx` | Take-once patterns (move out of shared state) |
| `crossbeam unbounded` | Blocking task queues | Lock-free MPSC for Python task dispatch |
| `tokio mpsc::unbounded` | Chunked response bodies | Async streaming without backpressure |
| `Cow<[u8]>` | Request/response bodies | Zero-copy when possible |

### Zero-Copy Patterns

- `Cow<[u8]>` for request bodies (borrowed when possible)
- `hyper::body::Bytes` (reference-counted, shared across tasks)
- `Box<[u8]>` for response body chunks (single owner)
- Direct pointer passing in callback scheduler (`watcher.as_ptr()`)

### Memory Lifecycle

- `Arc` reference counting for shared state across async tasks
- Explicit `drop()` calls inside `Python::attach()` for PyO3 objects
- `Py::drop_ref(py)` ensures Python refcount decremented with GIL held
- `TaskTracker` for connection lifecycle management (wait for all to complete)

---

## Connection Management

### HTTP/1.1

- **Keep-alive**: Enabled by default, configurable
- **Header read timeout**: 30s default, 1-60s configurable (prevents slowloris)
- **Max buffer size**: ~417KB default, minimum 8KB
- **Pipeline flush**: Configurable response batching

### HTTP/2

- **Keep-alive interval**: Optional ping frames to keep connections alive
- **Keep-alive timeout**: 20s default, closes on missing pong
- **Max concurrent streams**: 200 default per connection
- **Adaptive window**: Dynamic flow control based on RTT
- **Max header list size**: 16MB
- **Max frame size**: 16KB default

### Protocol Auto-Detection

```rust
let mut connb = hyper_util::server::conn::auto::Builder::new(TokioExecutor::new());
connb.http1().timer(...).header_read_timeout(...).keep_alive(...);
connb.http2().adaptive_window(...).keep_alive_interval(...);
```
With ALPN, the correct protocol is selected during TLS handshake. Without TLS, hyper's auto builder detects the protocol from the first bytes.

### TCP Optimizations

- `TCP_NODELAY` enabled (disables Nagle's algorithm)
- `SO_REUSEADDR` for fast socket rebinding
- `SO_REUSEPORT` on Linux/FreeBSD for kernel-level load balancing
- Non-blocking socket mode

---

## Logging and Observability

### Structured Logging (`granian/log.py`)

- Python standard library `logging` integration
- Configurable log levels: critical, error, warning, info, debug
- Custom `dictConfig` support for advanced logging pipelines
- `pyo3_log` bridge: Rust `log::info!()` etc. route to Python logging

### Access Logging

Default format:
```
[%(time)s] %(addr)s - "%(method)s %(path)s %(protocol)s" %(status)d %(dt_ms).3f
```
- Configurable format string
- Response time in milliseconds with 3 decimal places
- Enabled per-worker via callback wrapper

### Application Exception Logging (`src/utils.rs:44-51`)

```rust
pub(crate) fn log_application_callable_exception(py: Python, err: &pyo3::PyErr) {
    let tb = match err.traceback(py).map(|t| t.format()) { ... };
    log::error!("Application callable raised an exception\n{tb}{err}");
}
```
Full Python tracebacks are captured and logged at error level.

---

## Error Handling and Resilience

### Worker Crash Recovery (`granian/server/common.py:509-559`)

```python
# Crash loop detection
if any(cycle - self.respawned_wrks.get(idx, 0) <= 5.5 for idx in self.interrupt_children):
    logger.error('Worker crash loop detected, exiting')
    break
```
- Each worker has a watcher thread that detects unexpected exit
- Failed workers are respawned (if `respawn_failed_workers=True`)
- Crash loop detection: if a worker crashes within 5.5s of last respawn, server exits

### Graceful Error Responses

- `response_404()`: "Not found" with `Server: granian` header
- `response_500()`: "Internal server error" with `Server: granian` header
- ASGI errors: `error_flow!` macro returns proper Python exceptions
- WebSocket errors: proper close frames with reason codes

### Python Exception Handling

- All Python callback invocations wrapped in error handling
- Tracebacks captured via `PyTracebackMethods`
- Exceptions logged and converted to 500 responses (not leaked to clients)

### Connection Error Resilience

```rust
Err(err) => {
    log::info!("TCP handshake failed with error: {:?}", err);
    drop(permit);  // ← release semaphore even on error
}
```
Failed TCP handshakes or TLS negotiations don't leak semaphore permits.

---

## Worker Lifecycle Management

The Python `AbstractServer` class (`granian/server/common.py`) manages:

| Feature | Implementation |
|---------|---------------|
| **Respawn on failure** | Watcher thread per worker, `main_loop_interrupt` event, crash loop detection (5.5s window) |
| **Lifetime TTL** | Background thread sleeps for `workers_lifetime`, respawns expired workers at 95% threshold |
| **RSS monitoring** | `ProcInfoCollector` samples memory, multi-sample threshold before respawn |
| **Kill timeout** | `worker.join(timeout)` then `worker.kill()` as fallback |
| **Graceful reload** | SIGHUP handler → respawn all workers with delay between each |
| **Hot reload** | `watchfiles` integration, configurable paths/patterns/filters |
| **PID file** | Write/check/unlink with stale process detection |
| **Process naming** | Optional `setproctitle` integration |

### Lifetime TTL (`granian/server/common.py:378-381`)

```python
def _workers_lifetime_watcher(self, ttl):
    time.sleep(ttl)
    self.lifetime_signal = True
    self.main_loop_interrupt.set()
```
Workers older than 95% of TTL are respawned one at a time with configurable delay (default 3.5s).

### RSS Memory Monitoring (`granian/server/mp.py:374-399`)

```python
rss_data = self._rss_collector.memory(list(wpids.keys()))
for wpid, wmem in rss_data.items():
    if wmem >= self.workers_rss:
        samples = self._rss_wrk_samples.get(wpid, 0) + 1
        if samples >= self.rss_samples:
            to_restart.append(wrk.idx)
```
- `ProcInfoCollector` reads `/proc/[pid]/status` for RSS
- Multi-sample threshold prevents false positives from spikes
- Configurable sample interval and sample count

### Worker Respawn (`granian/server/common.py:339-359`)

- New worker started first, then old worker terminated
- Configurable delay between respawns (default 3.5s)
- Kill timeout with SIGKILL fallback
- Metrics tracking (`incr_respawn_err`, `incr_respawn_ttl`, `incr_respawn_rss`)

---

## Graceful Shutdown & Resource Cleanup

### Two-Phase Shutdown

```
Phase 1: Stop accepting new connections
  → sig.changed() breaks accept loop
  → connsig.notify_waiters() signals all active connections

Phase 2: Drain in-flight requests
  → conn.graceful_shutdown() tells hyper to finish current requests
  → tasks.wait().await waits for all connection handlers
  → mc_notify.notified().await waits for metrics flush
  → drop(wrk) cleans up worker state
```

### Kill Timeout (`granian/server/common.py:361-376`)

```python
def _stop_workers(self):
    for wrk in self.wrks:
        wrk.terminate()          # ← SIGTERM
    for wrk in self.wrks:
        wrk.join(self.workers_kill_timeout)
        if wrk.is_alive():
            wrk.kill()           # ← SIGKILL as last resort
            wrk.join()
```

### Signal Handling

| Signal | Action |
|--------|--------|
| `SIGINT` | Graceful shutdown |
| `SIGTERM` | Graceful shutdown |
| `SIGHUP` | Graceful reload (respawn all workers) |
| `SIGBREAK` (Windows) | Graceful shutdown |

### Resource Cleanup

**Python Object Lifecycle:**
```rust
Python::attach(|_| drop(wrk));  // ← drop worker with GIL held
Python::attach(|_| drop(rt));   // ← drop runtime with GIL held
```
PyO3 objects must be dropped with the GIL held. Granian explicitly attaches to the Python interpreter for cleanup.

**Socket Cleanup:**
- UDS (Unix Domain Socket) files are unlinked on shutdown
- PID files are removed if they contain the current PID
- Socket file descriptors are detached/closed properly

**IPC Cleanup:**
- `_ipc_sig.set()` signals IPC receiver threads to stop
- Unnamed pipes are dropped (OS closes file descriptors)

---

## Hot Reload

### File Watching (`granian/server/common.py:566-609`)

```python
for changes in watchfiles.watch(
    *self.reload_paths,
    watch_filter=reload_filter,
    stop_event=self.main_loop_interrupt,
    step=self.reload_tick,  # 50ms default
):
    self._stop_workers()
    self._spawn_workers(spawn_target, target_loader)
```
- Uses `watchfiles` (Rust-based file watcher)
- Configurable paths, ignore dirs, ignore patterns
- Custom filter class support
- Environment files reloaded on change
- Reload hooks called before respawn

### HUP Reload (`granian/server/common.py:495-504`)

```python
def _reload(self, spawn_target, target_loader):
    workers = list(range(self.workers))
    self._respawn_workers(workers, spawn_target, target_loader, delay=self.respawn_interval)
```
SIGHUP triggers rolling respawn: each worker is replaced one at a time with a delay, ensuring zero downtime.

---

## Configuration Validation

The server validates configuration at startup (`granian/server/common.py:617-720`):

- Workers > CPU cores → warning
- Runtime threads > CPU cores → warning
- Blocking threads > 1 on ASGI/RSGI → error
- WSGI blocking threads > (CPU × 2 + 1) → warning
- Workers lifetime < 60s → error
- UDS on Windows → error
- WebSocket on WSGI → auto-disabled with info
- WebSocket on HTTP/2-only → info
- Reload + lifetime/RSS/metrics → auto-disabled with info
- Environment files without dotenv extra → error
- Process name without setproctitle extra → error

---

## Hooks System

```python
server = Granian(...)

@server.on_startup
def startup_hook():
    print("Server starting")

@server.on_reload
def reload_hook():
    print("Reloading")

@server.on_shutdown
def shutdown_hook():
    print("Shutting down")
```

Three lifecycle hook points: startup (before workers), reload (before respawn), shutdown (after workers stopped).

---

## Macro-Driven Code Generation

Granian makes heavy use of Rust macros to generate specialized implementations without runtime dispatch:

### `acceptor_impl!` (4 implementations per listener type)
Generates `WorkerAcceptor` for: Plain×NoMetrics, TLS×NoMetrics, Plain×Metrics, TLS×Metrics

### `service_impl!` (4 implementations per protocol marker)
Generates `hyper::Service` for: Base×NoMetrics, Files×NoMetrics, Base×Metrics, Files×Metrics

### `conn_handle_impl!` / `conn_handle_metrics_impl!`
Generates connection handlers for: H1×NoUpgrade, H1×Upgrade, HA×NoUpgrade, HA×Upgrade — each with and without metrics

### `serve_fn!` (3 modes × 2 listener types)
Generates: `serve_mt`, `serve_st`, `serve_fut` for TCP and UDS

This produces **dozens of monomorphized implementations** at compile time, eliminating all runtime branching in the hot path. PhantomData type markers enable zero-cost variant selection.

---

## Platform Support

| Feature | Linux | macOS | FreeBSD | Windows |
|---------|-------|-------|---------|---------|
| TCP | Yes | Yes | Yes | Yes |
| UDS | Yes | Yes | Yes | No |
| SO_REUSEPORT | Yes | Yes | Yes | No |
| IPC/Metrics | Yes | Yes | Yes | No |
| Multi-worker | Yes | Yes | Yes | 1 only |
| Free-threaded Python | Yes | Yes | Yes | Yes |
| Process naming | Yes | Yes | Yes | Yes |

| Feature | Implementation |
|---------|---------------|
| `SO_REUSEPORT` | Enabled on Linux/FreeBSD for kernel load balancing |
| `SO_REUSEADDR` | Enabled everywhere for fast rebinding |
| `TCP_NODELAY` | Enabled everywhere (low latency) |
| Deferred listen | Linux/FreeBSD: listener created after fork for better scalability |
| UDS support | Unix only, with configurable permissions (octal) |
| Spawn method | Python 3.14 defaults to `forkserver`, Granian forces `fork` or `spawn` |
| GIL detection | `Py_GIL_DISABLED` compile flag for free-threaded Python paths |
| Windows IPC | Disabled due to `os.set_blocking` failure (metrics unavailable) |

---

## Dependencies (Rust)

| Crate | Version | Purpose |
|-------|---------|---------|
| `hyper` | 1.8 | HTTP/1.1 and HTTP/2 implementation |
| `hyper-util` | 0.1 | Auto-protocol detection, TokioExecutor |
| `tokio` | 1.x | Async runtime (multi-thread + current-thread) |
| `tokio-tungstenite` | 0.26 | WebSocket protocol |
| `tokio-util` | 0.7 | TaskTracker, ReaderStream |
| `pyo3` | 0.24 | Rust↔Python bindings |
| `tls-listener` | 0.11 | TLS listener wrapper |
| `rustls` | 0.23 | TLS implementation |
| `http-body-util` | 0.1 | Body combinators (Full, Empty, StreamBody, BoxBody) |
| `crossbeam-channel` | 0.5 | Lock-free channels for blocking runner |
| `interprocess` | 2.2 | Unnamed pipes for IPC metrics |
| `serde` | 1.x | Metrics serialization |
| `mime_guess` | 2.x | MIME type detection for static files |
| `percent-encoding` | 2.x | URI path decoding |
| `pyo3-log` | 0.12 | Rust log → Python logging bridge |

---

## Build Optimization

`Cargo.toml` release profile:
```toml
[profile.release]
lto = true             # Link-Time Optimization
strip = "debuginfo"    # Strip debug info
```
LTO enables cross-crate optimization, critical for the heavily monomorphized codebase.

---

## Key Design Decisions

1. **No pyo3_asyncio**: Custom callback scheduler is 38-55% faster
2. **Monomorphization over polymorphism**: All hot-path variants compiled as separate functions
3. **Separate Python thread pool**: Blocking runner decoupled from tokio, explicit GIL management
4. **Atomic metrics**: Zero mutex contention for counters in the request path
5. **Deferred response start**: Buffer HTTP intent to detect single vs chunked response (~4% optimization)
6. **SSE detection**: Content-Type check to immediately start chunked stream
7. **rustls over OpenSSL**: Memory safety, no system dependency, smaller binary
8. **Feature-gated allocators**: Choose jemalloc/mimalloc at compile time, zero runtime cost
