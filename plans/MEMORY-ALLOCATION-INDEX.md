# Memory Allocation Strategies - Complete Research Index

Comprehensive research compilation on memory allocation strategies in high-performance HTTP servers.

**Research Completed**: 2026-02-28
**Scope**: Strategy categories, HTTP server implementations, design tradeoffs
**Total Documents**: 4 comprehensive guides + this index

---

## Document Guide

### 1. **memory-allocation-research.md** (901 lines)

   **Purpose**: Deep, comprehensive research on all allocation strategies and implementations

   **Contents**:
   - Strategy Categories (8 types):
     - Pre-allocation & memory pools
     - Arena/Bump allocators
     - Slab allocators
     - Jemalloc-style allocators
     - tcmalloc allocators
     - Lock-free allocators
     - Custom allocators
     - Stack vs. heap allocation

   - Known HTTP Servers (8 detailed case studies):
     - Nginx (C, custom tiered allocator)
     - Envoy Proxy (C++, tcmalloc)
     - HAProxy (C, lightweight pools)
     - Node.js (JavaScript, V8 GC)
     - Rust ecosystem (Axum, Actix-web, Hyper, Tokio)
     - Apache HTTP Server (C, arena pools)
     - Lighttpd (C, minimal overhead)
     - h2o (C, region-based)

   - Key Questions answered:
     - What allocation strategy does each use?
     - What are the memory access patterns?
     - How do they handle concurrent allocations?
     - What about fragmentation concerns?
     - Memory efficiency vs. speed tradeoffs?

   - Implementation patterns and best practices
   - Complete source documentation with links

   **Best for**: Deep understanding, architectural decisions, historical context

---

### 2. **memory-allocation-quick-reference.md** (584 lines)

   **Purpose**: Practical implementation guide with code examples

   **Contents**:
   - Quick decision tree (flowchart format)
   - Allocator comparison matrix
   - 7 detailed strategy profiles with implementation checklists
   - 3 concrete code recipes:
     - Rust: Request-lifetime arena
     - C: Connection pool pattern
     - Rust: Multi-threaded pools with thread-local storage

   - Tuning parameters for jemalloc, tcmalloc, mimalloc
   - Monitoring & debugging strategies
   - Common pitfalls and solutions table
   - Benchmarking template
   - Specific recommendations for the-framework project

   **Best for**: Practical implementation, code examples, getting started

---

### 3. **memory-allocation-visual-guide.md** (400+ lines)

   **Purpose**: Visual representations of allocation strategies

   **Contents**:
   - 12 detailed diagrams showing:
     - Arena/Bump allocation memory layout
     - Pool-based allocator (LIFO pattern)
     - Slab allocator (size classes)
     - tcmalloc (thread-local caching)
     - jemalloc (arena-based design)
     - mimalloc (free list sharding)
     - Nginx request processing
     - Memory allocation latency comparison
     - Fragmentation over time patterns
     - Thread scaling behavior
     - Decision tree flowchart
     - Architecture recommendation matrix

   **Best for**: Visual learners, presentations, quick understanding

---

### 4. **This Index File**

   **Purpose**: Navigation and quick reference

   **Contents**:
   - Document overview and guide
   - Quick lookup tables
   - Strategy-to-server mapping
   - Selection criteria by use case
   - Key metrics and definitions
   - Links to all resources

---

## Quick Lookup: Strategy Categories

| Strategy | Best For | Speed | Memory | Complexity | Examples |
|----------|----------|-------|--------|-----------|----------|
| **Arena/Bump** | Request-lifetime | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐ | Apache, h2o, Nginx (small) |
| **Pool-per-Type** | Known objects | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐ | HAProxy, Connection pools |
| **Slab** | Fixed-size caching | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐ | Linux kernel, Nginx zones |
| **jemalloc** | General purpose | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | Redis, Facebook services |
| **tcmalloc** | Small allocs | ⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐ | Envoy, Google services |
| **mimalloc** | High concurrency | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | Modern Rust projects |
| **Lock-Free** | Real-time | ⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | Specialized systems |
| **GC (V8/JVM)** | Ease of use | ⭐⭐ | ⭐⭐⭐ | ⭐ | Node.js, Java servers |

---

## Server-to-Strategy Mapping

