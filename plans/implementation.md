# Implementation Plan: the-framework

Incremental, small-deliverable plan. Each step produces something you can run and
verify before moving on. Steps are ordered so that each one builds exactly one new
concept on top of proven ground.

**Tooling:**
- **Python 3.13+** managed by **uv** (no system Python, no virtualenv manual setup)
- **Zig** via Nix flake (`flake.nix` already provides it)
- All Python commands use `uv run`
- Dependencies added via `uv add <pkg>` (writes to `pyproject.toml` and `uv.lock`)
- Dev dependencies via `uv add --dev <pkg>`
- Project initialized with `uv init` or hand-written `pyproject.toml`

**Python quality rules:**
- **Strict typing everywhere.** All Python code must have complete type annotations.
  No `Any`, no `Unknown`, no untyped functions, no untyped variables.
- **mypy** is the type checker. Install with `uv add --dev mypy` in Step 0.2.
  Configure in `pyproject.toml` with `strict = true`:
  ```toml
  [tool.mypy]
  strict = true
  warn_return_any = true
  disallow_any_explicit = true
  disallow_any_generics = true
  ```
- **Run `uv run mypy theframework/` at the end of every step that touches Python.**
  The type checker must pass with zero errors before a step is considered done.
  This is a gate — not optional.

**Testing philosophy:**
- **No mocks.** Tests use real sockets, real io_uring rings, real forks. If a test
  needs a server, start a real server.
- **Black box.** Tests call public APIs only. Never import or call `_private`
  functions, never inspect internal state. If it's not part of the public
  interface, it doesn't exist to the test.
- **Less is more.** Each test should assert something meaningful about the overall
  behavior, not individual attributes. Assert the full response bytes, not
  `status == 200` and `headers["content-type"] == ...` separately. One good test
  beats five shallow ones.
- **Zig tests:** use Zig's built-in `test` blocks. Run with `zig build test`.
- **Python tests:** use `pytest`. Install with `uv add --dev pytest` in Step 0.2.
  Tests live in `tests/`. Run with `uv run pytest`.
- **Both test suites must pass at the end of every step.** This is a gate alongside mypy.

---

## Phase 0 — Project Scaffolding ✅ DONE

### Step 0.1: Zig project skeleton ✅ DONE

Set up the Zig build system inside the repo. No Python yet, just a Zig library
that compiles to a shared object.

**Deliverable:** `zig build` produces `libframework.so` exporting a single C ABI
function `int32_t framework_version(void)` that returns `1`.

**TODO:**
- [x] Create `build.zig` at repo root
- [x] Create `src/` directory for Zig sources
- [x] Create `src/main.zig` with `export fn framework_version() callconv(.C) i32`
- [x] Configure build.zig to produce a shared library (`b.addSharedLibrary`)
- [x] Link against libc (`lib.linkLibC()`) — needed later for liburing/Python
- [x] Verify: `zig build && nm -D zig-out/lib/libframework.so | grep framework_version`
- [x] Add `.gitignore` for `zig-out/`, `.zig-cache/`, `.venv/`
- [x] Update `flake.nix` to include `liburing` in packages (needed from Phase 2 onward)
- [x] Add a Zig test in `src/main.zig`:
  ```zig
  test "framework_version returns 1" {
      try std.testing.expectEqual(@as(i32, 1), framework_version());
  }
  ```
- [x] Configure `build.zig` to add a test step (`b.addTest`)
- [x] Gate: `zig build test` passes

**Why this matters:** Proves the toolchain works. Every later step depends on
`zig build` producing a loadable `.so`.

---

### Step 0.2: Python package skeleton ✅ DONE

Set up a Python package that can load the Zig .so via ctypes.

**Deliverable:** `uv run python -c "import theframework; print(theframework.version())"` prints `1`.

**TODO:**
- [x] `uv init --lib` to scaffold the project, or create `pyproject.toml` by hand:
  ```toml
  [project]
  name = "theframework"
  version = "0.1.0"
  requires-python = ">=3.13"

  [build-system]
  requires = ["hatchling"]
  build-backend = "hatchling.build"
  ```
- [x] Pin the Python version: `uv python pin 3.13` (creates `.python-version`)
- [x] Create `theframework/__init__.py`
- [x] In `__init__.py`, use `ctypes.CDLL` to load `libframework.so`
- [x] Expose `version()` that calls `framework_version()` from the .so
- [x] `uv add --dev mypy pytest` and add `[tool.mypy]` strict config to `pyproject.toml`
- [x] `uv sync` to create `.venv/` and install the package in editable mode
- [x] Verify: `uv run python -c "import theframework; print(theframework.version())"`
- [x] Add `.venv/` to `.gitignore`
- [x] Create `tests/test_version.py`:
  ```python
  import theframework

  def test_version_returns_integer() -> None:
      assert theframework.version() == 1
  ```
- [x] Gate: `uv run pytest` passes
- [x] Gate: `uv run mypy theframework/ tests/` passes with zero errors

**Why this matters:** Proves Python can call Zig. This is the bridge everything
else crosses.

---

## Phase 1 — Greenlet Fundamentals (Pure Python) ✅ DONE

Before touching Zig, understand the greenlet concurrency model in pure Python.
This phase builds the mental model that the rest of the project depends on.

### Step 1.1: Greenlet hello world ✅ DONE

**Deliverable:** A Python script that creates two greenlets, switches between them,
and prints interleaved output proving cooperative switching works.

**TODO:**
- [x] `uv add greenlet` (adds to `pyproject.toml` dependencies, updates `uv.lock`)
- [x] Verify greenlet is installed: `uv run python -c "import greenlet; print(greenlet.__version__)"`
- [x] Write `examples/greenlet_basics.py` (run with `uv run python examples/greenlet_basics.py`):
  - Create a "hub" greenlet and two "worker" greenlets
  - Workers switch back to hub after each print
  - Hub round-robins between workers
- [x] Understand: `greenlet.getcurrent()`, `greenlet.switch()`, parent greenlet
- [x] Understand: what happens when a greenlet finishes (returns to parent)
- [x] Understand: exception propagation across greenlet switches
- [x] Write `tests/test_greenlet_basics.py` — test the hub pattern itself:
  - Spawn N greenlets that each append to a shared list and yield back to the hub
  - After all greenlets complete, assert the list contents prove correct
    interleaving (e.g., `["w1-0", "w2-0", "w1-1", "w2-1", ...]`)
  - Test that a greenlet raising an exception propagates to the hub
- [x] Gate: `uv run pytest` and `uv run mypy examples/ tests/` pass

**Architectural note:** The hub pattern is the foundation. The hub is a greenlet
that owns the event loop. All other greenlets (one per connection) switch to the
hub when they need I/O. The hub switches back when I/O completes. Internalize this
before proceeding.

---

### Step 1.2: Hub + fake I/O scheduler in pure Python ✅ DONE

**Deliverable:** A pure-Python hub that simulates async I/O using `selectors` and
greenlets. An echo server on a TCP socket where each connection runs in its own
greenlet, but there's only one OS thread.

**TODO:**
- [x] Write `examples/greenlet_echo.py`:
  - Hub greenlet runs a `selectors.DefaultSelector` loop
  - `green_accept(sock)` → registers sock for READ, switches to hub, hub switches
    back when readable, returns the new connection
  - `green_read(sock, n)` → registers for READ, switches to hub, returns data
  - `green_write(sock, data)` → registers for WRITE, switches to hub, returns when done
  - Each accepted connection spawns a new greenlet running an echo handler
- [x] Hub maintains a dict: `{fd: greenlet}` to know who to wake
- [x] Write `tests/test_greenlet_echo.py` — real sockets, no mocks:
  - Start the echo server in a background thread (the server is single-threaded
    greenlets, but the test driver needs its own thread to connect)
  - Open a TCP connection, send `b"hello"`, assert recv returns `b"hello"`
  - Open multiple connections concurrently, send different data on each,
    assert each gets its own data back (proves connections are independent)
  - Send data, close the client socket, assert server doesn't crash
