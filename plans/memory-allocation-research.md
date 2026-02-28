# Memory Allocation Strategies in High-Performance HTTP Servers

Comprehensive research on allocation strategies, implementations, and tradeoffs used by leading HTTP servers and frameworks.

---

## 1. ALLOCATION STRATEGY CATEGORIES

### 1.1 Pre-allocation & Memory Pools

**Overview**: Pre-allocate memory in fixed-size blocks and reuse them throughout the server lifetime.

**Characteristics**:
- Memory blocks allocated upfront before handling requests
- Objects returned to pool after use for reuse
- Reduces fragmentation by reusing recently-freed memory

**Advantages**:
- Deterministic allocation patterns
- CPU cache efficiency (recently freed blocks still warm)
- Prevents external fragmentation in many cases

**Disadvantages**:
- Inflexible pool sizes (must be tuned per workload)
- Potential memory waste if actual allocations differ from predictions
- Additional bookkeeping overhead

**Real-world Usage**:
- **HAProxy**: Uses LIFO pool-based memory management where each released object goes back to its pool of origin, and similar-sized pools are merged to limit fragmentation. Objects are never freed since they're reused frequently.
- **Memcached**: Uses slab allocation with 1MB blocks separated into identical-sized chunks per slab

---

### 1.2 Arena/Bump Allocators

**Overview**: Allocate memory from pre-allocated arena, only advancing a pointer (bump). Entire arena freed when request completes.

**Characteristics**:
- Linear pointer bumping for allocation
- No fragmentation within arena
- Must free entire arena at once (request-lifetime bound)
- Zero allocation overhead per object

**Advantages**:
- Extremely fast (single pointer increment)
- Zero fragmentation within the arena
- Deterministic performance
- Excellent cache locality

**Disadvantages**:
- Requires bulk deallocation (request-lifetime bound)
- Difficult to handle objects with dynamic lifetimes
- Memory may be underutilized if requests vary in size

**Historical Significance**:
Arena allocators were instrumental in early C-based HTTP servers:
- **Apache HTTP Server**: "Pools" are explicit arena allocators, fundamental to APR (Apache Portable Runtime)
- **PostgreSQL**: Uses "memory contexts" based on arena concepts

**Research Reference**: Hanson's early arena allocator research demonstrated time performance per allocated byte superior to fastest-known heap allocation mechanisms at the time.

---

### 1.3 Slab Allocators

**Overview**: Divide fixed-size regions into slabs containing fixed-size chunks. Each chunk tracks free/allocated status.

**Characteristics**:
- Solves object-caching problem in kernel memory management
- Originally introduced in Solaris 2.4 by Jeff Bonwick
- Each slab holds identical-sized chunks for a specific type/size
- Keeps track of free and allocated chunks within slabs

**Allocation Flow**:
1. Allocator receives allocation request
2. Usually satisfied from free chunk in existing slab
3. If no free chunks, new slab created
4. On deallocation, chunk added to slab's free list

**Advantages**:
- Reduces fragmentation for similar-sized allocations
- Excellent for multi-type object caching
- Deterministic allocation patterns within size class

**Disadvantages**:
- Requires size-class clustering
- Overhead for small objects due to slab metadata
- More complex bookkeeping than simple pools

**Real-world Usage**:
- **Linux kernel**: Primary memory allocator for kernel objects
- **Nginx**: Uses slab allocator specifically for shared memory zones and header parsing
- **FreeBSD, modern Unix systems**: Standard OS-level allocator

**Deep Dive - Nginx Implementation**:
- Nginx implements custom slab allocator within shared memory zones
- Manages allocation/deallocation of small memory chunks in fixed-size regions
- Used extensively for HTTP header parsing and configuration caching

---

### 1.4 Jemalloc-style Allocators

**Overview**: Multi-threaded allocator using thread-local allocation with central coordination for large blocks.

**Core Concepts**:
- **Arenas**: Independent allocation domains to reduce contention
- **Runs**: Contiguous memory subdivided into regions
- **Size classes**: Small object allocations grouped by size (~40 classes)
- **Slabs**: Large page-aligned allocations broken into regions

**Thread-Local Performance**:
- Each thread maintains its own free list per size class
- When thread's free list depleted, it requests more from global (central) allocator
- When central allocator depleted, requests large contiguous page allocations (slabs)
- Slabs then broken up into objects of specific size class

