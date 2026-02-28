# Memory Allocation Strategies - Quick Reference & Implementation Guide

Practical quick-lookup guide for choosing and implementing memory allocation strategies in high-performance HTTP servers.

---

## Quick Decision Tree

```
START: Building an HTTP Server?
  │
  ├─→ How long do connections live?
  │     │
  │     ├─→ Very short (< 1 second), request-based
  │     │   └─→ USE: Arena/Bump Allocators (Apache model)
  │     │       └─→ SEE: Apache APR, h2o, Nginx pools
  │     │
  │     └─→ Long (connections persist), stream-based (HTTP/2)
  │         └─→ USE: tcmalloc or jemalloc
  │             └─→ SEE: Envoy, Node.js pattern
  │
  ├─→ How many threads/cores?
  │     │
  │     ├─→ Single-threaded or few threads
  │     │   └─→ USE: Simple pool-per-type (HAProxy style)
  │     │
  │     └─→ Many threads (> 8 cores)
  │         └─→ USE: Lock-free or tcmalloc/jemalloc
  │             └─→ Consider: mimalloc for best scaling
  │
  ├─→ Language/Platform?
  │     │
  │     ├─→ C/C++ with custom control
  │     │   └─→ USE: tcmalloc, jemalloc, or custom
  │     │
  │     ├─→ Rust
  │     │   └─→ USE: jemalloc or mimalloc allocator crate
  │     │
  │     ├─→ Python
  │     │   └─→ USE: Whatever Zig uses (if Zig backend)
  │     │
  │     └─→ Node.js / JVM
  │         └─→ USE: Default GC (V8, Orinoco, ZGC)
  │
  └─→ Memory constraints?
        │
        ├─→ Ultra-low memory
        │   └─→ USE: Lighttpd/HAProxy approach + custom tuning
        │
        └─→ Normal production
            └─→ USE: jemalloc as default safe choice
```

---

## Allocator Comparison Matrix

```
┌─────────────────┬──────────┬──────────┬─────────┬─────────┐
│ Allocator       │ Speed    │ Memory   │ Scaling │ Maturity│
├─────────────────┼──────────┼──────────┼─────────┼─────────┤
│ Bump            │ ★★★★★   │ ★★★★★   │ N/A*    │ ★★★     │
│ HAProxy pools   │ ★★★★    │ ★★★★    │ ★★      │ ★★★★    │
│ jemalloc        │ ★★★     │ ★★★★★   │ ★★★★★  │ ★★★★★   │
│ tcmalloc        │ ★★★★    │ ★★★     │ ★★★★   │ ★★★★★   │
│ mimalloc        │ ★★★★    │ ★★★★★   │ ★★★★★  │ ★★★★    │
│ Lock-free       │ ★★★     │ ★★★★    │ ★★★★★  │ ★★      │
│ Slab            │ ★★★★    │ ★★★★    │ ★★★    │ ★★★★    │
│ GC (V8/JVM)     │ ★★      │ ★★★     │ ★★★★   │ ★★★★★   │
└─────────────────┴──────────┴──────────┴─────────┴─────────┘
* Bump allocators don't scale independently; paired with threading model
```

---

## Strategy Profiles

### 1. Arena/Bump Allocator

**When to use**:
- Request-based HTTP servers
- Clear request lifetime boundaries
- Minimal inter-request state
- Predictable memory per request

**Implementation checklist**:
- [ ] Calculate max request memory (headers + body + processing)
- [ ] Allocate arena upfront per request
- [ ] Use single pointer for bumping allocations
- [ ] Deallocate entire arena on request completion
- [ ] Monitor for allocation failures (arena too small)

**Memory profile**: O(max_request_size) per concurrent request

**Examples**: Apache APR, h2o, Nginx small allocations

**Trade-off**: Wasted space if actual request size << max, but huge speed gain

---

### 2. Pool-per-Type Allocator

**When to use**:
- Known object types with predictable sizes
- Connection-based servers
- Reuse patterns clear
- Simplicity important

**Implementation checklist**:
- [ ] Define object types (connection, stream, buffer, etc.)
- [ ] Create fixed-size pool for each type
- [ ] Initialize pools at startup
- [ ] Allocate object → take from pool
- [ ] Deallocate object → return to pool
- [ ] Optionally merge similar-sized pools to reduce fragmentation

