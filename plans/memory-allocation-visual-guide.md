# Memory Allocation Strategies - Visual Guide

Diagrams and visual representations of allocation strategies used in high-performance HTTP servers.

---

## 1. Arena/Bump Allocator Memory Layout

### Linear Bump Allocation Pattern

```
Request Arrives
    │
    ▼
┌──────────────────────────────────────┐
│      Request Arena (4KB)             │
├──────────────────────────────────────┤
│ Offset:  0                           │
│ Pointer: ▲                           │  ← Bump pointer (allocation head)
│          │                           │
│          └─────────────────────────  │  ← Free space
└──────────────────────────────────────┘

Allocate headers (200 bytes):
┌──────────────────────────────────────┐
│ [Headers: 200 bytes]                 │
│                      ▲                │  ← Bump pointer moves
│                      │                │
│                      └────────────────│  ← Free space
└──────────────────────────────────────┘

Allocate body (300 bytes):
┌──────────────────────────────────────┐
│ [Headers][Body: 300 bytes]           │
│                           ▲           │  ← Bump pointer
│                           │           │
│                           └───────────│  ← Free space
└──────────────────────────────────────┘

Request Complete:
    │
    ▼
┌──────────────────────────────────────┐
│      [FREED - Ready for next req]    │
├──────────────────────────────────────┤
│ Offset:  0                           │
│ Pointer: ▲ (Reset)                   │
└──────────────────────────────────────┘

Characteristics:
  ✓ Zero fragmentation
  ✓ Ultra-fast allocation (pointer increment)
  ✓ Cache-friendly (sequential memory)
  ✗ Must deallocate entire arena
  ✗ Requires size prediction
```

---

## 2. Pool-Based Allocator (HAProxy Style)

### Object Pool Pattern

```
Initialization:
┌─────────────────────────────────────────┐
│   Connection Pool (LIFO Stack)          │
├─────────────────────────────────────────┤
│ [Conn 1] [Conn 2] [Conn 3] [Conn 4]   │
│                                    ▲    │  ← Top of stack
│                                    │    │
│                                    └────│  ← Next free object
└─────────────────────────────────────────┘

Request 1 arrives, take from pool:
┌─────────────────────────────────────────┐
│   Connection Pool (LIFO Stack)          │
├─────────────────────────────────────────┤
│ [Conn 1] [Conn 2] [Conn 3] [USED]      │
│                                    ▲    │
│                                    │    │
│                                    └────│
└─────────────────────────────────────────┘
         [Conn 4 in use by request 1]

Request 1 completes, return to pool:
┌─────────────────────────────────────────┐
│   Connection Pool (LIFO Stack)          │
├─────────────────────────────────────────┤
│ [Conn 1] [Conn 2] [Conn 3] [Conn 4]   │  ← Conn4 warm in CPU cache!
│                                    ▲    │
│                                    │    │
│                                    └────│
└─────────────────────────────────────────┘

Characteristics:
  ✓ LIFO keeps recently-used objects hot
  ✓ No fragmentation (pre-allocated)
  ✓ Fast allocation/deallocation
  ✗ Fixed pool sizes (must predict)
  ✗ Wasted space if actual < predicted
```

---

## 3. Slab Allocator (Nginx Shared Memory)

### Size Class Organization

