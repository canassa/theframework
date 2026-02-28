# Per-Connection Arena Allocator

Implementation plan for a per-request arena allocator.
A linked-list bump allocator with a hub-local chunk recycler. One allocator, one code path.

---

## Architecture Overview

```
                          Python (server.py)
                               │
                    hub_run(listen_fd, acceptor_fn, config_dict)
                               │
                               ▼
                  ┌────────────────────────────┐
                  │   ArenaConfig (Zig struct)  │
                  │   Parsed from Python dict   │
                  │   via callconv(.c) FFI      │
                  └────────┬───────────────────┘
                           │
                           ▼
              ┌────────────────────────────┐
              │     Per-Connection Arena    │
              │     (RequestArena)          │
              │                            │
              │  ┌──────────────────────┐  │
              │  │  first_chunk (4 KB)  │  │  Inline in Connection struct
              │  │  Bump allocate       │  │  Always available, zero alloc
              │  │  until full          │  │  Covers >95% of requests
              │  └───────┬──────────────┘  │
              │          │ full             │
              │          ▼                 │
              │  ┌──────────────────────┐  │
              │  │  Linked chunks       │  │  4 KB each, from hub-local
              │  │  from ChunkRecycler  │  │  LIFO free list
              │  │  (no malloc in       │  │  Grabbed on demand
              │  │  steady state)       │  │
              │  └───────┬──────────────┘  │
              │          │ single alloc    │
              │          │ >= 1022 bytes   │
              │          ▼                 │
              │  ┌──────────────────────┐  │
              │  │  Direct malloc       │  │  For oversized single allocs
              │  │  Linked into         │  │  Freed on reset
              │  │  arena.directs list  │  │  (rare path)
              │  └──────────────────────┘  │
              │                            │
              │  Hard limit check on       │
              │  total_allocated before    │
              │  every alloc. Returns      │
              │  null if exceeded.         │
              └────────────────────────────┘
```

### Memory Flow Per Request

```
     Client sends: "POST /api HTTP/1.1\r\nContent-Length: 50\r\n... (50 bytes body)"
                                              │
                                              ▼
     ┌─────────────────────────────────────────────────────┐
     │ green_recv() writes into arena's current chunk      │
     │ via currentFreeSlice() + commitBytes()              │
     │                                                     │
     │  Chunk layout (4 KB):                               │
     │  ┌────────┬──────────────────────────┬────────────┐ │
     │  │ *next  │  recv data (method,      │   FREE     │ │
     │  │ 8 B    │  headers, body) ~280 B   │  ~3808 B   │ │
     │  └────────┴──────────────────────────┴────────────┘ │
     │                                                     │
     │  py_helper_bytes_from_string_and_size() copies      │
     │  recv data into Python bytes. Arena slice is only   │
     │  read, never modified.                              │
     └─────────────────────────────────────────────────────┘
                                              │
                                              ▼
     ┌─────────────────────────────────────────────────────┐
     │ arena.reset():                                      │
     │   - Return linked chunks → ChunkRecycler (no free)  │
     │   - Free all directs                                │
     │   - Reset first_chunk offset to DATA_START          │
     │   - Ready for next request. ZERO free() calls       │
     │     in steady state.                                │
     └─────────────────────────────────────────────────────┘
```

### Key Design Decisions

**Chunk size: 4096 bytes**. Matches OS page size. The `next` pointer overlays
the first 8 bytes (union trick), leaving 4088 usable bytes. Most HTTP requests fit in one chunk.

**Direct threshold: `(4096 - 8) / 4 = 1022 bytes`**. Any single allocation
>= 1022 bytes goes to direct malloc. Prevents a single allocation from wasting >25% of a chunk.

**Union overlay for chunk linked list**. The `next` pointer shares the first 8 bytes with
the payload. Zero overhead for list management.

**Hub-local recycler, not thread-local**. Since we're single-threaded per worker process,
"hub-local" = "thread-local". Stored as a field on `Hub`, not `__thread`.