**Advantages**:
- Excellent multi-threaded scalability (30% higher throughput than tcmalloc with many threads)
- Low memory overhead across diverse allocation patterns
- Predictable behavior with complex workloads
- Superior fragmentation handling for long-running processes

**Disadvantages**:
- More complex internal structures than simpler allocators
- Slightly slower for small allocations vs. tcmalloc
- Higher memory overhead than specialized allocators

**Real-world Adoption**:
- **Redis**: Default allocator on Linux
- **Facebook**: Widely deployed internally
- **Web infrastructure**: Standard for long-running servers

---

### 1.5 tcmalloc (Google's Thread-Caching Malloc)

**Overview**: High-performance allocator optimized for small allocations with thread-local caching.

**Architecture**:
- **Thread-local cache**: Each thread maintains its own small object cache
- **Central free list**: Per-size-class central allocator
- **Page heap**: Manages large allocations
- **Profiling hooks**: Built-in heap profiling without overhead

**Performance Characteristics**:
- ~22% faster than jemalloc for small allocations
- ~50x throughput advantage over libc malloc for 4MB allocations
- Excellent for workloads with frequent small allocations
- Memory overhead higher than jemalloc in multi-threaded scenarios

**Real-world Usage**:
- **Envoy proxy**: Links statically to tcmalloc for performance and profiling
- **Google services**: Default internal allocator
- **C++ applications**: Common choice for high-performance services

**Envoy Proxy Integration**:
- Tcmalloc is fundamental to Envoy's memory management
- Heap profiling via `/heap_dump` endpoint
- Tracks both C++ new allocations and plain malloc calls
- Memory proto in Envoy API exposes tcmalloc heap statistics

---

### 1.6 Lock-Free Allocators

**Overview**: Allocators using atomic operations instead of locks for thread-safe concurrent allocation.

**Key Properties**:
- Progress guaranteed regardless of thread delays or preemption
- No deadlock risk, suitable for real-time systems and interrupt handlers
- Only use widely-available OS support and atomic instructions

**Notable Implementations**:

1. **Maged M. Michael's Allocator**
   - Guaranteed availability under arbitrary thread termination
   - Crash-failure resistant
   - Immune to deadlock regardless of scheduling policies

2. **NBmalloc**
   - Architecture inspired by Hoard concurrent allocator
   - Lock-free design with modular structure
   - Avoids false-sharing and heap-blowup

3. **XMalloc**
   - Uses only globally shared data structures
   - Sustains high allocation throughput
   - Economizes on memory traffic

4. **LRMalloc**
   - Modern state-of-the-art with lock-free guarantees
   - Priority inversion tolerance
   - Kill-tolerance availability and deadlock immunity

**Advantages**:
- Supports unlimited concurrent threads without degradation
- Suitable for hard real-time systems
- No priority inversion problems

**Disadvantages**:
- Complex implementation
- May have overhead vs. lock-based approaches for contended scenarios
- Less common in production HTTP servers

---

### 1.7 Custom Allocators

**Overview**: Application-specific allocators tailored to particular access patterns.

**Rationale**:
- General-purpose allocators may not match server's specific patterns
- Custom allocators can exploit domain knowledge for optimization

**Nginx Custom Allocator Example**:
- **Small allocations** (≤ size M): Allocated from chain of blocks, new block created when current is full
- **Large allocations** (> size M): Forwarded to system malloc via ngx_alloc
- **Tiered approach**: Balances between pool reuse and dynamic allocation

**Design Principles**:
- Understand application's allocation patterns first
- Design allocator around bottlenecks
- Avoid premature optimization without profiling

---

### 1.8 Stack vs. Heap Allocation

**Stack Allocation**:
- Fast: Simple pointer manipulation
- Automatic deallocation on scope exit
- Limited space per thread
- Better cache locality
- Contiguous memory layout
- Suitable for fixed-size buffers and temporary variables

**Heap Allocation**:
- Variable-size objects at runtime
- Shared across threads
- Larger address space
- More complex management
- Greater flexibility

**HTTP Server Context**:
- **Request headers**: Often fixed-size or small, good for stack allocation
- **Request body**: Variable size, requires heap
- **Buffer management**: Hybrid approach (fixed-size pools + dynamic)

---

## 2. KNOWN HTTP SERVERS - DETAILED ANALYSIS

### 2.1 Nginx (C)

**Overall Strategy**: Custom tiered memory allocator optimized for request processing.

**Architecture**:
- **Request-lifetime pools**: Memory tied to request lifecycle
- **Pool chains**: Blocks linked in chains for incremental growth
- **Shared memory zones**: Slab allocator for shared state
- **Two-tier allocation**: Small (pool) vs. large (malloc)

