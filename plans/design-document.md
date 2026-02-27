Here's the updated design document with all C references replaced by Zig and a new section covering the Zig rationale.

---

# Design Document: High-Performance Python HTTP Framework

## 1. Executive Summary

A Python HTTP framework built on three principles: no function coloring (no async/await), transparent concurrency (the runtime handles scheduling), and maximum I/O performance (io_uring on Linux).

The developer writes sequential, blocking-style code. The runtime transparently multiplexes I/O across hundreds of thousands of lightweight coroutines using greenlets, io_uring, and a pre-fork process model.

## 2. Design Philosophy

Python's `async/await` forces every caller in the call stack to declare itself as async. Whether a function performs I/O is an implementation detail that shouldn't leak into every signature. Go, Erlang, and Java (Loom) solve this correctly: the runtime handles scheduling, the programmer writes sequential code. This framework follows the same philosophy.

## 3. Architecture

### 3.1 Deployment Topology

Two layers:

- **Caddy/Nginx** — TLS termination, HTTP/2 & HTTP/3, static files, rate limiting, compression, slow-client buffering. Faces the public internet.
- **Framework server** — HTTP parsing, routing, handler execution, response generation. Sits behind the reverse proxy on a Unix socket, never exposed directly.

Communication is plain HTTP over a Unix socket. The overhead is ~50-100μs per request — negligible compared to actual handler work.

### 3.2 Process Model

Pre-fork: a master process spawns N worker processes (one per CPU core). Each worker runs its own io_uring event loop and greenlet hub. Workers share no state. The GIL is irrelevant — greenlets run on a single thread, parallelism comes from multiple processes.

All workers bind to the same Unix socket using `SO_REUSEPORT`. The kernel distributes connections across them. No master bottleneck, no upstream config in nginx.

### 3.3 Per-Worker Internals

Four layers inside each worker:

- **Event loop** — io_uring ring (liburing). Written in Zig. Submits and reaps I/O operations.
- **Scheduler** — Hub greenlet. Uses greenlet's C stack switching, called from Zig via direct C interop. Dispatches ready greenlets when their I/O completes.
- **Protocol** — HTTP parser. Written in Zig or wrapping llhttp via Zig's native C interop.
- **Application** — Routes, middleware, handlers. Written in Python.

## 4. Concurrency Model

### 4.1 Greenlets

Greenlets are stackful coroutines (C extension, `pip install greenlet`). Each has its own C call stack (~few KB). When you call `greenlet.switch()`, it saves the current stack pointer, instruction pointer, and registers, then restores the target greenlet's. It's a user-space context switch — nanoseconds, no kernel involvement.

Because it saves the entire C stack, a switch can happen at any call depth without intermediate functions knowing. No `yield`, no `await`, no coloring.

### 4.2 How I/O Works

When a handler calls `response.write(data)`:

1. The framework's write primitive submits an SQE to the io_uring ring
2. It calls `hub.switch()` to yield to the hub greenlet
3. The hub runs the event loop, reaping CQEs
4. When the write CQE arrives, the hub switches back to the handler
5. The handler resumes exactly where it left off

The developer sees `response.write(data)` — a blocking call that returns. The concurrency is invisible.

### 4.3 Preemption

None. A CPU-bound greenlet that never does I/O will starve everything else. This is by design — HTTP handlers are I/O-bound and naturally yield. CPU-heavy work should be offloaded via `framework.offload(func)` to a subprocess. Same contract Go enforces.

## 5. I/O Layer: io_uring

### 5.1 Why io_uring Over epoll

epoll (used by libuv/libev) is readiness-based: it tells you when a socket is ready, then you do the I/O yourself. Multiple syscalls per operation.

io_uring is completion-based: you submit the I/O, the kernel does it, and notifies you when it's done. Key advantages:

- **Batching** — Submit hundreds of operations through a shared memory ring buffer. One syscall (or zero) instead of one per operation.
- **No readiness step** — epoll says "ready, now read." io_uring does the read and delivers the result. One round trip instead of two.
- **Shared memory rings** — SQE/CQE queues are mapped into both user and kernel space. Writing an SQE is just a memory write.
- **SQPOLL mode** — A kernel thread polls the submission queue. Zero syscalls for I/O.
- **Linked operations** — Chain accept → read → write → close as a single sequence executed entirely in kernel space.

### 5.2 Why Not Just Use libuv/libev With io_uring

libuv has experimental io_uring support but only for file I/O, not network. Their entire architecture assumes the readiness model. Bolting completion semantics onto it yields minimal benefit. libev has no io_uring support and never will.

Building directly on io_uring means designing around the completion model from the start.

### 5.3 Platform Limitation

io_uring is Linux-only (kernel 5.1+). For development on macOS, the framework provides a fallback backend using the `selectors` stdlib module (kqueue on macOS, epoll on Linux). Slower, but functionally identical. Production is Linux anyway.

## 6. Why Zig

The native extension layer is written in Zig rather than C. The rationale:

