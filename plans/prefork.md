# Multi-Worker Architecture for theframework

## The Problem

theframework currently runs as a **single process with a single OS thread**. One io_uring event
loop (the "hub") drives all I/O, and greenlets provide cooperative multitasking within that
thread. This means:

- **Only one CPU core is used.** A 16-core machine wastes 15 cores.
- **If the process crashes, the entire server is down.** No redundancy.
- **A slow request blocks all others.** Greenlets are cooperative — a CPU-intensive handler
  (JSON serialization, template rendering, image processing) blocks the hub loop, stalling every
  connection until it finishes.

Every production Python web server solves this the same way: **run multiple copies of the server
in parallel, one per CPU core.** This document explores how to do that for theframework.

---

## Background: Why Processes, Not Threads?

A natural question: why not just use multiple threads within one process? In most languages
(Rust, Go, Java, C++), multi-threading gives you true parallelism — multiple threads execute
simultaneously on different CPU cores, sharing the same memory space.

Python has the **Global Interpreter Lock (GIL)**. The GIL is a mutex that ensures only one thread
can execute Python bytecode at a time. Even on a 16-core machine, a Python process with 16
threads will only run Python code on **one core at a time**. The other 15 threads are blocked
waiting for the GIL.

The GIL is released during I/O operations (socket reads/writes, file I/O, sleeping) and when
calling C extensions. theframework already exploits this — the hub releases the GIL during
`io_uring_enter()` (the syscall that waits for I/O completions). But Python code in request
handlers — routing, middleware, JSON parsing, template rendering — all holds the GIL.

The only way to get true CPU parallelism in Python is to use **separate processes**. Each process
has its own GIL, its own interpreter, and its own memory space. They run truly in parallel on
different cores.

> **Note:** Python 3.13 introduced experimental "free-threaded" mode that removes the GIL. This
> is not yet stable or widely adopted. We design for the standard GIL-enabled Python but note
> where free-threaded mode would change the calculus.

---

## Background: How Sockets Work with Multiple Processes

Before diving into architectures, you need to understand how the kernel handles network sockets
when multiple processes are involved.

### The Listening Socket and the Accept Queue

When a server calls `bind()` and `listen()` on a socket, the kernel creates an **accept queue**
for that socket. When a client connects (TCP 3-way handshake completes), the kernel places the
new connection into this queue. The server calls `accept()` to dequeue one connection at a time.

```
Client connects → [kernel accept queue] → server calls accept() → gets connection fd
```

The queue has a maximum depth (the "backlog" parameter to `listen()`). If the queue is full and
a new client tries to connect, the kernel silently drops the SYN packet — the client retries
after a timeout.

### File Descriptor Inheritance Across fork()

When a process calls `fork()`, the child process gets a **copy of the parent's file descriptor
table**. Both parent and child now have file descriptors pointing to the **same underlying kernel
objects**. For sockets, this means:

- If the parent has a listening socket on fd 5, the child also has fd 5 pointing to **the same
  kernel socket** with **the same accept queue**.
- Both processes can call `accept()` on fd 5. The kernel dequeues one connection per `accept()`
  call, regardless of which process made the call.
- Closing fd 5 in the child does NOT close the socket — the kernel keeps it open as long as at
  least one process still has it open.

This is the foundation of the "prefork" model: bind a socket, fork children, let them all
accept from the same socket.

### SO_REUSEPORT: Separate Sockets, Same Address

Linux 3.9 (2013) introduced `SO_REUSEPORT`, a socket option that allows **multiple sockets to
bind to the same address:port**. Without it, `bind()` would fail with `EADDRINUSE` if another
socket was already bound.

With SO_REUSEPORT:
- Each process creates its own independent socket
- Each calls `bind()` on the same address:port (e.g., `0.0.0.0:8000`)
- Each socket gets its **own separate accept queue**
- The kernel distributes incoming connections across sockets using a hash of the connection's
  4-tuple: (source IP, source port, destination IP, destination port)

```
Client A → [kernel hash] → socket owned by Worker 1 → Worker 1's accept queue
Client B → [kernel hash] → socket owned by Worker 3 → Worker 3's accept queue
Client C → [kernel hash] → socket owned by Worker 1 → Worker 1's accept queue
```

The key difference from shared-socket: there's no single queue that multiple processes compete
for. Each process has its own queue, and the kernel decides who gets each connection.