**Small Allocation Path**:
- Allocations ≤ predefined size M allocated from chain
- New chain node created when current exhausted
- Returns pointer from chain

**Large Allocation Path**:
- Allocations > size M go directly to ngx_alloc (system malloc)
- Pointer stored in pool for cleanup tracking

**Memory Control**:
- Automatic pool deallocation on request completion
- Provides good allocation performance and memory control
- Shared memory slab allocator for persistent data

**Header Parsing**:
- Nginx parses headers in C, passes to modules
- Typically parsed twice (Zig → Python pattern common in frameworks)
- Shared memory zones use slab allocator for efficiency

**Key Files** (in nginx source):
- `src/core/ngx_palloc.c` / `ngx_palloc.h`: Memory pool implementation
- `src/core/ngx_slab.c`: Shared memory slab allocator

**Problems Reported**:
- Memory fragmentation in long-running processes with complex configs
- Cycle pool fragmentation documented in OpenResty blogs
- Requires careful tuning of pool initial sizes

---

### 2.2 Envoy Proxy (C++)

**Overall Strategy**: Relies on tcmalloc with custom connection/stream pooling.

**Allocator**: tcmalloc (linked statically)

**Memory Management Features**:
- **Heap profiling**: `/heap_dump` endpoint for live profiling
- **Memory stats**: Exposed via admin API (Memory proto)
- **Overload manager**: Can reject requests when memory pressure too high

**Access Patterns**:
- Per-connection state allocated on heap
- Per-stream state allocated on heap
- Frequent small allocations (typical C++ pattern)
- tcmalloc handles contention automatically

**Size Classes**: Benefits from tcmalloc's ~40 size classes
- Reduces fragmentation from diverse allocation patterns
- Hot path optimized with thread-local caches

**Memory Concerns**:
- Long-running Envoy instances can accumulate memory
- Fragmentation mitigated by tcmalloc's design
- Profiling tools available for debugging