- [x] Measure: how many concurrent connections before things break?
- [x] Gate: `uv run pytest` and `uv run mypy examples/ tests/` pass

**Lesson learned:** Spawning a worker with `worker.switch()` from the acceptor
is a greenlet trap — the worker yields to hub, leaving the acceptor permanently
suspended. Fixed by using a `_ready` queue: the acceptor spawns workers into the
queue, and the hub drains it each iteration before blocking on `select()`.

**Architectural notes:**
- This is the exact same architecture as the final system, just with selectors
  instead of io_uring and Python instead of Zig for the hub.
- The `{fd: greenlet}` mapping is critical. In the real system this will be a
  Zig data structure (array indexed by fd for O(1) lookup since Linux fds are
  small contiguous integers).
- Notice that the hub never blocks in `selector.select()` for more than the
  timeout — this is the only point where the OS thread actually sleeps.

---

## Phase 2 — io_uring From Zig (Standalone, No Python) ✅ DONE

Learn io_uring in isolation before integrating with Python/greenlets.

**Completion model — design implications for every step in this phase and beyond:**

io_uring is completion-based, not readiness-based. This is NOT just "faster epoll."
It changes how every I/O operation is designed:

- **Buffer ownership transfers to the kernel.** When you submit a recv SQE with a
  buffer, that buffer belongs to the kernel until the CQE arrives. You must not
  read, write, reuse, or free it in the meantime. In epoll, you own the buffer
  and call `read()` into it on your own schedule. This means buffer lifecycle is
  tied to CQE reaping, not to application logic.
- **Operations are in-flight, not on-demand.** With epoll you say "notify me when
  ready" then do the I/O yourself. With io_uring you say "do this I/O" and it's
  already happening in the kernel. You've committed. There's no "check readiness
  first, then decide." This affects how you handle backpressure — you can't peek
  at readiness to decide whether to accept a new connection.
- **Errors are completions, not return codes.** In epoll code, `read()` returning
  `EAGAIN` means "try later." In io_uring, an error arrives as a CQE with a
  negative result. The operation already happened (and failed). You handle it as
  a completed-but-failed event, not a retry signal. Every CQE handler must check
  `cqe.res < 0`.
- **Cancellation is explicit and async.** In epoll, to stop watching an fd you
  just remove it from the epoll set — instant, synchronous. In io_uring, pending
  operations are actively in-flight in the kernel. To cancel, you submit
  `IORING_OP_ASYNC_CANCEL`, then wait for the cancellation CQE. Until that CQE
  arrives, the buffer and fd are NOT safe to reuse. This affects connection
  teardown: you must cancel pending ops → wait for cancel CQEs → submit close →
  wait for close CQE → only then free resources.
- **Submission is decoupled from completion.** You can submit 100 SQEs and reap
  CQEs in any order. This is a strength (batching), but it means you need robust
  bookkeeping: every in-flight operation must be tracked so you know what each CQE
  refers to (via `user_data`) and what resources it holds.

These implications apply to every step from Phase 2 onward. When in doubt, ask:
"who owns this buffer right now?" and "what happens if I need to tear down this
connection while operations are in-flight?"

### Step 2.1: io_uring echo server in pure Zig ✅ DONE

**Deliverable:** A standalone Zig program (not a .so, a regular executable) that
accepts TCP connections using io_uring and echoes data back. Single-threaded,
no greenlets, just a raw event loop.

**Implementation note:** Used Zig's built-in `std.os.linux.IoUring` instead of
the liburing C library. This is cleaner and avoids C interop complexity.

**TODO:**
- [x] Create `src/ring.zig` — thin wrapper around `std.os.linux.IoUring`:
  - `Ring.init(queue_depth)` → `linux.IoUring.init(queue_depth, 0)`
  - `Ring.deinit()` → `self.io.deinit()`
  - `Ring.submit()` → `self.io.submit()`
  - `Ring.waitCqe()` → `submit_and_wait(1)` + `copy_cqe()`
  - `Ring.prepAccept(fd, user_data)` → `self.io.accept(...)`
  - `Ring.prepRecv(fd, buf, user_data)` → `self.io.recv(...)`
  - `Ring.prepSend(fd, buf, user_data)` → `self.io.send(...)`
  - `Ring.prepClose(fd, user_data)` → `self.io.close(...)`
- [x] Create `src/echo_server.zig` — main function:
  - TCP socket, bind, listen on configurable port (default 9999)
  - Event loop: reap CQEs, dispatch based on `Conn.op` field
  - Uses pointer-based `user_data`: `Conn` struct stored at pointer, cast to/from u64
  - Per-connection heap allocation via `GeneralPurposeAllocator`
- [x] Build as executable: `b.addExecutable` in build.zig
- [x] Zig tests in `src/ring.zig`:
  - `Ring.init` / `Ring.deinit` succeeds
  - Full accept → recv → send → close cycle on real loopback socket
  - recv on closed fd returns negative CQE result (not a crash)
- [x] Gate: `zig build test` passes

**Architectural notes:**
- `user_data` on SQEs is how you correlate completions back to their context.
  In the final system, user_data will carry the greenlet pointer or connection ID.
- Errors from CQEs come as negative errno in `cqe.res`. Handle them (EAGAIN,
  ECONNRESET, EPIPE are common).
- io_uring SQ and CQ are separate sizes. CQ is typically 2× SQ. If the CQ fills
  up because you're not reaping fast enough, new submissions will fail.
- **IORING_FEAT_FAST_POLL** (kernel 5.7+): the kernel does internal polling for
  network I/O, so you don't need to use IOSQE_IO_LINK to chain poll+read.
  Just submit a recv and the kernel handles the wait internally.

---

### Step 2.2: io_uring multishot accept and buffer rings ✅ DONE

**Deliverable:** Upgrade the echo server to use multishot accept (one SQE accepts
many connections) and provided buffer rings (kernel picks buffers from a pre-registered
pool, avoiding one-buffer-per-recv allocation).

**Implementation note:** Used Zig stdlib's `IoUring.BufferGroup` which handles
kernel mmap registration, buffer lifecycle, and index management. The echo server v2
saves the recv CQE in the Conn struct so the buffer can be returned to the kernel
only after the send completes (correct buffer ownership lifecycle).

**TODO:**
- [x] Implement multishot accept via `ring.prepAcceptMultishot()`:
  - Check `IORING_CQE_F_MORE` flag — if set, the accept SQE is still active
  - If not set, resubmit it (both on success and error paths)
- [x] Implement provided buffer rings via `IoUring.BufferGroup`:
  - 64 buffers of 4KB each in buffer group 0
  - `buf_grp.recv(user_data, fd, 0)` — kernel picks a buffer
  - `buf_grp.get(cqe)` — extract received data from the kernel-selected buffer
  - `buf_grp.put(cqe)` — return buffer to kernel after send completes
- [x] Create `src/echo_server_v2.zig` — upgraded echo server with both features
- [x] Zig tests:
  - Multishot accept: 5 rapid connections from one SQE, verify all accepted,
    verify `IORING_CQE_F_MORE` flag set on intermediate CQEs
  - Buffer group lifecycle: 2 buffers, 6 rounds of send/recv — proves recycling
    works (6 > 2 means buffers must be reused)
- [x] Gate: `zig build test` passes

**Architectural notes:**
- Multishot accept is essential for high connection rates. Without it, you burn
  one SQE per accept and have a window where no accept is pending.
- Provided buffers eliminate the "allocate a buffer before you know if data is
  ready" problem. Critical for handling thousands of idle connections without
  wasting memory.