**Memory profile**: O(total_objects_per_type) allocated upfront

**Examples**: HAProxy, early HTTP/1.1 servers

**Trade-off**: Inflexible but predictable; no GC needed

---

### 3. tcmalloc (Thread-Caching Malloc)

**When to use**:
- C/C++ applications
- High throughput with small allocations
- Multi-threaded workloads
- Built-in profiling needed

**Integration**:
```bash
# Link with tcmalloc
gcc -o server server.c -ltcmalloc

# Or if using Bazel/CMake, add dependency
```

**Profiling**:
```cpp
// Enable profiling
HEAPPROFILE=/tmp/heap.prof ./server

// View results
pprof /tmp/heap.prof
```

**Tuning**:
```bash
# Environment variables
export TCMALLOC_SAMPLE_PARAMETER=524288  # Sample rate
export TCMALLOC_MAX_FREE_KB=1048576      # Max free per thread
```

**Trade-off**: Fast small allocations but higher memory overhead than jemalloc

---

### 4. jemalloc

**When to use**:
- C/C++ or Rust applications
- Long-running servers
- Diverse allocation patterns
- Multi-threaded with many threads

**Integration (Rust)**:
```toml
[dependencies]
jemallocator = "0.5"
```

```rust
#[global_allocator]
static GLOBAL: jemallocator::Jemalloc = jemallocator::Jemalloc;
```

**Integration (C/C++)**:
```bash
# Link with jemalloc
gcc -o server server.c -ljemalloc
```

**Profiling**:
```bash
# Build with profiling
./configure --enable-prof

# Run with profiling
MALLOC_CONF="prof:true" ./server

# Analyze
jeprof ./server jeprof.*.heap
```

**Tuning**:
```bash
# Number of arenas (default = # cores)
export MALLOC_CONF="narenas:4"

# Dirty page decay milliseconds
export MALLOC_CONF="dirty_decay_ms:0"  # Disable decay for low-latency
```

**Trade-off**: Better memory efficiency than tcmalloc, slightly slower on small allocations

---

### 5. mimalloc

**When to use**:
- Rust applications
- Very high concurrency (> 16 threads)
- Memory-constrained environments
- Benchmarking shows best memory/speed balance

**Integration (Rust)**:
```toml
[dependencies]
mimalloc = "0.1"
```

```rust
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;
```

**Integration (C/C++)**:
```bash
# Build mimalloc
git clone https://github.com/microsoft/mimalloc
cd mimalloc && mkdir -p build && cd build
cmake .. && make && sudo make install

# Link
gcc -o server server.c -lmimalloc
```

**Profiling**:
```bash
# Enable stats
export MIMALLOC_SHOW_STATS=1
export MIMALLOC_SHOW_ERRORS=1

./server
```

**Trade-off**: Best memory scaling, excellent for high concurrency, newer (less battle-tested than jemalloc)

---

## Implementation Recipes

### Recipe 1: Simple Request-Lifetime Arena (Zig)

```zig
const RequestArena = struct {
    allocator: std.mem.Allocator,
    buffer: []u8,
    used: usize,

    fn init(size: usize) !RequestArena {
        const buffer = try allocator.alloc(u8, size);
        return RequestArena{
            .allocator = allocator,
            .buffer = buffer,
            .used = 0,
        };
    }

    fn allocate(self: *RequestArena, size: usize) ![]u8 {
        if (self.used + size > self.buffer.len) {
            return error.ArenaFull;
        }
        const result = self.buffer[self.used..self.used+size];
        self.used += size;
        return result;
    }

    fn deinit(self: *RequestArena) void {
        self.allocator.free(self.buffer);
    }
};

// Usage in request handler
fn handleRequest(request: *HttpRequest) !void {
    var arena = try RequestArena.init(4096);
    defer arena.deinit();

    // All allocations go to arena
    const headers = try arena.allocate(max_headers_size);
    const body = try arena.allocate(max_body_size);

    // Process request...

    // Automatic cleanup on function exit
}
```

---