---

## Background: io_uring and fork()

theframework uses **io_uring**, Linux's high-performance async I/O interface. io_uring has
specific constraints around `fork()` that heavily influence our architecture choices.

### How io_uring Works in Memory

When you create an io_uring instance, the kernel allocates two ring buffers and maps them into
your process's memory using `mmap()` with `MAP_SHARED`:

- **Submission Queue (SQ):** you write I/O requests here
- **Completion Queue (CQ):** the kernel writes I/O results here

Your process and the kernel share these memory regions. You write an SQE (submission queue
entry) describing the I/O operation, then call `io_uring_enter()` to tell the kernel to process
it. The kernel completes the operation and writes a CQE (completion queue entry) with the result.

### What Happens When You fork() with an Active io_uring Ring

After `fork()`, the child process inherits the parent's memory mappings — including the mmap'd
ring buffers. Now **both processes point to the same SQ and CQ**:

- If the parent writes an SQE, the child can see it (and vice versa)
- If the kernel writes a CQE for the parent's request, the child might read it first
- Both processes can corrupt each other's ring head/tail pointers
- **The ring becomes unusable.** This is a data race at the kernel/userspace boundary.

The kernel's internal bookkeeping (registered files, buffer pools, the io_uring worker thread
pool) is also tied to the process that created the ring. The child cannot use the parent's
kernel-side state.

### The Rule: Fork First, Then Create Rings

The universally recommended pattern is:

```
1. Parent process: set up sockets, configuration, etc. (NO io_uring)
2. Parent process: fork() children
3. Each child: create its OWN io_uring ring
4. Each child: use its ring independently
```

This way, each process has a completely independent io_uring instance with its own SQ, CQ,
kernel state, and worker threads. No sharing, no races.

> **Technical note:** The C library `liburing` provides `io_uring_ring_dontfork()` which calls
> `madvise(MADV_DONTFORK)` on the ring memory to prevent inheritance. Zig's `IoUring` wrapper
> does NOT provide this function. We avoid the problem entirely by never creating rings before
> fork.

---

## Background: theframework's Current Architecture

Understanding what we have today:

```
Single process:
  ┌─────────────────────────────────────────────────┐
  │  Hub (global singleton)                          │
  │  ├── io_uring ring (256 SQ entries)              │
  │  ├── ConnectionPool (4096 slots)                 │
  │  ├── OpSlotTable (256 slots)                     │
  │  ├── fd_to_conn[4096] mapping                    │
  │  ├── ready queue (greenlets to resume)            │
  │  └── stop_pipe (for shutdown signaling)           │
  │                                                   │
  │  hub_run():                                       │
  │    1. Create acceptor greenlet                    │
  │    2. Loop:                                       │
  │       a. Drain ready queue (switch to greenlets)  │
  │       b. Release GIL                              │
  │       c. io_uring submit + wait for CQEs          │
  │       d. Re-acquire GIL                           │
  │       e. Process CQEs (wake greenlets)            │
  │    3. Cleanup                                     │
  └─────────────────────────────────────────────────┘
```

Key properties that affect the multi-worker design:

1. **The Hub is a global singleton** (`hub.zig:1353: var global_hub: ?*Hub = null`). Only one
   hub per process. This is fine — in a multi-process model, each process gets its own hub.

2. **All state is hub-local.** The connection pool, op slots, fd mapping, and ready queue are
   all fields of the Hub struct. Nothing is shared globally. After fork, each child gets an
   independent copy (but we won't fork with an active hub — we'll create it fresh).

3. **Greenlets are thread-local.** The greenlet library uses thread-local storage for the
   current greenlet. Each process (with its single thread) has its own greenlet tree.

4. **The monkey patching uses thread-local hub detection.** `hub_is_running()` checks a
   thread-local variable. Works naturally with multi-process.

5. **`serve()` in server.py is the entry point.** It creates a socket, binds, listens, then
   calls `hub_run()`. To add multi-worker, we need to split socket creation from hub execution.

**The takeaway:** the core Zig I/O layer needs zero changes for multi-process. The hub, ring,
pool, and greenlet machinery all work as-is. Multi-worker is purely a Python-side orchestration
concern: fork the process, then run the existing `hub_run()` in each child.

---

## The Options

