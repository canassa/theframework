# Arena Overhaul: Zero-Copy Request Pipeline

Redesign the request read path to eliminate all unnecessary copies, double-parsing,
and O(n^2) accumulation. Inspired by h2o's memory architecture: parse once in Zig,
pass structured results to Python, recycle everything.

---

## Table of Contents

1. [Current Problems](#1-current-problems)
2. [Goals](#2-goals)
3. [Architecture Overview](#3-architecture-overview)
4. [New Component: InputBuffer](#4-new-component-inputbuffer)
5. [New Component: BufferRecycler](#5-new-component-bufferrecycler)
6. [New FFI: `http_read_request`](#6-new-ffi-http_read_request)
7. [Header Pipeline: Parse Once](#7-header-pipeline-parse-once)
8. [Response Formatting in Zig](#8-response-formatting-in-zig)
9. [Request Lifecycle (End-to-End)](#9-request-lifecycle-end-to-end)
10. [Edge Cases](#10-edge-cases)
11. [Implementation Phases](#11-implementation-phases)
12. [Testing Strategy](#12-testing-strategy)
13. [Memory Budget](#13-memory-budget)
14. [Safety Analysis](#14-safety-analysis)
15. [Migration & Backward Compatibility](#15-migration--backward-compatibility)

---

## 1. Current Problems

### 1.1 O(n^2) Request Accumulation

`server.py:120-126`:
```python
buf = b""
while True:
    data = _framework_core.green_recv(fd, 8192)
    buf += data   # ← O(n) copy every iteration; total = O(n^2)
```

Python `bytes` is immutable. Every `buf += data` allocates a new object of size
`len(buf) + len(data)` and copies the entire old buffer plus the new data. A 1 MB
POST body arriving in 128 recv calls causes ~64 MB of total copying.

### 1.2 Triple Copy Per Recv

```
Arena chunk (Zig) → Python bytes (copy #1) → buf += data (copy #2) → parse input (copy #3)
```

`greenRecv()` writes into the arena, then immediately copies to a Python bytes object
via `py_helper_bytes_from_string_and_size()`. Python concatenates into `buf`. The
parser then scans `buf`. Three copies of the same data.

### 1.3 Headers Parsed Twice

Zig's `parseRequestFull()` parses all headers (method, path, Content-Length,
Connection, etc.) into a 64-element `[64]Header` stack array. It resolves `keep_alive`
from those headers, then **discards them**. Returns a 5-tuple to Python without headers.

Python re-parses from raw bytes in `Request._from_raw()`:
```python
header_section, _, _ = raw_bytes.partition(b"\r\n\r\n")
lines = header_section.split(b"\r\n")
for line in lines[1:]:
    if b": " in line:
        name, _, value = line.partition(b": ")
        headers[name.decode("latin-1").lower()] = value.decode("latin-1")
```

Problems:
- Same work done twice
- Python parsing is fragile (splits on `": "` with space; headers without space after
  colon are silently dropped)
- `dict` loses duplicate headers (e.g. multiple `Set-Cookie`)
- No validation of header syntax

### 1.4 Arena Used as Scratch Pad Only

The per-connection arena (4 KB inline chunk + ChunkRecycler overflow) is used only as
a transient recv buffer. Data is copied out immediately after each recv. The arena's
core value proposition — zero-copy accumulation, bulk deallocation, zero malloc in
steady state — is not leveraged.

### 1.5 Response Built Entirely in Python

`response.py:55-75` builds the HTTP response via Python f-strings, list join, and
latin-1 encoding. Five+ intermediate allocations per response. The Zig-side
`http_response.writeResponse()` exists but is only used for error rejections
(`sendRejectAndClose`), never for normal responses.

### 1.6 Double Close on `keep_alive=False`

`server.py:154-172`:
```python
if not keep_alive:
    _framework_core.green_close(fd)   # close #1
    return
# ...
finally:
    _framework_core.green_close(fd)   # close #2
```

The `finally` block always runs, so fd is closed twice. Between the two closes, the
kernel may reuse the fd number for a new connection — the second close would kill
someone else's socket.

---

## 2. Goals

| # | Goal | Metric |
|---|------|--------|
| G1 | **Zero O(n^2) accumulation** | Request data accumulated in Zig, never via Python `buf += data` |
| G2 | **Single-copy to Python** | Each field (method, path, headers, body) copied exactly once from Zig to Python |
| G3 | **Parse once** | Headers parsed by hparse in Zig, passed to Python as `list[tuple[bytes, bytes]]` |
| G4 | **Zero malloc on hot path** | After warmup, no malloc/free during request processing (only recycler ops) |
| G5 | **Production edge cases** | Pipelining, partial reads, slow clients, request limits, arena exhaustion, keep-alive compaction |
| G6 | **Zig-side response formatting** | Eliminate Python string concatenation for responses |
| G7 | **No regressions** | All existing tests pass, API backward compatible |

---

## 3. Architecture Overview

### Current Architecture

```
                    Python (server.py)                     Zig (hub.zig)
                    ┌──────────────────┐                  ┌───────────────┐
                    │  buf = b""       │  green_recv(fd)   │ Arena chunk   │
                    │  while True:     │ ◄──── copy ────── │ (4 KB)        │
                    │    data = recv() │                   │ scratch only  │
                    │    buf += data   │ O(n^2)            └───────────────┘
                    │    parse(buf)    │ ◄──── copy ─── parse in Zig,
                    │    re-parse hdrs │                 discard headers
                    │    build Request │
                    │    build Response│ ◄── Python f-strings
                    │    send()       │
                    └──────────────────┘
```

### New Architecture

```
         Python (server.py)                  Zig (hub.zig)
         ┌──────────────────┐               ┌──────────────────────────────┐
         │                  │ read_request() │  InputBuffer (contiguous)    │
         │  result = http_  │ ◄──── once ── │  ┌─────────────────────┐    │
         │    read_request()│                │  │ consumed │ data │free│    │
         │                  │                │  └─────────────────────┘    │
         │  (method, path,  │                │       ↑                     │
         │   headers, body, │                │   recv fills ───┐          │
         │   keep_alive)    │                │                  │          │
         │                  │                │   hparse reads ──┘          │
         │                  │                │   (zero-copy slices)        │
         │  resp.write(..)  │                │                             │
         │  resp._finalize()│ format_resp()  │  http_response.zig         │
         │                  │ ──── once ──►  │  writeResponse(buf,...)     │
         │                  │                │  green_send(fd, buf)        │
         └──────────────────┘               └──────────────────────────────┘
```

### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Contiguous InputBuffer, not arena chunks** | HTTP parser needs contiguous input. Arena chunks are non-contiguous. h2o uses a separate contiguous `h2o_buffer_t` for socket input, arena for processing. |
| **InputBuffer is connection-lifetime** | Persists across keep-alive requests. Leftover bytes from pipelining stay in the buffer. Avoids re-allocating between requests. |
| **RequestArena for per-request allocations (like h2o's `h2o_mem_pool_t`)** | Arena is the per-request bump allocator. Header structs are allocated from it (like h2o allocates `h2o_header_t` from the pool). Header name/value slices are zero-copy pointers into the InputBuffer (like h2o's `h2o_iovec_t` into `sock->input`). Arena is reset between requests — chunks return to recycler. No fixed header count limit. |
| **Power-of-2 buffer recycling** | Like h2o's buffer bins. Buffers are recycled by size class (4 KB, 8 KB, 16 KB, ...). No malloc on hot path after warmup. |
| **Capacity memory (tombstone)** | When an InputBuffer is reset between connections, remember the previous capacity. Next connection starts at that size, preventing re-growth oscillation. |
| **Recv+parse combined in Zig** | The recv loop, accumulation, and parsing all happen in Zig. Python calls one function (`http_read_request`) and gets back structured data. |

---

## 4. New Component: InputBuffer

A contiguous, growable byte buffer for accumulating recv data per connection. Modeled
on h2o's `h2o_buffer_t`.

### 4.1 Data Structure

```zig
// src/input_buffer.zig

pub const InputBuffer = struct {
    /// Pointer to the backing allocation (from recycler or inline).
    data: [*]u8,

    /// Total allocated capacity of the backing buffer.
    capacity: usize,

    /// Offset into `data` where unconsumed data begins.
    /// Advances on consume(), reset to 0 on compact().
    read_pos: usize,

    /// Offset into `data` where data ends / free space begins.
    /// Advances when recv adds data.
    write_pos: usize,

    /// Pointer to the hub-local buffer recycler.
    recycler: *BufferRecycler,

    /// True if `data` points to the inline first chunk (embedded in Connection).
    /// Inline buffers are never returned to the recycler.
    is_inline: bool,

    /// Previous capacity, preserved across reset() for capacity memory.
    /// Prevents re-growth oscillation on repeated connections.
    prev_capacity: usize,
};
```

### 4.2 Memory Layout

```
data ──►┌─────────┬─────────────────────┬──────────────┐
        │consumed │  unconsumed data    │  free space   │
        │ (waste) │  (pending parse)    │  (for recv)   │
        └─────────┴─────────────────────┴──────────────┘
        0       read_pos             write_pos       capacity
```

### 4.3 Operations

```zig
/// Return a read-only slice of unconsumed data (for the parser).
pub fn unconsumed(self: *const InputBuffer) []const u8 {
    return self.data[self.read_pos..self.write_pos];
}

/// Return how many unconsumed bytes are available.
pub fn unconsumedLen(self: *const InputBuffer) usize {
    return self.write_pos - self.read_pos;
}

/// Return a writable slice for recv (free space at the end).
/// Compacts or grows if free space < min_free.
pub fn reserveFree(self: *InputBuffer, min_free: usize) ![]u8 {
    const free = self.capacity - self.write_pos;
    if (free >= min_free) {
        return self.data[self.write_pos..self.capacity];
    }

    // Try compaction first: if consumed bytes > unconsumed, memmove is cheap
    const unconsumed_len = self.write_pos - self.read_pos;
    if (self.read_pos > 0 and unconsumed_len + min_free <= self.capacity) {
        self.compact();
        return self.data[self.write_pos..self.capacity];
    }

    // Must grow
    try self.grow(unconsumed_len + min_free);
    return self.data[self.write_pos..self.capacity];
}

/// Record that `n` bytes were written into the free space (after recv).
pub fn commitWrite(self: *InputBuffer, n: usize) void {
    std.debug.assert(self.write_pos + n <= self.capacity);
    self.write_pos += n;
}

/// Consume `n` bytes from the front (after parsing a request).
/// This is a pointer advance — zero copy, O(1).
pub fn consume(self: *InputBuffer, n: usize) void {
    std.debug.assert(self.read_pos + n <= self.write_pos);
    self.read_pos += n;
}

/// Compact: memmove unconsumed data to front of buffer.
/// Called lazily by reserveFree() when free space is insufficient
/// but consumed space is reclaimable.
fn compact(self: *InputBuffer) void {
    const len = self.write_pos - self.read_pos;
    if (self.read_pos == 0) return;
    if (len > 0) {
        std.mem.copyForwards(u8, self.data[0..len], self.data[self.read_pos..self.write_pos]);
    }
    self.read_pos = 0;
    self.write_pos = len;
}

/// Grow the buffer to at least `min_capacity` bytes.
/// Allocates a new buffer from the recycler, copies unconsumed data, returns old.
fn grow(self: *InputBuffer, min_capacity: usize) !void {
    // Round up to next power of 2 (>= MIN_BUFFER_SIZE)
    const new_cap = nextPow2(min_capacity);
    const new_buf = try self.recycler.acquire(new_cap);

    // Copy unconsumed data to new buffer
    const len = self.write_pos - self.read_pos;
    if (len > 0) {
        @memcpy(new_buf[0..len], self.data[self.read_pos..self.write_pos]);
    }

    // Return old buffer to recycler (unless it's the inline buffer)
    if (!self.is_inline) {
        self.recycler.release(self.data[0..self.capacity]);
    }

    self.data = new_buf.ptr;
    self.capacity = new_cap;
    self.read_pos = 0;
    self.write_pos = len;
    self.is_inline = false;
}

/// Reset for new connection. Returns buffer to recycler, remembers capacity.
/// After reset, buffer uses the inline first chunk again.
pub fn resetForNewConnection(self: *InputBuffer, inline_buf: [*]u8, inline_cap: usize) void {
    self.prev_capacity = self.capacity;
    if (!self.is_inline) {
        self.recycler.release(self.data[0..self.capacity]);
    }
    self.data = inline_buf;
    self.capacity = inline_cap;
    self.read_pos = 0;
    self.write_pos = 0;
    self.is_inline = true;
}

/// Reset for new request on same connection (keep-alive).
/// Preserves unconsumed data (pipelining). Compacts if beneficial.
pub fn resetForNewRequest(self: *InputBuffer) void {
    if (self.read_pos == self.write_pos) {
        // No leftover data — just reset positions
        self.read_pos = 0;
        self.write_pos = 0;
        return;
    }
    // Compact if more than half the buffer is consumed waste
    if (self.read_pos > self.capacity / 2) {
        self.compact();
    }
}
```

### 4.4 Inline Buffer

Each `Connection` embeds a small inline buffer (reusing the existing `inline_chunk`).
For requests that fit within this size (~4 KB), no recycler interaction is needed.

```zig
pub const Connection = struct {
    // ... existing fields ...
    input: InputBuffer,
    inline_buf: [INLINE_BUF_SIZE]u8,  // embedded, replaces inline_chunk

    // arena: RequestArena,  // kept for processing allocations (future use)
};

const INLINE_BUF_SIZE: usize = 4096;
```

The inline buffer handles the common case: simple GET requests (typically 200-800
bytes) and small API requests (< 4 KB). No allocation needed.

### 4.5 Compaction Strategy

Like h2o's `h2o_buffer_try_reserve()` (memory.c:470-478):

```
Before compact:   [consumed: 3500][data: 500][free: 96]   capacity=4096
After compact:    [data: 500][free: 3596]                  capacity=4096
```

**When to compact** (triggered by `reserveFree()` when free space is insufficient):
- `read_pos > 0` (there IS consumed waste to reclaim)
- `unconsumed_len + min_free <= capacity` (compaction yields enough space)

**When NOT to compact** (grow instead):
- Even after compaction, not enough space → must grow to a larger power-of-2

Compaction is a `memmove` of `unconsumed_len` bytes. For most requests, this is small
(< 100 bytes of pipelined leftovers). For worst case (pipelining with large partial
request), it's bounded by `capacity`.

---

## 5. New Component: BufferRecycler

Power-of-2 buffer recycling, modeled on h2o's buffer bin system. One per hub (hub-local,
effectively thread-local per worker process).

### 5.1 Data Structure

```zig
// src/buffer_recycler.zig

const MIN_POWER: u5 = 12;   // 4096 = 2^12
const MAX_POWER: u5 = 24;   // 16 MB = 2^24
const NUM_BINS: usize = MAX_POWER - MIN_POWER + 1;  // 13 bins

pub const BufferRecycler = struct {
    /// One LIFO stack per power-of-2 size class.
    /// bins[0] = 4 KB, bins[1] = 8 KB, ..., bins[12] = 16 MB
    bins: [NUM_BINS]std.ArrayListUnmanaged([*]u8),
    allocator: std.mem.Allocator,
    total_allocated: usize,  // monitoring

    pub fn init(allocator: std.mem.Allocator) BufferRecycler { ... }
    pub fn deinit(self: *BufferRecycler) void { ... }

    /// Acquire a buffer of at least `min_size` bytes.
    /// Returns a buffer of the next power-of-2 size >= min_size.
    pub fn acquire(self: *BufferRecycler, min_size: usize) ![]u8 {
        const power = sizeToPower(min_size);
        const bin_idx = power - MIN_POWER;
        const actual_size = @as(usize, 1) << power;

        // Try recycled first (LIFO, cache-warm)
        if (self.bins[bin_idx].pop()) |buf| {
            return buf[0..actual_size];
        }

        // Cold path: malloc
        const buf = try self.allocator.alloc(u8, actual_size);
        self.total_allocated += 1;
        return buf;
    }

    /// Return a buffer to the appropriate size-class bin.
    pub fn release(self: *BufferRecycler, buf: []u8) void {
        const power = sizeToPower(buf.len);
        const bin_idx = power - MIN_POWER;
        self.bins[bin_idx].append(self.allocator, buf.ptr) catch {
            self.allocator.free(buf);
        };
    }
};

/// Round up to the next power-of-2, returning the exponent.
/// Clamped to [MIN_POWER, MAX_POWER].
fn sizeToPower(size: usize) u5 {
    if (size <= (1 << MIN_POWER)) return MIN_POWER;
    const bits = @bitSizeOf(usize) - @clz(size - 1);
    return @intCast(@min(bits, MAX_POWER));
}
```

### 5.2 Size Classes

| Bin | Size | Use Case |
|-----|------|----------|
| 0 | 4 KB | Simple GET, API requests |
| 1 | 8 KB | Requests with many headers (cookies, auth) |
| 2 | 16 KB | Medium POST bodies |
| 3 | 32 KB | Large header sets |
| 4 | 64 KB | Typical form submissions |
| 5-8 | 128 KB - 1 MB | File uploads |
| 9-12 | 2 MB - 16 MB | Large uploads (rare) |

---

## 6. New FFI: `http_read_request`

### 6.1 Python-Facing API

```python
# _framework_core.pyi stub

def http_read_request(
    fd: int,
    max_header_size: int,
    max_body_size: int,
) -> tuple[str, str, bytes, bool, list[tuple[bytes, bytes]]] | None:
    """Read and parse one complete HTTP request from fd.

    Accumulates recv data in Zig, parses with hparse, returns structured
    result. Handles partial reads, pipelining, and keep-alive internally.

    Returns:
        (method, path, body, keep_alive, headers) on success.
        None if the client closed the connection (EOF).

    Raises:
        ValueError: Malformed HTTP (400 Bad Request).
        RuntimeError: Request too large (413 / 431).
        OSError: Network error.
    """
    ...
```

### 6.2 Zig Implementation

```zig
// src/hub.zig

/// http_read_request(fd, max_header_size, max_body_size) → tuple | None
///
/// Combined recv + accumulate + parse in Zig. No Python-side accumulation.
/// Uses the connection's InputBuffer for zero-copy accumulation.
/// Returns structured request data to Python.
pub fn pyHttpReadRequest(
    _: ?*PyObject,
    args: ?*PyObject,
) callconv(.c) ?*PyObject {
    var fd_long: c_long = 0;
    var max_header_size_long: c_long = 0;
    var max_body_size_long: c_long = 0;
    if (py.PyArg_ParseTuple(
        args, "lll",
        &fd_long, &max_header_size_long, &max_body_size_long,
    ) == 0) return null;

    const hub_ptr = getHub() orelse { ... };
    const fd: posix.fd_t = @intCast(fd_long);
    const max_header_size: usize = @intCast(max_header_size_long);
    const max_body_size: usize = @intCast(max_body_size_long);

    const conn = hub_ptr.getConn(fd) orelse { ... };

    // ── STEP 0: Reset arena + compact leftover data ──
    conn.arena.reset(&hub_ptr.recycler);  // return arena chunks to recycler
    conn.input.resetForNewRequest();

    // ── STEP 1: Try parsing from leftover data (pipelining) ──
    if (conn.input.unconsumedLen() > 0) {
        const result = tryParse(
            conn.input.unconsumed(),
            max_header_size, max_body_size,
            &conn.arena,
        );
        switch (result) {
            .complete => |req| {
                const py_result = buildPyResult(req) orelse return null;
                conn.input.consume(req.raw_bytes_consumed);
                return py_result;
            },
            .incomplete => {},  // fall through to recv
            .invalid => return raiseValueError(),
            .header_too_large => return raiseHeaderTooLarge(),
            .body_too_large => return raiseBodyTooLarge(),
            .arena_oom => return raiseArenaExhausted(),
        }
    }

    // ── STEP 2: Recv loop until complete request ──
    while (true) {
        // Ensure space for recv
        const free = conn.input.reserveFree(RECV_SIZE) catch {
            return raiseArenaExhausted();
        };
        const recv_len = @min(free.len, RECV_SIZE);

        // Submit recv SQE, suspend greenlet, resume on completion
        const nbytes = hub_ptr.doRecv(fd, conn, free[0..recv_len]) orelse {
            return null;  // error already set
        };

        if (nbytes == 0) {
            // EOF: client closed connection
            if (conn.input.unconsumedLen() > 0) {
                // Partial request in buffer — malformed
                return raiseValueError();  // 400
            }
            // Clean close (no pending data)
            return pyNone();
        }

        conn.input.commitWrite(nbytes);

        // Hard limit check: total accumulated data
        if (conn.input.unconsumedLen() > max_header_size + max_body_size) {
            return raiseRequestTooLarge();
        }

        // Reset arena before re-parsing (discard any partial parse allocations)
        conn.arena.reset(&hub_ptr.recycler);

        // Try parsing — headers allocated from arena
        const result = tryParse(
            conn.input.unconsumed(),
            max_header_size, max_body_size,
            &conn.arena,
        );
        switch (result) {
            .complete => |req| {
                // Build Python objects BEFORE consume — header slices
                // point into InputBuffer's unconsumed region
                const py_result = buildPyResult(req) orelse return null;
                conn.input.consume(req.raw_bytes_consumed);
                return py_result;
            },
            .incomplete => continue,  // recv more
            .invalid => return raiseValueError(),
            .header_too_large => return raiseHeaderTooLarge(),
            .body_too_large => return raiseBodyTooLarge(),
            .arena_oom => return raiseArenaExhausted(),
        }
    }
}

const RECV_SIZE: usize = 8192;
```

**Note on re-parsing cost**: Each recv triggers a full `tryParse()` from scratch over
all unconsumed data — O(n×m) in the worst case (n total bytes, m recv calls). This is
the standard approach (h2o, nginx, and most HTTP servers do the same). hparse is fast
enough that this is negligible for typical requests. For pathological cases (e.g. 1 MB
body arriving 1 byte at a time), the hard limit on `max_body_size` bounds the total
work. A resume-offset optimization (passing hparse the previous parse state) is possible
but not worth the complexity.

### 6.3 `tryParse` — Arena-Allocated Headers (h2o Pattern)

**Design**: Like h2o, header structs are bump-allocated from the per-request
`RequestArena`. Header name/value slices are zero-copy pointers into the
`InputBuffer` (like h2o's `h2o_iovec_t` pointers into `sock->input`). There is
no fixed header count limit — the arena grabs more chunks from the recycler
as needed.

hparse's `parseRequest` takes `headers: []Header` — a slice of any length.
We allocate an initial batch from the arena, and if hparse fills it completely
(meaning there may be more headers), we grow and re-parse.

```zig
/// Parsed result. `headers` is a slice into arena memory.
/// Valid until the arena is reset (end of request).
const ParsedRequest = struct {
    method: http.Method,
    path: []const u8,          // zero-copy slice into InputBuffer
    version: http.Version,
    headers: []const http.Header, // arena-allocated, values point into InputBuffer
    body: []const u8,            // zero-copy slice into InputBuffer
    raw_bytes_consumed: usize,
    keep_alive: bool,
};

const ParseResult = union(enum) {
    complete: ParsedRequest,
    incomplete,
    invalid,
    header_too_large,
    body_too_large,
    arena_oom,
};

const INITIAL_HEADER_CAPACITY: usize = 32;

/// Parse with arena-allocated headers. No fixed limit on header count.
///
/// Like h2o: header structs live in the per-request arena (bump-allocated),
/// header name/value slices are zero-copy pointers into buf (the InputBuffer).
/// If 32 headers aren't enough, we double and re-parse from the arena.
fn tryParse(
    buf: []const u8,
    max_header_size: usize,
    max_body_size: usize,
    arena: *RequestArena,
) ParseResult {
    var capacity: usize = INITIAL_HEADER_CAPACITY;

    // Allocate initial header array from the arena (bump allocation, O(1))
    var headers = arena.allocSlice(http.Header, capacity) catch return .arena_oom;

    // Note: arena.allocSlice(T, n) is a typed wrapper around arena.alloc():
    //   fn allocSlice(self: *RequestArena, comptime T: type, n: usize) ?[]T {
    //       const bytes = self.alloc(@sizeOf(T) * n) orelse return null;
    //       return @as([*]T, @ptrCast(@alignCast(bytes.ptr)))[0..n];
    //   }
    // Added to RequestArena in Phase 2.

    while (true) {
        var method: http.Method = .unknown;
        var path: ?[]const u8 = null;
        var version: http.Version = .@"1.0";
        var header_count: usize = 0;

        const header_bytes = hparse.parseRequest(
            buf, &method, &path, &version, headers, &header_count,
        ) catch |err| switch (err) {
            error.Incomplete => return .incomplete,
            error.Invalid => return .invalid,
        };

        // If hparse filled the entire slice, there may be more headers.
        // Grow and re-parse (like h2o's pool — just bump-allocate more).
        // The old allocation is abandoned in the arena (freed at request end).
        if (header_count == capacity) {
            capacity *= 2;
            headers = arena.allocSlice(http.Header, capacity) catch return .arena_oom;
            continue; // re-parse with larger slice
        }

        // Headers fit. Now validate limits.
        if (header_bytes > max_header_size) return .header_too_large;

        // Determine body length
        const content_length: usize = blk: {
            const cl = http.findHeader(headers[0..header_count], "content-length")
                orelse break :blk 0;
            const trimmed = std.mem.trim(u8, cl, &std.ascii.whitespace);
            break :blk std.fmt.parseInt(usize, trimmed, 10) catch return .invalid;
        };

        if (content_length > max_body_size) return .body_too_large;
        if (buf.len < header_bytes + content_length) return .incomplete;

        // Resolve keep-alive
        const ka = blk: {
            if (http.findHeader(headers[0..header_count], "connection")) |conn| {
                if (std.ascii.eqlIgnoreCase(conn, "close")) break :blk false;
                if (std.ascii.eqlIgnoreCase(conn, "keep-alive")) break :blk true;
            }
            break :blk version == .@"1.1";
        };

        return .{ .complete = .{
            .method = method,
            .path = path orelse return .invalid,
            .version = version,
            .headers = headers[0..header_count], // arena-owned slice
            .body = buf[header_bytes..header_bytes + content_length],
            .raw_bytes_consumed = header_bytes + content_length,
            .keep_alive = ka,
        }};
    }
}
```

**How this mirrors h2o:**

| h2o | Our design |
|-----|-----------|
| `h2o_header_t` structs allocated from `h2o_mem_pool_t` (bump allocator) | `http.Header` structs allocated from `RequestArena` (bump allocator) |
| Header name/value are `h2o_iovec_t` pointers into `sock->input` | Header `.key`/`.value` are `[]const u8` slices into InputBuffer |
| No fixed header count — pool grabs more chunks as needed | No fixed count — arena grabs more chunks, doubles capacity on overflow |
| Pool cleared at request end, chunks return to recycler | Arena reset at request end, chunks return to `ChunkRecycler` |
| Old allocations abandoned in pool (freed in bulk) | Old allocations abandoned in arena (freed in bulk at reset) |
| Zero allocation for the parse itself — just pointer ops | Zero-copy slices for name/value — only the Header structs are bump-allocated |

**Cost of re-parse on overflow**: If a request has > 32 headers (rare), we
re-parse the entire header section once. This is a cold path — the typical
request has 10-20 headers and fits in the initial 32-slot allocation.
The abandoned 32-slot array (1 KB) stays in the arena until request end —
same pattern as h2o's pool where old allocations are simply abandoned.

### 6.4 `buildPyResult` — Convert to Python Objects

```zig
/// Convert a parsed request to a Python 5-tuple.
///
/// IMPORTANT: Must be called BEFORE conn.input.consume() — header slices
/// are zero-copy pointers into the InputBuffer's unconsumed region.
/// After consume(), those bytes are logically freed and may be overwritten
/// by a future compact().
///
/// Header structs live in the arena and are valid until arena.reset().
fn buildPyResult(req: ParsedRequest) ?*PyObject {
    // Method string
    const method_str = methodToStr(req.method);
    const py_method = py.PyUnicode_FromStringAndSize(
        method_str.ptr, @intCast(method_str.len),
    ) orelse return null;

    // Path string (zero-copy slice into InputBuffer → copy to Python)
    const py_path = py.PyUnicode_FromStringAndSize(
        req.path.ptr, @intCast(req.path.len),
    ) orelse { py.py_helper_decref(py_method); return null; };

    // Body bytes (zero-copy slice into InputBuffer → copy to Python)
    const py_body = py.py_helper_bytes_from_string_and_size(
        @ptrCast(req.body.ptr), @intCast(req.body.len),
    ) orelse { ... cleanup ... };

    // Keep-alive bool
    const py_ka = if (req.keep_alive) py.py_helper_true() else py.py_helper_false();
    py.py_helper_incref(py_ka);

    // Headers: list[tuple[bytes, bytes]]
    // Header structs are in the arena, name/value slices point into InputBuffer
    const py_headers = buildPyHeaders(req.headers) orelse { ... cleanup ... };

    // Build 5-tuple: (method, path, body, keep_alive, headers)
    const tuple = py.py_helper_tuple_new(5) orelse { ... cleanup ... };
    _ = py.py_helper_tuple_setitem(tuple, 0, py_method);
    _ = py.py_helper_tuple_setitem(tuple, 1, py_path);
    _ = py.py_helper_tuple_setitem(tuple, 2, py_body);
    _ = py.py_helper_tuple_setitem(tuple, 3, py_ka);
    _ = py.py_helper_tuple_setitem(tuple, 4, py_headers);

    return tuple;
}

/// Convert arena-allocated headers to Python list[tuple[bytes, bytes]].
/// Each header's .key/.value are zero-copy slices into the InputBuffer.
/// This function copies the bytes into Python objects, after which
/// the InputBuffer region can be safely consumed.
fn buildPyHeaders(headers: []const http.Header) ?*PyObject {
    const list = py.PyList_New(@intCast(headers.len)) orelse return null;
    for (headers, 0..) |hdr, i| {
        const key = py.py_helper_bytes_from_string_and_size(
            @ptrCast(hdr.key.ptr), @intCast(hdr.key.len),
        ) orelse { py.py_helper_decref(list); return null; };

        const val = py.py_helper_bytes_from_string_and_size(
            @ptrCast(hdr.value.ptr), @intCast(hdr.value.len),
        ) orelse {
            py.py_helper_decref(key);
            py.py_helper_decref(list);
            return null;
        };

        const tuple = py.py_helper_tuple_new(2) orelse {
            py.py_helper_decref(val);
            py.py_helper_decref(key);
            py.py_helper_decref(list);
            return null;
        };
        _ = py.py_helper_tuple_setitem(tuple, 0, key);   // steals ref
        _ = py.py_helper_tuple_setitem(tuple, 1, val);   // steals ref
        _ = py.PyList_SetItem(list, @intCast(i), tuple);  // steals ref
    }
    return list;
}
```

### 6.5 `doRecv` — Internal Recv Helper

Extracted from `greenRecv()` but writes directly into the InputBuffer's free space
instead of the arena.

```zig
/// Submit a recv SQE for `buf`, suspend the greenlet, return bytes read.
/// Returns null on error (Python exception already set).
/// Returns 0 for EOF.
fn doRecv(
    self: *Hub,
    fd: posix.fd_t,
    conn: *Connection,
    buf: []u8,
) ?usize {
    const current = py.py_helper_greenlet_getcurrent() orelse return null;

    const user_data = conn_mod.encodeUserData(conn.pool_index, conn.generation);
    _ = self.ring.prepRecv(fd, buf, user_data) catch {
        py.py_helper_decref(current);
        py.py_helper_err_set_string(
            py.py_helper_exc_oserror(),
            "doRecv: failed to submit recv SQE",
        );
        return null;
    };

    conn.greenlet = current;
    conn.state = .reading;
    conn.pending_ops += 1;
    self.active_waits += 1;

    const hub_g = self.hub_greenlet orelse {
        // cleanup ...
        return null;
    };
    const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
    if (switch_result == null) return null;
    py.py_helper_decref(switch_result);

    const res = conn.result;
    if (res < 0) {
        py.py_helper_err_set_from_errno(-res);
        return null;
    }

    return @intCast(res);
}
```

---

## 7. Header Pipeline: Parse Once

### 7.1 Summary

Headers are parsed by `hparse` inside `tryParse()` into an arena-allocated
`[]Header` slice. The Header structs live in the `RequestArena` (bump-allocated,
like h2o's `h2o_header_t` in `h2o_mem_pool_t`). Header name/value slices are
zero-copy pointers into the `InputBuffer` (like h2o's `h2o_iovec_t` into
`sock->input`). `buildPyHeaders()` copies the bytes into Python objects, after
which the InputBuffer region can be consumed and the arena can be reset.

**No fixed header count limit.** Initial allocation is 32 headers from the arena.
If hparse fills it, the arena allocates a larger batch (doubling) and re-parses.
The old batch is abandoned in the arena (freed in bulk at request end — same
pattern as h2o's pool).

### 7.2 Python-Side Request Construction

```python
# theframework/request.py

class Request:
    __slots__ = (
        "method", "path", "full_path", "query_params",
        "headers", "body", "params", "_raw_headers",
    )

    @classmethod
    def _from_parsed(
        cls,
        method: str,
        path: str,
        body: bytes,
        headers: list[tuple[bytes, bytes]],
    ) -> Request:
        """Build Request from Zig-parsed fields. No re-parsing."""
        # Convert headers to dict (last value wins, backward compat)
        headers_dict: dict[str, str] = {}
        for key_bytes, val_bytes in headers:
            headers_dict[key_bytes.decode("latin-1").lower()] = (
                val_bytes.decode("latin-1")
            )

        # Split path and query string
        full_path = path
        if "?" in path:
            path_part, _, qs = path.partition("?")
            query_params = {
                k: v[0] for k, v in urllib.parse.parse_qs(qs).items()
            }
        else:
            path_part = path
            query_params = {}

        req = cls.__new__(cls)
        req.method = method
        req.path = path_part
        req.full_path = full_path
        req.query_params = query_params
        req.headers = headers_dict
        req.body = body
        req.params = {}
        req._raw_headers = headers  # preserve for duplicate access
        return req

    @property
    def raw_headers(self) -> list[tuple[bytes, bytes]]:
        """Headers as ordered list of (name, value) byte pairs.
        Preserves duplicates (e.g. multiple Set-Cookie).
        """
        return self._raw_headers
```

### 7.3 Header Format: Why `list[tuple[bytes, bytes]]`

| Property | Value |
|----------|-------|
| Preserves duplicates | Yes (list, not dict) |
| Preserves order | Yes (list) |
| Preserves original case | Yes (bytes, not decoded+lowered) |
| ASGI-compatible | Yes (same format as ASGI `headers`) |
| Immutable per entry | Yes (tuple) |
| Encoding | Raw bytes (latin-1 compatible) |
| Conversion to dict | Trivial: `{k.lower(): v for k, v in headers}` |

### 7.4 Header Count Limit

No fixed limit. The arena-based approach allocates headers dynamically:

1. Start with 32 slots from the arena (~1 KB, one bump-pointer advance)
2. If hparse fills all 32, double to 64, re-parse (old 32-slot array abandoned in arena)
3. Repeat until all headers fit

In practice, re-parse never happens — real-world requests have 10-20 headers.
The arena's bulk reset at request end frees all header allocations (including
any abandoned arrays from doubling) in one operation.

**Bounded by `max_header_size`**: Even without a count limit, the total header
bytes are bounded by `max_header_size` (32 KB default). A 32 KB header section
with minimal headers (e.g., `X: Y\r\n` = 6 bytes each) yields ~5400 headers —
the arena handles this fine (two 4 KB chunks).

---

## 8. Response Formatting in Zig

### 8.1 New FFI: `http_format_response`

Replace the Python-side response building in `Response._finalize()` with a Zig call
that writes directly into a buffer.

```python
# theframework/response.py — updated _finalize()

def _finalize(self) -> None:
    if self._finalized:
        return
    self._finalized = True

    body = b"".join(self._body_parts)

    # Format headers as list of (bytes, bytes) for Zig
    header_pairs: list[tuple[bytes, bytes]] = []
    if "Content-Length" not in self._headers:
        self._headers["Content-Length"] = str(len(body))
    for name, value in self._headers.items():
        header_pairs.append((name.encode("latin-1"), value.encode("latin-1")))

    # Single Zig call: formats status line + headers + body
    raw = _framework_core.http_format_response_full(
        self.status, header_pairs, body,
    )
    _framework_core.green_send(self._fd, raw)
```

### 8.2 Zig Implementation

```zig
/// http_format_response_full(status, headers, body) → bytes
///
/// Formats complete HTTP/1.1 response. Uses stack buffer for small
/// responses, heap for large ones.
pub fn pyHttpFormatResponseFull(
    _: ?*PyObject,
    args: ?*PyObject,
) callconv(.c) ?*PyObject {
    var status_long: c_long = 0;
    var headers_list: ?*PyObject = null;
    var body_obj: ?*PyObject = null;
    if (py.PyArg_ParseTuple(args, "lOS", &status_long, &headers_list, &body_obj) == 0)
        return null;

    // Extract body
    const body_ptr: [*]const u8 = @ptrCast(py.py_helper_bytes_as_string(body_obj.?) orelse ...);
    const body_len: usize = @intCast(py.py_helper_bytes_get_size(body_obj.?));

    // Extract headers from Python list
    const n_headers_raw = py.PyList_Size(headers_list.?);
    if (n_headers_raw < 0) return null;
    const n_headers: usize = @intCast(n_headers_raw);

    // Note: http_response.Header has .name/.value (not .key/.value like hparse.Header)
    var zig_headers: [64]http_response.Header = undefined;
    for (0..n_headers) |i| {
        const pair = py.PyList_GetItem(headers_list.?, @intCast(i)) orelse return null;
        const name_obj = py.py_helper_tuple_getitem(pair, 0) orelse return null;
        const val_obj = py.py_helper_tuple_getitem(pair, 1) orelse return null;

        zig_headers[i] = .{
            .name = extractBytesSlice(name_obj),
            .value = extractBytesSlice(val_obj),
        };
    }

    // Format into buffer
    const status = statusFromInt(@intCast(status_long)) orelse { ... };
    var buf: [65536]u8 = undefined;
    const n = http_response.writeResponse(
        &buf, status, zig_headers[0..n_headers], body_ptr[0..body_len],
    ) catch {
        // Response too large for stack buffer — use heap
        return formatLargeResponse(status, zig_headers[0..n_headers], body_ptr[0..body_len]);
    };

    return py.py_helper_bytes_from_string_and_size(@ptrCast(&buf), @intCast(n));
}
```

### 8.3 Large Response Fallback

When the response exceeds the 64 KB stack buffer, allocate from heap:

```zig
fn formatLargeResponse(
    status: http_response.StatusCode,
    headers: []const http.Header,
    body: []const u8,
) ?*PyObject {
    // Estimate size: status line (~30) + headers (~100 each) + body
    const est = 256 + headers.len * 128 + body.len;
    const buf = std.heap.c_allocator.alloc(u8, est) catch {
        _ = py.py_helper_err_no_memory();
        return null;
    };
    defer std.heap.c_allocator.free(buf);

    const n = http_response.writeResponse(buf, status, headers, body) catch {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "Response too large",
        );
        return null;
    };

    return py.py_helper_bytes_from_string_and_size(@ptrCast(buf.ptr), @intCast(n));
}
```

---

## 9. Request Lifecycle (End-to-End)

### 9.1 New Connection

```
1. greenAccept() → fd
2. pool.acquire() → conn
3. conn.input initialized with inline_buf (4 KB)
4. If conn.input.prev_capacity > INLINE_BUF_SIZE:
     Immediately grow to prev_capacity (capacity memory)
```

### 9.2 First Request on Connection

```
5. Python calls: http_read_request(fd, max_hdr, max_body)
6. Zig: arena.reset() — return chunks to recycler (no-op on first call)
7. Zig: conn.input.resetForNewRequest() — no-op (empty buffer)
8. Zig: conn.input.unconsumedLen() == 0 → skip parse attempt
9. Zig: recv loop:
   a. buf = conn.input.reserveFree(8192)
   b. nbytes = doRecv(fd, conn, buf)
   c. conn.input.commitWrite(nbytes)
   d. Hard limit check
   e. arena.reset() — discard partial parse allocs from previous iteration
   f. result = tryParse(conn.input.unconsumed(), ..., &arena)
      → headers bump-allocated from arena, slices point into InputBuffer
   g. If incomplete → goto (a)
   h. If complete:
      → buildPyResult(req) — copies header bytes into Python objects
      → conn.input.consume(consumed)
      → return Python tuple
10. Python receives tuple
11. Python: Request._from_parsed(method, path, body, headers)
12. Python: handler(request, response)
13. Python: response._finalize()
    → http_format_response_full(status, headers, body)
    → green_send(fd, response_bytes)
```

**Lifetime ordering**: arena data and InputBuffer slices are valid during step (h).
`buildPyResult` copies all data to Python objects. Then `consume()` invalidates
the InputBuffer region. Arena is reset at start of next `pyHttpReadRequest` call.

### 9.3 Keep-Alive: Subsequent Request (Clean)

```
14. Python checks keep_alive == True
15. Python calls http_read_request(fd, ...) again
16. Zig: arena.reset() — frees header allocs from previous request
17. Zig: conn.input.resetForNewRequest()
    → read_pos==write_pos → both reset to 0
18. Zig: recv loop (same as step 9)
19. → handler → response → send
```

### 9.4 Keep-Alive: Subsequent Request (Pipelined)

```
14. Previous request consumed 400 bytes, but recv brought 800 bytes
    → 400 bytes still in InputBuffer (unconsumed)
15. Python calls http_read_request(fd, ...)
16. Zig: arena.reset() — frees header allocs from previous request
17. Zig: conn.input.resetForNewRequest() — 400 bytes preserved
18. Zig: conn.input.unconsumedLen() == 400 → try parse from arena
19. If 400 bytes is a complete second request:
    → parse succeeds, buildPyResult, consume(400), return result
    → No recv needed at all (pure buffer parse)
20. If 400 bytes is incomplete:
    → recv more, accumulate, arena.reset + re-parse each iteration
```

### 9.5 Keep-Alive: Buffer Compaction

```
After many keep-alive requests, buffer might look like:
[consumed: 30 KB][unconsumed: 200 B][free: 1800 B]   capacity=32 KB

Next reserveFree(8192):
  free (1800) < min_free (8192)
  unconsumed (200) + min_free (8192) = 8392 <= capacity (32768) ✓
  → compact: memmove 200 bytes to front
  [unconsumed: 200 B][free: 32568 B]
  → no reallocation needed
```

### 9.6 Connection Close

```
21. keep_alive == false, or EOF, or error
22. Python: (falls through to finally block)
23. _framework_core.green_close(fd)
24. Zig: cancel-before-close → CLOSE SQE → CQE
25. Hub: releaseConnection(conn)
    → conn.arena.reset(&recycler) — return arena chunks to ChunkRecycler
    → conn.input.resetForNewConnection(inline_buf, INLINE_BUF_SIZE)
      — return buffer to BufferRecycler, remember capacity
    → pool.release(conn) — bumps generation
```

---

## 10. Edge Cases

### 10.1 Partial Reads

**Scenario**: GET request arrives in two TCP segments.

```
Segment 1: "GET /hello HTTP/1.1\r\nHost: ex"
Segment 2: "ample.com\r\n\r\n"
```

**Handling**:
1. First recv: 30 bytes into InputBuffer. tryParse → `.incomplete`
2. Second recv: 15 bytes appended. tryParse → `.complete`
3. consume(45), return parsed result

No special code needed — the recv loop handles this naturally.

### 10.2 Pipelined Requests

**Scenario**: Two requests in one TCP segment.

```
"GET /a HTTP/1.1\r\nHost: x\r\n\r\nGET /b HTTP/1.1\r\nHost: x\r\n\r\n"
```

**Handling**:
1. First `http_read_request`: recv gets 60 bytes. tryParse finds `/a` (30 bytes).
   consume(30). Return `/a` result. InputBuffer has 30 bytes unconsumed.
2. Second `http_read_request`: unconsumedLen() == 30. tryParse finds `/b`.
   consume(30). Return `/b` result. **No recv needed.**

### 10.3 Request Spanning Buffer Growth

**Scenario**: POST with 100 KB body, inline buffer is 4 KB.

```
1. First recv: 4096 bytes. tryParse: Content-Length: 102400, only 4096 available → incomplete.
2. reserveFree(8192): only 0 bytes free. Grow to 8 KB (or more based on Content-Length hint).
   Copy 4096 unconsumed bytes to new 8 KB buffer. Return old to recycler.
3. Continue recv+parse until 100 KB accumulated.
4. tryParse → complete. Return to Python.
```

**Optimization**: After seeing `Content-Length` in headers (during incomplete parse),
we could pre-grow the buffer to header_bytes + content_length. This avoids multiple
grow cycles. **Deferred** — not in initial implementation, but noted for future.

### 10.4 Slow Client (Header Timeout)

**Scenario**: Client sends first line, then stalls for 30 seconds.

**Current**: No timeout on the recv loop — server blocks forever.

**Plan** (Phase 3): Use io_uring's `IOSQE_IO_LINK` + `LINK_TIMEOUT` on the recv SQE
inside `doRecv()`:

```zig
fn doRecvWithTimeout(self: *Hub, fd, conn, buf, timeout_ms) ?usize {
    // Submit recv SQE with IO_LINK flag
    const sqe = self.ring.prepRecv(fd, buf, user_data) catch { ... };
    sqe.flags |= IOSQE_IO_LINK;

    // Submit linked timeout SQE
    var ts = linux.kernel_timespec{
        .sec = @divTrunc(timeout_ms, 1000),
        .nsec = @rem(timeout_ms, 1000) * 1_000_000,
    };
    _ = self.ring.prepLinkTimeout(&ts, IGNORE_SENTINEL) catch { ... };

    // ... suspend and resume ...

    if (res == -@as(i32, @intFromEnum(linux.E.CANCELED))) {
        // Timeout fired, recv was cancelled
        return error.Timeout;
    }
    // ...
}
```

Not in the initial implementation (the config has `read_timeout_ms` but it's marked
"future use"). Add as Phase 3.

### 10.5 Arena Exhaustion / Request Too Large

**Scenario**: Client sends a 2 MB body but `max_body_size` is 1 MB.

**Handling**:
1. tryParse sees `Content-Length: 2097152`
2. `content_length > max_body_size` → return `.body_too_large`
3. Zig raises RuntimeError("request body too large")
4. Python catches, sends 413 Payload Too Large, closes connection

**Scenario**: Client sends headers without Content-Length but keeps sending data.

**Handling**:
1. Each recv accumulates data
2. Hard limit check: `unconsumedLen() > max_header_size + max_body_size` → reject
3. This catches pathological clients without Content-Length

### 10.6 Buffer Growth Limit

**Scenario**: Malicious client sends data without ever completing a request.

**Handling**:
1. Hard limit on InputBuffer growth: `max_header_size + max_body_size + margin`
2. reserveFree() checks total buffer size before growing
3. If growth would exceed limit → return error → 413

```zig
fn grow(self: *InputBuffer, min_capacity: usize) !void {
    const max_allowed = self.max_buffer_size;  // from config
    if (min_capacity > max_allowed) return error.RequestTooLarge;
    // ... normal grow ...
}
```

### 10.7 Connection Reset During Recv

**Scenario**: Client TCP RST while server is waiting for recv.

**Handling**: io_uring CQE returns `cqe.res = -ECONNRESET` (or -EPIPE, -ENOTCONN).
doRecv() translates to OSError, which Python catches in the `except OSError: pass`
handler. Connection is closed in the `finally` block.

### 10.8 Half-Close (Client Sends FIN)

**Scenario**: Client sends complete request, then closes write half.

**Handling**: doRecv() returns 0 bytes (EOF). If unconsumedLen() > 0, it's a partial
request → ValueError. If 0 → clean close, return None.

### 10.9 Header-Only Request With Body Coming Later

**Scenario**: Client sends headers with `Content-Length: 50`, but body hasn't arrived
yet.

```
Recv 1: "POST /api HTTP/1.1\r\nContent-Length: 50\r\n\r\n"   (no body yet)
Recv 2: "{"key": "value", "data": "..."}"                     (50 bytes body)
```

**Handling**: tryParse on recv 1 sees Content-Length: 50 but `buf.len < header_bytes + 50`
→ returns `.incomplete`. Recv loop continues, gets body, tryParse succeeds.

### 10.10 Zero-Length Body

**Scenario**: GET request with no body (Content-Length absent or 0).

**Handling**: tryParse defaults content_length to 0 when Content-Length header is
absent. `buf.len >= header_bytes + 0` → complete. Body slice is empty (`buf[n..n]`).
Python receives `b""`.

### 10.11 Invalid Content-Length

**Scenario**: `Content-Length: abc` or `Content-Length: -1`.

**Handling**: `std.fmt.parseInt(usize, trimmed, 10)` fails → tryParse returns
`.invalid` → ValueError → Python catches, sends 400.

### 10.12 Multiple Content-Length Headers

**Scenario**: `Content-Length: 10\r\nContent-Length: 20\r\n`.

**Handling**: `findHeader()` returns the first match. This is consistent with most
HTTP servers (nginx uses first, h2o uses first). RFC 9110 says implementations SHOULD
reject requests with conflicting Content-Length values. **Future improvement**: validate
that all Content-Length values match.

### 10.13 Double Close Prevention

**Fix for current bug** (`server.py`):

```python
def _handle_connection(fd, handler, config):
    closed = False
    try:
        while True:
            result = _framework_core.http_read_request(fd, max_hdr, max_body)
            if result is None:
                return   # EOF, finally block closes

            method, path, body, keep_alive, headers = result
            request = Request._from_parsed(method, path, body, headers)
            response = Response(fd)

            try:
                handler(request, response)
            except Exception:
                # Send 500 if response not yet sent
                if not response._finalized:
                    _try_send_error(fd, 500)
                return

            response._finalize()

            if not keep_alive:
                return   # finally block closes

    except ValueError:
        _try_send_error(fd, 400)  # malformed request
    except RuntimeError as e:
        if "too large" in str(e) or "header" in str(e):
            _try_send_error(fd, 413)
        else:
            raise
    except OSError:
        pass  # network error, just close
    finally:
        try:
            _framework_core.green_close(fd)
        except Exception:
            pass


def _try_send_error(fd: int, status_code: int) -> None:
    """Best-effort error response. Does NOT close the fd (caller's finally does)."""
    reason = {400: "Bad Request", 413: "Payload Too Large", 500: "Internal Server Error"}
    body = reason.get(status_code, "Error").encode()
    resp = (
        f"HTTP/1.1 {status_code} {reason.get(status_code, 'Error')}\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + body
    try:
        _framework_core.green_send(fd, resp)
    except Exception:
        pass  # best effort
```

Key changes:
- **`_try_send_error` does NOT close the fd** — avoids double-close with `finally`
- **Only the `finally` block calls `green_close`** — single close point
- Handler exceptions caught → 500 response
- ValueError → 400 response
- RuntimeError with "too large" → 413 response

### 10.14 Keepalive Idle Timeout

**Scenario**: Client opens connection, sends one request, then idles for 60 seconds
before sending the next.

**Plan**: Between requests, use `doRecvWithTimeout()` with `keepalive_timeout_ms`
(default 5000ms). If timeout fires → close connection.

Not in initial implementation (Phase 3).

---

## 11. Implementation Phases

### Phase 1: InputBuffer + BufferRecycler

**New files**: `src/input_buffer.zig`, `src/buffer_recycler.zig`

**Tasks**:
- [x] Implement `BufferRecycler` with power-of-2 bins
  - [x] `acquire(min_size)` → pop from bin or malloc
  - [x] `release(buf)` → push to appropriate bin
  - [x] `deinit()` → free all cached buffers
  - [x] Tests: acquire/release round-trip, size class selection, LIFO reuse
- [x] Implement `InputBuffer`
  - [x] `unconsumed()`, `unconsumedLen()`
  - [x] `reserveFree(min)` with compaction and growth
  - [x] `commitWrite(n)`, `consume(n)`
  - [x] `compact()` — memmove unconsumed to front
  - [x] `grow()` — allocate from recycler, copy, return old
  - [x] `resetForNewConnection()` — return to recycler, use inline buf
  - [x] `resetForNewRequest()` — preserve unconsumed, compact if needed
  - [x] Tests: partial fill, compact, grow, reset, pipelining simulation
- [x] Add `BufferRecycler` to `Hub`
- [x] Add `InputBuffer` + `inline_buf` to `Connection`
- [x] Update `Connection.reset()` to reset InputBuffer
- [x] Update `ConnectionPool.init()` to initialize InputBuffer per connection
- [x] Update `ConnectionPool.fixupRecycler()` to also fix InputBuffer recycler ptrs
- [x] Gate: `zig build test`

### Phase 2: `http_read_request` + Header Pipeline + Updated Python

**Modified files**: `src/hub.zig`, `src/arena.zig`, `src/extension.zig`,
`theframework/server.py`, `theframework/request.py`, `stubs/_framework_core.pyi`

**Tasks**:
- [x] Add `allocSlice(comptime T, n)` to `RequestArena` (typed wrapper over `alloc`)
- [x] Implement `tryParse()` with arena-allocated headers (no fixed limit)
  - [x] Initial 32-slot allocation from arena
  - [x] Double-and-reparse if hparse fills the slice
  - [x] Limit checking (max_header_size, max_body_size)
- [x] Implement `doRecv()` — extracted recv helper writing to arbitrary buffer
- [x] Implement `pyHttpReadRequest()` — combined recv+accumulate+parse
  - [x] Reset arena at start of each call
  - [x] Call buildPyResult BEFORE consume (header slices point into InputBuffer)
- [x] Implement `buildPyResult()` + `buildPyHeaders()`
- [x] Register `http_read_request` in `extension.zig`
- [x] Update `Request._from_parsed()` in `request.py`
  - [x] Accept `list[tuple[bytes, bytes]]` headers
  - [x] Store as `_raw_headers`
  - [x] Add `raw_headers` property
- [x] Update `_handle_connection()` in `server.py`
  - [x] Replace recv+accumulate+parse loop with single `http_read_request` call
  - [x] Fix double-close bug (only close in `finally`)
  - [x] Add handler exception → 500 response
  - [x] Add ValueError → 400, RuntimeError("header too large") → 431, RuntimeError("...too large") → 413
- [x] Update stubs `_framework_core.pyi`
- [x] Keep `green_recv` and `http_parse_request` working (for monkey-patched sockets)
- [x] Gate: `zig build test`, `uv run pytest`, `uv run mypy theframework/ tests/`

### Phase 3: Response Formatting in Zig

**Modified files**: `src/hub.zig`, `src/extension.zig`, `theframework/response.py`,
`stubs/_framework_core.pyi`

**Tasks**:
- [x] Implement `pyHttpFormatResponseFull()` in `hub.zig`
  - [x] Accept status code, headers list, body bytes
  - [x] Use `http_response.writeResponse()` for small responses (stack buffer)
  - [x] Heap fallback for large responses (> 64 KB)
- [x] Register in `extension.zig`
- [x] Update `Response._finalize()` to call Zig formatter
- [x] Update stubs
- [x] Gate: `zig build test`, `uv run pytest`

### Phase 4: Timeouts (Deferred)

**Tasks**:
- [ ] Add `doRecvWithTimeout()` using IOSQE_IO_LINK + LINK_TIMEOUT
- [ ] read_timeout_ms: timeout during header accumulation
- [ ] keepalive_timeout_ms: timeout between requests on keep-alive connection
- [ ] handler_timeout_ms: timeout for Python handler execution (cancel connection)
- [ ] Gate: integration tests with slow clients

---

## 12. Testing Strategy

### 12.1 Unit Tests (Zig)

**InputBuffer**:
- Unconsumed slice correctness after write + consume
- Compact: data integrity after memmove
- Grow: data preserved across reallocation
- ResetForNewConnection: buffer returned to recycler, inline buf restored
- ResetForNewRequest with leftover data: unconsumed preserved
- ResetForNewRequest with no leftover: positions zeroed
- reserveFree triggers compact when beneficial
- reserveFree triggers grow when compact insufficient
- Hard limit: grow refuses beyond max_buffer_size

**BufferRecycler**:
- Size class mapping: 4095 → 4096, 4097 → 8192, etc.
- Acquire from empty bin → malloc
- Release + acquire → same pointer (LIFO)
- Multiple bins independent
- Deinit frees all cached buffers (test allocator leak check)

**tryParse**:
- Complete GET request → `.complete` with correct fields
- Incomplete request → `.incomplete`
- Malformed request → `.invalid`
- Headers > max_header_size → `.header_too_large`
- Body > max_body_size → `.body_too_large`
- Content-Length: 0 → complete with empty body
- Missing Content-Length → complete with empty body
- Invalid Content-Length → `.invalid`
- Duplicate headers → all preserved in arena-allocated slice
- 32 headers → fits in initial allocation, no re-parse
- 33+ headers → triggers double-and-reparse, all headers preserved
- 100+ headers → multiple doublings, all correct
- Header name/value slices point into input buffer (zero-copy verified)
- Arena OOM → `.arena_oom`

### 12.2 Integration Tests (Python)

**Basic**:
- Simple GET → 200 with correct body
- POST with JSON body → handler receives body
- Headers accessible via `request.headers` dict
- Headers accessible via `request.raw_headers` list

**Pipelining**:
- Send two requests in one TCP segment → both handled, correct order
- Send three requests in one segment → all three handled

**Partial reads**:
- Send request one byte at a time (via TCP_NODELAY + tiny writes)
- Send headers in one segment, body in another
- Send half of Content-Length body, then rest

**Keep-alive**:
- 100 sequential requests on one connection
- Connection: close header → connection closed after response
- HTTP/1.0 without Connection: keep-alive → closed

**Limits**:
- Headers > max_header_size → 431 response, connection closed
- Body > max_body_size → 413 response, connection closed
- Connection after rejection → works (server didn't crash)

**Errors**:
- Malformed request line → 400
- Invalid Content-Length → 400
- Handler raises exception → 500 response
- Client disconnects mid-request → no crash, no resource leak

**Duplicate headers**:
- Send multiple Set-Cookie headers → all in raw_headers
- Send multiple X-Custom headers → dict has last, raw_headers has all

**Response**:
- Response with custom headers → headers present in response
- Response > 64 KB body → works (heap fallback)
- Response with non-ASCII latin-1 headers → correctly encoded

### 12.3 Stress Tests

- 1000 concurrent connections, 100 requests each → no leaks, no crashes
- Alternating small and large requests → buffer grows and shrinks correctly
- Pipelined + keep-alive + errors mixed → all handled correctly

---

## 13. Memory Budget

### 13.1 Per-Connection Overhead

| Component | Size | Notes |
|-----------|------|-------|
| Connection struct fields | ~32 B | fd, state, generation, etc. |
| InputBuffer struct | ~48 B | pointers + positions |
| Inline buffer | 4096 B | Embedded, handles 95%+ of requests |
| RequestArena struct | ~64 B | Per-request bump allocator (headers, processing data) |
| Inline chunk (arena) | 4096 B | Embedded in Connection, handles header allocs for typical requests |
| **Total per connection** | **~8.3 KB** | |

### 13.2 Total Baseline (4096 connections)

| Component | Size |
|-----------|------|
| Connection array | 4096 × 8.3 KB = **33 MB** |
| BufferRecycler | ~1 KB (empty bins, grows on demand) |
| ChunkRecycler | ~1 KB (empty, grows on demand) |
| fd_to_conn array | 4096 × 2 B = **8 KB** |
| **Total baseline** | **~33 MB** |

### 13.3 Under Load

With 1000 concurrent connections, 100 overflowing inline buffer:
- 100 recycled buffers at 8 KB: 800 KB
- 50 recycled buffers at 16 KB: 800 KB
- ChunkRecycler: ~50 chunks = 200 KB
- **Total under load: ~35 MB**

### 13.4 Comparison with Current

Current implementation:
- Connection array: 4096 × ~4.2 KB = ~17 MB (with inline chunk)
- Python `buf` objects: unpredictable, depends on concurrent request sizes
- Python intermediate bytes objects: 2-3× per recv call

New implementation:
- Connection array: 4096 × ~8.3 KB = ~33 MB (with inline buffer + inline chunk)
- No Python buf accumulation
- Single Python object creation per parsed field

Trade-off: 16 MB more baseline memory for zero O(n^2) copies, zero double-parsing,
and predictable memory behavior.

If baseline memory is a concern, `max_connections` can be lowered or `INLINE_BUF_SIZE`
can be reduced to 2048 (still handles most GET requests).

---

## 14. Safety Analysis

### 14.1 Memory Safety

| Property | Guarantee |
|----------|-----------|
| No use-after-free | InputBuffer data valid until consume()/compact(). `buildPyResult()` copies all data to Python objects BEFORE `consume()` is called. Arena data valid until `arena.reset()`, which happens at start of next `pyHttpReadRequest` call — after Python has its objects. |
| No double-free | InputBuffer returned to recycler exactly once (in resetForNewConnection or grow). Inline buffer never freed. Arena chunks returned to ChunkRecycler exactly once in reset(). |
| No buffer overflow | reserveFree() always ensures capacity before recv. commitWrite() has debug assert. Arena bump allocator checks remaining space before advancing. |
| No dangling pointers | Header structs in arena, name/value slices into InputBuffer — both valid during `buildPyResult()`. Python objects created (copying bytes) before either is invalidated. Ordering: buildPyResult → consume → (later) arena.reset. |
| Bounded growth | InputBuffer.grow() checks max_buffer_size. tryParse checks max_header_size and max_body_size. Arena growth bounded by max_header_size (headers) + max_body_size (indirectly). |
| Compaction safety | `compact()` uses `copyForwards()` for left-shift (overlapping src > dst). Only called from `reserveFree()`, never while header slices are live. |

### 14.2 Concurrency Safety

| Property | Guarantee |
|----------|-----------|
| No data races | One greenlet per connection. Hub is single-threaded. BufferRecycler is hub-local. |
| No stale CQEs | Generation-based validation (existing mechanism, unchanged). |
| GIL correctness | GIL released only during submitAndWait. All Python object creation under GIL. |

### 14.3 Error Recovery

| Error | Recovery |
|-------|----------|
| malloc failure in grow() | Return error → Python gets RuntimeError → 413, close |
| recv returns error | OSError propagated to Python → close connection |
| recv returns 0 (EOF) | Return None to Python → close connection |
| Invalid HTTP | ValueError → 400, close connection |
| Handler exception | Catch → 500, close connection |
| Timeout (Phase 4) | ECANCELED → close connection |

---

## 15. Migration & Backward Compatibility

### 15.1 API Changes

| API | Change | Compatibility |
|-----|--------|---------------|
| `green_recv(fd, max)` | **Kept** (for monkey-patched sockets) | No change |
| `http_parse_request(buf)` | **Kept** (legacy, still works) | No change |
| `http_read_request(fd, max_hdr, max_body)` | **New** | Additive |
| `http_format_response_full(status, hdrs, body)` | **New** | Additive |
| `Request._from_raw()` | **Deprecated** (kept working) | Backward compat |
| `Request._from_parsed()` | **New** (primary path) | Additive |
| `Request.raw_headers` | **New property** | Additive |
| `Response._finalize()` | **Changed** (uses Zig formatter) | Internal only |
| `_handle_connection()` | **Changed** (uses http_read_request) | Internal only |

### 15.2 Migration Order

1. **Phase 1** (InputBuffer + BufferRecycler): New files only, no existing code changes.
   All existing tests pass.

2. **Phase 2** (http_read_request): New FFI function added. `_handle_connection`
   updated to use it. Old `green_recv`/`http_parse_request` path still works for
   monkey-patched sockets. `Request._from_raw` kept as deprecated fallback.

3. **Phase 3** (Response formatting): `Response._finalize` updated to use Zig
   formatter. Old Python formatting removed. Pure internal change.

### 15.3 Rollback Plan

Each phase is independently testable. If Phase 2 causes issues:
- Revert `server.py` to use `green_recv` + `http_parse_request` loop
- Keep Phase 1 (InputBuffer infrastructure) for future use
- No Zig-side changes needed for rollback (new functions are additive)