```
Shared Memory Region (e.g., 64MB for cache)
├─────────────────────────────────────────────────────┐
│                                                     │
│  Size Class 8:                                      │
│  ┌──┬──┬──┬──┬──┬──┬──┬──┬──┬──┐                   │
│  │08│08│08│08│08│08│08│08│08│08│ [used: ■■■□□□]   │
│  └──┴──┴──┴──┴──┴──┴──┴──┴──┴──┘                   │
│                                                     │
│  Size Class 16:                                     │
│  ┌────┬────┬────┬────┬────┐                        │
│  │ 16 │ 16 │ 16 │ 16 │ 16 │ [used: ■■□□]          │
│  └────┴────┴────┴────┴────┘                        │
│                                                     │
│  Size Class 32:                                     │
│  ┌───────┬───────┬───────┬───────┐                 │
│  │  32   │  32   │  32   │  32   │ [used: ■■■□]    │
│  └───────┴───────┴───────┴───────┘                 │
│                                                     │
│  Size Class 64:                                     │
│  ┌──────────┬──────────┬──────────┐                │
│  │    64    │    64    │    64    │ [used: ■■□]    │
│  └──────────┴──────────┴──────────┘                │
│                                                     │
│  ... more size classes ...                          │
│                                                     │
└─────────────────────────────────────────────────────┘

Allocation flow:
  Request 26 bytes
       │
       ▼
  Find size class ≥ 26: Class 32
       │
       ▼
  Find free chunk in Class 32 free list
       │
       ▼
  Return pointer to free chunk
       │
       ▼
  Mark chunk as used

Deallocation:
  Return chunk pointer
       │
       ▼
  Find its size class (32)
       │
       ▼
  Add back to Class 32 free list

Characteristics:
  ✓ Reduces fragmentation (size classes)
  ✓ Good for caching, persistent data
  ✓ No object death = no memory leak
  ✗ Internal fragmentation (26 bytes → 32 allocated)
  ✗ More complex bookkeeping
```

---

## 4. tcmalloc: Thread-Local Caching

### Multi-Tier Architecture

```
┌──────────────────────────────────────────────────────────┐
│                     Thread 1                             │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  ┌──────────────────────────────────┐                   │
│  │ Thread-Local Cache (No Locking)  │                   │
│  ├──────────────────────────────────┤                   │
│  │ Size Class 8:  [obj][obj][obj]  │  ← Fast path     │
│  │ Size Class 16: [obj][obj]       │  ← Just CAS      │
│  │ Size Class 32: [obj][obj][obj][obj]                │
│  │ ... (40+ size classes)          │                   │
│  │                                  │                   │
│  │ When depleted ──────────────────┐│                   │
│  └──────────────────────────────────┘│                   │
│                                       │                   │
│  ┌────────────────────────────────────▼──┐              │
│  │  Central Free List (With Locking)     │              │
│  ├───────────────────────────────────────┤              │
│  │ Size Class 8:  [obj]×50               │              │
│  │ Size Class 16: [obj]×30               │              │
│  │ Size Class 32: [obj]×20               │              │
│  │ ... (shared across all threads)       │              │
│  │                                        │              │
│  │ When depleted ──────────────┐          │              │
│  └────────────────────────────┬┘          │              │
│                               │           │              │
│                  ┌────────────▼──────┐    │              │
│                  │   Page Heap       │    │              │
│                  │ [slab][slab]...   │    │              │
│                  │ (From OS)         │    │              │
│                  └───────────────────┘    │              │
└──────────────────────────────────────────────────────────┘

Thread 2:
┌──────────────────────────────────────────────────────────┐
│  ┌──────────────────────────────────┐                   │
│  │ Thread-Local Cache (No Locking)  │  ← Independent!   │
│  │ Size Class 8:  [obj][obj]       │                   │
│  │ Size Class 16: [obj][obj][obj]  │                   │
│  └──────────────────────────────────┘                   │
│           │                                             │
│           └──→ Central Free List (Shared - Lock here)   │
└──────────────────────────────────────────────────────────┘

Characteristics:
  ✓ Fast path: Thread-local = zero locking
  ✓ Contention only at central free list
  ✓ ~22% faster than jemalloc for small allocs
  ✗ Higher memory overhead in multi-threaded
  ✗ Each thread keeps its own cache
```

---

## 5. jemalloc: Arena-Based Design

### Multiple Independent Arenas