**References**:
- [Envoy admin API](https://www.envoyproxy.io/docs/envoy/latest/api-v3/admin/v3/memory.proto)
- Overload manager documentation

---

### 2.3 HAProxy (C)

**Overall Strategy**: Lightweight pool-based allocator optimized for simplicity and speed.

**Design Philosophy**:
- Simple and fast object pooling
- Pool-per-type design
- LIFO (stack) ordering for cache efficiency
- No centralized allocator, each type manages its pool

**Core Mechanism**:
- Objects returned to their origin pool (LIFO)
- Recently-freed objects still warm in CPU caches
- Similar-sized pools merged to limit fragmentation
- Released objects never freed (always reused)

**Memory Control**:
- `-m limit` option: Limits total allocatable memory across processes
- On memory allocation failure: Frees all available pool objects before retry
- Per-process memory limits via maxconn and maxsslconn

**Connection Management**:
- MaxMemFree: Maximum free memory per allocator
- In threaded MPMs, each thread has separate allocator
- Tuned for connection-heavy workloads

**Fragmentation Strategy**:
- Stack-ordered pools keep hot memory together
- Pool merging reduces fragmentation
- No long-lived allocations mixing with short-lived

---

### 2.4 Node.js (JavaScript - V8 Engine)

**Overall Strategy**: Generational garbage collection with automatic memory management.

**Memory Hierarchy**:
- **Young Space (New Generation)**:
  - Where new objects allocate
  - Frequent garbage collection (Scavenge)
  - Most objects "die young" (generational hypothesis)

- **Old Space (Old Generation)**:
  - Objects surviving multiple GC cycles promoted here
  - Less frequent collection (Mark-Sweep)
  - May perform Compaction to reduce fragmentation

**GC Algorithms**:
1. **Scavenge**: Fast collection on Young Space
2. **Mark-Sweep**: Traverses entire object graph, marks reachable objects, sweeps unmarked
3. **Compaction** (optional): Rearranges live objects to reduce fragmentation

**Orinoco Pipeline**:
- Modern V8 GC framework
- Parallel, concurrent, and incremental collection
- Reduces pause times significantly

**Memory Retention Strategy**:
- V8 retains acquired memory segments from OS
- Even after objects become garbage, memory held
- Anticipates future allocation needs
- Minimizes performance cost of frequent OS requests/releases

**Real-World Behavior**:
- Heap grows and shrinks based on load
- Long-running Node servers may hold onto memory
- Suitable for high-throughput but variable-latency patterns

**References**:
- [V8 GC documentation](https://github.com/thlorenz/v8-perf/blob/master/gc.md)
- [Node.js Memory Understanding and Tuning](https://nodejs.org/en/learn/diagnostics/memory/understanding-and-tuning-memory)

---

### 2.5 Rust Ecosystem - Axum, Actix-web, Hyper, Tokio

**Overall Strategy**: Relies on standard allocators (jemalloc, mimalloc) with careful async memory patterns.

#### Axum (via Tokio)
- **Allocator**: Can use jemalloc or mimalloc for best results
- **Memory Profile**: Lowest memory consumption vs. Actix-web
- **Performance**: Excellent real-world performance with predictable behavior
- **Async Pattern**: Zero-copy streaming possible with bounded buffers
- **Note**: Some memory growth observed under sustained load, returns after hours

#### Actix-web
- **Allocator**: Default system allocator or jemalloc
- **Memory Profile**: More predictable than Axum historically
- **Throughput**: High raw throughput in synthetic benchmarks
- **Optimization**: Highly optimized request handling pipeline

#### Hyper
- **Position**: Lower-level HTTP library used by Axum
- **Memory**: No built-in pooling, relies on allocator choice
- **Concurrency**: Designed for use with Tokio runtime

#### Tokio (Async Runtime)
- **Memory Caching**: Allocators cache freed memory, don't return immediately to OS
- **Design Rationale**: Anticipates future allocations of same size/layout
- **Channel Tuning**: Buffer sizes affect both memory usage and backpressure
- **Task Allocation**: Only yields at `.await` points (cooperative scheduling)

**Key Insight**: Rust frameworks don't implement custom allocators typically; instead, they choose high-performance allocators (jemalloc, mimalloc) and optimize data structures for cache efficiency.

**Allocator Choice Impact**:
- Mimalloc with Axum showed best memory results in benchmarks
- Jemalloc good for diverse allocation patterns
- Default allocator adequate for many workloads

---

### 2.6 Apache HTTP Server (C)

**Overall Strategy**: Request-lifetime arena allocators called "pools" with module cooperation.

**Architecture**:
- **APR (Apache Portable Runtime)**: Provides cross-platform abstraction
- **Memory pools**: Arena-style allocators per request
- **Module integration**: Modules allocate from request pool

**Pool Lifecycle**:
```
Server start
  ↓
Create server pool (never freed)
  ↓
Per request: Create request pool
  ↓
Process request (allocate from pool)
  ↓
Free entire request pool (automatic)
  ↓
Next request
```

**MPM Variations**:
- **Prefork MPM**: Each child process handles one request at a time
- **Event MPM**: Listeners thread offloads some work, frees workers for new requests
- **Worker (threaded)**: Multiple threads per process, each with request pool

**Memory Management Details**:
- MaxMemFree: Maximum free memory per allocator
- MaxConnectionsPerChild: Limits process memory growth via recycling
- Thread-local allocators in threaded MPMs

**Two-Phase Commitment**:
- Initial allocation per request
- Must fit all request data in that pool
- No inter-request deallocation in pool

**References**:
- [Apache MPM Common Documentation](https://httpd.apache.org/docs/current/mod/mpm_common.html)
- [Apache Module Allocation API](https://www.nginx.com/resources/wiki/extending/api/alloc/)

---

### 2.7 Lighttpd (C)

**Overall Strategy**: Minimalist allocator design focusing on low memory footprint.

**Philosophy**:
- Minimal memory overhead
- Efficient CPU utilization
- Speed optimizations throughout

**Characteristics**:
- Very low memory footprint compared to Apache, Nginx
- Small CPU load
- Suitable for resource-constrained environments
- Scales well on servers with load problems

**Allocation Approach**:
- Similar to HAProxy: lightweight pooling
- Focus on deterministic behavior
- Minimal per-connection overhead

---

### 2.8 h2o HTTP Server (C)

**Overall Strategy**: Region-based memory management with HTTP request/connection association.

**Memory Pool Architecture**:
- **Request pools**: Memory associated with HTTP request
- **Connection pools**: Memory associated with connection
- **Auto-deallocation**: Pool memory freed when request/connection closes

**Allocation Pattern**:
```
Request received
  ↓
Allocate request memory block from pool
  ↓
Small allocations return portions of block (no return to pool)
  ↓
Request complete
  ↓
Entire memory block freed
```

**Buffering Strategy**:
- Default temp-buffer-threshold: 32MB
- Overflow storage to directory for large allocations
- Threshold tunable or disableable

**Performance Focus**:
- Quicker response time vs. older generation servers
- Less CPU and memory bandwidth utilization
- Modern HTTP/2 and HTTP/3 support with efficient memory use

**Key Innovation**: Combines request-lifetime allocation with stream-based HTTP/2 patterns

---

## 3. KEY QUESTIONS - COMPREHENSIVE ANSWERS

### 3.1 What allocation strategy does each use?

| Server | Primary Strategy | Secondary | Rationale |
|--------|-----------------|-----------|-----------|
| **Nginx** | Custom tiered pools | Slab (shared zones) | Exploit request patterns; control fragmentation |
| **Envoy** | tcmalloc | Connection pooling | Diverse C++ allocation patterns; profiling |
| **HAProxy** | Pool per type (LIFO) | — | Simplicity and cache efficiency |
| **Apache** | Arena (pools) | — | Request-lifetime allocation; module cooperation |
| **Lighttpd** | Lightweight pools | — | Minimal overhead priority |
| **h2o** | Region-based pools | — | HTTP request/connection association |
| **Axum** | Allocator agnostic | jemalloc/mimalloc | Rust language memory safety |
| **Actix-web** | Allocator agnostic | jemalloc | Throughput optimization |
| **Hyper** | System allocator | — | Low-level library, delegates |
| **Tokio** | System allocator | — | Runtime only; apps choose |
| **Node.js** | Generational GC | — | JavaScript memory model |

---

### 3.2 What are the memory access patterns?

**Pattern 1: Request-Lifetime Allocations** (Nginx, Apache, h2o)
- Single large allocation per request
- Rapid allocation during request setup
- Bulk deallocation on request completion
- Sequential access patterns within request

**Pattern 2: Connection-Based Allocations** (Envoy, HAProxy)
- Long-lived connection objects
- Per-stream/per-request objects within connection
- Mixed lifetimes within single connection
- Frequent small allocations and deallocations

**Pattern 3: Generational Allocations** (Node.js)
- Young space: Frequent short-lived objects
- Old space: Fewer, longer-lived objects
- Generational hypothesis: Most objects die young
- Spatial locality crucial for GC pauses

**Pattern 4: Async Task Allocations** (Tokio, Axum, Actix)
- Per-task local state
- Shared data via Arc/atomic reference counting
- Variable lifetimes crossing await boundaries
- Concurrent independent allocations

**Access Characteristics**:
- **Temporal**: Request-based servers show clear burst patterns
- **Spatial**: Arena allocators leverage cache lines; bump allocation is cache-friendly
- **Concurrency**: Lock-free designs eliminate contention points
- **Fragmentation**: Pool strategies reduce external fragmentation

---

### 3.3 How do they handle concurrent allocations?

**Method 1: Thread-Local Caches** (tcmalloc, jemalloc)
- Each thread maintains own free lists per size class
- Zero synchronization on fast path
- Central allocator handles cache misses
- Scales linearly with thread count

**Method 2: Per-Thread Allocators** (Apache threaded MPM, HAProxy)
- Each thread has dedicated allocator
- No sharing between threads
- No locks needed
- Proportional memory overhead to thread count

**Method 3: Lock-Free Primitives** (LRMalloc, XMalloc, NBmalloc)
- Atomic operations replace locks
- Wait-free progress guarantees
- Scalable to unlimited threads
- Higher complexity, rare in HTTP servers

**Method 4: Central Coordination** (Nginx, h2o)
- Short-lived connection/request objects
- Minimal inter-thread contention
- Central allocator acceptable for request granularity
- Works well with event loops

**Method 5: Garbage Collection** (Node.js, JVM-based servers)
- Automatic memory management
- Pause times during GC cycles
- Scalable to many concurrent tasks
- GC must be tuned for latency requirements

---

### 3.4 What about fragmentation concerns?

**External Fragmentation Problem**:
- Occurs when free memory is scattered in small chunks
- Large allocation request can't be satisfied despite sufficient free memory
- Worse with variable-sized allocations over time

**Solutions by Category**:

#### Slab Allocators (Nginx shared zones, Linux kernel)
- Fixed size classes prevent fragmentation
- Merging similar-sized slabs reduces waste
- Tradeoff: Some internal fragmentation from size class overhead

#### Arena/Pool Allocators (Apache, h2o)
- No external fragmentation within arena
- Bulk deallocation prevents long-term accumulation
- Tradeoff: Must deallocate atomically; can't free individual objects

#### Jemalloc
- 40+ size classes match common allocation patterns
- Run-based organization reduces fragmentation
- Better memory overhead than tcmalloc for complex workloads
- Studies show <30% overhead

#### tcmalloc
- Thread-local caches keep blocks warm
- Central free lists per size class
- Page-level allocation for large blocks
- Good fragmentation handling

#### Mimalloc (Microsoft)
- **Free list sharding**: Page-local lists distributed across 3 lists
- Reduces contention and improves cache locality
- 5.3× faster than glibc malloc, 50% less RSS
- Near-linear scaling with threads

#### HAProxy Strategy
- LIFO pool ordering keeps recently-freed blocks hot
- Pool merging for similar sizes
- Never free objects (only reuse)
- Simple and effective for connection objects

#### Memcached Approach
- Fixed 1MB slab blocks
- Each slab divided into identical chunks
- Memory not reclaimed on item expiry (chunk stays in slab)
- Prevents fragmentation through uniformity

---

### 3.5 Memory efficiency vs. speed tradeoffs?

**Speed-Optimized Approaches**:
- tcmalloc: ~22% faster for small allocations
- Bump allocators: Single pointer increment, extremely fast
- Thread-local caches: Zero-lock fast path
- Cost: May hold more memory than necessary

**Memory-Optimized Approaches**:
- Jemalloc: Lower memory overhead than tcmalloc (10-30%)
- Mimalloc: 50% less RSS than malloc on heavy workloads
- Slab allocators: Reduced internal fragmentation
- Cost: Slightly slower allocation

**Balanced Approach**:
- **Request-lifetime pools**: Both fast (bump within pool) and efficient (bulk free)
- **Size classes**: Good fragmentation without extreme overhead
- **Adaptive tuning**: Observe actual patterns and adjust pool sizes

**Specific Tradeoffs**:

| Metric | Fastest | Most Efficient | Tradeoff |
|--------|---------|---|-----------|
| Allocation speed | Bump allocators | Slab allocators | Flexibility vs. speed |
| Memory overhead | Arenas (no tracking) | jemalloc | Overhead tracking vs. fragmentation |
| Deallocation speed | Bump (bulk) | Pool-based | Granularity vs. bulk cost |
| Fragmentation | jemalloc/Mimalloc | Slab/Arena | Adaptive vs. predictable |
| Scaling | Mimalloc/tcmalloc | jemalloc | Small allocations vs. threads |

**Real-World Metrics**:
- Arena allocators: 10-100× faster for request-heavy workloads
- tcmalloc vs. jemalloc: tcmalloc wins on throughput (small allocations), jemalloc wins on memory (multi-threaded)
- Mimalloc shows 5.3× improvement over glibc with 50% less memory

---

## 4. IMPLEMENTATION PATTERNS FOR FRAMEWORKS

### 4.1 Best Practices for HTTP Servers

1. **Profile First**: Understand allocation patterns before choosing strategy
2. **Bound Allocations**: Know max memory per request, connection, task
3. **Pool Reuse**: Cache frequently-allocated objects
4. **Batch Deallocation**: Free multiple objects together when possible
5. **Separate Lifetimes**: Keep long-lived and short-lived objects in separate arenas
6. **Thread-Local Allocation**: Avoid lock contention on shared allocators
7. **Size Classes**: Group allocations by size, allocate from appropriate class
8. **Monitor Fragmentation**: Track memory growth over time
9. **Tune OS Parameters**: vm.min_free_kbytes, memory compaction settings
10. **Consider Specialized Allocators**: jemalloc or mimalloc for high concurrency

### 4.2 Recommended Strategies by Scenario

**High-Throughput, Short-Lived Requests**:
- Use arena/bump allocators (Apache, h2o model)
- Pool size: 1-4KB per request typical
- Bulk deallocation on request completion
- Excellent cache locality

**Long-Lived Connections, Stream-Based** (HTTP/2):
- Use tcmalloc or jemalloc
- Connection-level memory pools
- Per-stream allocation within connection
- Handle fragmentation via allocator

**Concurrent Async Tasks** (Rust, Node.js):
- Use high-performance allocator (mimalloc, jemalloc)
- Let runtime/language handle memory
- Focus on reducing allocations via pooling
- Monitor allocator behavior via profiling

**Memory-Constrained Environments**:
- Use lightweight pooling (HAProxy, Lighttpd model)
- Minimize allocator overhead
- Fixed-size buffers and pools
- Trade latency variance for memory efficiency

---

## 5. SOURCES & FURTHER READING

### Academic & Research Papers

- [Slab Allocation - Wikipedia](https://en.wikipedia.org/wiki/Slab_allocation)
- [The Slab Allocator: An Object-Caching Kernel Memory Allocator - Jeff Bonwick](https://people.eecs.berkeley.edu/~kubitron/courses/cs194-24-S14/hand-outs/bonwick_slab.pdf)
- [Scalable Lock-Free Dynamic Memory Allocation - Maged M. Michael](https://www.cs.tufts.edu/~nr/cs257/archive/neal-glew/mcrt/Non-blocking%20data%20structures/p35-michael.pdf)
- [On the Impact of Memory Allocation on High-Performance Query Processing](https://arxiv.org/pdf/1905.01135)
- [Mimalloc: Free List Sharding in Action](https://www.microsoft.com/en-us/research/wp-content/uploads/2019/06/mimalloc-tr-v1.pdf)

### Nginx

- [Memory Management API - NGINX Wiki](https://www.nginx.com/resources/wiki/extending/api/alloc/)
- [How OpenResty and Nginx Allocate and Manage Memory](https://blog.openresty.com/en/how-or-alloc-mem/)
- [Nginx Memory Allocation Analysis - Medium](https://medium.com/@zpcat/nginxs-memory-allocation-be49c81b4832)
- [Memory Pool Debug Tool - ngx_debug_pool](https://github.com/chobits/ngx_debug_pool)

### Envoy

- [Envoy Memory Proto Documentation](https://www.envoyproxy.io/docs/envoy/latest/api-v3/admin/v3/memory.proto)
- [Heap Profiling in Envoy](https://www.envoyproxy.io/docs/envoy/latest/faq/debugging/how_to_dump_heap_profile_of_envoy)

### Apache

- [Apache MPM Common - Official Docs](https://httpd.apache.org/docs/current/mod/mpm_common.html)
- [Apache Performance Tuning: MPM Directives](https://www.liquidweb.com/blog/apache-performance-tuning-mpm-directives/)

### General Allocator Comparison

- [libmalloc, jemalloc, tcmalloc, mimalloc - Exploring Different Memory Allocators](https://dev.to/frosnerd/libmalloc-jemalloc-tcmalloc-mimalloc-exploring-different-memory-allocators-4lp3)
- [C++ Memory Allocation Performance: tcmalloc vs. jemalloc](https://linuxvox.com/blog/c-memory-allocation-mechanism-performance-comparison-tcmalloc-vs-jemalloc/)
- [Battle of the Mallocators - Small Datum Blog](http://smalldatum.blogspot.com/2025/04/battle-of-mallocators.html)

### Arena Allocators

- [Region-based Memory Management - Wikipedia](https://en.wikipedia.org/wiki/Region-based_memory_management)
- [High Performance Memory Management: Arena Allocators](https://medium.com/@sgn00/high-performance-memory-management-arena-allocators-c685c81ee338)
- [Arena-Based Allocation in Compilers](https://medium.com/@inferara/arena-based-allocation-in-compilers-b96cce4dc9ac)
- [Guide to using arenas in Rust - LogRocket](https://blog.logrocket.com/guide-using-arenas-rust/)

### Rust Frameworks

- [Axum vs Actix: Choosing the Best Rust Framework](https://dev.to/sanjay_serviots_08ee56986/axum-vs-actix-choosing-the-best-rust-framework-for-web-development-49ch)
- [Rust Web Frameworks in 2026](https://aarambhdevhub.medium.com/rust-web-frameworks-in-2026-axum-vs-actix-web-vs-rocket-vs-warp-vs-salvo-which-one-should-you-2db3792c79a2)
- [Axum Streaming with Bounded Memory](https://users.rust-lang.org/t/axum-streaming-data-to-multiple-http-clients-with-bounded-memory-usage/94668)

### Node.js / V8

- [Node.js Garbage Collection - RisingStack](https://blog.risingstack.com/node-js-at-scale-node-js-garbage-collection/)
- [Node.js Memory Understanding and Tuning](https://nodejs.org/en/learn/diagnostics/memory/understanding-and-tuning-memory)
- [V8 Garbage Collection Illustrated Guide](https://medium.com/@_lrlna/garbage-collection-in-v8-an-illustrated-guide-d24a952ee3b8)
- [V8 Performance Profiling](https://github.com/thlorenz/v8-perf/blob/master/gc.md)

### Fragmentation & Solutions

- [Memory Fragmentation: Understanding and Solutions](https://www.softwareverify.com/blog/memory-fragmentation-your-worst-nightmare/)
- [Linux Kernel vs. Memory Fragmentation](https://www.pingcap.com/blog/linux-kernel-vs-memory-fragmentation-2/)
- [Memcached Memory Allocation](https://www.virtuozzo.com/company/blog/memcached-memory-allocation/)
- [Buffer Fragmentation and Challenges - The Node Book](https://www.thenodebook.com/buffers/fragmentation-and-challenges/)

### Mimalloc

- [Mimalloc GitHub Repository](https://github.com/microsoft/mimalloc)
- [Mimalloc Performance Documentation](https://microsoft.github.io/mimalloc/bench.html)
- [Free List Sharding in Mimalloc](https://link.springer.com/chapter/10.1007/978-3-030-34175-6_13)

### Tokio / Hyper

- [Tokio Async Runtime](https://tokio.rs/)
- [Practical Guide to Async Rust and Tokio](https://medium.com/@OlegKubrakov/practical-guide-to-async-rust-and-tokio-99e818c11965)
- [Network Programming with Tokio](https://dasroot.net/posts/2026/02/network-programming-rust-tokio/)

### HAProxy

- [HAProxy Configuration Manual](https://www.haproxy.com/documentation/haproxy-configuration-manual/1-7r2/management/)
- [Connection Limits & Queues](https://www.haproxy.com/blog/protect-servers-with-haproxy-connection-limits-queues/)

### H2O

- [H2O GitHub Repository](https://github.com/h2o/h2o)
- [H2O - Optimized HTTP Server Presentation](https://www.slideshare.net/kazuho/h2o-20141103pptx)

---

## 6. SUMMARY TABLE - QUICK REFERENCE

| Aspect | Leader | Key Advantage | Tradeoff |
|--------|--------|---------------|----------|
| **Allocation Speed** | Bump allocators | Single pointer increment | Must deallocate bulk |
| **Memory Efficiency** | Jemalloc / Mimalloc | Low fragmentation, overhead | Slightly slower than tcmalloc |
| **Throughput (small allocs)** | tcmalloc | 22% faster | Higher memory overhead |
| **Multi-threaded Scaling** | Mimalloc / Jemalloc | Near-linear scaling | More complex implementation |
| **Simplicity** | HAProxy pools | Easy to understand | Less flexible |
| **Long-running Stability** | Jemalloc | Predictable fragmentation | Not fastest for small allocs |
| **Lock-free Guarantee** | LRMalloc | Immune to deadlock | Research prototype, not mainstream |
| **Request-pattern Optimization** | Nginx pools | Matches HTTP semantics | Custom allocator overhead |
| **Memory Bound Guarantee** | Arena allocators | Predictable per-request limit | Can't allocate variably |
| **Production Maturity** | tcmalloc, jemalloc | Well-tested at scale | Requires tuning |

---

## 7. RECOMMENDATIONS FOR THE FRAMEWORK

For `the-framework` (Python + Zig HTTP server with greenlets + io_uring):

### Considerations

1. **Greenlet Memory Model**: Each greenlet has stack, but all share same heap
2. **Request-Lifetime Allocations**: Natural fit for arena allocators
3. **Zig's Allocator Abstraction**: Zig has first-class allocator concept
4. **Python Integration**: Python has its own memory management (GC)
5. **io_uring Integration**: No special allocation requirements from io_uring

### Potential Strategies

**Option 1: Request-Lifetime Arena** (Recommended for HTTP)
- Allocate arena per request in Zig
- All request data lives in arena
- Deallocate entire arena on request completion
- Fast allocation (bump), no fragmentation, matches HTTP semantics

**Option 2: Shared Pool with Prefork**
- Worker processes each get memory pools
- Pre-allocate buffers for typical request sizes
- Reuse buffers across requests
- Memory isolation between workers

**Option 3: Hybrid (Arena + Pools)**
- Arena for request lifecycle
- Pools for fixed-size buffers (e.g., header parsing)
- Combine benefits of both approaches

### Header Parsing Memory Pattern

Based on MEMORY.md context:
- Headers parsed once in Zig (not twice)
- Passed to Python as list of tuples
- Natural fit for arena: Parse entire header block, deallocate with request

---

**Document compiled: 2026-02-28**
**Research scope: Comprehensive survey of high-performance HTTP server memory allocation strategies**