### By Programming Language

**C (Low-level control)**:
- Nginx: Custom tiered allocator (pools + malloc)
- HAProxy: Pool-per-type (LIFO)
- Lighttpd: Lightweight pools
- h2o: Region-based pools
- Apache: Arena allocators (APR)

**C++ (Flexibility + Performance)**:
- Envoy: tcmalloc (linked statically)

**Rust (Memory Safety)**:
- Axum: Allocator-agnostic (jemalloc recommended)
- Actix-web: Allocator-agnostic (jemalloc/mimalloc)
- Hyper: System allocator (app chooses)

**JavaScript (Automatic Management)**:
- Node.js: V8 garbage collection (Orinoco pipeline)

---

### By Request Lifetime

**Short-lived (< 1 second)**:
- Arena/Bump allocators (fastest, best memory)
- Pool-based allocators (simpler)
- Examples: Apache, h2o, traditional HTTP servers

**Long-lived (HTTP/2 streams)**:
- tcmalloc or jemalloc (handle mixed lifetimes)
- Connection-level state allocation
- Examples: Envoy, modern HTTP/2 servers

**Very Long-lived (Node.js, JVM)**:
- Garbage collection (automatic)
- Generational hypothesis (most objects die young)
- Examples: Node.js, Java servers

---

## Performance Profile Summary

### Allocation Speed (Fast → Slow)
1. **Bump allocators** (~0.05-0.1 μs)
2. **Pool allocators** (~0.5-1.5 μs)
3. **tcmalloc** (~2-5 μs, thread-local hit)
4. **jemalloc** (~5-10 μs, arena hit)
5. **System malloc** (~100+ μs, with syscalls)

### Memory Efficiency (Low overhead → High)
1. **Arena allocators** (0% internal fragmentation)
2. **jemalloc** (5-10% overhead)
3. **Slab allocators** (10-15% from size classes)
4. **tcmalloc** (20-30% overhead)
5. **System malloc** (50%+ in worst case)

### Scalability with Threads (Linear → Sublinear)
1. **mimalloc** (1.0x slope, near-perfect)
2. **jemalloc** (0.95x slope, excellent)
3. **tcmalloc** (0.7x slope, decent to 16 cores)
4. **System malloc** (<0.5x slope, hits wall early)

---

## Decision Criteria Reference

### Choose ARENA/BUMP if:
- [ ] Requests have clear lifetime boundary
- [ ] Can pre-calculate max request memory
- [ ] Building HTTP server or similar request-based system
- [ ] Throughput and latency critical
- [ ] Per-request memory predictable

### Choose POOL-BASED if:
- [ ] Known fixed object types
- [ ] Clear allocation/deallocation patterns
- [ ] Simplicity important
- [ ] Embedded/resource-constrained system
- [ ] Prefer deterministic behavior

### Choose JEMALLOC if:
- [ ] General-purpose C/C++ application
- [ ] Multi-threaded (many cores)
- [ ] Long-running process
- [ ] Memory efficiency important
- [ ] Can handle 5-10 μs allocation latency

### Choose TCMALLOC if:
- [ ] Throughput absolutely critical
- [ ] Lots of small allocations
- [ ] Can accept higher memory overhead
- [ ] Built-in profiling needed
- [ ] Latency variance acceptable

### Choose MIMALLOC if:
- [ ] Rust application (via crate)
- [ ] Very high concurrency (32+ threads)
- [ ] Latest allocator technology acceptable
- [ ] 5.3× performance over malloc important
- [ ] 50% memory savings valuable

### Choose GC (V8/JVM) if:
- [ ] Using JavaScript or Java
- [ ] Language runtime provides it
- [ ] Ease of memory management critical
- [ ] Pause times acceptable (tunable)
- [ ] Not implementing low-level server

---

## Key Metrics to Monitor

