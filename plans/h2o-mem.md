# H2O HTTP Server — Memory Allocation Architecture

A deep analysis of the memory management strategies used by the [h2o](https://github.com/h2o/h2o) HTTP server. H2O implements a layered, zero-lock memory system designed for high-throughput HTTP serving with minimal allocation overhead on the hot path.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [The Arena Pool: `h2o_mem_pool_t`](#2-the-arena-pool-h2o_mem_pool_t)
3. [The Recycling Allocator: `h2o_mem_recycle_t`](#3-the-recycling-allocator-h2o_mem_recycle_t)
4. [The Buffer System: `h2o_buffer_t`](#4-the-buffer-system-h2o_buffer_t)
5. [The Double Buffer: `h2o_doublebuffer_t`](#5-the-double-buffer-h2o_doublebuffer_t)
6. [Token Interning and String Management](#6-token-interning-and-string-management)
7. [HPACK Dynamic Table: Shared Ownership](#7-hpack-dynamic-table-shared-ownership)
8. [Lifetime Model: Connections, Requests, and Streams](#8-lifetime-model-connections-requests-and-streams)
9. [Socket-Level Memory](#9-socket-level-memory)
10. [Zero-Copy Strategies](#10-zero-copy-strategies)
11. [Periodic Garbage Collection](#11-periodic-garbage-collection)
12. [The General-Purpose Cache: `h2o_cache_t`](#12-the-general-purpose-cache-h2o_cache_t)
13. [Design Principles Summary](#13-design-principles-summary)

---

## 1. Architecture Overview

H2O's memory system is built on three core allocators plus several specialized subsystems:

```
                    ┌─────────────────────────────────────────┐
                    │           h2o_mem_pool_t                │
                    │        (per-request arena)              │
                    ├──────────┬──────────┬───────────────────┤
                    │ chunks   │ directs  │ shared_refs       │
                    │ (≤1022B) │ (>1022B) │ (refcounted)      │
                    └────┬─────┴────┬─────┴────┬──────────────┘
                         │          │          │
              ┌──────────▼──┐   ┌───▼───┐  ┌───▼──────────────┐
              │ 4KB chunks  │   │malloc()│  │malloc() + refcnt │
              │ (bump alloc)│   │ each   │  │ + dispose cb     │
              └──────┬──────┘   └───────┘  └──────────────────┘
                     │
            ┌────────▼──────────────────┐
            │  h2o_mem_recycle_t        │
            │  (thread-local free list) │
            │  h2o_mem_pool_allocator   │
            │  memsize=4096             │
            └────────┬──────────────────┘
                     │
              ┌──────▼──────┐
              │ malloc/free │
              └─────────────┘

 ┌──────────────────────────────────────┐
 │       Buffer Bin System              │
 │  Power-of-2 size classes (≥4096)     │
 │  Each bin = h2o_mem_recycle_t        │
 │  Dynamically created on demand       │
 ├──────────────────────────────────────┤
 │  bin[0]: 4096 (2^12)                 │
 │  bin[1]: 8192 (2^13)                 │
 │  bin[2]: 16384 (2^14)                │
 │  ...                                 │
 │  zero_sized: sizeof(h2o_buffer_t)    │
 └──────────────────────────────────────┘
```

**Key properties:**
- **Thread-local everything**: all recycling allocators are `__thread`-local. Zero lock contention.
- **Arena + bulk free**: per-request data uses bump allocation, freed in one call at request end.
- **Reference counting for cross-lifetime sharing**: when data must outlive one lifetime but not another (e.g., HPACK table entries shared with request pools).
- **Abort-on-OOM**: all allocation wrappers (`h2o_mem_alloc`, `h2o_mem_realloc`) call `h2o_fatal` on failure. No error-return allocation paths.

---

## 2. The Arena Pool: `h2o_mem_pool_t`

**Files:** `include/h2o/memory.h:127-132`, `lib/common/memory.c:154-219`

The primary per-request allocator. A classic bump/arena allocator with 4KB chunks.

### 2.1 Data Structures

```c
typedef struct st_h2o_mem_pool_t {
    union un_h2o_mem_pool_chunk_t *chunks;           // linked list of 4KB chunks
    size_t chunk_offset;                              // bump pointer within current chunk
    struct st_h2o_mem_pool_shared_ref_t *shared_refs; // linked list of ref-counted objects
    struct st_h2o_mem_pool_direct_t *directs;         // linked list of large direct mallocs
} h2o_mem_pool_t;

// Chunk: union overlay — next pointer shares space with payload
union un_h2o_mem_pool_chunk_t {
    union un_h2o_mem_pool_chunk_t *next;  // intrusive linked-list pointer
    char bytes[4096];                      // 4KB payload
};
// Usable space per chunk: 4096 - sizeof(void*) = 4088 bytes on 64-bit
```

### 2.2 Allocation Algorithm

Three tiers based on size (`memory.c:190-219`):

| Size range | Strategy | Details |
|---|---|---|
| 0 bytes | Returns valid pointer | `sz` forced to 1 |
| 1–1021 bytes | **Bump allocation** from 4KB chunk | O(1), aligned to requested alignment. When chunk is full, new chunk from recycler. |
| ≥1022 bytes | **Direct malloc** | Individually `malloc`'d, linked into `pool->directs` list |

The threshold `(4096 - 8) / 4 = 1022` prevents any single allocation from wasting more than 25% of a chunk.

**Initialization trick** (`memory.c:154-160`): `chunk_offset` is set to `sizeof(chunk->bytes)` (4096), which is "past the end." This forces the first allocation to trigger a new chunk, avoiding a special "no chunk yet" code path.

**Alignment**: Fully parametric via `ALIGN_TO(x, a)` = `(x + a - 1) & ~(a - 1)`. The public macro `h2o_mem_alloc_pool(pool, type, cnt)` automatically computes alignment from the type via `__alignof__`.

### 2.3 Growing Beyond One Chunk

The pool is a **linked list** of chunks, not a single fixed-size arena. When the current chunk fills up, it simply grabs another one:

```c
// memory.c:205-212 — when current chunk is full
if (sizeof(pool->chunks->bytes) - pool->chunk_offset < sz) {
    union un_h2o_mem_pool_chunk_t *newp = h2o_mem_alloc_recycle(&h2o_mem_pool_allocator);
    newp->next = pool->chunks;
    pool->chunks = newp;    // push onto linked list
    pool->chunk_offset = ALIGN_TO(sizeof(newp->next), alignment);
}
```

There is no limit on the number of chunks. If request headers span 20 `h2o_header_t` structs and fill the first 4KB chunk, the pool grabs a second chunk from the recycler and keeps bumping.

If any **single** allocation is ≥ 1022 bytes (e.g., a huge `Cookie` header value), it bypasses chunks entirely and goes straight to `malloc` via the `pool->directs` list.

Note that the **raw request bytes** don't live in the pool at all — they live in `sock->input` (an `h2o_buffer_t`). The parsed header names/values are typically just borrowed `h2o_iovec_t` pointers into that buffer. The pool is used for the `h2o_header_t` structs themselves, duplicated strings, and other request-processing data.

### 2.3 Pool vs. Recycler: Where Chunks Live

A critical distinction: the **pool** and the **thread-local recycler** are separate structures. Chunks bounce between them — they are never `malloc`'d or `free`'d on a per-request basis in steady state.

```
Request 1 starts:
  pool.chunks = NULL (empty)
  ↓ alloc needed
  recycler stack: [empty] → malloc(4096) from OS   ← only time malloc is called
  chunk goes into pool.chunks

Request 1 ends:
  h2o_mem_clear_pool()
  chunk moves:  pool.chunks → recycler stack: [chunk_A]
  pool.chunks = NULL again (pool is empty)

Request 2 starts:
  pool.chunks = NULL (empty)
  ↓ alloc needed
  recycler stack: [chunk_A] → pop chunk_A           ← no malloc, just a pointer op
  chunk_A goes into pool.chunks

Request 2 ends:
  chunk_A moves:  pool.chunks → recycler stack: [chunk_A]
  ...and so on forever — no malloc/free on the hot path
```

The **pool is ephemeral** — fully cleared every request, goes back to empty. The **recycler is the long-lived cache** — it persists across requests, holding freed chunks so they can be reused. After the first few requests warm up the cache, allocation and deallocation are just push/pop on a thread-local stack.

### 2.4 Deallocation: Bulk Free

`h2o_mem_clear_pool()` (`memory.c:162-188`) frees everything in three phases:

1. **Shared refs**: walk list, `h2o_mem_release_shared()` each (decrement refcnt, free if zero).
2. **Directs**: walk list, `free()` each.
3. **Chunks**: walk list, return each to thread-local `h2o_mem_pool_allocator` recycler (NOT `free()`).

After clearing, the pool is in a valid state for reuse — no re-initialization needed.

### 2.5 Shared (Ref-Counted) Allocations

```c
void *h2o_mem_alloc_shared(h2o_mem_pool_t *pool, size_t sz, void (*dispose)(void *));
```

These are individually `malloc`'d objects with a header containing `refcnt` and `dispose` callback (`memory.h:117-121`). When linked to a pool, clearing the pool decrements the refcnt. The object is only freed when refcnt reaches zero.

`h2o_mem_link_shared()` both increments the refcnt AND links to a new pool — enabling shared ownership across two lifetimes (e.g., HPACK table + request pool).

---

## 3. The Recycling Allocator: `h2o_mem_recycle_t`

**Files:** `include/h2o/memory.h:106-115`, `lib/common/memory.c:82-152`

A fixed-size, thread-local free-list with adaptive watermark-based eviction.

### 3.1 Data Structures

```c
typedef struct st_h2o_mem_recycle_conf_t {
    size_t memsize;        // fixed size of each allocation
    uint8_t align_bits;    // alignment as power-of-two bits
} h2o_mem_recycle_conf_t;

typedef struct st_h2o_mem_recycle_t {
    const h2o_mem_recycle_conf_t *conf;
    H2O_VECTOR(void *) chunks;    // LIFO stack of cached free pointers
    size_t low_watermark;          // minimum pool size since last GC
} h2o_mem_recycle_t;
```

### 3.2 Operations

- **Alloc** (`memory.c:102-115`): Pop from `chunks` stack (LIFO, cache-friendly). If empty, `posix_memalign` / `malloc` from OS.
- **Free** (`memory.c:117-126`): Push onto `chunks` stack. Under AddressSanitizer, `free()` directly instead (for use-after-free detection).
- **GC** (`memory.c:128-152`): See [Section 11](#11-periodic-garbage-collection).

### 3.3 Instances

All are `__thread`-local:

| Instance | Location | `memsize` | Purpose |
|---|---|---|---|
| `h2o_mem_pool_allocator` | `memory.c:82-83` | 4096 | Recycles 4KB arena chunks |
| `h2o_socket_ssl_buffer_allocator` | `socket.c:194` | ~65KB | Recycles TLS output buffers |
| `h2o_socket_zerocopy_buffer_allocator` | `socket.c:195` | ~65KB | Recycles zero-copy send buffers |
| `buffer_recycle_bins[N]` | `memory.c:257-277` | 2^(12+N) | Power-of-2 buffer bins |

---

## 4. The Buffer System: `h2o_buffer_t`

**Files:** `include/h2o/memory.h:137-174`, `lib/common/memory.c:254-555`

A growable byte buffer with power-of-2 recycling, capacity memory, and mmap fallback.

### 4.1 Data Structure

```c
typedef struct st_h2o_buffer_t {
    size_t capacity;              // capacity or desired next capacity if empty
    size_t size;                  // amount of data available
    char *bytes;                  // pointer to start of data (NULL if prototype)
    h2o_buffer_prototype_t *_prototype;
    int _fd;                      // fd for mmap-backed buffers (-1 otherwise)
    char _buf[1];                 // flexible array: inline storage
} h2o_buffer_t;
```

### 4.2 Three States

| State | `bytes` | `_prototype` | Meaning |
|---|---|---|---|
| **Prototype (sentinel)** | NULL | NULL | Zero-cost initial state, points into prototype's `_initial_buf` |
| **Tombstone** | NULL | non-NULL | Empty buffer that remembers previous capacity |
| **Active** | non-NULL | non-NULL | Live buffer with data |

### 4.3 Key Operations

**Init** (`memory.h:496-499`): Zero-cost. Buffer pointer set to prototype's `_initial_buf`. No allocation.

**Reserve** (`memory.c:409-503`): The most complex operation, with five paths:
1. Prototype state → allocate via `buffer_allocate()`
2. Tombstone → free tombstone, allocate at remembered capacity
3. Sufficient space → no-op
4. Compactable → `memmove()` data to front (when `(size + min_guarantee) * 2 <= capacity`)
5. Must grow → double capacity until it fits, allocate from bin or mmap

**Consume** (`memory.c:505-516`): **Pointer-advance** — never copies data. `bytes` moves forward, `size` decrements. Gap reclaimed lazily during next compaction.

**Consume All** (`memory.c:518-532`): Two modes:
- `record_capacity=1`: Creates a zero-sized tombstone that remembers the old capacity. Next reserve tries that size first.
- `record_capacity=0`: Reverts to prototype sentinel. No capacity memory.

### 4.4 Power-of-2 Bin Recycling

```c
#define H2O_BUFFER_MIN_ALLOC_POWER 12  // smallest bin = 4096 bytes

static __thread struct {
    struct buffer_recycle_bin_t { ... } *bins;  // dynamically sized array
    size_t largest_power;                        // largest bin that exists
    h2o_mem_recycle_t zero_sized;                 // for tombstone buffers
} buffer_recycle_bins;
```

Size class computed via `__builtin_clzll` (count leading zeros) → `ceil(log2(sz))`, clamped to minimum 12 (4KB). Bins created on demand.

**Allocate** (`memory.c:379-407`): Two-try strategy:
1. Try `desired_capacity` first, but only if a recycled chunk already exists for that size class
2. Fall back to `min_capacity`, creating the bin if needed

### 4.5 mmap Fallback for Large Buffers

Configured via `h2o_buffer_mmap_settings_t` (`memory.h:165-169`):
- `threshold`: capacity at which buffer transitions to mmap (default 32MB for sockets)
- `fn_template`: mkstemp template for temp files

When triggered (`memory.c:441-488`):
1. Create temp file via `mkstemp()`, immediately `unlink()` it
2. Expand file via `posix_fallocate()` or `ftruncate()`
3. `mmap(PROT_READ | PROT_WRITE, MAP_SHARED)`
4. Copy existing data, free old malloc buffer
5. The `h2o_buffer_t` struct itself lives at the start of the mmap region

Purpose: prevents pathological connections (slow clients, large uploads) from exhausting physical RAM. The OS can page mmap'd data to disk under memory pressure.

### 4.6 Buffer-to-Pool Linking

```c
void h2o_buffer_link_to_pool(h2o_buffer_t *buffer, h2o_mem_pool_t *pool);
```

Creates a shared allocation in the pool holding a pointer to the buffer. When the pool is cleared, the dispose callback calls `h2o_buffer_dispose()`. This ties a buffer's lifetime to a request pool without the buffer being allocated from the pool. Used in the file handler (`file.c:527`) to tie response body buffers to request lifetime.

---

## 5. The Double Buffer: `h2o_doublebuffer_t`

**File:** `include/h2o/memory.h:176-180`

Enables producer-consumer pipelining for response streaming.

```c
typedef struct st_h2o_doublebuffer_t {
    h2o_buffer_t *buf;         // the "sending" buffer
    unsigned char inflight : 1;
    size_t _bytes_inflight;
} h2o_doublebuffer_t;
```

**Prepare** (`memory.h:552-569`): If the doublebuffer's `buf` is empty, swap it with the `*receiving` buffer via a **three-pointer swap** — no data copy. Then mark bytes as inflight and return an iovec for the I/O layer.

```c
// Zero-copy swap
h2o_buffer_t *t = db->buf;
db->buf = *receiving;
*receiving = t;
```

**Consume** (`memory.h:577-588`): After I/O completion, consume the inflight bytes. If all data sent, `h2o_buffer_consume_all(record_capacity=1)` to preserve capacity for reuse.

Used by FastCGI handler (`lib/handler/fastcgi.c`) and reverse proxy (`lib/core/proxy.c`) for streaming response bodies.

---

## 6. Token Interning and String Management

### 6.1 String Representation: `h2o_iovec_t`

```c
typedef struct st_h2o_iovec_t {
    char *base;
    size_t len;
} h2o_iovec_t;
```

A fat pointer with **no ownership information**. Ownership is determined by context:

| Ownership | How created | Freed by |
|---|---|---|
| Borrowed | `h2o_iovec_init(ptr, len)` | Owner of underlying buffer |
| Pool-owned | `h2o_strdup(pool, s, len)` | `h2o_mem_clear_pool()` |
| Heap-owned | `h2o_strdup(NULL, s, len)` | Caller via `free()` |
| Shared/refcounted | `h2o_strdup_shared(pool, s, len)` | When refcnt reaches 0 |
| Static/interned | Token pointer (`&token->buf`) | Never (compile-time constant) |

### 6.2 Token System: 85 Pre-Interned Header Names

**Files:** `include/h2o/token.h`, `lib/common/token_table.h`

All common HTTP header names are compile-time constants in a global array:

```c
h2o_token_t h2o__tokens[H2O_MAX_TOKENS];  // 85 entries
// Accessed via: H2O_TOKEN_CONTENT_TYPE, H2O_TOKEN_AUTHORIZATION, etc.
```

**Lookup** (`token_table.h:274-698`): Code-generated switch-on-length tree. For each header name length, switches on the last character, then `memcmp` on the rest. O(1) dispatch + single `memcmp`.

**Identity check** (`token.c:25-28`):
```c
int h2o_iovec_is_token(const h2o_iovec_t *buf)
{
    return &h2o__tokens[0].buf <= buf && buf <= &h2o__tokens[H2O_MAX_TOKENS - 1].buf;
}
```

A **pointer-range check** — tests if the pointer falls within the static `h2o__tokens[]` array. O(1), zero allocation. Used everywhere to avoid ref-counting static strings.

**Header lookup by pointer identity** (`headers.c:50-58`):
```c
ssize_t h2o_find_header(const h2o_headers_t *headers, const h2o_token_t *token, ssize_t cursor)
{
    for (++cursor; cursor < headers->size; ++cursor) {
        if (headers->entries[cursor].name == &token->buf)  // pointer compare!
            return cursor;
    }
    return -1;
}
```

When names are tokens, header lookup is O(n) pointer comparisons, not O(n*m) string comparisons.

### 6.3 Vector Management: `H2O_VECTOR`

```c
#define H2O_VECTOR(type) struct { type *entries; size_t size; size_t capacity; }
```

**Growth strategy** (`memory.c:540-555`): Start at 4, double until `>= new_capacity`.

**Pool-aware**: When `pool != NULL`, allocates from pool — old entries are abandoned (freed with pool). When `pool == NULL`, uses `realloc`.

---

## 7. HPACK Dynamic Table: Shared Ownership

**Files:** `include/h2o/http2_common.h:86-101`, `lib/http2/hpack.c`

The most sophisticated cross-lifetime pattern in h2o.

### 7.1 Table Structure

A **ring buffer** of `(name*, value*)` pairs with modular-arithmetic indexing:

```c
typedef struct st_h2o_hpack_header_table_t {
    struct st_h2o_hpack_header_table_entry_t *entries; // ring buffer
    size_t num_entries, entry_capacity, entry_start_index;
    size_t hpack_size;          // sum of (32 + name_len + value_len)
    size_t hpack_capacity;      // negotiated capacity
} h2o_hpack_header_table_t;
```

### 7.2 The `alloc_buf` Pattern

```c
static h2o_iovec_t *alloc_buf(h2o_mem_pool_t *pool, size_t len)
{
    h2o_iovec_t *buf = h2o_mem_alloc_shared(pool, sizeof(h2o_iovec_t) + len + 1, NULL);
    buf->base = (char *)buf + sizeof(h2o_iovec_t);
    buf->len = len;
    return buf;
}
```

**Single contiguous allocation**: the `h2o_iovec_t` header and string data are allocated together. The `base` pointer points right after the header. This gives reference counting, cache locality, and one allocation call.

### 7.3 Two-Tier Reference Management

During HPACK decoding (`hpack.c:318-434`):

1. **Static table references**: Pointer to `h2o_hpack_static_table[i].name` (a token). Never ref-counted.
2. **Dynamic table references**: `h2o_mem_link_shared(request_pool, name)` — increments refcnt AND links to request pool.
3. **New entries decoded**: `alloc_buf()` creates a shared allocation. If the name matches a known token, the token pointer replaces it.
4. **Indexing into dynamic table**: `h2o_mem_addref_shared(name)` keeps the table's reference.

**Lifecycle**: When the request pool is cleared, the refcnt decrements. When the table evicts the entry, the refcnt decrements again. The memory is freed only when both the pool AND the table have released their references.

### 7.4 Encoding Side

The encoder-side dynamic table (`hpack.c:829-908`) makes **independent copies** via `alloc_buf(NULL, ...)` (no pool, standalone shared entries). This prevents data lifetime entanglement with request pools.

---

## 8. Lifetime Model: Connections, Requests, and Streams

### 8.1 HTTP/1.1

```
┌──────────────────────────────────────────────┐
│  st_h2o_http1_conn_t  (one malloc)           │
│  ├── h2o_socket_t *sock       [CONNECTION]   │
│  ├── h2o_buffer_t *req_body   [REQUEST]      │
│  └── h2o_req_t req (embedded) [REQUEST pool] │
│       └── h2o_mem_pool_t pool                │
└──────────────────────────────────────────────┘
```

- **Connection struct**: single `h2o_mem_alloc(sizeof(*conn))`. The `h2o_req_t` is **embedded** (not heap-allocated), because HTTP/1.1 handles one request at a time.
- **Keep-alive recycling** (`http1.c:110-132`): Between requests, `h2o_dispose_request()` clears the pool, then `h2o_init_request()` re-initializes it. The socket's input buffer persists across requests.
- **Connection teardown** (`http1.c:134-148`): Dispose request → close socket → `free(conn)`.

### 8.2 HTTP/2

```
┌──────────────────────────────────────────────────────┐
│  st_h2o_http2_conn_t  (one malloc)                   │
│  ├── h2o_socket_t *sock              [CONNECTION]    │
│  ├── khash_t *streams                [CONNECTION]    │
│  ├── h2o_hpack_header_table_t (×2)   [CONNECTION]    │
│  ├── h2o_buffer_t *_write.buf        [CONNECTION]    │
│  └── streams:                                        │
│       ┌──────────────────────────────┐               │
│       │ st_h2o_http2_stream_t        │  (per-stream  │
│       │ ├── h2o_buffer_t *req_body   │   malloc)     │
│       │ └── h2o_req_t req (embedded) │               │
│       │      └── h2o_mem_pool_t pool │               │
│       └──────────────────────────────┘               │
└──────────────────────────────────────────────────────┘
```

- **Connection-level**: socket, HPACK tables, write buffers, scheduler tree, stream hash table.
- **Stream-level**: each stream is `h2o_mem_alloc(sizeof(*stream))`, with its own embedded `h2o_req_t` and pool.
- **Stream close** (`stream.c:68-79`): unregister from hash → dispose request body → clear pool → `free(stream)`.
- **Recently closed streams** (`connection.c:270-288`): A 10-slot ring buffer of truncated stream structs (only `stream_id` + `_scheduler`) for HTTP/2 priority dependency preservation.

### 8.3 Lifetime Summary

| Resource | HTTP/1.1 | HTTP/2 |
|---|---|---|
| Connection struct | `malloc`, freed on close | `malloc`, freed on close |
| Socket + input buffer | Connection lifetime | Connection lifetime |
| HPACK tables | N/A | Connection lifetime, entries shared-refcounted |
| Request struct | Embedded in conn | Embedded in per-stream malloc |
| Request pool | Cleared between keep-alive requests | Cleared on stream close |
| Request body buffer | Per-request, `h2o_buffer_t` | Per-stream, `h2o_buffer_t` |
| Parsed headers | Point into `sock->input` or pool-copied | Shared-refcounted with HPACK table |

---

## 9. Socket-Level Memory

**Files:** `lib/common/socket.c`, `lib/common/socket/evloop.c.h`

### 9.1 Socket Allocation

Sockets are individually `h2o_mem_alloc(sizeof(*sock))` — no recycling at struct level. The evloop wrapper (`st_h2o_evloop_socket_t`) embeds `h2o_socket_t` as its first member.

### 9.2 TLS Buffer Management

| Buffer | Allocator | Lifetime |
|---|---|---|
| `ssl->input.encrypted` | `h2o_buffer_t` (recycled) | Connection — freed in `destroy_ssl()` |
| `ssl->output.buf` | `h2o_mem_alloc_recycle()` from SSL buffer allocator | Per-write — allocated on demand, freed after write completes |
| `_write_buf.flattened` | `h2o_mem_alloc_recycle()` from SSL buffer allocator | Per-write — for sendvec flattening |

The SSL output buffer (`~65KB`) is **not pre-allocated**. It's obtained from the thread-local recycler on demand (`init_ssl_output_buffer`, socket.c:290-299) and returned after the write completes (`dispose_ssl_output_buffer`, socket.c:301-321). If the buffer was expanded beyond standard size (e.g., large certificate chains), it's `free()`'d instead of recycled.

### 9.3 Why Two Separate SSL Buffer Allocators?

The `h2o_socket_zerocopy_buffer_allocator` exists separately from `h2o_socket_ssl_buffer_allocator` because zero-copy buffers may be retained by the kernel after `sendmsg(MSG_ZEROCOPY)`. Keeping separate pools prevents cross-contamination.

### 9.4 Socket Pool (Upstream Connections)

`h2o_socketpool_t` (`include/h2o/socketpool.h`) is a **connection pool for reverse proxy upstream connections** only (not incoming client connections). It stores exported socket state (`h2o_socket_export_t`) in a mutex-protected linked list with configurable capacity and idle timeout (default 2 seconds).

---

## 10. Zero-Copy Strategies

H2O employs four tiers of zero-copy:

### 10.1 Pointer-Advance Consumption

`h2o_buffer_consume()` never copies data — it advances the `bytes` pointer and decrements `size`. The gap is reclaimed lazily during the next compaction in `h2o_buffer_try_reserve()`.

### 10.2 Double Buffer Pointer Swap

`h2o_doublebuffer_prepare()` swaps buffer pointers rather than copying data. The full receiving buffer becomes the sending buffer, and the empty sending buffer becomes the new receiving buffer.

### 10.3 `sendfile(2)` for Static Files

For unencrypted connections, the file handler (`lib/handler/file.c:199-245`) uses `sendfile(2)` to transfer file data directly from fd to socket, bypassing userspace entirely. Platform-specific implementations for Linux, macOS, and FreeBSD.

### 10.4 `MSG_ZEROCOPY` for TLS Output

On Linux with compatible kernel support (`evloop.c.h:233-256`):
- Activated only when picotls is used with a non-temporal AEAD cipher
- Requires `SO_ZEROCOPY` socket option, data >= 4KB, and buffer at standard recycler size
- After `sendmsg(MSG_ZEROCOPY)`, the buffer is pushed into a per-socket ring buffer (`st_h2o_socket_zerocopy_buffers_t`)
- Kernel notifications arrive via `MSG_ERRQUEUE` / `SO_EE_ORIGIN_ZEROCOPY`
- Buffers are reclaimed via `handle_zerocopy_notification()` (`epoll.c.h:73-133`)
- Socket close is deferred until all zerocopy buffers are reclaimed — `shutdown(SHUT_RDWR)` is called first, then actual `close(fd)` only after kernel releases all buffers

---

## 11. Periodic Garbage Collection

**File:** `lib/core/util.c:941-966`

### 11.1 The GC Entry Point

```c
uint32_t h2o_cleanup_thread(uint64_t now, h2o_context_t *ctx_optional)
{
    if (ctx_optional != NULL)
        h2o_filecache_clear(ctx_optional->filecache);

    static __thread uint64_t next_gc_at;
    if (now >= next_gc_at) {
        int full = now == 0;
        h2o_buffer_clear_recycle(full);
        h2o_socket_clear_recycle(full);
        h2o_mem_clear_recycle(&h2o_mem_pool_allocator, full);
        next_gc_at = now + 1000;  // next GC in 1 second
    }
}
```

Called **once per event loop iteration** (`src/main.c:4553`), but actual GC runs at most once per second. Passing `now == 0` triggers a full cleanup (used during shutdown).

### 11.2 The Low-Watermark Algorithm

```c
void h2o_mem_clear_recycle(h2o_mem_recycle_t *allocator, int full)
{
    if (full) {
        allocator->low_watermark = 0;
    } else {
        size_t delta = (allocator->low_watermark + 1) / 2;  // halve
        allocator->low_watermark = allocator->chunks.size - delta;
    }
    while (allocator->chunks.size > allocator->low_watermark)
        free(allocator->chunks.entries[--allocator->chunks.size]);
}
```

- The `low_watermark` tracks the **minimum cache size** observed during a GC interval
- Each GC pass releases **half** of the low watermark count
- Under steady load: cache stabilizes at working-set size
- Under declining load: cache shrinks by ~50% per second
- Under zero load: converges to 0, all cached memory returned to OS
- Under `full` GC: everything freed immediately

This is an elegant adaptive strategy that avoids both cache thrashing during bursty traffic and unbounded memory growth during idle periods.

---

## 12. The General-Purpose Cache: `h2o_cache_t`

**Files:** `include/h2o/cache.h`, `lib/common/cache.c`

An LRU+TTL cache with optional thread safety.

```c
struct st_h2o_cache_t {
    khash_t(cache) *table;       // O(1) hash lookup
    h2o_linklist_t lru;          // LRU doubly-linked list
    h2o_linklist_t age;          // insertion-order list for TTL
    size_t size, capacity;
    uint64_t duration;           // TTL in ticks
    void (*destroy_cb)(h2o_iovec_t value);
    pthread_mutex_t mutex;       // only if MULTITHREADED flag
};
```

- **Keys**: always copied via `h2o_strdup(NULL, ...)` (malloc-based)
- **Values**: owned by caller, cache receives by value, `destroy_cb` called on eviction
- **Reference counting**: `_refcnt` with `__sync_fetch_and_add/sub` (GCC atomic builtins). `h2o_cache_fetch()` increments, `h2o_cache_release()` decrements; freed when refcnt reaches 0.
- **Dual eviction**: capacity-based (LRU order) and TTL-based (insertion order)

---

## 13. Design Principles Summary

### 13.1 Core Principles

1. **Arena allocation per request**: All temporary request-processing memory comes from `req->pool`, enabling single-call bulk deallocation. No per-object free overhead on the hot path.

2. **Thread-local recycling**: All frequently-allocated fixed-size objects (4KB pool chunks, ~65KB TLS buffers, power-of-2 I/O buffers) use `__thread`-local free lists. Zero lock contention, zero syscall overhead in steady state.

3. **Adaptive cache sizing**: The low-watermark halving algorithm automatically sizes recycling caches to the working set. Memory is returned to the OS gradually during idle periods.

4. **Reference counting for cross-lifetime sharing**: When data must be shared between two independent lifetimes (e.g., HPACK dynamic table and request pool), shared-refcounted entries handle ownership correctly without complex lifecycle management.

5. **Lazy allocation / zero-cost initialization**: Buffers start as zero-cost sentinels (pointers into static prototypes). Memory is allocated only on first write. Sockets don't pre-allocate TLS buffers.

6. **Capacity memory**: The tombstone mechanism in `h2o_buffer_consume_all` remembers previous capacity, preventing re-growth oscillation on subsequent requests.

7. **mmap safety valve**: Buffers exceeding 32MB transition to file-backed mmap, allowing the OS to page under memory pressure without the server being OOM-killed.

### 13.2 Allocation Hot Path Analysis

For a typical HTTP/1.1 keep-alive request in steady state:

| Operation | Allocator | Cost |
|---|---|---|
| Read from socket into `sock->input` | Buffer reserve (recycled) | Near-zero (usually has space) |
| Parse request line + headers | Zero-copy pointers into `sock->input` | Zero allocation |
| Allocate pool memory for request processing | Bump allocation from recycled 4KB chunk | ~3 pointer ops |
| Build response headers | Pool allocation | ~3 pointer ops per header |
| Encrypt response (TLS) | Recycled ~65KB buffer | Pop from thread-local stack |
| Send response | `sendmsg()` / `sendfile()` | Zero copy |
| End of request: clear pool | Return chunks to recycler | ~3 pointer ops per chunk |

In the steady state, the hot path involves **zero `malloc`/`free` syscalls**. All allocation and deallocation happens within thread-local data structures with O(1) operations.

### 13.3 Key Design Patterns

| Pattern | Where | Benefit |
|---|---|---|
| Token interning (compile-time array + pointer-range check) | Header names | Zero-allocation header matching, O(1) identity comparison |
| Single-allocation structs (`offsetof` + flexible member) | `alloc_buf()`, filecache, shared entries | Cache-friendly, fewer allocations |
| Pointer-range ownership detection | `h2o_iovec_is_token()`, `value_is_part_of_static_table()` | Zero-cost ownership checks, no metadata overhead |
| Union overlay for intrusive lists | Pool chunk's `next` pointer shares bytes with payload | Zero overhead for list management |
| Abort-on-OOM | All allocation wrappers | Eliminates error-return allocation paths, simpler code |
| ASan-aware recycling | `h2o_mem_free_recycle()` bypasses cache under ASan | Enables use-after-free detection in debug builds |
