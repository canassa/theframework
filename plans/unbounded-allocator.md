# Unbounded Arena Allocator

Remove the artificial `max_request_size` hard limit from `RequestArena`. Match H2O's design: the pool grows without bound, and `reset()` reclaims everything. The only limit is system memory.

## Why

The current arena enforces `max_request_size` (1,114,112 bytes). This is a departure from H2O's design with no upside:

1. **It doesn't protect against DoS.** A malicious client can open thousands of connections, each using just under the limit. Real DoS protection comes from connection limits, rate limiting, and timeouts — not per-request memory caps.

2. **It creates the hard limit collision.** The response-formatting plan (`plans/response-formatting.md`) wants to allocate responses from the arena. But request + response share the same budget. A 500 KB request body + 700 KB response = 1.2 MB, which exceeds the 1.1 MB limit. The plan needs a malloc fallback that defeats the arena's purpose.

3. **H2O doesn't do it.** H2O's `h2o_mem_pool_t` has no `max_size` field, no `total_allocated` counter, no bounds check (`h2o/lib/common/memory.c:190-218`). The pool grows by adding chunks and direct allocations. `h2o_mem_clear_pool()` frees everything. The only limit is `malloc` returning NULL, which triggers `h2o_fatal()`.

4. **The framework already has the right limits elsewhere.** `max_header_size` (32 KB) and `max_body_size` (1 MB) are checked during HTTP parsing, before data enters the arena. These are the right place to enforce limits — at the protocol level, not the allocator level.

## Current State

### `RequestArena` struct (`src/arena.zig`)

```
fields:
  chunks: ?*Chunk              — linked list head
  chunk_offset: usize          — bump offset in current chunk
  directs: ?*DirectAlloc       — linked list of large mallocs
  first_chunk: *Chunk          — inline 4 KB chunk (never recycled)
  recycler: *ChunkRecycler     — hub-local free list
  config: ArenaConfig          — REMOVE dependency on this
  total_allocated: usize       — REMOVE
  direct_threshold: usize      — KEEP (computed from chunk size)
```

### Where `total_allocated` / `max_request_size` are checked

| Location | Check | Purpose |
|----------|-------|---------|
| `arena.zig:71` `alloc()` | `total_allocated + sz > max_request_size` | Gate every allocation |
| `arena.zig:119` `currentFreeSlice()` | `total_allocated >= max_request_size` | Gate recv buffer |
| `arena.zig:82,96,110` | `total_allocated += size` | Accounting |
| `arena.zig:138` `commitBytes()` | `total_allocated += nbytes` | Accounting |
| `arena.zig:171` `reset()` | `total_allocated = 0` | Reset |
| `arena.zig:176` `totalUsed()` | return `total_allocated` | Monitoring |

### Where `max_request_size` is configured

| Location | What |
|----------|------|
| `config.zig:14` | Field declaration: `max_request_size: u32` |
| `config.zig:26` | Default: `1_114_112` |
| `hub.zig:1464` | Python dict extraction |

## Changes

### Step 1: Remove `total_allocated` and `max_request_size` check from `RequestArena`

**File: `src/arena.zig`**

Remove the `total_allocated` field and the `config` field. The arena only needs `direct_threshold` from the config — pass it directly in `init()`.

Remove the hard limit check in `alloc()`:

```zig
// BEFORE
pub fn alloc(self: *RequestArena, sz: usize) ?[]u8 {
    if (self.total_allocated + sz > self.config.max_request_size) return null;
    const size = if (sz == 0) 1 else sz;
    if (size >= self.direct_threshold) return self.allocDirect(size);
    // ...
}

// AFTER
pub fn alloc(self: *RequestArena, sz: usize) ?[]u8 {
    const size = if (sz == 0) 1 else sz;
    if (size >= self.direct_threshold) return self.allocDirect(size);
    // ...
}
```

Remove the hard limit check in `currentFreeSlice()`:

```zig
// BEFORE
pub fn currentFreeSlice(self: *RequestArena, max_len: usize) ?[]u8 {
    if (self.total_allocated >= self.config.max_request_size) return null;
    // ...
}

// AFTER
pub fn currentFreeSlice(self: *RequestArena, max_len: usize) ?[]u8 {
    // No hard limit — grows until malloc fails
    // ...
}
```