```
Application Start
     │
     ▼
Create 4 Arenas (one per core, typically)

┌──────────────────┐  ┌──────────────────┐
│    Arena 0       │  │    Arena 1       │
├──────────────────┤  ├──────────────────┤
│ Runs:            │  │ Runs:            │
│ ┌──────────────┐ │  │ ┌──────────────┐ │
│ │ [region][r] │ │  │ │ [region][r]  │ │
│ │ [region][r] │ │  │ │ [region][r]  │ │
│ │ [region][r] │ │  │ │ [region][r]  │ │
│ └──────────────┘ │  │ └──────────────┘ │
│                  │  │                  │
│ Size classes:    │  │ Size classes:    │
│ 8,16,32,48,...   │  │ 8,16,32,48,...   │
└──────────────────┘  └──────────────────┘

┌──────────────────┐  ┌──────────────────┐
│    Arena 2       │  │    Arena 3       │
├──────────────────┤  ├──────────────────┤
│ Runs:            │  │ Runs:            │
│ ┌──────────────┐ │  │ ┌──────────────┐ │
│ │ [region][r] │ │  │ │ [region][r]  │ │
│ │ [region][r] │ │  │ │ [region][r]  │ │
│ │ [region][r] │ │  │ │ [region][r]  │ │
│ └──────────────┘ │  │ └──────────────┘ │
└──────────────────┘  └──────────────────┘

Thread Allocation:
  Thread 0 allocates  ──→  Arena 0 (minimal contention)
  Thread 1 allocates  ──→  Arena 1
  Thread 2 allocates  ──→  Arena 2
  Thread 3 allocates  ──→  Arena 3
  Thread 4 allocates  ──→  Arena 0 (wraps)
  ...

Characteristics:
  ✓ Lower memory overhead than tcmalloc
  ✓ Better scaling with many threads (30% better)
  ✓ Excellent fragmentation handling
  ✗ Slightly slower on small allocations than tcmalloc
  ✗ More complex internal structure
```

---

## 6. mimalloc: Free List Sharding

### Per-Page Free Lists with Sharding

```
Physical Page (4KB)
┌──────────────────────────────────────────┐
│                                          │
│  [obj][obj][obj][obj][obj][obj][obj]    │
│   ▲    ▲    ▲    ▲    ▲    ▲    ▲       │
│   │    │    │    │    │    │    │       │
│   └────┴────┴────┴────┴────┴────┘       │
│       Object Pointers                   │
│                                          │
└──────────────────────────────────────────┘
              Page Header:
         Tracks 3 sharded free lists

┌─────────────────────────────────────────┐
│  Shard 0:  [obj] → [obj] → NULL        │  ← Lock-free queue
│  Shard 1:  [obj] → NULL                │     with CAS
│  Shard 2:  [obj] → [obj] → [obj]       │     per shard
└─────────────────────────────────────────┘

Multi-threaded allocation:
  Thread 0 allocates from page
       ↓
  Try Shard 0 (33% chance)  ← Low contention!
       ↓
  If empty, try next shard
       ↓
  If all shards empty, ask central allocator

Thread 1 allocates from same page
       ↓
  Try Shard 1 (different shard, no contention!)
       ↓
  Success without locks

Characteristics:
  ✓ Distributed free lists = reduced contention
  ✓ 5.3× faster than glibc malloc
  ✓ 50% less memory than malloc
  ✓ Near-linear scaling with threads
  ✗ Newer, less battle-tested than jemalloc
```

---

## 7. Nginx Request Processing Memory Layout

### Request Lifecycle in Nginx