### Option A: Prefork with Inherited Socket

This is the classic model used by nginx (pre-2015), gunicorn, and most Unix servers since the
1990s.

#### How It Works

```
                    ┌─────────────────┐
                    │  Parent Process  │
                    │                  │
                    │  1. socket()     │
                    │  2. bind()       │
                    │  3. listen()     │
                    │  4. fork() × N   │
                    │  5. Supervise    │
                    └───────┬─────────┘
                            │ inherited listen_fd
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
     ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
     │   Worker 1   │ │   Worker 2   │ │   Worker N   │
     │              │ │              │ │              │
     │ create Hub   │ │ create Hub   │ │ create Hub   │
     │ (io_uring)   │ │ (io_uring)   │ │ (io_uring)   │
     │              │ │              │ │              │
     │ accept() ────┤ │ accept() ────┤ │ accept() ────┤
     │ on shared fd │ │ on shared fd │ │ on shared fd │
     │              │ │              │ │              │
     │ handle reqs  │ │ handle reqs  │ │ handle reqs  │
     └──────────────┘ └──────────────┘ └──────────────┘
                            │
                   ┌────────┴────────┐
                   │ ONE accept queue │
                   │ (kernel-managed) │
                   └─────────────────┘
```

1. **Parent** creates the listening socket, binds to the address, and starts listening.
2. **Parent** forks N worker processes. Each child inherits the listen fd.
3. **Parent** closes its copy of the listen fd (it doesn't need it). Enters a supervisor loop.
4. **Each child** creates its own Hub (with its own io_uring ring, connection pool, etc.).
5. **Each child** submits `accept` SQEs on the inherited listen fd through its own io_uring ring.
6. When a connection arrives, the kernel completes **exactly one** worker's accept SQE. That
   worker handles the connection. The others remain waiting.

#### The Thundering Herd Problem

Historically, when multiple processes blocked on `accept()` on the same socket, the kernel would
wake **all** of them when a connection arrived. Only one would succeed; the rest would discover
the connection was already taken and go back to sleep. This wasted CPU on the failed wakeups.

With io_uring, this is largely a non-issue. Each worker has a pending accept SQE in the kernel.
When a connection arrives, the kernel completes exactly one SQE — the others remain pending. No
wasted wakeups.

For traditional `epoll`-based servers, Linux 4.5 introduced `EPOLLEXCLUSIVE` which wakes only
one waiter. We don't need this since we use io_uring.

#### Worker Distribution

With a shared accept queue, the kernel tends to have a **LIFO (Last In, First Out) bias** — the
most recently sleeping process is woken first. Under light load, this means one or two workers
handle most requests while others sit idle. Under heavy load, it evens out because all workers
are busy.

Cloudflare measured this bias with `epoll`: the busiest worker handled ~3x more connections than
the least busy one. With io_uring's accept, the bias may be different (depends on the kernel's
SQE completion ordering), but some unevenness is expected.

For theframework, running behind a reverse proxy like nginx, the upstream connection pool usually
keeps a steady flow of requests to all workers, reducing the impact of uneven distribution.

#### Pros

- **Simplest implementation.** The parent creates one socket; children inherit it. No special
  socket options or per-worker socket creation.
- **No connection loss on worker restart.** When a worker dies, its unaccepted connections remain
  in the shared accept queue. Another worker picks them up. During rolling restarts, no
  connections are dropped.
- **Works on all Linux kernels.** No minimum kernel version requirements beyond io_uring support
  (5.1+, practically 5.10+ for the features we use).
- **Proven pattern.** Used by gunicorn, uwsgi, Apache prefork MPM, and pre-2015 nginx. Decades
  of battle testing.
- **Simple shutdown.** Parent stops forking, sends SIGTERM to all children. Children drain their
  in-flight requests, close their io_uring rings, and exit.

#### Cons

- **Uneven load distribution.** The LIFO bias means some workers get more connections than others.
  This is noticeable under light-to-moderate load but disappears under heavy load.
- **Accept queue contention.** Under very high connection rates (tens of thousands of new
  connections per second), workers contend on the shared accept queue lock. With io_uring's
  kernel-side accept, this contention is much lower than with userspace `accept()` calls, but
  it still exists.
- **Single backlog.** The accept queue depth is shared. If all workers are busy, the shared queue
  fills faster than N separate queues would.

---