Remove `total_allocated += size` from `alloc`, `allocNewChunk`, `allocDirect`, `commitBytes`.

Remove `total_allocated = 0` from `reset()`.

Remove the `totalUsed()` method.

**New struct fields:**

```zig
pub const RequestArena = struct {
    chunks: ?*Chunk,
    chunk_offset: usize,
    directs: ?*DirectAlloc,
    first_chunk: *Chunk,
    recycler: *ChunkRecycler,
    direct_threshold: usize,

    pub fn init(first_chunk: *Chunk, recycler: *ChunkRecycler, direct_threshold: usize) RequestArena {
        // ...
    }
    // ...
};
```

### Step 2: Remove `max_request_size` from `ArenaConfig`

**File: `src/config.zig`**

Delete the `max_request_size` field from `ArenaConfig`. Keep `max_header_size` and `max_body_size` — those are protocol-level limits used during HTTP parsing, not allocator limits.

```zig
// BEFORE
pub const ArenaConfig = extern struct {
    chunk_size: u32,
    max_header_size: u32,
    max_body_size: u32,
    max_request_size: u32,      // DELETE
    max_connections: u16,
    // ...
};

// AFTER
pub const ArenaConfig = extern struct {
    chunk_size: u32,
    max_header_size: u32,
    max_body_size: u32,
    max_connections: u16,
    // ...
};
```

Update `defaults()` to remove the `max_request_size` line.

### Step 3: Update `ConnectionPool.init()`

**File: `src/connection.zig`**

Pass `config.directThreshold()` instead of the full config:

```zig
// BEFORE
connections[i].arena = RequestArena.init(
    &connections[i].inline_chunk,
    recycler,
    config,
);

// AFTER
connections[i].arena = RequestArena.init(
    &connections[i].inline_chunk,
    recycler,
    config.directThreshold(),
);
```

### Step 4: Update `hub.zig`

**File: `src/hub.zig`**

Remove `max_request_size` from the Python config dict extraction (`hub.zig:1464`).

Update `greenRecv()` error message at line 454-458. Currently says "request too large (arena exhausted)" when `currentFreeSlice` returns null. After the change, null means malloc failed (OOM), not "too large". Update the message:

```zig
// BEFORE
"green_recv: request too large (arena exhausted)",

// AFTER
"green_recv: out of memory",
```

### Step 5: Update tests

**File: `src/arena.zig` tests**

- **Delete** `test "hard limit: alloc beyond max_request_size returns null"` — the limit no longer exists.
- **Delete** `test "currentFreeSlice returns null when hard limit exceeded"` — same reason.
- **Update** all remaining tests: `RequestArena.init()` now takes `(first_chunk, recycler, direct_threshold)` instead of `(first_chunk, recycler, config)`. Pass `ArenaConfig.defaults().directThreshold()` or just `1022` directly.
- **Remove** assertions on `arena.totalUsed()` — the method no longer exists.

**File: `src/config.zig` tests**

- **Update** `test "defaults returns expected values"` — remove `max_request_size` assertion.

**File: `src/connection.zig` tests**

- **Update** `ConnectionPool.init` calls — config no longer has `max_request_size`.
- **Remove** `conn.arena.totalUsed()` assertions.

## What This Enables

With the hard limit gone, the response-formatting plan (`plans/response-formatting.md`) becomes simpler:

1. `estimateResponseSize()` computes a tight upper bound (same as before).
2. `arena.alloc(est)` allocates from the per-connection arena. No limit collision. No malloc fallback needed.
3. `arena.reset()` at request end reclaims everything — chunks go to the recycler, directs are freed.

The malloc fallback in `formatResponseFromArena` can be removed entirely. The only failure mode is true OOM (malloc returns null), which is handled the same way H2O handles it — return an error up the stack.

## What Still Protects Against Abuse

- `max_header_size` (32 KB) — rejects requests with oversized headers during parsing
- `max_body_size` (1 MB) — rejects requests with oversized bodies during parsing
- `max_connections` (4096) — caps total concurrent connections
- Connection timeouts (read, keepalive, handler) — reclaim idle connections
- OS `ulimit` / cgroup memory limits — hard ceiling on process memory

These are the right controls. The allocator shouldn't second-guess what the parser already validated.