```
Request arrives → Parse in Zig

Process Request:

┌─────────────────────────────────────────────┐
│         Request Pool (Arena)                │
├─────────────────────────────────────────────┤
│                                             │
│  ┌──────────────────────────────────────┐  │
│  │ HTTP Request Structure               │  │
│  │  - Method, URI, Version              │  │
│  │  - 32 headers                        │  │
│  │  - Query string                      │  │
│  │  - Cookies                           │  │
│  │  - Path parameters                   │  │
│  │  - Temporary state                   │  │
│  └──────────────────────────────────────┘  │
│                                             │
│  ┌──────────────────────────────────────┐  │
│  │ Module State (all modules)           │  │
│  │  - rewrite module vars               │  │
│  │  - proxy module buffers              │  │
│  │  - auth module state                 │  │
│  │  - ...more modules                   │  │
│  └──────────────────────────────────────┘  │
│                                             │
│  ┌──────────────────────────────────────┐  │
│  │ Response Buffers                     │  │
│  │  - Headers to send                   │  │
│  │  - Body data                         │  │
│  └──────────────────────────────────────┘  │
│                                             │
│         [Free space in pool]                │
│                                             │
└─────────────────────────────────────────────┘

Request Handler:
  Allocates: ngx_palloc(request_pool, size)
  Returns:   pointer into pool
  No need:   to track individual allocations

Request Complete:
  Single operation: ngx_destroy_pool(request_pool)
  Result: All memory freed at once

Characteristics:
  ✓ Very fast allocation (just pointer bump)
  ✓ No fragmentation within request
  ✓ Deterministic cleanup (request lifetime)
  ✓ Excellent cache locality
  ✗ Pool must be sized for max request
  ✗ Can't free individual objects
```

---

## 8. Memory Allocation Latency Comparison

### Allocation Time (microseconds)

```
Bump Allocator
│ ▁
│ ▁▁▁
│ ▁▁▁▁  ← ~0.05-0.1 microseconds
│ ▁▁▁▁▁
└─────────────────────────────────────
  Single pointer increment

Pool Allocator (simple)
│  ▄
│  ▄▄▄
│  ▄▄▄▄  ← ~0.5-1.5 microseconds
│  ▄▄▄▄▄
└─────────────────────────────────────
  List traversal + mark used

tcmalloc (hit thread-local)
│    ▇
│    ▇▇▇  ← ~2-5 microseconds
│    ▇▇▇▇
│    ▇▇▇▇▇
└─────────────────────────────────────
  Atomic operations, cache

jemalloc (hit arena)
│      █
│      ██▄
│      ███▄  ← ~5-10 microseconds
│      ████▄
│      █████▄
└─────────────────────────────────────
  Lock acquisition, bookkeeping

malloc (system)
│            ▓▓▓
│         ▓▓▓▓▓▓  ← ~100+ microseconds
│      ▓▓▓▓▓▓▓▓▓
│   ▓▓▓▓▓▓▓▓▓▓▓▓▓
└─────────────────────────────────────
  Kernel syscall if needed

Relative Performance (100,000 allocations):
  Bump:          0.1s   ███
  Pool:          0.5s   ███████
  tcmalloc:      2.0s   ██████████████████████████
  jemalloc:      3.0s   ████████████████████████████████
  malloc:      100.0s   ████████████████████████████████ (off chart)
```

---

## 9. Memory Fragmentation Over Time

### Long-Running Server Behavior

```
Scenario: 100,000 requests, each 4KB

Arena Allocator (Perfect Reuse):
Memory
│ ████████
│ ████████
│ ████████  ← Constant 4MB used (all reused)
│ ████████
│ ────────
└───────────────────────────────────── time
  Fragmentation: 0%

Pool Allocator (Perfect Reuse):
Memory
│ █████████
│ █████████
│ █████████  ← Constant 5MB (pools + overhead)
│ █████████
│ ─────────
└─────────────────────────────────────time
  Fragmentation: 0% (pre-allocated)

tcmalloc (Good but Imperfect):
Memory
│ █████████
│ █████████▄
│ ██████████▄
│ ██████████▄  ← Grows to 8MB due to fragmentation
│ ───────────
└─────────────────────────────────────time
  Fragmentation: ~15-20%

jemalloc (Excellent):
Memory
│ █████████
│ █████████▄
│ ██████████ ← Stable at 6.5MB
│ ██████████
│ ──────────
└─────────────────────────────────────time
  Fragmentation: ~5-10%

malloc (Worst Case):
Memory
│ ███████████
│ ███████████▄
│ ████████████▄▄
│ █████████████▄▄▄  ← Can reach 50MB+
│ ──────────────────
└─────────────────────────────────────time
  Fragmentation: 50%+
```