### Option B: Prefork with SO_REUSEPORT

This is the modern model used by nginx (post-2015, with `reuseport`), Granian, and
high-throughput servers.

#### How It Works

```
                    ┌─────────────────┐
                    │  Parent Process  │
                    │                  │
                    │  1. fork() × N   │
                    │  2. Supervise    │
                    └───────┬─────────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
     ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
     │   Worker 1   │ │   Worker 2   │ │   Worker N   │
     │              │ │              │ │              │
     │ socket()     │ │ socket()     │ │ socket()     │
     │ SO_REUSEPORT │ │ SO_REUSEPORT │ │ SO_REUSEPORT │
     │ bind(:8000)  │ │ bind(:8000)  │ │ bind(:8000)  │
     │ listen()     │ │ listen()     │ │ listen()     │
     │              │ │              │ │              │
     │ create Hub   │ │ create Hub   │ │ create Hub   │
     │ (io_uring)   │ │ (io_uring)   │ │ (io_uring)   │
     │              │ │              │ │              │
     │ accept() ──┐ │ │ accept() ──┐ │ │ accept() ──┐ │
     │            │ │ │            │ │ │            │ │
     └────────────┘ │ └────────────┘ │ └────────────┘ │
          own queue ▼      own queue ▼      own queue ▼
     ┌──────────┐    ┌──────────┐    ┌──────────┐
     │ queue 1  │    │ queue 2  │    │ queue N  │
     └──────────┘    └──────────┘    └──────────┘
              ▲             ▲             ▲
              └─────────────┼─────────────┘
                    kernel hash distributes
                    incoming connections
```

1. **Parent** forks N worker processes. **No socket creation in the parent.**
2. **Each child** creates its own socket with `SO_REUSEPORT`, binds to the same address:port,
   and listens.
3. **Each child** creates its own Hub and starts accepting on its own socket.
4. The **kernel** distributes incoming connections across the N sockets using a hash of the
   connection's 4-tuple (source IP, source port, destination IP, destination port).
5. **Parent** enters a supervisor loop, monitoring children and handling signals.

#### The Hash Distribution

The kernel computes a hash from the incoming connection's 4-tuple and uses it to select which
socket (and thus which worker) receives the connection. This is done at the TCP layer — when
the SYN packet arrives, the kernel picks a socket and places the connection in that socket's
accept queue.

The hash provides **statistically even distribution** across workers. If you have 4 workers and
1000 connections from diverse source addresses, each worker gets roughly 250 connections.

**Edge case:** If all traffic comes from one source IP (e.g., a single load balancer in front),
the source port is the only varying component in the hash. Since source ports are typically
sequential, the distribution may still be reasonably even, but it's not guaranteed.

#### The Connection Drop Problem

This is the most significant drawback of SO_REUSEPORT. When a worker dies or restarts, its
socket closes, and the hash mapping changes. The kernel re-distributes future connections
across the remaining sockets. But connections that are **mid-handshake** (the TCP 3-way
handshake takes a few milliseconds) can get disrupted:

1. Client sends SYN → kernel hashes to Worker 2's socket, places in Worker 2's SYN queue
2. Worker 2 crashes and its socket closes
3. Client sends ACK (completing handshake) → kernel hashes to Worker 3's socket
4. Worker 3 doesn't know about this handshake → sends RST → connection fails

The client sees a "Connection reset" error and must retry.

**Mitigations:**
- **`tcp_migrate_req`** (kernel 5.14+): A sysctl that migrates pending connections from a
  closed SO_REUSEPORT socket to another socket in the group. Set
  `net.ipv4.tcp_migrate_req = 1`.
- **Behind a reverse proxy:** nginx retries failed upstream connections transparently. The
  end user never sees the error. This is theframework's deployment model.
- **Rolling restart:** Start the new worker before stopping the old one. The old worker's
  socket remains open until all its in-flight connections finish.

#### Pros

- **Zero accept contention.** Each worker has its own accept queue. No shared lock.
- **Even distribution.** The kernel hash provides statistically balanced distribution across
  workers, regardless of load level.
- **Better latency under high connection rates.** No queueing behind other workers' accept
  calls. Each worker's accept is independent.
- **Per-worker backlog.** Each socket has its own accept queue depth. Total capacity is
  N × backlog instead of 1 × backlog.