- These are kernel 5.19+ features. The fallback path should work without them
  (degrade to single-shot accept and pre-allocated per-fd buffers).

---

## Phase 3 — Zig ↔ Python ↔ Greenlet Bridge ✅ DONE

This is the hardest phase. The Zig code needs to call Python's C API and greenlet's
C API. Getting this right unlocks everything; getting it wrong causes segfaults.

**Implementation notes:**
- Used a C helper file (`py_helpers.h`/`.c`) to wrap macro-based Python and greenlet
  C API operations that Zig's `@cImport` cannot translate (Py_None, METH_NOARGS,
  PyGreenlet_Import, PyGreenlet_Switch, etc.).
- Module tables (PyMethodDef, PyModuleDef) are runtime-initialized in PyInit because
  their sentinel values and flag constants come from extern C functions.
- Greenlet C API function-pointer table is initialized via `py_helper_greenlet_import()`
  which calls `PyCapsule_Import("greenlet._C_API", 0)`.
- Build system uses configurable `python-include` and `greenlet-include` paths with
  sensible defaults. Extension is installed with correct name (no `lib` prefix) via
  `addInstallFile`.
- Type stubs live in `stubs/` directory (separate from `theframework/` to avoid mypy
  module name conflicts).

### Step 3.1: Zig as a Python C extension module ✅ DONE

**Deliverable:** A Python extension module written in Zig that Python can `import`
directly (not via ctypes). `import _framework_core; _framework_core.hello()` prints
"hello from Zig".

**TODO:**
- [x] @cImport Python.h from Zig (via `py_helpers.h` C shim wrapping macros)
- [x] Configure build.zig:
  - Find Python include path
  - Find greenlet include path
  - Output name matches platform suffix (no `lib` prefix)
- [x] Implement `PyInit__framework_core` — the module init function
- [x] Handle reference counting correctly: `Py_INCREF`, `Py_DECREF` via helpers
- [x] Create type stub `stubs/_framework_core.pyi`
- [x] Write `tests/test_zig_extension.py`
- [x] Gate: `uv run pytest` and `uv run mypy theframework/ tests/` pass

---

### Step 3.2: Call greenlet's C API from Zig ✅ DONE

**Deliverable:** Zig extension module creates a greenlet, switches to it (running a
Python callable), and switches back. Proves Zig can orchestrate greenlet switches.

**TODO:**
- [x] C helpers wrap greenlet.h macros (PyGreenlet_Import, _New, _Switch, etc.)
- [x] In module init, call `py_helper_greenlet_import()` to initialize C API
- [x] Implement `greenlet_switch_to(callable)` and `greenlet_ping_pong(callable, n)`
- [x] Update `_framework_core.pyi` stub with new functions
- [x] Write `tests/test_greenlet_zig.py` (value return, exception propagation, 1000 rapid switches)
- [x] Gate: `uv run pytest` and `uv run mypy theframework/ tests/` pass

---

### Step 3.3: Minimal hub in Zig with greenlet switching ✅ DONE

**Deliverable:** A Zig-implemented hub greenlet that round-robins between Python
worker greenlets. No I/O yet — just proving the hub loop works from Zig.

**TODO:**
- [x] Implement `run_hub(workers)` in Zig — round-robin loop using PyList,
      PyGreenlet_Switch, and active/started checks
- [x] Workers switch to hub (parent) to yield. Hub picks next worker and switches to it.
- [x] Write `tests/test_zig_hub.py`:
  - 3 workers × 3 iterations: assert round-robin order
  - Worker raises: assert exception propagates to caller
  - Mixed workers: assert partial execution before exception
- [x] Gate: `uv run pytest` and `uv run mypy theframework/ tests/` pass

**Why this matters:** This is the skeleton of the real event loop. In the final
system, workers yield when they do I/O, and the hub picks the next worker whose
I/O has completed. Here we just round-robin to prove the mechanics.

---

## Phase 4 — The Core: io_uring Hub With Greenlets ✅ DONE

Connect io_uring to the greenlet hub. This is the heart of the framework.

**Implementation notes:**
- Hub struct uses a connection pool (ConnectionPool) with generation-validated
  user_data encoding to handle fd reuse safely. Accept operations use a separate
  sentinel-based tracking since the listen fd doesn't belong to the pool.
- GIL is released before the blocking `submitAndWait(1)` syscall via
  `PyEval_SaveThread`/`PyEval_RestoreThread` helpers to prevent deadlock
  between the hub thread and Python threads.
- CQEs are batch-drained (up to 256 at once) and all woken greenlets are added
  to the ready queue for batch-fair scheduling.
- green_send handles partial sends with an internal loop — resubmits remainder
  until all data is sent.
- Connection pool uses opaque `*anyopaque` for greenlet pointers to avoid
  linking libpython in Zig-only tests.

### Step 4.1: io_uring hub that wakes greenlets on I/O completion ✅ DONE

**Deliverable:** A Zig extension module that runs a hub greenlet with an io_uring
event loop. Python worker greenlets can call `green_read(fd)` and `green_write(fd, data)`
which submit io_uring SQEs and yield to the hub. The hub reaps CQEs and resumes
the correct greenlet. Test: a TCP echo server where handlers are Python greenlets.

**TODO:**
- [x] Define the Zig `Hub` struct:
  - `ring: Ring` — the io_uring instance from Phase 2
  - `waiting: [MAX_FDS]?*PyObject` — array mapping fd → waiting greenlet
    (fds are small integers on Linux, so a flat array is O(1) and cache-friendly)
  - `current: *PyObject` — the currently running greenlet
- [x] Implement `hub_loop()` in Zig:
  ```
  while (true) {
      submit_pending_sqes();
      cqe = ring.waitCqe();  // blocks here — this is the only blocking point
      greenlet = waiting[cqe.fd];
      waiting[cqe.fd] = null;
      store cqe result somewhere the greenlet can read it;
      PyGreenlet_Switch(greenlet, result, NULL);
      // when greenlet switches back to hub, we're here again
  }
  ```
- [x] Implement Python-callable I/O primitives (exposed from Zig):
  - `_framework_core.green_accept(listen_fd)` → returns new fd
  - `_framework_core.green_recv(fd, max_bytes)` → returns bytes
  - `_framework_core.green_send(fd, data)` → returns bytes sent
  - Each: submit SQE, register current greenlet in `waiting[fd]`, switch to hub
- [x] Python echo server:
  ```python
  def handle_connection(fd):
      while True:
          data = _framework_core.green_recv(fd, 4096)
          if not data:
              break
          _framework_core.green_send(fd, data)

  def acceptor(listen_fd):
      while True:
          client_fd = _framework_core.green_accept(listen_fd)
          # spawn a new greenlet for this connection
          g = greenlet.greenlet(lambda: handle_connection(client_fd), parent=hub)
          g.switch()
  ```
- [x] Update `_framework_core.pyi` stub with I/O primitives
- [x] Write `tests/test_iouring_echo.py` — real TCP sockets, real io_uring:
  - Start the echo server (bind to a random port), connect a client socket,
    send `b"hello world"`, assert recv returns `b"hello world"`
  - Open 10 concurrent connections from 10 threads, each sends unique data,
    assert each receives its own data back
  - Connect, send, abruptly close the client — assert the server stays alive
    and subsequent connections still work
- [ ] Benchmark: `wrk` or `hey` against the echo server
- [x] Gate: `uv run pytest` and `uv run mypy theframework/ tests/` pass

**Architectural notes:**
- **The blocking point:** `ring.waitCqe()` is the ONLY place the OS thread blocks.
  All other "blocking" is cooperative greenlet switching. This is the key insight.