---

## 10. Thread Scaling: Allocator Performance

### Throughput vs. Thread Count

```
Throughput (billions allocations/sec)

jemalloc:
  ▲
  │     ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄
  │   ▄▄                  ← Linear scaling!
  │▄▄▄
  └──────────────────────────── threads
     1    4    8   16   32
     Slope: ~1.0 (perfect scaling)

tcmalloc:
  ▲
  │        ▄▄▄▄▄▄▄
  │   ▄▄▄▄▄        ← Scales well to ~16 threads
  │▄▄
  └────────────────────────────threads
     1    4    8   16   32
     Slope: ~0.7 (contention at central allocator)

mimalloc:
  ▲
  │       ▄▄▄▄▄▄▄▄▄▄▄▄▄▄
  │   ▄▄▄
  │▄
  └────────────────────────────threads
     1    4    8   16   32
     Slope: ~1.0 (near-perfect like jemalloc)

malloc:
  ▲
  │          ▁▁▁▁
  │       ▄▄▄
  │   ▄▄▄           ← Hits contention wall early
  │▄▄
  └────────────────────────────threads
     1    4    8   16   32
     Slope: <0.5 (heavy contention)

Key Insight:
  - Allocators with per-thread/per-core design scale linearly
  - Central allocators hit contention wall at 8-16 threads
  - Choose allocator based on expected thread count
```

---

## 11. Decision Tree Flowchart

```
                    SELECT ALLOCATOR
                          │
                ┌─────────┴─────────┐
                │                   │
          How long requests?      Runtime?
                │                   │
        ┌───────┴────────┐    ┌─────┴──────┐
        │                │    │             │
      SHORT         LONG/STREAM  GC?    CUSTOM?
    (< 100ms)       (HTTP/2)    │             │
        │                │    ┌─┴──┐    ┌────┴────┐
        │                │    │    │    │         │
        ▼                ▼   YES  NO  C  CPP/R   PYTHON/JS
    ARENA            tcmalloc  │   │   │   │       │
      │                 │      ▼   ▼   ▼   ▼       ▼
      │                 │      GC  JEM V8  pymalloc
      │         ┌───────┴──┐     │   │  │
      │    MANY THREADS?   ONE │   │  │
      │         │              ▼   ▼  ▼
      │       ┌─┴──┐      auto OK OK
      │       │    │
      │      YES  NO
      │       │    │
      ▼       ▼    ▼
   ┌─────┬──────┬──────┐
   │     │      │      │
  BUMP MIME JEMALLOC SIMPLE
  FAST POOL  POOL
```

---

## 12. Architecture Decision Matrix

### Recommended Allocator by Use Case

```
                Small     Medium    High      Very
              Footprint  Traffic  Traffic   High
              Server     Server   Server    Traffic

Request-based:
Apache  ────────●────────────────────────────
h2o     ────────●────────────────────────────
Nginx   ────────●────────────●───────────────

Connection-based:
Envoy   ────────────────────●───────────●───
HAProxy ─────●────────────────────────────
Node.js ────────────────────●───────────●───

Concurrent
(Async):
Rust    ────────────────────●───────────●───
  Axum  ───────────────────────●────────●──
  Actix ────────────────────●────────────●──

Special:
Limited───────●────────────────────────────
Memory

Ultra-────────────────────────────────────●─
 Fast

Legend:
  ● = Good choice
  ○ = Acceptable
  ─ = Avoid
```

---

**Last updated: 2026-02-28**

These visual guides complement the detailed research documents. Each diagram shows:
- Memory layout at each stage
- Data flow and allocation patterns
- Latency/performance characteristics
- Scaling behavior with concurrency
- Fragmentation patterns over time