- **Scales better with many cores.** 32-worker servers see measurable improvement over shared
  socket. nginx reported 2-3x throughput increase with `reuseport`.
- **Natural io_uring affinity.** Each ring only talks to its own socket. Clean ownership model.

#### Cons

- **Connection drops on worker restart.** Mid-handshake connections can be lost when a worker
  dies or restarts. Mitigated by `tcp_migrate_req` (kernel 5.14+) and by running behind a
  reverse proxy.
- **More complex socket lifecycle.** Each worker must create, bind, listen, and close its own
  socket, plus handle `EADDRINUSE` races during startup.
- **Kernel 3.9+ required.** This is universally available on any modern Linux system (3.9 was
  released in 2013), so not a practical concern.
- **Uneven distribution from single source.** If a single load balancer sends all traffic, the
  hash has less entropy. Normally not a problem behind nginx (which uses multiple upstream
  connections with different source ports).

---

### Option C: Thread-Per-Core

This is the model used by the highest-performance io_uring-native systems: Seastar (ScyllaDB),
Glommio (Datadog), and Monoio (ByteDance).

#### How It Works

```
     Single process
     ┌────────────────────────────────────────────────┐
     │                                                │
     │  Thread 0 (pinned to CPU 0)                    │
     │  ├── Hub + io_uring ring                       │
     │  ├── socket(SO_REUSEPORT) → own accept queue   │
     │  ├── ConnectionPool (4096 slots)               │
     │  └── greenlet tree                             │
     │                                                │
     │  Thread 1 (pinned to CPU 1)                    │
     │  ├── Hub + io_uring ring                       │
     │  ├── socket(SO_REUSEPORT) → own accept queue   │
     │  ├── ConnectionPool (4096 slots)               │
     │  └── greenlet tree                             │
     │                                                │
     │  Thread N (pinned to CPU N)                    │
     │  ├── Hub + io_uring ring                       │
     │  ├── socket(SO_REUSEPORT) → own accept queue   │
     │  ├── ConnectionPool (4096 slots)               │
     │  └── greenlet tree                             │
     │                                                │
     └────────────────────────────────────────────────┘
```

Instead of multiple processes, a single process runs N threads, each pinned to a specific CPU
core. Each thread has its own io_uring ring, its own hub, its own connection pool, and its own
greenlet tree. There is **no shared state** between threads — it's a "shared-nothing" model
within a single process.

#### Why This Is the Fastest Model

- **Zero IPC overhead.** No inter-process communication needed (no pipes, no shared memory, no
  signals between processes). Everything is process-local.
- **Perfect CPU cache locality.** Each thread's data stays on one CPU core. No cache line bouncing
  between cores.
- **Minimal memory overhead.** Code pages, Python interpreter, and imported modules are shared
  (same process). Only per-thread data (hub, pool, ring) is duplicated. Compare with multi-process
  where each process duplicates the entire Python interpreter.
- **No fork() complexity.** No concerns about shared file descriptors, inherited locks, or
  copy-on-write semantics.

#### Why This Doesn't Work Today

**The GIL.** In standard Python, only one thread can execute Python bytecode at a time. Having N
threads each running request handlers means N-1 of them are blocked on the GIL while one runs.
Worse than multi-process, because:

1. Multi-process: N processes × 1 thread = N cores utilized
2. Multi-thread with GIL: 1 process × N threads = 1 core utilized (the others are GIL-blocked)

The io_uring I/O would still be parallel (GIL is released during `io_uring_enter()`), but all
Python handler code would be serialized. You'd get parallel I/O with serial computation — worse
than the prefork model for any workload with non-trivial handler logic.

#### When This Becomes Viable

**Free-threaded Python (3.13+, experimental).** Python 3.13 introduced a build mode that removes
the GIL entirely. With the GIL gone, multiple threads can execute Python bytecode in true
parallel. Granian 2.0 already supports this: when running on free-threaded Python, it uses
threads instead of processes.

For theframework, adopting thread-per-core would require:
- Making the Hub thread-local instead of a global singleton
- Ensuring greenlet works correctly in multi-threaded mode (greenlet 3.x supports this)
- Verifying the monkey patching is thread-safe
- Waiting for free-threaded Python to stabilize

**This is the long-term architecture goal**, but not the right choice for today.

#### Pros