- **GIL interaction:** The GIL is held during `waitCqe()`, but that's fine — no
  other Python code can run anyway (all greenlets are yielded to the hub). If you
  ever want background threads, you'd release GIL before `waitCqe()` with
  `Py_BEGIN_ALLOW_THREADS` and reacquire before switching greenlets.
- **Batching:** Don't submit SQEs one at a time. Accumulate them and submit in
  batch before entering `waitCqe()`. The ring buffer naturally supports this.
- **Multiple CQEs:** `io_uring_wait_cqe` returns one CQE. Use
  `io_uring_peek_batch_cqe` to drain all ready CQEs before switching to any
  greenlet. This prevents pathological scheduling where one busy fd starves others.
- **user_data encoding:** Store the fd (or a connection ID) in the SQE's user_data
  field. When the CQE arrives, extract it to look up the waiting greenlet.
  If you need to distinguish multiple pending operations on the same fd (e.g., a
  recv and a send), use a connection ID instead of raw fd.

---

### Step 4.2: Connection state machine ✅ DONE

**Deliverable:** Proper connection lifecycle management — accept, process, keep-alive
or close — with clean resource cleanup.

**TODO:**
- [x] Define a `Connection` struct in Zig:
  - fd, state (accepting/reading/writing/closing), read buffer, write buffer
  - Associated greenlet pointer
  - Allocator for per-connection memory
- [x] Implement connection pool: pre-allocate connection structs, reuse on close
- [x] Handle connection errors gracefully (remember: errors are CQEs, not return codes):
  - ECONNRESET during recv → error propagated to Python as OSError
  - EPIPE during send → error propagated to Python as OSError
  - Timeout (no data for N seconds) → deferred to Phase 10
- [x] Implement the cancel-before-close teardown sequence (completion model requirement):
  1. Submit `IORING_OP_ASYNC_CANCEL` for all pending ops on the fd
  2. Wait for cancellation CQEs (buffers are NOT safe to reuse until these arrive)
  3. Submit `IORING_OP_CLOSE`
  4. Wait for close CQE
  5. Only NOW free buffers, clear the `waiting[fd]` slot, return connection to pool
- [x] Handle partial sends: if send CQE reports less than requested, resubmit remainder
- [ ] Handle partial reads: accumulate into buffer until a complete request is available
- [ ] Implement clean shutdown: cancel all in-flight ops, close all connections, drain all CQEs
- [x] Write `tests/test_connection_lifecycle.py`:
  - Send a 1MB payload, assert the full 1MB echoes back (exercises partial
    send/recv reassembly)
  - Open a connection, send half a message, close the socket. Open a new
    connection immediately — assert the new connection works (exercises fd reuse)
  - Open 50 connections, close them all at once, then open 50 more — assert
    all 50 new connections work (exercises connection pool reuse)
- [x] Zig tests in `src/connection.zig`:
  - Test the connection pool: allocate N, free them, allocate N again — assert
    the same slots are reused
  - Test user_data encode/decode round-trip
  - Test generation validation in pool.lookup
  - Test pool exhaustion
- [x] Gate: `zig build test`, `uv run pytest`, `uv run mypy theframework/ tests/` pass

**Architectural notes:**
- **File descriptor reuse (completion model makes this harder):** Linux reuses fd
  numbers aggressively. Because close is async in io_uring, there's a window
  between submitting close and the close CQE where the fd *might* already be
  reused by a new accept on another ring. Use a generation counter per fd slot:
  store `(fd, generation)` in SQE user_data. When reaping CQEs, discard if the
  generation doesn't match. Only increment generation and clear the slot after
  the close CQE arrives.
- **Greenlet lifecycle:** When a connection closes, its greenlet must be properly
  finalized. A greenlet that's switched away from and never switched back to will
  be garbage collected by Python. But any Zig-side state associated with it must
  also be freed.
- **Memory pressure:** Each connection needs buffer space. With 100K connections,
  that's 100K × buffer_size. Use small initial buffers and grow on demand, or use
  io_uring provided buffers (Phase 2.2) to share a pool.

---

## Phase 5 — HTTP Parsing ✅ DONE

### Step 5.1: HTTP/1.1 request parser using hparse ✅ DONE