**No GC**. Chunks stay in the recycler until hub shutdown. Watermark-based GC can be added later.

---

## Zig Expert Review Findings

**Overall**: The plan is well-designed. No blockers. See notes below.

### Must Fix (Before Implementation)

- **Issue 1: `ChunkRecycler.acquire()` uses `.popOrNull()`** — Zig 0.15.2's `ArrayListUnmanaged` has `.pop()`, not `.popOrNull()`. This will be a compile error. Change line 232 in Phase 2 to use `.pop()`.

### Should Fix (Robustness)

- **Issue 9: `commitBytes()` has no bounds check** — The method advances `chunk_offset` without verifying `nbytes <= remaining_chunk_space`. Safe in current usage (nbytes comes from kernel recv, which respects buffer size), but a defensive check is cheap. Consider adding: `assert(self.chunk_offset + nbytes <= Chunk.CHUNK_SIZE);` in debug mode.

### Design Observations (Not Bugs)

- **Issue 16: ChunkRecycler uses `ArrayListUnmanaged` instead of intrusive linked list** — The ArrayList approach causes malloc/reallocation during warmup when the recycler is growing (once it reaches steady state, no more reallocs). Intrusive lists could avoid this entirely. Current approach is simpler but has minor warmup cost. Low priority.

- **Issue 12: `ArenaConfig` declared as `extern struct`** — Harmless but unnecessary (struct never crosses FFI boundary). Could be a regular `struct`. Low priority.