- **Highest possible performance.** No IPC, no fork overhead, perfect cache locality.
- **Lower memory usage.** Shared code pages and Python interpreter.
- **Simpler deployment.** One process to manage instead of N.
- **Proven in high-performance systems.** ScyllaDB serves millions of operations/second with
  this model.

#### Cons

- **Killed by the GIL.** Not viable with standard Python. Requires free-threaded Python 3.13+
  which is experimental and not production-ready.
- **No crash isolation.** A segfault in one thread kills the entire process (all workers). With
  multi-process, only the crashed worker dies.
- **Requires hub refactoring.** The global singleton hub must become thread-local. The monkey
  patching needs thread-safety review.
- **Greenlet thread-safety.** While greenlet 3.x supports multiple threads, each thread has its
  own greenlet tree. Need to verify no global state leaks.

---

### Option D: External Process Manager (No Built-In Multi-Worker)

Instead of implementing multi-worker inside theframework, delegate process management to an
external tool like systemd, supervisor, or Docker.

#### How It Works

```
     systemd / supervisor / docker-compose
     ├── theframework instance 1 (port 8000, SO_REUSEPORT)
     ├── theframework instance 2 (port 8000, SO_REUSEPORT)
     ├── theframework instance 3 (port 8000, SO_REUSEPORT)
     └── theframework instance N (port 8000, SO_REUSEPORT)
```

Each instance is a completely independent process. They all bind to the same port using
SO_REUSEPORT. systemd handles starting, stopping, restarting, and crash recovery.

Example systemd unit:

```ini
# /etc/systemd/system/myapp@.service
[Service]
ExecStart=/usr/bin/python -m myapp --port 8000
Restart=always
RestartSec=1

# Start 4 instances: systemctl start myapp@{1..4}
```

#### Pros

- **Zero implementation effort.** No multi-worker code to write, test, or maintain.
- **Maximum isolation.** Each instance is fully independent. A bug in one cannot affect others.
- **Proven infrastructure.** systemd is rock-solid, battle-tested, and handles edge cases (crash
  loops, resource limits, cgroups) that a custom process manager would need years to match.
- **Simple mental model.** Each process is identical and independent. No parent/child
  relationship to reason about.

#### Cons

- **No coordinated graceful shutdown.** systemd sends SIGTERM to each instance independently.
  There's no "drain all workers, then stop" semantic — each instance handles shutdown on its own
  timeline.
- **No preload optimization.** Each instance loads the Python interpreter and application code
  independently. No copy-on-write sharing of the application's memory. N workers use ~N× the
  memory of a single worker.
- **No centralized metrics.** Each instance has its own metrics. Aggregation must happen at the
  monitoring layer (Prometheus scraping N endpoints instead of one).
- **No automatic worker count tuning.** You must manually configure the number of instances. No
  runtime adjustment based on CPU count.
- **No rolling restart.** Restarting all instances means a brief period where some are down.
  systemd's `RestartSec` helps, but there's no coordination.
- **Requires SO_REUSEPORT.** Without it, only one instance can bind to the port.
- **Operational complexity shifts to deployment.** Instead of one process to manage, operators
  manage N. Configuration changes must be applied to all instances.

---

### Option E: Parent as Acceptor, Pass File Descriptors to Workers

A less common model where the parent process accepts all connections and distributes them to
workers by sending file descriptors over Unix domain sockets.

#### How It Works

```
     ┌──────────────────────┐
     │    Parent Process     │
     │                       │
     │  socket() → bind()    │
     │  listen() → accept()  │──── all connections arrive here
     │                       │
     │  Round-robin or LB:   │
     │  send fd to worker    │
     │  via Unix socket      │
     └───┬──────┬──────┬────┘
         │      │      │     sendmsg(SCM_RIGHTS) per connection
         ▼      ▼      ▼
     ┌──────┐┌──────┐┌──────┐
     │ W1   ││ W2   ││ WN   │
     │      ││      ││      │
     │ recv ││ recv ││ recv │
     │ fd   ││ fd   ││ fd   │
     │      ││      ││      │
     │handle││handle││handle│
     └──────┘└──────┘└──────┘
```

The parent is the only process that calls `accept()`. It receives new connections and sends the
file descriptor to a worker process via `sendmsg()` with `SCM_RIGHTS` (the Unix mechanism for
passing file descriptors between processes over a Unix domain socket).