**Deliverable:** Integrate [hparse](https://github.com/nikneym/hparse) — a
zero-copy, SIMD-accelerated HTTP/1.1 parser written in pure Zig — and wrap it
in a thin `src/http.zig` module that provides a `Request` struct for the rest
of the framework. Standalone — testable without io_uring or greenlets.

**Why hparse:**
- Zero-copy, zero-alloc: all returned slices point into the caller's buffer.
- SIMD-accelerated via Zig's `@Vector` — benchmarks show ~12% faster than
  picohttpparser with 85% lower memory overhead.
- Streaming-first: returns `error.Incomplete` when the buffer holds a partial
  request, so the caller can read more data and retry without re-parsing.
- Pure Zig, no C dependency — integrates via `zig fetch`.

**hparse API (from `src/root.zig`):**
```zig
pub const Method = enum(u8) { unknown, get, post, head, put, delete, connect, options, trace, patch };
pub const Version = enum(u1) { @"1.0", @"1.1" };
pub const Header = struct { key: []const u8, value: []const u8 };
pub const ParseRequestError = error{ Incomplete, Invalid };

pub fn parseRequest(
    slice: []const u8,
    method: *Method,
    path: *?[]const u8,
    version: *Version,
    headers: []Header,
    header_count: *usize,
) ParseRequestError!usize   // returns bytes consumed
```

**TODO:**
- [x] Add hparse dependency:
  ```sh
  zig fetch --save https://github.com/nikneym/hparse/archive/4fb3d836e804eab0469bb128f521ad00b1085731.tar.gz
  ```
- [x] Update `build.zig`:
  - Add hparse dependency and import its module
  - Wire it into the extension module and the http.zig module
- [x] Create `src/http.zig` — thin wrapper around hparse:
  - `Request` struct: `method: Method`, `path: []const u8`, `version: Version`,
    `headers: []Header` (slice into a fixed `[64]Header` buffer),
    `header_count: usize`, `body: []const u8`, `bytes_consumed: usize`
  - `parseRequest(buf: []const u8) -> ParseError!Request`
    Calls `hparse.parseRequest`, then extracts the body based on
    `Content-Length` header (or returns `error.Incomplete` if body is truncated).
    Returns a `Request` with all slices pointing into `buf`.
  - `findHeader(headers: []Header, name: []const u8) -> ?[]const u8`
    Case-insensitive header lookup helper.
  - `isKeepAlive(req: Request) -> bool`
    Returns true if `Connection: keep-alive` or HTTP/1.1 default.
- [x] Handle edge cases:
  - Incomplete request (need more data) — `hparse` returns `error.Incomplete`,
    our wrapper propagates it
  - Invalid request — `hparse` returns `error.Invalid`, handler sends 400
  - `Content-Length` body — after header parse succeeds, check if
    `buf[bytes_consumed..].len >= content_length`; if not, return `error.Incomplete`
  - `Connection: keep-alive` vs `close` — `isKeepAlive()` helper
  - Oversized headers (>8KB) — enforced at the read layer (Phase 5.3),
    not in the parser
  - `Transfer-Encoding: chunked` — deferred to a follow-up; initial version
    requires `Content-Length` for request bodies
- [x] Zig tests in `src/http.zig` — feed raw bytes, assert parsed result:
  - A complete `GET /path HTTP/1.1\r\nHost: localhost\r\n\r\n` → assert
    method == `.get`, path == `"/path"`, version == `.@"1.1"`,
    Host header present, body empty
  - A `POST` with `Content-Length: 5` body `"hello"` → assert body == `"hello"`
  - A truncated request (missing final `\r\n`) → assert `error.Incomplete`
  - Garbage bytes → assert `error.Invalid`
  - Two pipelined requests in one buffer → parse first, advance by
    `bytes_consumed + content_length`, parse second — assert both correct
  - A POST where headers are complete but body is short → assert `error.Incomplete`
  - `Connection: close` → `isKeepAlive` returns false
  - HTTP/1.1 with no Connection header → `isKeepAlive` returns true (default)
- [x] Gate: `zig build test` passes

**Architectural notes:**
- hparse is zero-copy: all string fields are slices into the original buffer.
  The buffer must outlive the `Request` struct.
- hparse handles partial headers natively (`error.Incomplete`). Our wrapper
  adds body-completeness checking on top.
- HTTP pipelining: `bytes_consumed` from `parseRequest` tells you exactly where
  the next request starts. The connection handler can loop: parse → handle →
  advance buffer → parse again.
- For future chunked transfer-encoding support, add a `ChunkedReader` that
  wraps `green_recv` and decodes chunks incrementally. This can be added
  without changing the `Request` struct (body becomes a reader instead of a
  slice).

---

### Step 5.2: HTTP response writer in Zig ✅ DONE

**Deliverable:** A Zig module that serializes HTTP responses (status line, headers,
body) into a buffer for sending.

**TODO:**
- [x] Create `src/http_response.zig`:
  - `writeResponse(writer, status, headers, body)` — write a complete response
  - `writeHead(writer, status, headers)` — write status + headers only (for streaming)
  - `writeChunk(writer, data)` — write a chunked transfer encoding chunk
  - `writeEnd(writer)` — write the final 0-length chunk
- [x] Support `Content-Length` responses (when body size is known) and chunked
  responses (when streaming)
- [x] Support common status codes (200, 201, 204, 301, 400, 404, 500)
- [x] Zig tests in `src/http_response.zig` — call writer, assert output bytes:
  - Write a 200 response with a body → assert the full output matches the
    expected raw HTTP bytes (`HTTP/1.1 200 OK\r\nContent-Length: ...\r\n\r\n...`)
  - Write a chunked response (head + 2 chunks + end) → assert the output is
    valid chunked encoding that a standard HTTP parser would accept
  - Write a 404 with no body → assert `Content-Length: 0`
- [x] Round-trip test: generate a response with the writer, parse it back with
  the parser from 5.1 (if applicable) or verify against the raw expected bytes
- [x] Gate: `zig build test` passes

---

### Step 5.3: HTTP server integration ✅ DONE

**Deliverable:** The io_uring + greenlet echo server from Phase 4 now speaks HTTP.
`curl http://localhost:PORT/` returns "hello world" with proper HTTP headers.

**TODO:**
- [x] In the connection handler greenlet:
  1. `green_recv` until a complete HTTP request is parsed
  2. Extract method and path
  3. Generate an HTTP response with "hello world" body
  4. `green_send` the response
  5. If keep-alive, loop back to step 1. If close, close fd.
- [x] Handle `Connection: keep-alive` — reuse the connection greenlet
- [ ] Handle request pipelining (optional, low priority)
- [x] Write `tests/test_http_server.py` — use stdlib `http.client` or raw sockets:
  - Send a `GET /` request, assert the full response is a valid HTTP response
    with status 200 and body `b"hello world"` (parse the raw bytes, don't assert
    headers individually — check the whole response is well-formed)
  - Send two requests on the same connection (keep-alive), assert both get
    correct responses
  - Send a request with `Connection: close`, assert the server closes the
    connection after responding
- [ ] Benchmark: requests/sec for "hello world" response
- [x] Gate: `uv run pytest`, `uv run mypy theframework/ tests/` pass

**Implementation notes:**
- Exposed `http_parse_request()` and `http_format_response()` as Python-callable
  extension methods bridging Zig's http.zig and http_response.zig to Python.
- Added `parseRequestFull()` to http.zig — resolves keep-alive while headers
  are still on the stack (avoids dangling-slice UB from `parseRequest` + `isKeepAlive`).
- Added `green_close(fd)` to properly close fds through the connection pool.
- HTTP handler loop runs in Python using `green_recv` / `http_parse_request` /
  `http_format_response` / `green_send` — same pattern as the echo handler
  but with HTTP framing.

---

## Phase 6 — Python Framework API ✅ DONE

### Step 6.1: Request and Response objects ✅ DONE

**Deliverable:** Python `Request` and `Response` classes that wrap the parsed HTTP
data and provide a clean API for handlers.

**TODO:**
- [x] Create `theframework/request.py`:
  - `Request.method` — str
  - `Request.path` — str
  - `Request.headers` — dict-like (case-insensitive keys)
  - `Request.body` — bytes (read on demand via greenlet I/O)
  - `Request.query_params` — parsed query string
  - `Request.json()` — parse body as JSON
- [x] Create `theframework/response.py`:
  - `Response.status` — int
  - `Response.headers` — dict
  - `Response.write(data)` — buffers data (sent on finalize)
  - `Response.set_header(name, value)`
  - `Response.set_status(code)`
  - Buffered response model: handler writes, framework sends with Content-Length on finalize
- [x] `Response._finalize()` formats HTTP response and calls `_framework_core.green_send()`
- [x] Write `tests/test_request_response.py` — start a real server, use real HTTP:
  - `POST /echo` with a JSON body → assert the handler can read `request.json()`
    and the client receives the echoed JSON back as a valid HTTP response
  - `GET /path?foo=bar&baz=1` → assert the handler sees the correct query params
    and the response arrives intact
  - Handler sets a custom status and header → assert the raw response contains them
- [x] Gate: `uv run pytest` and `uv run mypy theframework/ tests/` pass

---

### Step 6.2: Router and app object ✅ DONE

**Deliverable:** `@app.route("/path")` decorator, URL matching, handler dispatch.

**TODO:**
- [x] Create `theframework/app.py`:
  - `Framework` class with `route(path, methods=["GET"])` decorator
  - Path matching: exact match first, then parameterized (`/users/{id}`)
  - `dispatch(request)` → find handler, call it with (request, response)
  - 404 default handler for unmatched routes
  - 405 for wrong method
- [x] Path parameter extraction: `/users/{id}` → `request.params["id"]`
- [x] Create `theframework/server.py`:
  - `Framework.run(host, port)` — start the server
  - Wires together: socket setup → io_uring hub → accept loop → per-connection
    greenlet → parse HTTP → dispatch → handler → send response
- [x] Write `tests/test_routing.py` — start a real server, use real HTTP:
  - Register 3 routes (`/a`, `/b`, `/users/{id}`). Send requests to each, assert
    correct handler responded (check the response body, not internals)
  - `GET /nonexistent` → assert 404 response
  - `POST /a` when `/a` only allows GET → assert 405 response
  - `GET /users/42` → assert the handler received `id=42` (check via response body)
- [x] Gate: `uv run pytest` and `uv run mypy theframework/ tests/` pass

---

### Step 6.3: Middleware ✅ DONE

**Deliverable:** Middleware chain that wraps handlers. Each middleware can run code
before/after the handler and short-circuit the chain.

**TODO:**
- [x] Define middleware interface:
  ```python
  def logging_middleware(request, response, next):
      print(f"{request.method} {request.path}")
      next(request, response)
      print(f"→ {response.status}")
  ```
- [x] `app.use(middleware)` — register middleware
- [x] Middleware chain executes in registration order, innermost is the route handler
- [x] Short-circuit: if a middleware writes a response without calling next, the
  handler is skipped (e.g., auth middleware returning 401)
- [x] Write `tests/test_middleware.py` — start a real server, use real HTTP:
  - Register a middleware that adds a custom header `X-Middleware: hit` to every
    response. Send a request, assert the header is present in the raw response.
  - Register an auth middleware that returns 401 if a header is missing. Send a
    request without the header → assert 401. Send with the header → assert 200
    and the handler's body (proves the chain continued).
  - Register two middlewares that each append to a response header (e.g.,
    `X-Order: first,second`). Assert the header value proves execution order.
- [x] Gate: `uv run pytest` and `uv run mypy theframework/ tests/` pass

---

## Phase 7 — Pre-fork Process Model

### Step 7.1: Master-worker fork

**Deliverable:** A master process that forks N worker processes. Each worker runs
its own io_uring hub and greenlet scheduler. All workers bind to the same socket.

**TODO:**
- [ ] Implement in Python (process management doesn't need to be in Zig):
  - Master creates the listen socket with `SO_REUSEPORT`
  - Master forks N children (N = cpu_count by default, configurable)
  - Each child sets up its own io_uring ring and greenlet hub
  - Each child calls `accept` on the shared socket
- [ ] Signal handling:
  - Master catches SIGTERM/SIGINT → sends SIGTERM to all workers → waits → exits
  - Master catches SIGCHLD → if a worker dies, fork a replacement
  - Workers catch SIGTERM → stop accepting, drain connections, exit
- [ ] Graceful shutdown: workers finish in-flight requests before exiting
- [ ] Write `tests/test_prefork.py` — real processes, real signals:
  - Start the server with `workers=2`. Send a request, assert 200 (proves at
    least one worker is alive and serving).
  - Find worker PIDs (from master's internal tracking or `/proc`), kill one
    with SIGKILL. Wait briefly, send another request — assert 200 (proves
    master respawned a replacement).
  - Send SIGTERM to master. Wait for exit. Assert master process has exited and
    no orphan workers remain.
- [ ] Gate: `uv run pytest` and `uv run mypy theframework/ tests/` pass

**Architectural notes:**
- **Fork before or after io_uring init?** After. Each worker must have its own
  io_uring ring. io_uring rings are tied to the process that created them —
  they don't survive fork.
- **Fork before or after Python module imports?** Before heavy imports to benefit
  from copy-on-write memory sharing. But after loading the Zig extension, since
  the ring must be per-process.
- **SO_REUSEPORT:** Each worker binds its own socket to the same address:port.
  The kernel distributes incoming connections across them. No single-process
  accept bottleneck. This replaced the old SO_REUSEADDR + fork-after-listen model.
- **Worker death:** The master must handle SIGCHLD and respawn. Use a simple
  loop: `while worker_count < N: fork()`.

---

### Step 7.2: Unix socket mode

**Deliverable:** Workers listen on a Unix domain socket instead of TCP, for deployment
behind Caddy/Nginx.

**TODO:**
- [ ] Support `app.run(unix="/tmp/framework.sock")`
- [ ] Create the socket, bind, chmod 666 (or configurable)
- [ ] Workers use SO_REUSEPORT on the Unix socket
- [ ] Clean up socket file on shutdown
- [ ] Write `tests/test_unix_socket.py` — real Unix socket:
  - Start server on a tmpdir socket path. Connect via `socket.AF_UNIX`, send
    a raw HTTP request, assert a valid HTTP response comes back.
  - After server shutdown, assert the socket file is cleaned up (doesn't exist).
- [ ] Gate: `uv run pytest` and `uv run mypy theframework/ tests/` pass

---

## Phase 8 — Streaming and WebSockets

### Step 8.1: Streaming responses

**Deliverable:** Handlers can call `response.write()` multiple times. The first call
sends headers with `Transfer-Encoding: chunked`. Subsequent calls send chunks.

**TODO:**
- [ ] First `response.write()` call:
  - Serialize status + headers + `Transfer-Encoding: chunked`
  - green_send the head
  - green_send the first chunk (with chunk length prefix)
- [ ] Subsequent `response.write()` calls: send a chunk
- [ ] When the handler returns: send the terminating 0-length chunk
- [ ] If only one `write()` is called and the handler returns, use `Content-Length`
  instead of chunked (optimization: known-size responses are simpler for clients)
- [ ] Write `tests/test_streaming.py` — real HTTP, no mocks:
  - Handler calls `response.write()` three times with different chunks. Client
    reads the response — assert the full body is the concatenation of all chunks
    and the transfer encoding is chunked.
  - Handler calls `response.write()` once with a known body. Assert the response
    uses `Content-Length` (not chunked).
  - SSE test: handler writes 3 SSE events with small delays. Client reads
    incrementally — assert all 3 events arrive and are correctly framed.
- [ ] Gate: `uv run pytest` and `uv run mypy theframework/ tests/` pass

---

### Step 8.2: WebSocket support

**Deliverable:** `@app.websocket("/ws")` decorator. `ws.recv()` and `ws.send()`
as blocking calls in a greenlet. WebSocket upgrade handshake.

**TODO:**
- [ ] Implement WebSocket upgrade:
  - Detect `Upgrade: websocket` header
  - Validate `Sec-WebSocket-Key`
  - Respond with 101 Switching Protocols + `Sec-WebSocket-Accept`
- [ ] Implement WebSocket frame parser in Zig:
  - Parse frame header (fin, opcode, mask, payload length)
  - Handle masking (client → server frames are masked)
  - Handle fragmented frames
  - Handle control frames (ping, pong, close)
- [ ] Implement `ws.recv()`: green_recv a frame, return payload
- [ ] Implement `ws.send(data)`: frame the payload, green_send
- [ ] Implement `ws.close()`: send close frame, wait for close frame, close fd
- [ ] Update `_framework_core.pyi` stub with WebSocket frame functions
- [ ] Zig tests in `src/ws.zig` (or wherever the frame parser lives):
  - Encode a frame, decode it back — assert the payload round-trips correctly
  - Decode a masked frame (as clients send) — assert unmasked payload matches
  - Decode a close frame — assert the opcode is detected correctly
- [ ] Write `tests/test_websocket.py` — real TCP, real WebSocket handshake:
  - Perform a WebSocket upgrade via raw socket (send the Upgrade request, read
    the 101 response, validate `Sec-WebSocket-Accept`). Then send a text frame,
    assert the echo handler sends it back as a valid WebSocket text frame.
  - Send a close frame, assert the server responds with a close frame and the
    connection terminates.
- [ ] Gate: `zig build test`, `uv run pytest`, `uv run mypy theframework/ tests/` pass

---

## Phase 9 — Compatibility and Fallback

### Step 9.0: Cooperative I/O primitives ✅ DONE

**Deliverable:** New Zig-level primitives — `green_sleep`, `green_connect`,
`green_register_fd` — that allow greenlets to perform operations beyond the
accept/recv/send/close set. These are the foundation that monkey patching
(Step 9.2) redirects stdlib calls to, and they're immediately useful in
handlers (e.g., sleeping, making outbound HTTP requests).

**Why this is needed:**

The current hub architecture ties every io_uring operation to the connection
pool. User data encoding is `pool_index(16b) | generation(32b) | reserved(16b)`,
and CQE dispatch looks up the pool to find the waiting greenlet. This works
for server-side socket I/O but breaks for:

- **Timeouts** (`IORING_OP_TIMEOUT`) — no fd, no connection, nowhere to store
  the waiting greenlet.
- **Outbound connects** (`IORING_OP_CONNECT`) — the fd is created by Python
  (or by `socket()` syscall), not by `accept()`, so it's not in the pool.
- **Arbitrary fds** — monkey-patched `socket.socket` creates its own fds.
  `green_recv`/`green_send` reject them because `getConn()` returns null.

**Architecture change: Operation slots**

Add a fixed-size array of "operation slots" alongside the connection pool.
Each slot stores a greenlet pointer, result, and generation counter — just
like a Connection, but not tied to an fd.

```
Operation slot table (256 slots):
┌─────────┬──────────────┬────────────┬────────┐
│ slot_idx│ greenlet     │ generation │ result │
├─────────┼──────────────┼────────────┼────────┤
│ 0       │ <PyObject*>  │ 17         │ 0      │
│ 1       │ null         │ 3          │ -      │
│ ...     │              │            │        │
└─────────┴──────────────┴────────────┴────────┘
```

**User data discrimination:**

Use bit 63 as a discriminator. The connection pool never sets bit 63 (pool
indices are ≤4096, well within 16 bits). Sentinels (STOP, ACCEPT) have
distinctive values that don't collide.

- `bit 63 = 0`: connection pool operation (existing, unchanged)
- `bit 63 = 1`: operation slot — `slot_index(15b) | generation(32b) | reserved(16b)`

CQE dispatch in `hubLoop()` adds one check before the pool lookup:

```
if (user_data has bit 63 set):
    decode slot_index and generation
    lookup in op_slot table, validate generation
    store result, wake greenlet
else:
    existing pool-based dispatch (unchanged)
```

**TODO:**

- [x] Create `src/op_slot.zig`:
  - `OpSlot` struct: `greenlet: ?*anyopaque`, `result: i32`, `generation: u32`
  - `OpSlotTable` with fixed-size array (256 slots), free list, acquire/release
  - `encodeUserData(slot_index, generation)` — sets bit 63
  - `decodeSlotIndex(user_data)`, `decodeGeneration(user_data)`
  - `isOpSlot(user_data) -> bool` — checks bit 63
  - Zig tests: acquire/release/reuse, user_data round-trip, generation validation

- [x] Update `src/hub.zig` — add OpSlotTable, update CQE dispatch:
  - Add `op_slots: OpSlotTable` to Hub struct
  - In CQE loop: check `OpSlotTable.isOpSlot(user_data)` before pool lookup
  - If op slot: validate generation, store result, wake greenlet, release slot

- [x] Implement `green_sleep(seconds: float) -> None`:
  - Acquire an op slot, store current greenlet
  - Convert seconds to `kernel_timespec` (tv_sec + tv_nsec)
  - Submit `IORING_OP_TIMEOUT` SQE with op-slot user_data
  - Yield to hub
  - Resume: release op slot, check result (ETIME is success), return None
  - Note: `Ring` needs a new `prepTimeout(ts, user_data)` method

- [x] Implement `green_connect(host: str, port: int) -> int`:
  - Create TCP socket via `posix.socket(AF_INET, SOCK_STREAM, 0)`
  - Register fd in connection pool (acquire slot, set fd, map fd_to_conn)
  - Acquire an op slot, store current greenlet
  - Build `sockaddr_in` from host/port
  - Submit `IORING_OP_CONNECT` SQE with op-slot user_data
  - Yield to hub
  - Resume: release op slot, check result (0 = success), return fd
  - On error: release connection, raise OSError
  - Note: `Ring` needs a new `prepConnect(fd, addr, user_data)` method

- [x] Implement `green_register_fd(fd: int) -> None`:
  - Acquire a connection pool slot
  - Set fd, map fd_to_conn[fd] = pool_index
  - Return None
  - This lets monkey-patched sockets use green_recv/green_send
  - If pool exhausted, raise RuntimeError

- [x] Implement `green_unregister_fd(fd: int) -> None`:
  - Lookup connection by fd
  - Release the pool slot, clear fd_to_conn mapping
  - Does NOT close the fd (Python owns the socket lifecycle)

- [x] Update `src/ring.zig`:
  - Add `prepTimeout(ts: *const kernel_timespec, user_data: u64)` method
  - Add `prepConnect(fd: fd_t, addr: *const sockaddr, addrlen: u32, user_data: u64)` method

- [x] Update `src/extension.zig` — add Python-callable wrappers:
  - `green_sleep(args)` → calls `Hub.greenSleep()`
  - `green_connect(args)` → calls `Hub.greenConnect()`
  - `green_register_fd(args)` → calls `Hub.greenRegisterFd()`
  - `green_unregister_fd(args)` → calls `Hub.greenUnregisterFd()`

- [x] Update `stubs/_framework_core.pyi`:
  - `def green_sleep(seconds: float) -> None: ...`
  - `def green_connect(host: str, port: int) -> int: ...`
  - `def green_register_fd(fd: int) -> None: ...`
  - `def green_unregister_fd(fd: int) -> None: ...`

- [x] Zig tests in `src/op_slot.zig`:
  - Acquire N slots, release them, acquire N again — assert reuse
  - User data encode/decode round-trip with bit 63 set
  - Generation validation: stale lookup returns null

- [x] Write `tests/test_cooperative_io.py` — real io_uring, real greenlets:
  - `test_green_sleep`: handler calls `green_sleep(0.1)`, measure wall time,
    assert it took ≥100ms. Send a second request concurrently to a fast handler,
    assert the fast handler responds while the sleeper is still sleeping (proves
    the event loop isn't blocked).
  - `test_green_connect`: start a second TCP server on an ephemeral port. From
    a handler, call `green_connect(host, port)`, then `green_send` + `green_recv`
    on the returned fd, assert round-trip works. Proves outbound connections work
    cooperatively.
  - `test_register_fd`: from a handler, create a `socket.socket()` in Python,
    call `green_register_fd(sock.fileno())`, then use `green_send`/`green_recv`
    on it. Assert data round-trips correctly. Proves external fds can be brought
    into the io_uring hub.

- [x] Gate: `zig build test`, `uv run pytest`, `uv run mypy theframework/ tests/` pass

**Implementation notes:**

- Op slot table implemented in `src/op_slot.zig` with 256 slots, bit-63
  discriminated user_data, and generation-validated lookups.
- Hub CQE dispatch checks `isOpSlot(user_data)` before pool lookup.
- `green_sleep` uses `IORING_OP_TIMEOUT` via stdlib's `IoUring.timeout()`.
  `-ETIME` is treated as success (normal timeout expiry).
- `green_connect` creates a socket, registers it in the pool, then submits
  `IORING_OP_CONNECT` via op slot. On error, cleans up both pool and socket.
- `green_register_fd` / `green_unregister_fd` manage pool membership for
  externally-created fds without any I/O operations.
- IPv4 addresses parsed by a simple `parseIpv4` helper in hub.zig.
  DNS resolution is synchronous (deferred to a follow-up).

**Architectural notes:**

- **Op slot pool size (256):** Much smaller than the connection pool (4096).
  Each op slot is transient — acquired before submitting the SQE, released when
  the CQE arrives. Even under heavy load, 256 concurrent non-connection
  operations (timeouts, connects) is generous. Can be increased later.
- **IORING_OP_TIMEOUT result:** The kernel returns `-ETIME` when the timeout
  expires normally. This is not an error — it's the expected success case.
  The implementation must treat `ETIME` as success and only raise on other
  negative results.
- **green_connect vs green_register_fd:** `green_connect` is the high-level
  primitive (create socket + connect + register, all cooperative). `green_register_fd`
  is the low-level escape hatch for monkey patching, where Python code creates
  the socket itself and just needs to register it for cooperative I/O.
- **DNS resolution:** `green_connect` takes a host string. For the initial
  implementation, resolve synchronously with `getaddrinfo` (blocking but fast
  for IP addresses and cached hostnames). True async DNS is a follow-up.
- **File I/O:** `IORING_OP_READ`/`IORING_OP_WRITE` for files can use the same
  op-slot mechanism. Deferred to a follow-up step — file I/O needs offset
  tracking and different buffer management. The op-slot architecture supports
  it without further changes to the dispatch logic.

---

### Step 9.2: Monkey patching with gevent

**Prerequisite:** Step 9.0 (cooperative I/O primitives). The patching layer
redirects stdlib calls to `green_sleep`, `green_connect`, `green_register_fd`,
`green_recv`, `green_send`, `green_close`.

**Deliverable:** `theframework.patch_all()` patches stdlib socket/ssl/time/select
so third-party libraries work transparently with greenlets.

**TODO:**
- [ ] `uv add --optional compat gevent` (optional dependency group for compat layer)
- [ ] Research gevent's patching mechanism — can we use it independently of
  gevent's hub? Or do we need to run gevent's hub alongside ours?
- [ ] Option A: Use gevent's hub as the fallback (selectors backend) and our
  io_uring hub for framework I/O. Gevent-patched libraries go through gevent.
- [ ] Option B: Patch stdlib modules to use our own greenlet I/O primitives.
  More work, but no gevent dependency. Now feasible thanks to Step 9.0.
- [ ] Start with Option A — it's proven and lower risk
- [ ] Write `tests/test_monkey_patch.py` — real outbound I/O from inside a handler:
  - Register a handler that uses `urllib.request.urlopen` (stdlib, no extra dep)
    to fetch a URL. Start a second local HTTP server as the target. Assert the
    handler completes and the client gets the proxied response. Proves patched
    socket I/O doesn't block the event loop.
  - While that handler is "blocked" on outbound I/O, send a second request to a
    fast handler on the same server — assert it responds without waiting for the
    first. Proves concurrency is maintained.
- [ ] Gate: `uv run pytest` and `uv run mypy theframework/ tests/` pass

---

## Phase 10 — Hardening and Performance

### Step 10.1: Timeouts

**TODO:**
- [ ] Connection read timeout: if no complete request arrives within N seconds, close
- [ ] Keep-alive timeout: if no new request arrives within N seconds, close
- [ ] Handler timeout: if a handler takes more than N seconds, kill its greenlet
  and return 504
- [ ] Implement using io_uring timeout SQEs (`IORING_OP_TIMEOUT`) or a timer fd
- [ ] Write `tests/test_timeouts.py`:
  - Connect a socket but send nothing. Assert the server closes the connection
    within the configured read timeout (check that recv returns empty / EOF).
  - Send a valid request to a handler that sleeps longer than the handler timeout.
    Assert the client receives a 504 response.
- [ ] Gate: `uv run pytest` and `uv run mypy theframework/ tests/` pass

---

### Step 10.2: Backpressure

**TODO:**
- [ ] Limit max concurrent connections per worker (reject with 503 when full)
- [ ] Limit request body size (return 413 when exceeded)
- [ ] Limit header size (return 431 when exceeded)
- [ ] If the SQ is full, stop accepting new connections until space frees up
- [ ] Write `tests/test_backpressure.py`:
  - Configure `max_connections=5`. Open 6 connections — assert the 6th gets
    a 503 or connection refused.
  - Send a request with a body larger than `max_body_size` — assert 413 response.
  - Send a request with headers exceeding `max_header_size` — assert 431 response.
- [ ] Gate: `uv run pytest` and `uv run mypy theframework/ tests/` pass

---

### Step 10.3: Benchmarking and profiling

**TODO:**
- [ ] Set up a reproducible benchmark: `wrk -t4 -c1000 -d30s`
- [ ] Measure: requests/sec, latency p50/p99/p999, memory usage, CPU usage
- [ ] Compare against: uvicorn, gunicorn+gevent, Go net/http
- [ ] Profile Zig code with `perf` and flamegraphs
- [ ] Profile Python code: `uv add --dev py-spy` then `uv run py-spy`
- [ ] Identify bottlenecks and optimize

---

## Phase 11 — CPU Offloading

### Step 11.1: framework.offload()

**Deliverable:** `framework.offload(func, *args)` runs a CPU-bound function in a
subprocess and returns the result to the calling greenlet without blocking the event loop.

**TODO:**
- [ ] Spawn a pool of worker subprocesses (configurable size)
- [ ] Communication via Unix socketpairs or pipes (io_uring can do I/O on pipes)
- [ ] `offload(func, *args)`:
  1. Pickle the function and args
  2. Send to an available subprocess via pipe
  3. Yield the greenlet (wait for I/O completion)
  4. Subprocess runs the function, pickles the result, sends back
  5. CQE arrives on the pipe → hub resumes the greenlet
  6. Greenlet receives the result
- [ ] Handle subprocess death: respawn and return an error to the waiting greenlet
- [ ] Write `tests/test_offload.py`:
  - Handler calls `offload(compute_sum, 1, 2)` where `compute_sum` returns `3`.
    Assert the HTTP response contains `3`. Proves the round-trip works.
  - Register two handlers: one calls `offload` with a CPU-heavy function (e.g.,
    `sum(range(10**7))`), the other returns immediately. Send both requests
    concurrently — assert the fast handler responds without waiting for the
    slow one. Proves I/O handlers aren't starved.
- [ ] Gate: `uv run pytest` and `uv run mypy theframework/ tests/` pass

---

## Phase 12 — Compatibility Fallback

### Step 12.1: selectors fallback for macOS/non-Linux

**Deliverable:** When io_uring is not available, the hub falls back to a
`selectors`-based event loop. Same greenlet model, same Python API, just slower I/O.

**TODO:**
- [ ] Create `theframework/backends/selector_hub.py`:
  - Same hub interface as the Zig io_uring hub
  - Uses `selectors.DefaultSelector` (kqueue on macOS, epoll on Linux)
  - `green_accept`, `green_recv`, `green_send` — same API, implemented in Python
- [ ] Auto-detect at import time: if io_uring Zig module loads, use it.
  Otherwise, fall back to selectors.
- [ ] Document: selectors backend is for development only, not production
- [ ] Re-run the existing test suite (`tests/test_http_server.py`, `tests/test_routing.py`,
  etc.) with the selectors backend forced on. All must pass — same behavior,
  different I/O engine. Use an env var or pytest fixture to select the backend.
- [ ] Gate: `uv run pytest` and `uv run mypy theframework/ tests/` pass

---

## Appendix: Key Risks and Mitigations

### Zig @cImport of Python.h
Python.h uses complex macros, conditional compilation, and platform-specific
defines. Zig's C translation may choke on some of them. Mitigation: create a
minimal `framework_python.h` that includes only the functions you need, with
explicit prototypes instead of relying on macro expansion.

### Greenlet stack switching vs Zig's stack
Greenlet swaps C stacks by saving/restoring registers and the stack pointer.
Zig-generated code on those stacks will survive the switch, but Zig's safety
runtime (stack canaries, overflow detection) may be confused by the sudden stack
change. Mitigation: test thoroughly in both Debug and ReleaseSafe modes. If
needed, disable stack probes for hub functions.

### io_uring kernel version fragmentation
Features like multishot accept (5.19), provided buffers (5.7/5.19), and
IORING_SETUP_COOP_TASKRUN (5.19) aren't available on older kernels. Mitigation:
probe at startup (`io_uring_probe`), use features conditionally, require a
minimum kernel (e.g., 6.1 LTS).

### fd reuse races
When a connection closes and a new one opens, the kernel may assign the same fd.
If a pending CQE for the old connection arrives after the new connection starts,
it will corrupt state. Mitigation: use a generation counter per fd slot. Store
`(fd, generation)` in SQE user_data. When reaping CQEs, discard if generation
doesn't match.

### Greenlet memory and GC
Each greenlet has a C stack (default ~800KB virtual, ~few KB resident). 100K
greenlets = 100K stacks. Python's GC must be able to collect dead greenlets.
Mitigation: keep greenlet stacks small, reuse greenlet objects via a pool,
ensure no reference cycles between greenlets and connection state.

### SQPOLL security
SQPOLL mode (`IORING_SETUP_SQPOLL`) creates a kernel thread that continuously
polls the submission queue — zero syscalls for I/O. But it requires elevated
privileges (CAP_SYS_ADMIN or io_uring restrictions in recent kernels).
Mitigation: make SQPOLL opt-in, document the privilege requirements.