- **Issue 18: No alignment support in `RequestArena.alloc()`** — Already acknowledged as future work. Fine for current use (recv buffers don't need alignment). Can add later if parsing headers into aligned structs.

- **Issue 23: `fd_to_conn` dynamic allocation under-specified** — Phase 4 mentions "dynamically allocate `fd_to_conn`" but doesn't detail the change. Current `[MAX_FDS]?u16` is only 8 KB (negligible), so leaving it static is fine. Minor loose end.

---

## Phase 1: Config Infrastructure

All limits and sizes configurable from Python. The Zig side receives them as a C struct
populated by parsing a Python dict.

### Data Structures

```zig
// src/config.zig

pub const ArenaConfig = extern struct {
    chunk_size: u32,              // default 4096
    max_header_size: u32,         // default 32768 (32 KB)
    max_body_size: u32,           // default 1048576 (1 MB)
    max_request_size: u32,        // default 1114112 (1 MB + 64 KB)
    max_connections: u16,         // default 4096
    read_timeout_ms: u32,         // default 30000 (future use)
    keepalive_timeout_ms: u32,    // default 5000 (future use)
    handler_timeout_ms: u32,      // default 30000 (future use)

    pub fn defaults() ArenaConfig { ... }

    /// Direct allocation threshold: (chunk_size - sizeof(next_ptr)) / 4
    pub fn directThreshold(self: ArenaConfig) usize {
        return (@as(usize, self.chunk_size) - @sizeOf(?*anyopaque)) / 4;
    }
};

/// Parse ArenaConfig from a Python dict. Missing keys use defaults.
pub fn configFromPyDict(dict: ?*PyObject) ArenaConfig { ... }
```

### Python-to-Zig Config Passing

```zig
// src/hub.zig — updated pyHubRun

/// hub_run(listen_fd, acceptor_fn, config_dict) -> None
pub fn pyHubRun(_: ?*PyObject, args: ?*PyObject) callconv(.c) ?*PyObject {
    var listen_fd_long: c_long = 0;
    var acceptor_fn: ?*PyObject = null;
    var config_dict: ?*PyObject = null;
    if (py.PyArg_ParseTuple(args, "lO|O", &listen_fd_long, &acceptor_fn, &config_dict) == 0)
        return null;

    const config = configFromPyDict(config_dict);
    // ... hub initialization uses config ...
}
```

### Updated Python API

```python
# theframework/app.py

class Framework:
    def run(self, host="127.0.0.1", port=8000, *, workers=1,
            max_header_size=32768, max_body_size=1_048_576,
            max_connections=4096) -> None:
        config = {
            "max_header_size": max_header_size,
            "max_body_size": max_body_size,
            "max_connections": max_connections,
        }
        serve(self._dispatch, host, port, workers=workers, config=config)
```

### TODO

- [x] Create `src/config.zig` with `ArenaConfig`, `defaults()`, `directThreshold()`, `configFromPyDict()`
- [x] Update `pyHubRun()` in `src/hub.zig` to accept optional 3rd argument (config dict)
- [x] Pass config to `Hub.init(allocator, config)`
- [x] Update `theframework/server.py`: pass config dict to `hub_run`
- [x] Update `theframework/app.py`: add config kwargs to `run()`
- [x] Update `stubs/_framework_core.pyi`: add config parameter to `hub_run`
- [x] Zig tests: `defaults()` returns expected values, `directThreshold()` computes correctly
- [x] Gate: `zig build test`, `uv run pytest`, `uv run mypy theframework/ tests/`

---

## Phase 2: ChunkRecycler

Hub-local LIFO stack of free 4 KB chunks.
Starts empty, grows on demand. No pre-allocation. No GC (for now).

### Data Structures

```zig
// src/chunk_pool.zig

/// A 4 KB chunk. The `next` pointer overlays the first 8 bytes of the
/// payload (union trick from h2o's un_h2o_mem_pool_chunk_t).
pub const Chunk = extern union {
    next: ?*Chunk,
    bytes: [CHUNK_SIZE]u8,

    pub const CHUNK_SIZE: usize = 4096;
    /// Offset where bump allocation starts (after the next pointer).
    pub const DATA_START: usize = @sizeOf(?*Chunk);
    /// Usable bytes per chunk.
    pub const USABLE: usize = CHUNK_SIZE - DATA_START;
};

/// Hub-local LIFO stack of free chunks. Chunks return here on
/// arena.reset() and are grabbed from here on arena overflow.
/// In steady state, no malloc/free happens.
pub const ChunkRecycler = struct {
    free_stack: std.ArrayListUnmanaged(*Chunk),
    allocator: std.mem.Allocator,
    total_allocated: usize,  // chunks ever malloc'd (for monitoring)

    pub fn init(allocator: std.mem.Allocator) ChunkRecycler {
        return .{ .free_stack = .{}, .allocator = allocator, .total_allocated = 0 };
    }

    pub fn deinit(self: *ChunkRecycler) void {
        for (self.free_stack.items) |chunk| {
            self.allocator.destroy(chunk);
        }
        self.free_stack.deinit(self.allocator);
    }

    /// Pop from stack (LIFO, cache-warm) or malloc a new chunk.
    pub fn acquire(self: *ChunkRecycler) !*Chunk {
        if (self.free_stack.pop()) |chunk| return chunk;
        const chunk = try self.allocator.create(Chunk);
        self.total_allocated += 1;
        return chunk;
    }

    /// Push onto stack. No free() — cached for reuse.
    pub fn release(self: *ChunkRecycler, chunk: *Chunk) void {
        self.free_stack.append(self.allocator, chunk) catch {
            self.allocator.destroy(chunk); // OOM growing stack → free chunk
        };
    }

    pub fn cachedCount(self: *const ChunkRecycler) usize {
        return self.free_stack.items.len;
    }
};
```

### Hub Integration

```zig
// src/hub.zig

pub const Hub = struct {
    // ... existing fields ...
    recycler: ChunkRecycler,  // NEW
    config: ArenaConfig,       // NEW

    pub fn init(allocator: std.mem.Allocator, config: ArenaConfig) !Hub {
        // ...
        var recycler = ChunkRecycler.init(allocator);
        // ... pass &recycler to ConnectionPool.init() ...
    }

    pub fn deinit(self: *Hub) void {
        self.pool.deinit();
        self.recycler.deinit();  // free all cached chunks
        // ...
    }
};
```

### TODO

- [x] Create `src/chunk_pool.zig` with `Chunk` union and `ChunkRecycler`
- [x] Add `recycler: ChunkRecycler` field to `Hub`
- [x] Initialize in `Hub.init()`, deinit in `Hub.deinit()`
- [x] Zig tests:
  - [x] `@sizeOf(Chunk) == 4096`
  - [x] `Chunk.DATA_START == @sizeOf(?*Chunk)` (8 on 64-bit)
  - [x] Acquire from empty recycler → new allocation
  - [x] Acquire/release round-trip → same pointer reused
  - [x] `cachedCount` increments on release, decrements on acquire
  - [x] `deinit` frees all cached chunks (no leak under test allocator)
- [x] Gate: `zig build test`

---

## Phase 3: RequestArena

Per-connection bump allocator with linked chunk list + direct allocs.

### Data Structures

```zig
// src/arena.zig

/// Direct allocation header. For single allocations >= direct_threshold.
/// Individually malloc'd and linked into the arena's directs list.
const DirectAlloc = struct {
    next: ?*DirectAlloc,
    size: usize,  // payload size (for freeing)
    // payload bytes follow immediately after this header
};

/// Per-connection arena allocator, h2o-style.
///
/// The first_chunk is embedded in the Connection struct (always available,
/// zero allocation). Additional chunks come from the hub-local ChunkRecycler.
/// Large single allocations go through direct malloc.
///
/// All memory is reclaimed in one call to reset().
pub const RequestArena = struct {
    /// Head of linked list of chunks. Points to the current (most recently
    /// allocated) chunk. New chunks are prepended.
    chunks: ?*Chunk,

    /// Bump offset within the current chunk.
    chunk_offset: usize,

    /// Head of linked list of direct (large) allocations.
    directs: ?*DirectAlloc,

    /// The inline first chunk (embedded in Connection). Never returned
    /// to the recycler — only reset.
    first_chunk: *Chunk,

    /// Hub-local chunk recycler.
    recycler: *ChunkRecycler,

    /// Config snapshot.
    config: ArenaConfig,

    /// Total bytes allocated this request (for hard limit checking).
    total_allocated: usize,

    /// Cached direct-alloc threshold.
    direct_threshold: usize,

    pub fn init(first_chunk: *Chunk, recycler: *ChunkRecycler, config: ArenaConfig) RequestArena {
        first_chunk.next = null;
        return .{
            .chunks = first_chunk,
            .chunk_offset = Chunk.DATA_START,
            .directs = null,
            .first_chunk = first_chunk,
            .recycler = recycler,
            .config = config,
            .total_allocated = 0,
            .direct_threshold = config.directThreshold(),
        };
    }

    /// Allocate `sz` bytes. Returns null if hard limit exceeded or OOM.
    pub fn alloc(self: *RequestArena, sz: usize) ?[]u8 {
        if (self.total_allocated + sz > self.config.max_request_size) return null;

        const size = if (sz == 0) 1 else sz;

        // Large allocation: direct malloc
        if (size >= self.direct_threshold) return self.allocDirect(size);

        // Try bump in current chunk
        if (Chunk.CHUNK_SIZE - self.chunk_offset >= size) {
            const start = self.chunk_offset;
            self.chunk_offset += size;
            self.total_allocated += size;
            return self.chunks.?.bytes[start..][0..size];
        }

        // Current chunk full — grab a new one from recycler
        return self.allocNewChunk(size);
    }

    fn allocNewChunk(self: *RequestArena, size: usize) ?[]u8 {
        const new_chunk = self.recycler.acquire() catch return null;
        new_chunk.next = self.chunks;
        self.chunks = new_chunk;
        self.chunk_offset = Chunk.DATA_START + size;
        self.total_allocated += size;
        return new_chunk.bytes[Chunk.DATA_START..][0..size];
    }

    fn allocDirect(self: *RequestArena, size: usize) ?[]u8 {
        const header_size = @sizeOf(DirectAlloc);
        const total = header_size + size;
        const raw = std.heap.c_allocator.alloc(u8, total) catch return null;
        const header: *DirectAlloc = @ptrCast(@alignCast(raw.ptr));
        header.next = self.directs;
        header.size = size;
        self.directs = header;
        self.total_allocated += size;
        return raw[header_size..][0..size];
    }

    /// Get writable space in the current chunk for recv.
    /// If the current chunk is full, grabs a new one from the recycler.
    pub fn currentFreeSlice(self: *RequestArena, max_len: usize) ?[]u8 {
        // Hard limit check
        if (self.total_allocated >= self.config.max_request_size) return null;

        const remaining = Chunk.CHUNK_SIZE - self.chunk_offset;
        if (remaining == 0) {
            const new_chunk = self.recycler.acquire() catch return null;
            new_chunk.next = self.chunks;
            self.chunks = new_chunk;
            self.chunk_offset = Chunk.DATA_START;
            const available = Chunk.USABLE;
            return new_chunk.bytes[Chunk.DATA_START..][0..@min(max_len, available)];
        }
        return self.chunks.?.bytes[self.chunk_offset..][0..@min(max_len, remaining)];
    }

    /// Advance bump pointer after data has been written into the slice
    /// returned by currentFreeSlice().
    pub fn commitBytes(self: *RequestArena, nbytes: usize) void {
        std.debug.assert(self.chunk_offset + nbytes <= Chunk.CHUNK_SIZE);
        self.chunk_offset += nbytes;
        self.total_allocated += nbytes;
    }

    /// Reset for the next request. Return linked chunks to recycler,
    /// free directs, reset first_chunk for reuse.
    pub fn reset(self: *RequestArena) void {
        // Return linked chunks to recycler (skip first_chunk)
        var chunk = self.chunks;
        while (chunk) |c| {
            const next_chunk = c.next;
            if (c != self.first_chunk) {
                self.recycler.release(c);
            }
            chunk = next_chunk;
        }

        // Free all direct allocations
        var direct = self.directs;
        while (direct) |d| {
            const next_direct = d.next;
            const total = @sizeOf(DirectAlloc) + d.size;
            const raw: [*]u8 = @ptrCast(d);
            std.heap.c_allocator.free(raw[0..total]);
            direct = next_direct;
        }

        // Reset first chunk
        self.first_chunk.next = null;
        self.chunks = self.first_chunk;
        self.chunk_offset = Chunk.DATA_START;
        self.directs = null;
        self.total_allocated = 0;
    }

    pub fn totalUsed(self: *const RequestArena) usize {
        return self.total_allocated;
    }

    pub fn currentChunkRemaining(self: *const RequestArena) usize {
        return Chunk.CHUNK_SIZE - self.chunk_offset;
    }
};
```

### TODO

- [x] Create `src/arena.zig` with `RequestArena` and `DirectAlloc`
- [x] Zig tests:
  - [x] Small alloc fits in first chunk (no recycler interaction)
  - [x] Fill first chunk, next alloc grabs from recycler
  - [x] Large alloc (>= direct_threshold) goes to direct malloc
  - [x] Reset returns chunks to recycler (verify cachedCount)
  - [x] Reset frees directs
  - [x] Reset + re-alloc reuses first chunk (chunk_offset back to DATA_START)
  - [x] Hard limit: alloc beyond max_request_size returns null
  - [x] `currentFreeSlice` + `commitBytes` round-trip
  - [x] Multiple allocs across multiple chunks, verify all slices valid
  - [x] Zero-size alloc returns valid 1-byte slice
- [x] Gate: `zig build test`

---

## Phase 4: Integration

Replace `recv_buf` in Connection with arena. Update greenRecv. Update ConnectionPool.
**This phase is an atomic change** — Connection, ConnectionPool, and Hub all change together.

### Connection Struct Changes

```zig
// src/connection.zig

pub const Connection = struct {
    fd: posix.fd_t,
    state: ConnectionState,
    generation: u32,
    greenlet: ?*anyopaque,
    pending_ops: u8,
    pool_index: u16,
    result: i32,

    // --- NEW ---
    arena: RequestArena,
    inline_chunk: Chunk,  // 4 KB embedded, used as arena's first_chunk

    // --- REMOVED ---
    // recv_buf: ?[]u8,
    // send_buf: ?[]const u8,
    // send_offset: usize,
    // send_total: usize,

    pub fn reset(self: *Connection) void {
        self.fd = -1;
        self.state = .idle;
        self.greenlet = null;
        self.pending_ops = 0;
        self.result = 0;
        self.arena.reset();
    }
};
```

**Connection size**: ~4.2 KB each (with inline 4 KB chunk). With `max_connections=4096`:
connections array = ~17 MB.

### ConnectionPool Changes

```zig
// src/connection.zig

pub const ConnectionPool = struct {
    connections: []Connection,     // heap-allocated, dynamically sized
    free_list: std.ArrayListUnmanaged(u16),
    allocator: std.mem.Allocator,

    pub fn init(
        allocator: std.mem.Allocator,
        config: ArenaConfig,
        recycler: *ChunkRecycler,
    ) !ConnectionPool {
        const max_conns: usize = config.max_connections;
        const connections = try allocator.alloc(Connection, max_conns);

        for (0..max_conns) |i| {
            connections[i] = .{
                .fd = -1,
                .state = .idle,
                .generation = 0,
                .greenlet = null,
                .pending_ops = 0,
                .pool_index = @intCast(i),
                .result = 0,
                .inline_chunk = undefined,
                .arena = undefined,
            };
            connections[i].arena = RequestArena.init(
                &connections[i].inline_chunk,
                recycler,
                config,
            );
        }

        // ... free list init same as before, using max_conns ...
    }

    pub fn deinit(self: *ConnectionPool) void {
        for (self.connections) |*conn| conn.arena.reset();
        self.allocator.free(self.connections);
        self.free_list.deinit(self.allocator);
    }

    pub fn release(self: *ConnectionPool, conn: *Connection) void {
        conn.generation +%= 1;
        conn.reset();  // calls arena.reset()
        self.free_list.append(self.allocator, conn.pool_index) catch {};
    }

    // acquire(), lookup() — same logic, use self.connections.len
};
```

### Updated greenRecv

```zig
// src/hub.zig

pub fn greenRecv(self: *Hub, fd: posix.fd_t, max_bytes: usize) ?*PyObject {
    const conn = self.getConn(fd) orelse { ... };

    // Get writable space from the arena (no heap alloc)
    const buf = conn.arena.currentFreeSlice(max_bytes) orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "green_recv: request too large (arena exhausted)",
        );
        return null;
    };

    // ... submit recv SQE, suspend, resume (same as before) ...

    const nbytes: usize = @intCast(conn.result);

    // Commit the bytes to the arena (advance bump pointer)
    conn.arena.commitBytes(nbytes);

    // Copy into Python bytes object
    return py.py_helper_bytes_from_string_and_size(
        @ptrCast(buf.ptr),
        @intCast(nbytes),
    );
    // No free — arena.reset() handles everything
}
```

Key changes from current greenRecv:
- `conn.arena.currentFreeSlice(max_bytes)` replaces `self.allocator.alloc(u8, max_bytes)`
- `conn.arena.commitBytes(nbytes)` after recv completes
- No `self.allocator.free(buf)` anywhere
- No `conn.recv_buf` field

### The Contiguous Input Non-Problem

The HTTP parser requires contiguous input. This is a non-issue because:
1. Each `green_recv` returns a Python bytes object (copy from arena)
2. Python concatenates: `buf += data`
3. The parser is called on the contiguous Python bytes
4. Arena chunks don't need to provide contiguous multi-chunk views

### Other Hub Changes

- `releaseConnection()`: remove `recv_buf` freeing; `pool.release(conn)` calls `arena.reset()`
- `deinit()`: remove per-connection `recv_buf` cleanup
- `greenUnregisterFd()`: remove `recv_buf` freeing

### TODO

- [x] Modify `src/connection.zig`:
  - [x] Add `arena: RequestArena` and `inline_chunk: Chunk` to Connection
  - [x] Remove `recv_buf`, `send_buf`, `send_offset`, `send_total`
  - [x] Update `Connection.reset()` to call `arena.reset()`
  - [x] Change ConnectionPool to `[]Connection` (heap-allocated)
  - [x] Update `ConnectionPool.init()` to accept `ArenaConfig` and `*ChunkRecycler`
  - [x] Replace `POOL_SIZE` with `self.connections.len`
  - [x] Add `fixupRecycler()` to re-point arena recycler ptrs after Hub placement
- [x] Modify `src/hub.zig`:
  - [x] `recycler: ChunkRecycler` and `config: ArenaConfig` already on Hub (Phase 1-2)
  - [x] Update `Hub.init()`: init recycler before pool, pass to pool
  - [x] Call `pool.fixupRecycler(&hub_ptr.recycler)` in `pyHubRun` after Hub placement
  - [x] Update `greenRecv()`: `currentFreeSlice` + `commitBytes`, remove all `allocator.free`
  - [x] Remove all `conn.recv_buf` references (releaseConnection, deinit, greenUnregisterFd)
- [x] Update `build.zig`: connection.zig tests need `link_libc = true` for arena's c_allocator
- [x] Python integration tests (all 187 existing tests pass):
  - [x] Send request < 4 KB, verify response
  - [x] 100 sequential keep-alive requests (arena reset + chunk reuse)
  - [x] Request with ~20 KB headers (multi-chunk)
  - [x] 50 concurrent connections
- [x] Gate: `zig build test`, `uv run pytest`, `uv run mypy theframework/ tests/`

---

## Phase 5: Hard Limits

Enforce max_header_size, max_body_size at appropriate points.

### Enforcement Points

| Check | Where | Response |
|---|---|---|
| `arena.alloc()` / `currentFreeSlice()` returns null | total > max_request_size | RuntimeError → 413 |
| After header parse | header_bytes > max_header_size | 431 |
| Content-Length value | content_length > max_body_size | 413 |
| `ConnectionPool.acquire()` returns null | pool exhausted | 503 |

### Implementation

The `max_request_size` check is already in `RequestArena.alloc()` (Phase 3).

**Header/body size checks** — done on the Python side:

```python
# theframework/server.py

def _handle_connection(fd, handler, config):
    buf = b""
    try:
        while True:
            data = _framework_core.green_recv(fd, 4096)
            if not data:
                break
            buf += data

            while True:
                result = _framework_core.http_parse_request(buf)
                if result is None:
                    break

                method, path, body, consumed, keep_alive, headers = result
                buf = buf[consumed:]

                header_bytes = consumed - len(body)
                if header_bytes > config.get("max_header_size", 32768):
                    _send_error(fd, 431)
                    return
                if len(body) > config.get("max_body_size", 1_048_576):
                    _send_error(fd, 413)
                    return

                # ... normal handler dispatch ...

    except RuntimeError as e:
        if "arena exhausted" in str(e) or "too large" in str(e):
            _send_error(fd, 413)
    except OSError:
        pass
    finally:
        _framework_core.green_close(fd)
```

**Connection limit** — reject with 503 in `greenAccept` when pool is exhausted.

### TODO

- [x] Add `payload_too_large = 413` to `http_response.zig` StatusCode enum
- [x] Update Python `_handle_connection` with limit checks
- [x] Add `_send_error` helper function in `server.py`
- [x] Catch RuntimeError from arena exhaustion → 413
- [x] Add `sendRejectAndClose` to Hub for connection-limit 503
- [x] Python integration tests:
  - [x] POST body > max_body_size → 413
  - [x] Headers > max_header_size → 431
  - [x] After rejection, next connection works
- [x] Gate: `zig build test`, `uv run pytest`, `uv run mypy theframework/ tests/`

---

## File Organization

```
src/
  config.zig          [NEW]  ArenaConfig struct, defaults, Python dict parser
  chunk_pool.zig      [NEW]  Chunk union, ChunkRecycler (hub-local free list)
  arena.zig           [NEW]  RequestArena (bump + linked chunks + direct allocs)
  connection.zig      [MOD]  Connection uses arena + inline_chunk; pool dynamic
  hub.zig             [MOD]  Hub owns ChunkRecycler + config; arena-based greenRecv
  extension.zig       [MOD]  Updated hub_run doc
  http_response.zig   [MOD]  Add 413 status code
  ring.zig            [---]  No changes
  http.zig            [---]  No changes
  op_slot.zig         [---]  No changes

theframework/
  server.py           [MOD]  Pass config dict, handle arena errors, limit checks
  app.py              [MOD]  Add config kwargs to run()
  request.py          [---]  No changes

stubs/
  _framework_core.pyi [MOD]  Updated hub_run signature

build.zig             [MOD]  Add test targets for new modules
```

## Memory Budget

With defaults (`chunk_size=4096`, `max_connections=4096`):

- Connection array: `4096 × ~4.2 KB = ~17 MB` (includes inline chunks)
- ChunkRecycler: starts at 0, grows to match concurrent request demand
- fd_to_conn: `65536 × 2 = 128 KB`
- **Total baseline: ~17.1 MB**

Under load with 1000 concurrent requests, each overflowing one chunk:
- ~1000 additional cached chunks: `1000 × 4 KB = 4 MB`
- **Total: ~21.1 MB**

## Memory Safety Guarantees

1. **No use-after-free**: Arena slices valid from allocation until `arena.reset()`. Reset
   happens at a well-defined point (between requests or on connection close).
2. **No double-free**: Arena memory is never individually freed. Only `reset()` reclaims.
3. **No buffer overflow**: Every `alloc()` checks bounds against chunk size and hard limits.
4. **No memory leak**: Chunks return to recycler on reset. Recycler freed on hub shutdown.
   Directs freed on reset.
5. **No fragmentation**: Bump allocation is fragmentation-free. Every reset starts from
   offset `DATA_START`.
6. **Bounded memory**: Total capped at `max_request_size` per connection.

## Hot Path Performance

For a typical HTTP/1.1 keep-alive request in steady state:

| Operation | Cost |
|---|---|
| `currentFreeSlice()` | 1 comparison + pointer arithmetic |
| `commitBytes()` | 2 additions |
| `reset()` (1 chunk, no directs) | Reset offset to DATA_START. Zero function calls. |
| `reset()` (N extra chunks) | N pushes onto recycler stack |

**Zero malloc/free on the hot path** after the recycler warms up (first few requests).

## Migration Strategy

Each phase produces a working system:
1. **Phase 1** (config) — purely additive, no existing code changes
2. **Phase 2** (ChunkRecycler) — new file, minimal Hub changes
3. **Phase 3** (RequestArena) — new file, no existing code changes
4. **Phase 4** (integration) — atomic change to Connection + Hub. One commit.
5. **Phase 5** (hard limits) — enforcement on top of working system

## Backward Compatibility

- `hub_run` third argument is optional (`|O` format). Existing callers unaffected.
- `green_recv` returns identical Python bytes objects. Transparent to Python code.
- Connection pool behavior unchanged from Python's perspective.

## Future Improvements (Not In Scope)

- **GC**: Watermark-based cleanup of recycler
- **Body streaming**: `request.stream()` for large bodies without full buffering
- **Alignment**: Add alignment parameter to `alloc()` for alignment requirements
- **Shared refs**: Reference-counted allocations for cross-lifetime data sharing