#### Pros

- **Perfect load balancing.** The parent can implement any distribution strategy: round-robin,
  least-connections, weighted, etc.
- **No thundering herd.** Only one process accepts.
- **No SO_REUSEPORT needed.** Only one socket.
- **The parent can pre-inspect connections.** It could read the first bytes (e.g., TLS SNI or
  HTTP Host header) to route to different worker pools.

#### Cons

- **Significant per-connection overhead.** Every connection requires a `sendmsg()` + `recvmsg()`
  round-trip between parent and worker. This adds microseconds per connection and syscall
  overhead.
- **Parent is a bottleneck.** All connections funnel through one process. Under high connection
  rates, the parent's accept + distribute loop becomes the limiting factor.
- **Complex implementation.** Unix domain socket pair management, fd passing, worker registration,
  error handling for dead workers, etc.
- **io_uring complication.** The parent would need its own io_uring ring for accepting. The
  accepted fd would need to be registered in the worker's connection pool after transfer, but
  the fd number might differ between processes (the kernel assigns a new fd number in the
  receiving process).
- **Rarely used in practice.** nginx, gunicorn, uvicorn, and Granian all use shared socket or
  SO_REUSEPORT instead.

---

## Comparison Matrix

| | Inherited Socket (A) | SO_REUSEPORT (B) | Thread-Per-Core (C) | External Manager (D) | FD Passing (E) |
|---|---|---|---|---|---|
| **Implementation effort** | Low | Low-Medium | High | None | High |
| **Accept contention** | Slight (io_uring mitigates) | None | None | None (SO_REUSEPORT) | None |
| **Load distribution** | LIFO bias (uneven) | Hash-based (even) | Hash-based (even) | Hash-based (even) | Perfect (custom) |
| **Connection loss on restart** | None | Possible (mitigated) | N/A (single process) | Possible | None |
| **Memory overhead** | N × process | N × process | 1 × process | N × process | N × process |
| **Crash isolation** | Yes (per-worker) | Yes (per-worker) | No (shared process) | Yes (per-instance) | Yes (per-worker) |
| **Centralized metrics** | Yes (parent) | Yes (parent) | Yes (in-process) | No | Yes (parent) |
| **Works with GIL** | Yes | Yes | No | Yes | Yes |
| **Graceful shutdown** | Coordinated | Coordinated | Coordinated | Independent | Coordinated |
| **CoW memory sharing** | Yes (preload) | Limited | N/A | No | Yes (preload) |
| **Minimum kernel** | 5.10+ (io_uring) | 5.10+ | 5.10+ | 5.10+ | 5.10+ |
| **Per-connection overhead** | None | None | None | None | sendmsg + recvmsg |
| **Used by** | gunicorn, nginx | Granian, nginx | Seastar, Glommio | Many small apps | HAProxy (partially) |

---

## Recommendation

**Option A (Prefork + Inherited Socket) as the initial implementation.**

**Option B (SO_REUSEPORT) as a follow-up enhancement.**

**Option C (Thread-Per-Core) as the long-term architecture goal for free-threaded Python.**

### Why Start with Option A

1. **Lowest risk, fastest to ship.** The core I/O layer (hub, ring, pool, greenlets) needs zero
   changes. Multi-worker is entirely Python-side orchestration: fork, run existing `hub_run()` in
   each child, supervise.

2. **No connection loss.** The shared accept queue means connections are never dropped during
   worker restarts. For a framework getting its first multi-worker support, this simplicity
   matters.

3. **Battle-tested pattern.** Gunicorn has run this model for 15+ years in production. The
   failure modes are well-understood and well-documented.

4. **The accept contention "problem" is theoretical for our use case.** Behind nginx, the
   connection rate to the backend is modest (nginx maintains a pool of upstream connections,
   typically 10-100 per worker). The LIFO bias is irrelevant at these rates.

### Why Add Option B Later

1. **Better distribution at scale.** When running 16+ workers on a large machine, SO_REUSEPORT's
   hash-based distribution becomes measurably more efficient than the shared queue.

2. **Future multi-process socket isolation.** Each worker having its own socket simplifies
   per-worker backpressure, metrics, and configuration.

3. **Easy to add on top of Option A.** The parent creates the socket with SO_REUSEPORT, children
   also create their own sockets with SO_REUSEPORT. The rest of the orchestration is identical.