### Recipe 2: Connection Pool (C)

```c
#define POOL_SIZE 1024
#define CONNECTION_POOL_SIZE 65536

typedef struct {
    int fd;
    struct sockaddr_in addr;
    char buffer[CONNECTION_POOL_SIZE];
    size_t buffer_used;
} Connection;

typedef struct {
    Connection* objects;
    bool* available;
    size_t size;
    size_t count;
} ConnectionPool;

ConnectionPool* pool_create(size_t size) {
    ConnectionPool* pool = malloc(sizeof(ConnectionPool));
    pool->objects = malloc(sizeof(Connection) * size);
    pool->available = malloc(sizeof(bool) * size);
    pool->size = size;
    pool->count = 0;

    for (size_t i = 0; i < size; i++) {
        pool->available[i] = true;
    }

    return pool;
}

Connection* pool_acquire(ConnectionPool* pool) {
    for (size_t i = 0; i < pool->size; i++) {
        if (pool->available[i]) {
            pool->available[i] = false;
            pool->count++;
            return &pool->objects[i];
        }
    }
    return NULL; // Pool exhausted
}

void pool_release(ConnectionPool* pool, Connection* conn) {
    for (size_t i = 0; i < pool->size; i++) {
        if (&pool->objects[i] == conn) {
            pool->available[i] = true;
            pool->count--;
            break;
        }
    }
}
```

---

### Recipe 3: Multi-threaded Allocator with Thread-Local Pools

```rust
use std::sync::Arc;
use parking_lot::Mutex;

/// Per-thread pool
thread_local! {
    static BUFFER_POOL: BufferPool = BufferPool::new(1024);
}

struct BufferPool {
    available: Vec<Vec<u8>>,
}

impl BufferPool {
    fn new(capacity: usize) -> Self {
        let mut available = Vec::with_capacity(32);
        for _ in 0..32 {
            available.push(vec![0u8; capacity]);
        }
        BufferPool { available }
    }

    fn get_buffer(&mut self, size: usize) -> Vec<u8> {
        if let Some(mut buf) = self.available.pop() {
            if buf.capacity() >= size {
                buf.clear();
                return buf;
            }
        }
        vec![0u8; size]
    }

    fn return_buffer(&mut self, buf: Vec<u8>) {
        if self.available.len() < 32 {
            self.available.push(buf);
        }
    }
}

// Usage
fn process_request() {
    BUFFER_POOL.with(|pool| {
        let mut pool = pool.borrow_mut();
        let mut buffer = pool.get_buffer(4096);

        // Use buffer...

        pool.return_buffer(buffer);
    });
}
```

---

## Tuning Parameters by Allocator

### jemalloc

| Parameter | Default | Recommended | Effect |
|-----------|---------|-------------|--------|
| `narenas` | # cores | Same as cores | Number of memory arenas |
| `dirty_decay_ms` | 10000 | 0 (low-latency) | How aggressively to decay dirty pages |
| `muzzy_decay_ms` | 10000 | Same | Decay for muzzy (partially-dirty) pages |
| `lg_extent_max_active_fit` | 6 | 6 | Extent-based allocation threshold |

### tcmalloc

| Parameter | Default | Recommended | Effect |
|-----------|---------|-------------|--------|
| `TCMALLOC_SAMPLE_PARAMETER` | 524288 | Same | Sampling interval for profiling |
| `TCMALLOC_MAX_FREE_KB` | 512MB | Per-workload | Max free bytes per thread |
| `TCMALLOC_HEAP_LIMIT_MB` | Unlimited | Set per-process | Hard limit on heap |

### mimalloc

| Parameter | Default | Recommended | Effect |
|-----------|---------|-------------|--------|
| `MIMALLOC_SHOW_STATS` | 0 | 1 (dev) | Enable statistics output |
| `MIMALLOC_EAGER_COMMIT_DELAY` | 0 | 1 (NUMA) | Delay commit for NUMA |
| `MIMALLOC_ALLOW_DECOMMIT` | 1 | 1 | Allow decommit for memory pressure |

---

## Monitoring & Debugging

### Memory Profiling Strategies