- **No undefined behavior** — C's UB around pointer arithmetic, signed overflow, and aliasing is a constant source of bugs in event loop code. Zig makes safety checks explicit and catches them at compile time or as runtime safety checks in debug builds.
- **Direct C interop with zero overhead** — Zig can call C functions (liburing, greenlet's `greenlet.h` API) directly with no bindings layer, FFI bridge, or code generation. It can also export C ABI functions, so Python's ctypes/cffi loads the Zig `.so` as a normal native extension.
- **Comptime** — Compile-time code execution enables generating lookup tables, specializing hot paths (e.g., HTTP method dispatch), and eliminating runtime branching in the event loop.
- **No hidden allocations** — Every allocation is explicit and controlled, which matters in the event loop where ring buffers, connection state, and greenlet stacks are managed at high volume.
- **Cross-compilation** — A single `zig build` produces binaries for any Linux target. This simplifies distributing prebuilt Python wheels for multiple architectures.
- **Builds as a Python extension** — Zig produces `.so` files that Python loads as native extensions. The `zig cc` compiler is a drop-in replacement for gcc/clang, so setuptools and meson integrate without friction.

## 7. Zig vs Python Boundary

The hot path is Zig. Everything else is Python.

**Written in Zig:**
- io_uring event loop and hub
- HTTP parser (custom Zig implementation or wrapping llhttp via C interop)
- Socket read/write primitives that bridge io_uring completions to greenlet switches
- Pre-fork worker management
- Greenlet switching bridge (calling greenlet's C API directly from Zig)

**Written in Python:**
- URL routing and dispatch
- Request/Response objects
- Middleware chain
- User-facing API (decorators, config)
- Monkey patching compat layer

This follows the pattern of every high-performance Python server: gevent (libev + Cython), uvicorn (uvloop + httptools), Node.js (libuv + llhttp + V8). The event loop and parser are native code; the application layer is the high-level language.

### 7.1 Greenlet Integration From Zig

Greenlet exposes a C API via `greenlet.h` for switching from native code. Zig can `@cImport` this header directly and call `PyGreenlet_Switch`, `PyGreenlet_New`, etc. with no wrapper or FFI overhead. The hub loop in Zig reaps a CQE, looks up which greenlet is waiting on that file descriptor, and calls the greenlet switch function to resume it — all in native code before returning to Python.

## 8. Calling Convention

### 8.1 No WSGI, No ASGI

The framework defines its own interface. WSGI can't do WebSockets or streaming. ASGI is built on async/await. Since we own the server, we don't need either.

### 8.2 Native Interface

```python
app = Framework()

@app.route("/hello")
def hello(request, response):
    response.write(b"hello world")

@app.route("/stream")
def stream(request, response):
    for chunk in generate_data():
        response.write(chunk)

@app.websocket("/ws")
def websocket(ws):
    msg = ws.recv()
    ws.send(msg)

app.run(port=8000)
```

Streaming and WebSockets work with plain synchronous code. No async, no await. The runtime does its job.

## 9. Third-Party Library Compatibility

### 9.1 The Problem

Libraries using stdlib `socket` (requests, database drivers) will issue real blocking I/O that starves the event loop.

### 9.2 Monkey Patching (Opt-In)

```python
import myframework
myframework.patch_all()  # opt-in
import requests  # now greenlet-aware
```

Patches `socket`, `ssl`, `time.sleep`, `select`, DNS functions, and threading primitives. Python has no stdlib hooks for pluggable I/O backends — monkey patching is the only option.

### 9.3 Leverage gevent

Rather than maintaining patches from scratch, use gevent's battle-tested patching. The framework's own I/O goes through io_uring (fast path). Patched third-party calls go through gevent on libev/libuv (compat path). Two backends, one greenlet hub.

C extensions that do their own I/O at the C level (e.g., librdkafka) will still block. This is a known, documented limitation shared with gevent.

## 10. GIL and Free-Threaded Python

The GIL is irrelevant. Greenlets run on one thread (no contention), parallelism comes from processes (bypasses GIL entirely).

Free-threaded Python (3.13+) offers marginal benefit: threads for CPU offloading instead of subprocesses. But greenlet's stack switching wasn't designed for concurrent access from multiple real threads, and free-threading support is under-tested. Not worth the risk at this stage.

## 11. Dependencies

- **greenlet** — Required. Stackful coroutines. C extension, ~20 years mature, no deps.
- **liburing (system)** — Required on Linux. Zig calls liburing's C API directly via `@cImport`.
- **httptools** — Optional. HTTP parsing. Can alternatively implement the parser in Zig for zero external deps on the parsing path.
- **gevent** — Optional. Only needed if users want monkey patching for third-party blocking libraries.

## 12. Risks and Tradeoffs

- **Linux-only** — io_uring doesn't exist on macOS/Windows. Mitigated by selectors fallback for dev; production servers are Linux.
- **No precedent** — Nobody has built greenlet + io_uring in Python. The pieces exist, but the integration is uncharted.
- **Swimming upstream** — Python ecosystem standardized on async/await. Mitigated by owning the full stack and providing monkey patching compat.
- **Cooperative starvation** — No preemption for CPU-bound handlers. Mitigated by documenting the contract and providing `offload()`.
- **C extension blocking** — Third-party C libs doing their own I/O bypass greenlets. Known limitation, same as gevent.
- **io_uring security history** — Past kernel CVEs. Mitigated by requiring recent kernels; io_uring has matured significantly.
- **Zig maturity** — Zig has not yet reached 1.0. The language and std library may have breaking changes. Mitigated by the fact that the Zig layer is a contained extension module with a stable C ABI boundary to Python; internal Zig changes don't affect the framework's public API.

## 13. Summary

Greenlets for concurrency without coloring. io_uring for maximum I/O throughput. Pre-fork for CPU parallelism. Zig for a safe, performant native extension layer with direct C interop. A custom synchronous calling convention that supports streaming and WebSockets. Caddy/Nginx in front for TLS and HTTP/2/3. Monkey patching via gevent as an opt-in compat layer.

Nobody has built this combination before. The architecture is sound, the pieces exist, and the philosophy is proven by Go, Erlang, and Loom.