4. **The connection-drop concern is largely irrelevant** behind nginx (transparent retries) and
   with `tcp_migrate_req` on modern kernels.

### Why Keep Option C in Mind

1. **The performance ceiling is significantly higher.** Zero IPC, perfect cache locality, minimal
   memory overhead.

2. **Free-threaded Python is coming.** Python 3.13 has experimental support. By the time
   theframework is mature, free-threaded Python may be stable.

3. **The hub design already works per-thread.** Making it thread-local instead of global is a
   small refactor. Greenlet 3.x already supports multi-threaded use.

### Why Not Option D

External process management is a valid deployment strategy, and users can already do this today
(run N instances with SO_REUSEPORT). But a built-in multi-worker model provides:

- Coordinated graceful shutdown
- Automatic crash recovery with loop detection
- Centralized metrics
- Copy-on-write memory sharing with preloaded applications
- A single entry point (`theframework serve --workers 4`)

### Why Not Option E

FD passing adds per-connection overhead and implementation complexity with no meaningful benefit
over shared-socket or SO_REUSEPORT. The parent-as-bottleneck problem makes it worse at scale,
not better. No modern Python server uses this model.

---

## Implementation Sketch (Option A)

This is a high-level outline of what the implementation would look like. Not a detailed spec.

### Parent Process

```python
def serve(handler, host, port, workers=None):
    if workers is None:
        workers = os.cpu_count() or 1

    # Phase 1: Create and bind the listening socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.bind((host, port))
    sock.listen(1024)
    listen_fd = sock.fileno()

    if workers == 1:
        # Single-worker mode: no fork, run directly (current behavior)
        _run_worker(listen_fd, handler)
        return

    # Phase 2: Fork workers
    children = {}
    for i in range(workers):
        pid = os.fork()
        if pid == 0:
            # Child process
            _run_worker(listen_fd, handler)
            os._exit(0)
        children[pid] = i

    # Phase 3: Supervisor loop
    # ... handle signals, monitor children, respawn on crash ...
```

### Worker Process (Child)

```python
def _run_worker(listen_fd, handler):
    # Install signal handlers
    signal.signal(signal.SIGTERM, _handle_shutdown)

    # Create hub and run (existing code path)
    hub_run(listen_fd, lambda: _run_acceptor(listen_fd, handler))
```

### What Changes in Existing Code

1. **`server.py`:** Split `serve()` into socket creation (parent) and hub execution (worker).
   Add fork loop and supervisor.
2. **Signal handling:** Parent catches SIGINT/SIGTERM, forwards to children. Children catch
   SIGTERM and call `hub_stop()`.
3. **Nothing in Zig changes.** Hub, ring, pool, greenlets — all work as-is.

### What Gets Added

1. **Worker supervisor:** Monitor children with `os.waitpid()`. Respawn crashed workers.
   Detect crash loops (same worker crashing within N seconds).
2. **Graceful shutdown:** On SIGTERM, stop accepting new connections, drain in-flight
   requests, then exit.
3. **Configuration:** `--workers N` option (default: CPU count).

---

## References and Prior Art

- **nginx architecture:** https://blog.nginx.org/blog/inside-nginx-how-we-designed-for-performance-scale
- **nginx SO_REUSEPORT:** https://www.f5.com/company/blog/nginx/socket-sharding-nginx-release-1-9-1
- **Gunicorn design:** https://docs.gunicorn.org/en/stable/design.html
- **Granian architecture:** https://github.com/emmett-framework/granian
- **Seastar shared-nothing:** https://seastar.io/shared-nothing/
- **Glommio thread-per-core:** https://www.datadoghq.com/blog/engineering/introducing-glommio/
- **io_uring and fork:** https://man7.org/linux/man-pages/man2/io_uring_setup.2.html
- **SO_REUSEPORT kernel docs:** https://lwn.net/Articles/542629/
- **SO_REUSEPORT connection drops:** https://blog.cloudflare.com/the-sad-state-of-linux-socket-balancing/
- **tcp_migrate_req:** https://lwn.net/Articles/837506/
- **Tokio work-stealing scheduler:** https://tokio.rs/blog/2019-10-scheduler
- **io_uring worker pool:** https://blog.cloudflare.com/missing-manuals-io_uring-worker-pool/