**For tcmalloc**:
```bash
# Snapshot profiling
./server > heap.prof 2>&1
pprof server heap.prof

# Continuous profiling
env HEAPPROFILE=/tmp/heap.prof ./server
```

**For jemalloc**:
```bash
# Enable profiling
env MALLOC_CONF="prof:true" ./server

# Analyze
jeprof ./server jeprof.*.heap --text | head -20
```

**For Rust (using valgrind)**:
```bash
# Massif tool for heap profiling
valgrind --tool=massif ./target/release/server
```

### Key Metrics to Monitor

1. **Resident Set Size (RSS)**
   - Actual physical memory used
   - Watch for growth over time (fragmentation indicator)

2. **Virtual Memory (VSZ)**
   - Total address space reserved
   - Allocator overhead reflected here

3. **Allocation Rate**
   - Allocations per second
   - Indicates pressure on allocator

4. **Fragment Ratio**
   - Free memory / total memory
   - Fragmentation = 1 - (live / free)

5. **GC Pause Time** (for GC-based languages)
   - Time spent in garbage collection
   - Critical for latency-sensitive apps

---

## Common Pitfalls & Solutions

| Pitfall | Symptom | Solution |
|---------|---------|----------|
| **Arena too small** | Allocation failures | Pre-measure max request size, add margin (1.5x) |
| **Thread pool contention** | High lock contention | Use lock-free allocator or more arenas |
| **Fragmentation over time** | Growing RSS despite stable load | Switch to jemalloc, tune decay parameters |
| **Pool starvation** | Requests rejected when buffers full | Increase pool size, monitor actual usage |
| **Memory not released to OS** | Allocator caching | tcmalloc/jemalloc normal; consider `MALLOC_TRIM_THRESHOLD` |
| **GC pause spikes** | Latency jitter in Node/JVM | Tune GC: disable compaction for throughput, enable for latency |
| **False sharing** | Lock contention on cache lines | Use per-core arenas, lock-free primitives |

---

## Benchmarking Template

```bash
#!/bin/bash
# Compare allocators on same workload

SERVERS=("malloc" "jemalloc" "tcmalloc" "mimalloc")
RESULTS="benchmark_results.txt"

for allocator in "${SERVERS[@]}"; do
    echo "=== Testing $allocator ===" | tee -a $RESULTS

    # Build with allocator
    make clean && make ALLOCATOR=$allocator

    # Warmup
    ./server &
    PID=$!
    sleep 2

    # Measure
    ab -n 100000 -c 100 http://localhost:8080/

    # Memory snapshot
    ps aux | grep server | grep -v grep >> $RESULTS

    kill $PID
    sleep 1
done
```

---

## Decision Matrix for the-framework

Given the project context (Python + Zig, greenlets, io_uring, pre-fork):

| Component | Recommended | Rationale |
|-----------|------------|-----------|
| **HTTP Server Core (Zig)** | Arena allocator | Request-lifetime allocation; fast; matches HTTP semantics |
| **Request Parser (Zig)** | Arena sub-pool | Headers parsed once, stay in arena until request complete |
| **Buffer Management** | Slab allocator | Fixed-size header/body buffers; prevent fragmentation |
| **Worker Process Startup** | Pre-allocated pools | Pre-fork: each worker pre-allocates buffers on startup |
| **Greenlet Stacks** | Preallocated | Fixed-size stacks per greenlet, allocated upfront |
| **Python Layer** | Default (PyMalloc) | Python's GC handles this; focus Zig optimization |
| **Profiling** | jemalloc | If profiling needed, use jemalloc in production compile |

---

## Further Learning

**Papers & Articles**:
- "Arena Allocators" - Various medium.com posts
- "Slab Allocation" - Jeff Bonwick's original paper
- "Mimalloc: Free List Sharding" - Microsoft Research

**Source Code**:
- nginx: `src/core/ngx_palloc.c`
- Apache APR: `memory/` directory
- jemalloc: `src/` directory on GitHub
- mimalloc: Microsoft/mimalloc repository

**Profiling Tools**:
- pprof (tcmalloc, jemalloc)
- Valgrind Massif (generic)
- perf (Linux kernel)
- instruments (macOS)

---

**Last updated: 2026-02-28**