| Metric | What It Measures | Acceptable Range | Action If High |
|--------|-----------------|-----------------|-----------------|
| **RSS (Resident Set)** | Physical memory used | Stable over time | Fragmentation? Switch allocator |
| **VSZ (Virtual)** | Total address space | 1.5-2x RSS | Normal allocator overhead |
| **Allocation Rate** | Allocs per second | Workload dependent | Reduce if over-allocating |
| **Fragmentation Ratio** | Free/Total memory | < 15% | Normal with pools |
| **GC Pause Time** | Stop-the-world duration | < 50ms for P99 | Tune GC, increase heap |
| **Deallocation Rate** | Frees per second | Matches allocs | Missing deallocations = leak |
| **Peak Memory** | Max usage observed | Predictable | Good sign |
| **Sustained Memory** | After load gone | Should drop | Allocator caching (normal) |

---

## Common Patterns in High-Performance Servers

### Pattern 1: Request-Lifetime Allocation
Used by: Apache, h2o, Nginx (partially)
```
allocate_arena() → allocate_from_arena() × N → free_arena()
Characteristics: O(1) deallocation, deterministic, fragmentation-free
```

### Pattern 2: Connection + Stream Allocation
Used by: Envoy, modern HTTP/2 servers
```
allocate_connection() → allocate_stream() × N → free_stream() → free_connection()
Characteristics: Nested lifetimes, mixed allocations, needs smart allocator
```

### Pattern 3: Thread-Local Pools
Used by: Tokio, Actix-web, HAProxy
```
thread_local_pool.get() → use → thread_local_pool.return()
Characteristics: Zero contention, pre-warmed cache, per-thread memory
```

### Pattern 4: Generational GC
Used by: Node.js, JVM-based servers
```
allocate(young_space) → [promote to old_space] → GC cycle → cleanup
Characteristics: Automatic, pause times tunable, memory fragmented by design
```

---

## Recommendations by Scenario

### Scenario: Building HTTP/1.1 Server
- **Allocator**: Arena per request
- **Approach**: Pre-allocate single large block per request
- **Deallocation**: Bulk deallocation on request completion
- **Expected performance**: 1-10 microseconds per allocation
- **Memory efficiency**: Near-zero fragmentation

### Scenario: Building HTTP/2 Proxy
- **Allocator**: jemalloc or tcmalloc
- **Approach**: Connection-level allocation, stream allocation
- **Deallocation**: Individual stream cleanup, bulk connection cleanup
- **Expected performance**: 2-10 microseconds per allocation
- **Memory efficiency**: 5-30% overhead

### Scenario: Building Async Rust Framework
- **Allocator**: mimalloc (system allocator or via crate)
- **Approach**: Tokio runtime + async/await, allocator delegates
- **Deallocation**: Automatic via Rust ownership
- **Expected performance**: 0-5 microseconds (cached), 100+ if uncached
- **Memory efficiency**: 10-20% overhead

### Scenario: Building Node.js HTTP Server
- **Allocator**: V8 GC (generational)
- **Approach**: Write synchronous code, V8 handles memory
- **Deallocation**: GC cycles (Young: frequent, Old: infrequent)
- **Expected performance**: Depends on GC tuning
- **Memory efficiency**: 20-40% overhead from GC

### Scenario: Resource-Constrained Embedded HTTP Server
- **Allocator**: Simple pools or arena
- **Approach**: Minimal allocations, fixed-size buffers
- **Deallocation**: Bulk or implicit reuse
- **Expected performance**: Prioritize stability over speed
- **Memory efficiency**: Minimize absolute memory usage

---

## Implementation Checklist

### For Arena Allocator
- [ ] Measure max request size (all headers + body + scratch)
- [ ] Add 50% margin for safety
- [ ] Allocate single block per request on arrival
- [ ] Use single pointer for bump allocation
- [ ] Deallocate entire block on request completion
- [ ] Handle OOM gracefully (arena full)
- [ ] Monitor actual vs. allocated (tuning)

### For Pool-Based Allocator
- [ ] Define all object types
- [ ] Calculate initial pool size per type
- [ ] Implement LIFO stack ordering (for cache)
- [ ] Add overflow handling (expand or block)
- [ ] Implement cleanup (validate no stray pointers)
- [ ] Monitor pool utilization
- [ ] Test under peak load

### For jemalloc Integration
- [ ] Link statically (`LD_PRELOAD` or link flag)
- [ ] Configure arena count (default: # cores)
- [ ] Set decay parameters for your workload
- [ ] Enable profiling in development
- [ ] Monitor RSS over time
- [ ] Test fragmentation under sustained load

### For tcmalloc Integration
- [ ] Link with `-ltcmalloc`
- [ ] Set thread cache size appropriately
- [ ] Configure `TCMALLOC_MAX_FREE_KB`
- [ ] Enable heap profiling for analysis
- [ ] Monitor for memory bloat
- [ ] Compare with jemalloc if multi-threaded

### For mimalloc Integration (Rust)
- [ ] Add to `Cargo.toml`: `mimalloc = "0.1"`
- [ ] Enable globally: `#[global_allocator] static GLOBAL: mimalloc::MiMalloc = ...`
- [ ] Enable stats: `MIMALLOC_SHOW_STATS=1`
- [ ] Benchmark allocation rate
- [ ] Test under high concurrency

---

## Resource Links by Topic

### Academic & Research
- Bonwick's Slab Allocator paper (seminal work)
- Michael's Lock-Free Allocator papers
- Microsoft Mimalloc research paper
- TCMalloc design documentation

### Allocator Projects
- [jemalloc GitHub](https://github.com/jemalloc/jemalloc)
- [tcmalloc at Google](https://github.com/google/tcmalloc)
- [mimalloc at Microsoft](https://github.com/microsoft/mimalloc)

### Server Source Code
- [Nginx](https://github.com/nginx/nginx) - See `src/core/ngx_palloc.c`
- [Envoy](https://github.com/envoyproxy/envoy) - tcmalloc integration
- [h2o](https://github.com/h2o/h2o) - Region-based allocation
- [HAProxy](http://www.haproxy.org/) - Pool-based design
- [Apache httpd](https://github.com/apache/httpd) - APR memory pools

### Tools
- pprof (tcmalloc, jemalloc profiling)
- Valgrind Massif (generic heap profiling)
- perf (Linux kernel profiling)
- Instruments (macOS profiling)

---

## Common Mistakes to Avoid

1. **Choosing allocator before profiling**
   - Measure actual allocation patterns first
   - Don't assume overhead without data

2. **Oversizing pools**
   - Wastes memory if actual usage << predicted
   - Monitor utilization and tune down

3. **Undersizing pools**
   - Causes allocation failures under load
   - Test to peak load before deployment

4. **Ignoring allocator decay settings**
   - Can cause memory to not return to OS
   - Tune `dirty_decay_ms`, `TCMALLOC_MAX_FREE_KB`

5. **Not considering access patterns**
   - Arena allocator great for request-lifetime
   - Wrong choice for mixed-lifetime allocation

6. **Assuming more cores = better with wrong allocator**
   - System malloc degrades with cores
   - jemalloc/mimalloc scale much better

7. **Not accounting for thread-local overhead**
   - Each thread keeps its own caches
   - High thread count = high memory overhead

8. **Ignoring fragmentation in long-running processes**
   - Fragmentation accumulates over weeks/months
   - Monitor fragmentation metrics

---

## Related Documentation

- **CLAUDE.md**: Project overview and tooling
- **implementation.md**: Overall framework implementation plan
- **missing.md**: Known gaps and TODOs
- **headers.md**: Header parsing architecture

---

## Next Steps for the-framework

Based on this research:

1. **Choose allocation strategy**:
   - Recommended: Request-lifetime arena in Zig
   - Alternative: Hybrid arena + pools

2. **Design arena sizing**:
   - Measure typical request size
   - Add margin for worst-case
   - Implement overflow handling

3. **Implement header parsing in arena**:
   - Parse headers once in Zig (not twice)
   - Keep headers in arena lifetime
   - Pass to Python as completed structure

4. **Test fragmentation**:
   - Monitor memory growth over sustained load
   - Validate arena reuse across requests

5. **Profile allocation hotspots**:
   - Identify most frequent allocations
   - Consider specialized pools if needed

6. **Document memory guarantees**:
   - Max memory per request
   - Max concurrent requests
   - Peak memory under load

---

**Research completed by**: Claude Code AI
**Date**: 2026-02-28
**Status**: Comprehensive research available in 4 documents + this index
**Recommended reading order**:
1. This index (navigation)
2. Visual guide (10 minutes, understand patterns)
3. Quick reference (30 minutes, implementation)
4. Full research (2+ hours, deep understanding)
