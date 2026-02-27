# HTTP Server Benchmarking Plan

Comprehensive guide for benchmarking `the-framework` fairly, reproducibly, and
with statistical rigor. This document covers hardware setup, OS tuning, tooling,
scenarios, metrics, analysis, and comparison methodology.

---

## Table of Contents

1. [Philosophy](#1-philosophy)
2. [Hardware & Environment Setup](#2-hardware--environment-setup)
3. [OS & Kernel Tuning](#3-os--kernel-tuning)
4. [Benchmarking Tools](#4-benchmarking-tools)
5. [The Coordinated Omission Problem](#5-the-coordinated-omission-problem)
6. [Benchmark Scenarios](#6-benchmark-scenarios)
7. [Metrics to Collect](#7-metrics-to-collect)
8. [Statistical Methodology](#8-statistical-methodology)
9. [Fair Comparison Methodology](#9-fair-comparison-methodology)
10. [Profiling & Deep Analysis](#10-profiling--deep-analysis)
11. [io_uring-Specific Considerations](#11-io_uring-specific-considerations)
12. [Continuous Benchmarking](#12-continuous-benchmarking)
13. [Reporting & Presentation](#13-reporting--presentation)
14. [Execution Checklist](#14-execution-checklist)

---

## 1. Philosophy

### What benchmarks actually measure

A benchmark does not measure "how fast the server is." It measures the behavior
of a specific system under a specific workload in a specific environment. Change
any variable and you get different numbers. The goal is not to produce the biggest
number — it is to **understand system behavior** and make **informed engineering
decisions**.

### Core principles

1. **Measure what matters.** Throughput without latency is meaningless. A server
   doing 1M req/s with 5s p99 latency is worse than one doing 500K req/s with
   1ms p99. Always report both.

2. **Latency distributions, not averages.** Averages hide tail behavior. A server
   with 1ms average and 10s p99 will be reported as "1ms" by anyone measuring
   averages. Use percentiles: p50, p75, p90, p95, p99, p99.9, max.

3. **Reproducibility over raw numbers.** A benchmark that can't be reproduced is
   a benchmark that can't be trusted. Document every parameter, pin every version,
   script every step.

4. **The client must not be the bottleneck.** If the load generator saturates
   before the server, you're benchmarking the client. Verify this explicitly.

5. **Warm up before measuring.** JIT compilation, TCP slow start, buffer pool
   initialization, THP defrag, CPU frequency ramp-up — many systems need time
   to reach steady state. Always discard the warmup period.

6. **One variable at a time.** When comparing, change exactly one thing. Same
   hardware, same OS, same workload, same concurrency, same network path. The
   only difference is the server under test.

---

## 2. Hardware & Environment Setup

### 2.1 Machine topology

**Ideal setup: two dedicated bare-metal machines connected by a direct cable or
a dedicated switch.**

```
┌──────────────┐    10GbE / 25GbE    ┌──────────────┐
│  Client Box  │◄────────────────────►│  Server Box  │
│  (load gen)  │   direct cable or    │  (SUT)       │
│              │   dedicated switch   │              │
└──────────────┘                      └──────────────┘
```

- **Why separate machines:** Loopback (`127.0.0.1`) bypasses the network stack
  almost entirely — no checksums, no segmentation, no NIC interrupts, no real
  TCP behavior. Results on loopback are not representative of production. Loopback
  also means client and server compete for the same CPU, memory bandwidth, and
  cache.

- **When loopback is acceptable:** Early development, smoke tests, regression
  detection (relative changes), and scenarios where network I/O is not the
  bottleneck (e.g., CPU-bound request processing). Always label loopback results
  clearly.

- **Cloud VMs:** Acceptable for relative comparisons (A vs B on the same VM type)
  but not for absolute numbers. Noisy neighbors, shared NICs, hypervisor
  overhead, and CPU steal make numbers unreliable run-to-run. If using cloud,
  use dedicated/metal instances and run many iterations to average out noise.

### 2.2 CPU configuration

```bash
# 1. Set CPU governor to performance (disable frequency scaling)
for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    echo performance | sudo tee "$cpu"
done

# 2. Disable turbo boost (prevents thermal-driven frequency variation)
# Intel:
echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo
# AMD:
echo 0 | sudo tee /sys/devices/system/cpu/cpufreq/boost

# 3. Verify all cores run at the same frequency
cat /proc/cpuinfo | grep "cpu MHz"
```

**Why this matters:** Turbo boost dynamically changes CPU frequency based on
thermal headroom. A benchmark that starts cool gets higher clocks than one that
starts after a previous run. Locking the frequency eliminates this variable.

### 2.3 CPU isolation and pinning

```bash
# Reserve CPUs 2-7 for the server (adjust for your topology)
# Add to kernel command line (GRUB_CMDLINE_LINUX):
#   isolcpus=2-7 nohz_full=2-7 rcu_nocbs=2-7

# Pin the server process to isolated CPUs
taskset -c 2-7 ./server

# Pin the client to non-isolated CPUs (if same machine, which you should avoid)
taskset -c 0-1 wrk2 ...
```

**Why:** `isolcpus` removes CPUs from the general scheduler. No kernel threads,
no cron jobs, no random workloads will run on those cores. `nohz_full` disables
timer ticks on those cores (no scheduler interrupts). `rcu_nocbs` offloads RCU
callbacks to other cores.

### 2.4 NUMA awareness

```bash
# Check NUMA topology
numactl --hardware
# or
lscpu | grep NUMA

# Pin server to a single NUMA node
numactl --cpunodebind=0 --membind=0 ./server

# Ensure the NIC is on the same NUMA node
cat /sys/class/net/eth0/device/numa_node
```

**Why:** Cross-NUMA memory access adds 50-100ns latency per access. If the
server runs on NUMA node 0 but the NIC interrupts land on NUMA node 1, every
packet involves a cross-node hop.

### 2.5 NIC configuration (when using real network)

```bash
# Increase ring buffer sizes
ethtool -G eth0 rx 4096 tx 4096

# Disable interrupt coalescing for lowest latency (increases CPU usage)
ethtool -C eth0 rx-usecs 0 tx-usecs 0

# Or tune coalescing for throughput
ethtool -C eth0 adaptive-rx on adaptive-tx on

# Set RSS (Receive Side Scaling) to distribute across server cores
ethtool -L eth0 combined 6  # match server core count

# Pin NIC IRQs to server cores
# Find IRQs: cat /proc/interrupts | grep eth0
# Set affinity: echo <cpu_mask> > /proc/irq/<N>/smp_affinity
```

### 2.6 Hyperthreading

Disable it for benchmark runs. HT siblings share execution resources (ALUs,
cache, branch predictors). A server "using 4 cores" on an HT system has
different performance characteristics than one using 4 physical cores. HT adds
variance and makes results harder to interpret.

```bash
# Disable HT at runtime
echo 0 | sudo tee /sys/devices/system/cpu/cpu<N>/online  # for each HT sibling
# Or disable in BIOS for consistency
```

**Exception:** If your production environment uses HT, benchmark with it on.
Match the benchmark environment to production.

---

## 3. OS & Kernel Tuning

### 3.1 Network stack tuning

```bash
# File descriptor limits
ulimit -n 1000000
# Or permanently in /etc/security/limits.conf:
#   * soft nofile 1000000
#   * hard nofile 1000000

# TCP tuning
sudo sysctl -w net.core.somaxconn=65535           # listen backlog
sudo sysctl -w net.core.netdev_max_backlog=65535   # NIC rx queue
sudo sysctl -w net.ipv4.tcp_max_syn_backlog=65535  # SYN queue
sudo sysctl -w net.ipv4.ip_local_port_range="1024 65535"  # client ports
sudo sysctl -w net.ipv4.tcp_tw_reuse=1             # reuse TIME_WAIT ports
sudo sysctl -w net.ipv4.tcp_fin_timeout=10          # faster TIME_WAIT expiry

# Buffer sizes (important for large transfers)
sudo sysctl -w net.core.rmem_max=16777216
sudo sysctl -w net.core.wmem_max=16777216
sudo sysctl -w net.ipv4.tcp_rmem="4096 87380 16777216"
sudo sysctl -w net.ipv4.tcp_wmem="4096 65536 16777216"

# Disable SYN cookies (they add overhead, and we control the environment)
sudo sysctl -w net.ipv4.tcp_syncookies=0

# Enable TCP Fast Open (reduces connection establishment latency)
sudo sysctl -w net.ipv4.tcp_fastopen=3
```

### 3.2 Memory configuration

```bash
# Disable swap (prevents unpredictable latency spikes from page faults)
sudo swapoff -a

# Disable Transparent Huge Pages (THP) — defragmentation causes latency spikes
echo never | sudo tee /sys/kernel/mm/transparent_hugepage/enabled
echo never | sudo tee /sys/kernel/mm/transparent_hugepage/defrag

# Optionally: use explicit huge pages if your workload benefits
# sudo sysctl -w vm.nr_hugepages=1024
```

### 3.3 Reduce system noise

```bash
# Stop unnecessary services
sudo systemctl stop cron atd snapd unattended-upgrades

# Disable kernel audit
sudo auditctl -e 0

# Disable NMI watchdog (saves a CPU and avoids periodic interrupts)
sudo sysctl -w kernel.nmi_watchdog=0

# Move kernel RCU callbacks off benchmark CPUs (if using isolcpus)
# Already handled by rcu_nocbs boot param

# Verify nothing else runs on the benchmark CPUs
ps -eo pid,psr,comm | grep -E "^\s+[0-9]+\s+(2|3|4|5|6|7)\s"
```

### 3.4 io_uring specific kernel settings

```bash
# Increase max io_uring entries (default may be too low for high-concurrency tests)
sudo sysctl -w kernel.io_uring_max_entries=32768

# Ensure the user can create io_uring instances
# Check: cat /proc/sys/kernel/io_uring_disabled  (should be 0)

# For SQPOLL mode (if testing it): needs CAP_SYS_ADMIN or:
sudo sysctl -w kernel.io_uring_sqpoll_threads=1
```

---

## 4. Benchmarking Tools

### 4.1 Primary tool: wrk2

**wrk2** (by Gil Tene) is the recommended primary tool. It is a fork of wrk that
fixes the coordinated omission problem (see Section 5).

```bash
# Install
git clone https://github.com/giltene/wrk2.git
cd wrk2 && make

# Basic usage — constant request rate
wrk2 -t4 -c100 -d30s -R10000 --latency http://server:8080/

# Parameters:
#   -t4          4 threads (match to client CPU cores)
#   -c100        100 open connections
#   -d30s        30 second duration
#   -R10000      Target rate: 10,000 requests/second
#   --latency    Print full latency distribution
```

**Key difference from wrk:** wrk is "open loop" in theory but "closed loop"
in practice — it sends the next request only after the previous one completes.
If a response takes 1s, wrk silently reduces the request rate for that second
but reports latency as if requests were sent on time. This hides queuing delay.

wrk2 maintains a **constant request rate** regardless of response time. If
responses are slow, requests queue up, and the measured latency reflects the
actual user experience (including queue wait time). This is correct.

### 4.2 Secondary tools

#### wrk (original)

```bash
wrk -t4 -c100 -d30s http://server:8080/
```

Useful for finding **maximum throughput** (closed-loop saturation). Not useful
for latency measurement due to coordinated omission. Use wrk to find the
throughput ceiling, then use wrk2 at various rates below that ceiling to
measure latency.

#### hey

```bash
# Install
go install github.com/rakyll/hey@latest

# Usage
hey -n 100000 -c 200 http://server:8080/
```

Simple, produces a latency histogram. Suffers from coordinated omission.
Good for quick smoke tests.

#### vegeta

```bash
# Install
go install github.com/tsenart/vegeta@latest

# Usage — constant rate attack
echo "GET http://server:8080/" | vegeta attack -rate=10000/s -duration=30s | vegeta report
echo "GET http://server:8080/" | vegeta attack -rate=10000/s -duration=30s | vegeta plot > plot.html
```

**Strengths:** Constant-rate mode (like wrk2), outputs HDR histograms, can plot
latency over time, supports reading targets from a file (mixed URLs/methods).
Excellent for rate-sweep experiments. Written in Go.

#### h2load (HTTP/2 benchmarking)

```bash
# Part of nghttp2 package
h2load -n100000 -c100 -t4 https://server:8080/
```

Use when benchmarking HTTP/2 specifically. Supports multiplexed streams.

#### bombardier

```bash
bombardier -c 100 -d 30s -l http://server:8080/
```

Prints latency percentiles. Go-based, easy to install. Closed-loop like wrk.

#### k6

```bash
k6 run script.js
```

Scriptable in JavaScript. Better for complex scenarios (multi-step flows, mixed
workloads, think time). Heavier than wrk2 but more flexible. Has built-in
coordinated omission handling when using `constant-arrival-rate` executor.

#### TechEmpower Framework Benchmarks (TFB)

Not a tool but a **methodology and harness**. The gold standard for framework
comparisons. Uses wrk for throughput measurement with a well-defined set of
scenarios (plaintext, JSON, database, fortune). Results are at
[tfb-status.techempower.com](https://www.techempower.com/benchmarks/).

**We should eventually submit the-framework to TFB.** In the meantime, replicate
their scenarios locally for comparability.

### 4.3 Tool selection matrix

| Goal                          | Tool              | Loop type   |
|-------------------------------|-------------------|-------------|
| Find max throughput           | wrk               | Closed      |
| Measure latency at target QPS | wrk2 or vegeta    | Open        |
| Quick smoke test              | hey or bombardier | Closed      |
| Complex multi-step scenario   | k6                | Configurable|
| HTTP/2 benchmarking           | h2load            | Closed      |
| Framework comparison          | TFB methodology   | Closed      |
| Latency over time analysis    | vegeta            | Open        |

**Always use at least two tools** to cross-validate results. If wrk and wrk2
disagree on throughput, investigate — one of them is misconfigured or bottlenecked.

---

## 5. The Coordinated Omission Problem

This is the single most important concept in latency benchmarking. Coined by
Gil Tene (Azul Systems), it describes a systematic measurement error present in
most benchmarking tools.

### The problem

In a **closed-loop** benchmark (wrk, hey, ab), the client waits for a response
before sending the next request. If one request takes 1 second instead of 1ms:

- The client sends 999 fewer requests during that second
- Those 999 "would-have-been" requests are never measured
- They would have experienced 1ms, 2ms, ..., 999ms of queuing delay
- The reported latency distribution is missing its worst data points

This is like a hospital that only measures wait times for patients who've already
been seen. The ones still in the waiting room are omitted from the statistics.

### The magnitude

Consider a server that processes 99.99% of requests in 1ms but stalls for 1
second once per 10,000 requests:

| Metric | Measured (wrk) | Actual (wrk2) |
|--------|---------------|---------------|
| p50    | 1ms           | 1ms           |
| p99    | 1ms           | 500ms         |
| p99.9  | 1ms           | 999ms         |
| max    | 1000ms        | 1000ms        |

The closed-loop tool reports p99 = 1ms. The actual p99 is 500ms. **Off by 500x.**

### The solution

Use an **open-loop** benchmark tool that maintains a constant request rate
regardless of response times:

1. **wrk2** — sends requests on a fixed schedule (Poisson distribution)
2. **vegeta** — `attack -rate=N/s` mode
3. **k6** — `constant-arrival-rate` executor

### How we use this

1. Find max throughput with wrk (closed-loop). Call it T_max.
2. Run wrk2 at 10%, 25%, 50%, 75%, 90%, 95% of T_max.
3. Report latency distributions at each rate.
4. The point where p99 starts degrading is the **practical capacity**.

This "rate sweep" gives a far more useful picture than a single throughput number.

---

## 6. Benchmark Scenarios

### 6.1 Micro-benchmarks (component isolation)

These measure individual components in isolation. Useful for development and
optimization, not for marketing.

#### M1: Plaintext (TFB-compatible)

```
Request:  GET /plaintext HTTP/1.1
Response: "Hello, World!" (13 bytes)
Headers:  Content-Type: text/plain, Content-Length: 13, Date, Server
```

Measures: Framework overhead, connection handling, response serialization.
This is the "speed of light" for the framework — the minimum overhead per request.

**TFB specification:** Response must include `Date` header, `Server` header,
and `Content-Type: text/plain`. Body must be exactly `Hello, World!`.

#### M2: JSON serialization (TFB-compatible)

```
Request:  GET /json HTTP/1.1
Response: {"message":"Hello, World!"} (27 bytes)
Headers:  Content-Type: application/json, Content-Length: 27, Date, Server
```

Measures: JSON serialization overhead on top of M1.

#### M3: Routing overhead

```
Requests: GET /a, GET /b/1, GET /c/1/d/2, GET /e?x=1&y=2 (cycle through)
Response: 200 OK with route identifier in body
```

Register 100 routes with a mix of static, parameterized, and query string
patterns. Measures router lookup performance under realistic route table sizes.

#### M4: Header parsing stress

```
Request:  GET / with 50 headers (total ~4KB header block)
Response: 200 OK minimal
```

Measures: HTTP parser performance with realistic header counts. Some frameworks
degrade significantly with many headers.

#### M5: Request body parsing

```
Request:  POST /echo with Content-Length body
Sizes:    0B, 1KB, 10KB, 100KB, 1MB
Response: Echo the body back
```

Measures: Body I/O performance, buffer management, memory allocation patterns.

### 6.2 Application-level benchmarks

These simulate realistic workloads and are more meaningful for comparisons.

#### A1: API endpoint (JSON in, JSON out)

```
Request:  POST /api/users with {"name": "test", "email": "test@example.com"}
Response: {"id": 1, "name": "test", "email": "test@example.com", "created": "..."}
```

Measures: Full request-response cycle with body parsing and serialization. No
database, but includes realistic Python handler code.

#### A2: Mixed workload

```
60% GET  /api/items       (list, ~1KB response)
20% GET  /api/items/{id}  (detail, ~200B response)
10% POST /api/items       (create, 100B request body, 200B response)
10% GET  /health          (tiny response)
```

Measures: Behavior under realistic traffic patterns. Different code paths,
different response sizes, different handler complexity.

#### A3: Keep-alive vs. new connections

```
Same workload as M1, but:
- Variant A: reuse connections (keep-alive, default in HTTP/1.1)
- Variant B: close after each request (Connection: close)
- Variant C: close and reconnect every 100 requests
```

Measures: Connection establishment overhead, TCP handshake cost, io_uring
accept performance.

#### A4: Large response streaming

```
Request:  GET /stream
Response: 1MB of data, chunked transfer encoding
```

Measures: Streaming I/O, buffer management, chunked encoding overhead.

### 6.3 Concurrency sweep

For each scenario above, vary concurrency systematically:

| Level          | Connections | Represents                                |
|----------------|-------------|-------------------------------------------|
| Minimal        | 1           | Serial baseline, no concurrency effects   |
| Low            | 8           | Small API service                         |
| Medium         | 64          | Medium web service                        |
| High           | 256         | Busy API gateway                          |
| Very high      | 1,024       | High-traffic service                      |
| Extreme        | 10,000      | C10K scenario                             |
| Ridiculous     | 100,000     | C100K stress test (if hardware supports)  |

**Plot throughput and latency vs. concurrency.** The shape of this curve reveals
the server's scaling behavior:

- Linear scaling → good parallelism, no contention
- Sublinear → some contention (locks, shared state, cache pressure)
- Cliff → resource exhaustion (fd limits, memory, SQ/CQ overflow)
- Inverse → thrashing (too many greenlets, GC pressure, context switch storm)

### 6.4 Rate sweep (latency under load)

For the plaintext and JSON scenarios, at a fixed concurrency (e.g., 256
connections), sweep the request rate:

```
Rate: 10%, 25%, 50%, 60%, 70%, 80%, 85%, 90%, 95%, 100% of max throughput
```

At each rate, collect the full latency distribution (p50 through p99.9).

**Plot latency percentiles vs. rate.** The "hockey stick" inflection point
where tail latency spikes is the practical capacity of the system. This is
far more useful than a single "max requests/sec" number.

### 6.5 Sustained load (stability test)

```
Run: M1 (plaintext) at 70% of max throughput for 1 hour
Collect: latency over time (1-second buckets), memory usage, CPU temperature
```

Reveals: memory leaks, GC pauses, thermal throttling, fd leaks, buffer pool
exhaustion. Any degradation over time indicates a bug.

---

## 7. Metrics to Collect

### 7.1 Primary metrics (always report)

| Metric                   | How to collect                                      |
|--------------------------|-----------------------------------------------------|
| Requests/second          | wrk2/vegeta output                                  |
| Latency p50              | wrk2 `--latency` / vegeta report                    |
| Latency p75              | wrk2 / vegeta                                       |
| Latency p90              | wrk2 / vegeta                                       |
| Latency p99              | wrk2 / vegeta                                       |
| Latency p99.9            | wrk2 / vegeta                                       |
| Latency max              | wrk2 / vegeta                                       |
| Error count & rate       | wrk2 non-2xx/3xx count, vegeta error report         |
| Transfer rate (bytes/s)  | wrk2 / vegeta output                                |

### 7.2 System metrics (collect for deep analysis)

| Metric                     | How to collect                                  |
|----------------------------|-------------------------------------------------|
| CPU utilization (%)        | `mpstat -P ALL 1` or `perf stat`                |
| CPU user vs system vs idle | `mpstat 1`                                      |
| Context switches/sec       | `vmstat 1` or `perf stat -e context-switches`   |
| Voluntary vs involuntary   | `pidstat -w 1`                                  |
| Memory RSS                 | `ps -o rss -p <pid>` or `/proc/<pid>/status`    |
| Memory growth over time    | Sample RSS every second during the run          |
| Network throughput         | `sar -n DEV 1` or `ip -s link show`             |
| Interrupt rate             | `cat /proc/interrupts` before/after              |
| Syscall count              | `perf stat -e raw_syscalls:sys_enter`           |
| io_uring SQE submissions   | `bpftrace` or `perf trace`                      |
| io_uring CQE completions   | `bpftrace` or `perf trace`                      |
| TCP retransmissions        | `ss -ti` or `netstat -s`                        |
| TIME_WAIT socket count     | `ss -s`                                         |
| File descriptors in use    | `ls /proc/<pid>/fd | wc -l`                     |

### 7.3 Collection script outline

```bash
#!/bin/bash
# Collect system metrics during a benchmark run
SERVER_PID=$1
DURATION=$2
OUTDIR=$3

mkdir -p "$OUTDIR"

# Background metric collectors
mpstat -P ALL 1 "$DURATION" > "$OUTDIR/cpu.txt" &
vmstat 1 "$DURATION" > "$OUTDIR/vmstat.txt" &
pidstat -p "$SERVER_PID" -w 1 "$DURATION" > "$OUTDIR/ctxsw.txt" &

# Sample RSS every second
for i in $(seq 1 "$DURATION"); do
    echo "$(date +%s) $(grep VmRSS /proc/$SERVER_PID/status | awk '{print $2}')" \
        >> "$OUTDIR/memory.txt"
    sleep 1
done &

# Sample fd count every 5 seconds
for i in $(seq 1 $((DURATION / 5))); do
    echo "$(date +%s) $(ls /proc/$SERVER_PID/fd 2>/dev/null | wc -l)" \
        >> "$OUTDIR/fds.txt"
    sleep 5
done &

wait
```

---

## 8. Statistical Methodology

### 8.1 Number of runs

**Minimum 5 runs per configuration.** Preferably 10. Report the median of the
5 runs, plus the min and max to show the spread.

Why not average? Averages are distorted by outliers. If one run has a kernel
hiccup, the average is wrong. The median is robust to single-run anomalies.

### 8.2 Warmup

Each run consists of:

1. **Warmup phase (10-30s):** Send traffic at the target rate. Discard all
   measurements. This allows:
   - CPU frequency ramp-up (if not pinned)
   - TCP slow start on all connections
   - Python bytecode caching / greenlet pool warming
   - io_uring buffer pool initialization
   - Kernel socket buffer allocation

2. **Measurement phase (30-60s):** Collect all metrics.

3. **Cooldown (5s):** Let connections drain. Don't overlap with the next run.

wrk2 doesn't have a built-in warmup, so run it twice: first run is warmup
(discard results), second run is measurement.

### 8.3 Steady-state detection

Before accepting a measurement run, verify steady state:

- Plot throughput over time (1-second buckets). It should be flat, not ramping
  up or degrading.
- If throughput varies by more than ±5% across the measurement window, extend
  the warmup or investigate the cause.

### 8.4 Outlier handling

**Do not discard outliers** from the latency distribution. They're real and
they matter. If p99.9 is 100x worse than p99, that's important data.

**Do discard outlier runs** (entire benchmark runs). If one of 10 runs produces
throughput 50% lower than the others, it's likely a system-level anomaly
(thermal throttling, background process, GC storm). Discard it and note it.
Use the remaining 9.

### 8.5 Reporting confidence

For each metric, report:

```
Throughput: 245,812 req/s (median of 10 runs, range: 241,003 — 248,105)
Latency p99: 2.31ms (median of 10 runs, range: 2.12ms — 2.87ms)
```

The range tells the reader how stable the results are. A tight range means
high confidence. A wide range means the environment is noisy or the server
behavior is non-deterministic.

### 8.6 HDR Histogram

Use [HDR Histogram](http://hdrhistogram.org/) for latency recording. It
provides:

- Constant memory regardless of sample count
- Configurable precision (3 significant digits is standard)
- Value quantization that preserves the shape of the distribution
- Merge support (combine histograms from multiple threads)

wrk2 uses HDR Histogram internally. vegeta outputs it. You can also record
custom histograms in the server itself for internal latency breakdowns.

---

## 9. Fair Comparison Methodology

### 9.1 Comparison targets

Primary comparisons (Python ecosystem):

| Framework       | Server     | Concurrency model        |
|-----------------|------------|--------------------------|
| the-framework   | io_uring   | greenlets                |
| uvicorn         | uvloop     | asyncio (single process) |
| gunicorn+uvicorn| uvloop     | asyncio (multi-process)  |
| gunicorn+gevent | gevent     | greenlets                |
| hypercorn       | asyncio    | asyncio                  |
| granian         | Rust       | asyncio                  |

Secondary comparisons (other languages, for context):

| Framework    | Language | Concurrency model     |
|--------------|----------|-----------------------|
| Go net/http  | Go       | goroutines            |
| actix-web    | Rust     | tokio                 |
| axum         | Rust     | tokio                 |
| Bun.serve    | Zig/JS   | event loop            |
| Drogon       | C++      | event loop + threads  |

### 9.2 Fairness rules

1. **Same hardware, same OS, same kernel.** Run all frameworks on the same
   machine (or same VM type), same boot, same tuning.

2. **Same number of CPUs.** If the-framework uses 4 cores, configure uvicorn
   with 4 workers. Don't compare 1 worker vs. 8 workers.

3. **Equivalent application code.** Same handler logic, same response body,
   same routing complexity. No extra middleware in one and not the other.

4. **Best-effort tuning for all.** Don't use default configs — tune each
   framework. Use `--workers`, `--backlog`, connection limits as recommended
   by each framework's documentation. The comparison should be "both at their
   best," not "ours tuned vs. theirs default."

5. **Same HTTP features.** If one framework has keep-alive enabled, all should.
   If one is HTTP/1.1 only, test all in HTTP/1.1 mode.

6. **Pin versions.** Record exact versions of everything:
   ```
   Python 3.13.1, uvicorn 0.30.1, uvloop 0.20.0, gunicorn 22.0.0,
   greenlet 3.1.0, Linux 6.8.0, the-framework 0.2.0
   ```

7. **Same connection count.** The client uses the same `-c` value for all tests.
   Some frameworks perform disproportionately well at certain concurrency levels.
   Sweep the full range.

8. **Verify correctness.** Before benchmarking, verify each framework returns
   the correct response. A framework that returns an empty body is "faster"
   but the comparison is meaningless.

### 9.3 Common mistakes to avoid

- **Comparing single-process to multi-process.** Always normalize. If the
  framework is single-process single-threaded, note it. Compare per-core
  throughput as well as total.

- **Ignoring startup time.** Some frameworks take seconds to start. This
  matters for serverless but not for long-running servers. Note it but don't
  include it in throughput numbers.

- **Benchmarking debug mode.** Ensure all frameworks run in production/release
  mode. Python: no debugger, no `-X dev`. Rust: `--release`. Go: standard
  build. Zig: `ReleaseFast` or `ReleaseSafe`.

- **Accidental logging.** Some frameworks log every request by default. This
  adds massive overhead. Disable logging in all frameworks.

- **DNS resolution in the benchmark path.** Use IP addresses, not hostnames.
  DNS lookups add variable latency.

- **TLS termination.** Don't compare plaintext to TLS. If testing TLS, use
  the same certificate and cipher suite.

### 9.4 What's fair and what's not

**Fair:**
- the-framework (io_uring + greenlets) vs. uvicorn (epoll + asyncio)
- the-framework 4-worker vs. gunicorn 4-worker
- the-framework vs. Go net/http (both optimized for their platform)

**Unfair (but interesting, label clearly):**
- the-framework vs. raw C epoll server (no framework overhead vs. framework)
- the-framework single process vs. nginx (multi-process, decades of optimization)
- the-framework vs. actix-web (Rust has no GC, no interpreter overhead)

**Unfair (don't do):**
- the-framework optimized vs. uvicorn defaults
- the-framework on bare metal vs. uvicorn in Docker
- the-framework with logging off vs. gunicorn with access log on

---

## 10. Profiling & Deep Analysis

### 10.1 Linux perf

```bash
# Record CPU profile while benchmark runs
perf record -g -p $(pgrep server) -- sleep 30

# Generate flamegraph
perf script | stackcollapse-perf.pl | flamegraph.pl > flame.svg

# Count specific events
perf stat -e cycles,instructions,cache-misses,context-switches \
    -p $(pgrep server) -- sleep 30
```

**What to look for:**
- Hot functions (where is time spent?)
- Syscall frequency (are we making unnecessary syscalls?)
- Cache miss rate (data locality issues?)
- Branch mispredictions (unpredictable code paths?)

### 10.2 Flamegraphs

Generate separate flamegraphs for:

1. **On-CPU:** Where the server spends CPU time
2. **Off-CPU:** Where the server blocks (sleeping, waiting for I/O)
3. **Differential:** Compare two configurations (before/after optimization)

```bash
# Off-CPU flamegraph (requires BPF)
# Using bcc's offcputime:
offcputime-bpfcc -p $(pgrep server) 30 > offcpu.stacks
flamegraph.pl --color=io < offcpu.stacks > offcpu-flame.svg
```

### 10.3 Python profiling

```bash
# py-spy: sampling profiler, attaches to running process, low overhead
py-spy record -o profile.svg --pid $(pgrep -f "python.*server")

# py-spy top: live CPU view
py-spy top --pid $(pgrep -f "python.*server")
```

**Caveat with greenlets:** py-spy may not correctly attribute time to greenlets
since they share a single OS thread. The flamegraph will show the hub's stack
most of the time. To profile individual handlers, add manual timing:

```python
import time

def handler(request, response):
    t0 = time.monotonic_ns()
    # ... handler code ...
    elapsed = time.monotonic_ns() - t0
    # report elapsed to metrics system
```

### 10.4 Zig / native profiling

```bash
# perf record with DWARF debug info
perf record -g --call-graph dwarf -p $(pgrep server) -- sleep 30

# Valgrind cachegrind (for cache behavior — HIGH overhead, not for throughput)
valgrind --tool=cachegrind ./server
# Then: cg_annotate cachegrind.out.<pid>

# Instruction-level hotspots
perf annotate -s <function_name>
```

### 10.5 Syscall analysis

```bash
# Count syscalls by type (low overhead with perf)
perf trace -p $(pgrep server) -s 2>trace.txt
# Look at summary at the end: which syscalls, how many, total time

# Verify io_uring is used (not falling back to regular syscalls)
# Should see io_uring_enter, NOT read/write/accept
strace -c -p $(pgrep server)  # WARNING: strace has ~30% overhead
```

**For io_uring specifically:**
- You should see very few syscalls per request (ideally <1 on average)
- Most I/O should happen through `io_uring_enter`
- If you see `read`, `write`, `accept4` syscalls, something is bypassing io_uring

### 10.6 bpftrace for io_uring internals

```bash
# Count io_uring submissions per second
bpftrace -e 'tracepoint:io_uring:io_uring_submit_req { @count = count(); }
             interval:s:1 { printf("SQEs/s: %d\n", @count); clear(@count); }'

# Histogram of CQE completion latency
bpftrace -e '
tracepoint:io_uring:io_uring_submit_req { @start[args->req] = nsecs; }
tracepoint:io_uring:io_uring_complete {
    if (@start[args->req]) {
        @usecs = hist((nsecs - @start[args->req]) / 1000);
        delete(@start[args->req]);
    }
}'
```

### 10.7 Memory profiling

```bash
# Track RSS over time (built into the collection script above)

# Heap profiling with heaptrack (for Zig allocations)
heaptrack ./server &
# ... run benchmark ...
# heaptrack generates a .zst file, analyze with heaptrack_gui or heaptrack_print

# For Python memory:
# tracemalloc (builtin) — add to server startup
# python -X tracemalloc server.py
```

---

## 11. io_uring-Specific Considerations

### 11.1 Kernel version matters

io_uring performance and features vary significantly across kernel versions:

| Kernel | Feature                                          |
|--------|--------------------------------------------------|
| 5.1    | io_uring introduced                              |
| 5.6    | IORING_FEAT_FAST_POLL (async poll for network)   |
| 5.7    | Provided buffers (buffer groups)                 |
| 5.10   | io_uring_register_iowq_max_workers               |
| 5.19   | Multishot accept, IORING_SETUP_COOP_TASKRUN      |
| 6.0    | Multishot recv, send_zc (zero-copy send)          |
| 6.1    | IORING_SETUP_SINGLE_ISSUER optimization           |
| 6.7    | Major performance improvements in CQE posting     |

**Always record the kernel version.** Run benchmarks on the same kernel.
Document minimum kernel requirements.

### 11.2 io_uring tuning knobs to benchmark

Test each of these independently to measure their impact:

| Knob                       | What it does                              | Expected impact     |
|----------------------------|-------------------------------------------|---------------------|
| Queue depth (16-4096)      | SQ/CQ ring size                          | Affects batching    |
| SQPOLL                     | Kernel-side SQ polling (no syscalls)     | -50% syscalls       |
| COOP_TASKRUN               | Cooperative completion processing         | Lower latency       |
| SINGLE_ISSUER              | Single-thread optimization                | Lower overhead      |
| Provided buffer count      | Number of pre-registered recv buffers     | Memory vs. perf     |
| Provided buffer size       | Size of each recv buffer                 | Fragment vs. waste  |
| Batch submit size          | How many SQEs before io_uring_enter      | Throughput vs. lat  |
| Multishot accept vs single | One SQE accepts many vs one              | Accept rate         |

### 11.3 io_uring vs epoll comparison

To demonstrate the value of io_uring, benchmark the selectors fallback backend
(Phase 9.1) against the io_uring backend:

```
Same server, same handlers, same hardware.
Only difference: I/O backend (io_uring vs selectors/epoll).
```

This isolates the io_uring benefit from all other framework design decisions.

### 11.4 Greenlet overhead measurement

Micro-benchmark the cost of greenlet context switches:

```python
import greenlet, time

def ping(n, hub):
    for _ in range(n):
        hub.switch()

hub = greenlet.getcurrent()
worker = greenlet.greenlet(lambda: ping(1_000_000, hub))
t0 = time.monotonic_ns()
for _ in range(1_000_000):
    worker.switch()
elapsed_ns = time.monotonic_ns() - t0
print(f"Greenlet switch: {elapsed_ns / 1_000_000:.0f} ns/switch")
```

Expected: ~100-300ns per switch. Compare this to:
- Python asyncio task switch (~500-1000ns)
- OS thread context switch (~1000-5000ns)
- Go goroutine switch (~200-400ns)

---

## 12. Continuous Benchmarking

### 12.1 Goals

- Detect performance regressions before they reach main
- Track performance trends over time
- Provide a historical record for each commit

### 12.2 Approach

Use a **dedicated benchmark machine** (even a small one) with a CI job that
runs on every PR and nightly on main.

```yaml
# .github/workflows/benchmark.yml (conceptual)
benchmark:
  runs-on: [self-hosted, benchmark]  # dedicated bare-metal runner
  steps:
    - checkout
    - build: zig build -Doptimize=ReleaseFast
    - warmup: wrk2 -R5000 -d10s  # discard
    - benchmark: wrk2 -R10000 -d30s --latency | parse_results.py
    - compare: compare_with_baseline.py --threshold 5%
    - store: store_results.py --commit=$SHA
```

### 12.3 Regression detection

Use the approach from the [Continuous Benchmarking](https://bencher.dev/)
methodology:

1. Maintain a rolling baseline (last 10 runs on main)
2. New result: is it within mean ± 2 standard deviations of the baseline?
3. If outside: flag as regression (or improvement), require manual review
4. Threshold: 5% for throughput, 10% for p99 latency (latency is noisier)

### 12.4 Tools

- **bencher.dev** — hosted continuous benchmarking, free for open source
- **github-action-benchmark** — stores results in gh-pages, plots over time
- **codspeed** — GitHub action, compares PR vs main, comments on PR
- **Custom script** — store JSON in a git-tracked file, plot with matplotlib

---

## 13. Reporting & Presentation

### 13.1 What to include in benchmark reports

Every benchmark report must include:

1. **Environment:**
   - Hardware (CPU model, core count, RAM, NIC)
   - OS and kernel version
   - Zig version, Python version, framework version
   - All tuning parameters applied (sysctl, CPU governor, etc.)

2. **Methodology:**
   - Tool used and exact command line
   - Warmup duration and measurement duration
   - Number of runs and aggregation method (median)
   - Whether results are open-loop (wrk2) or closed-loop (wrk)

3. **Results:**
   - Table: throughput, latency percentiles (p50-p99.9), errors
   - Chart: latency distribution (histogram or CDF)
   - Chart: throughput vs. concurrency
   - Chart: latency vs. request rate (rate sweep)

4. **Analysis:**
   - Where is the bottleneck? (CPU, network, kernel, application)
   - How does it compare? (with methodology caveats)
   - What are the next optimization targets?

### 13.2 Visualization

#### Latency distribution plot (CDF)

```
100% ┤                                          ──────────
 99% ┤                                    ─────╯
 95% ┤                              ─────╯
 90% ┤                        ─────╯
 75% ┤              ─────────╯
 50% ┤      ───────╯
 25% ┤   ──╯
     └────┬──────┬──────┬──────┬──────┬──────┬──────┬──
         0.1   0.2    0.5    1.0    2.0    5.0   10.0  ms
```

Plot multiple frameworks on the same CDF to visually compare latency
distributions.

#### Rate sweep plot (hockey stick)

```
p99 latency
    │
20ms├                                          •
    │                                        •
10ms├                                      •
    │                                    •
 5ms├                                 •
    │                             •
 2ms├               • • • • • •
 1ms├ • • • • • • •
    └──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──
      10  20  30  40  50  60  70  80  90 100  % of max RPS
```

The "elbow" of the hockey stick is the practical capacity.

#### Throughput vs. concurrency

```
RPS
     │                     ────────────────
250K ├                ────╯
     │           ────╯
200K ├       ───╯
     │    ──╯
150K ├  ─╯
     │ ╯
100K ├╯
     │
 50K ├
     └──┬──┬──┬──┬──┬──┬──┬──┬──
        1  8  32 64 128 256 512 1K  connections
```

### 13.3 Anti-patterns in reporting

- **Don't report only max throughput.** Always pair with latency.
- **Don't report only averages.** Use percentiles.
- **Don't compare across different hardware.** Normalize or don't compare.
- **Don't use bar charts for latency.** They hide the distribution. Use CDFs.
- **Don't cherry-pick concurrency levels.** Show the full sweep.
- **Don't hide errors.** If 5% of requests fail at high load, report it.

---

## 14. Execution Checklist

### Phase 1: Setup (one-time)

- [ ] Acquire dedicated benchmark hardware (or identify a stable VM)
- [ ] Install and configure OS with tuning from Section 3
- [ ] Install benchmarking tools: wrk, wrk2, vegeta, hey
- [ ] Install profiling tools: perf, flamegraph scripts, py-spy, bpftrace
- [ ] Create the metric collection scripts from Section 7.3
- [ ] Create result parsing scripts (wrk2 output → JSON → charts)
- [ ] Set up comparison frameworks: install uvicorn, gunicorn, hypercorn, granian
- [ ] Create equivalent "hello world" applications for each framework
- [ ] Verify all frameworks return identical responses (`curl` + `diff`)
- [ ] Document the complete environment in `benchmark-env.md`

### Phase 2: Baseline measurements

- [ ] Run M1 (plaintext) with wrk to find max throughput for each framework
- [ ] Run M1 with wrk2 rate sweep (10%–95% of max) for each framework
- [ ] Run M2 (JSON) with same methodology
- [ ] Run concurrency sweep for M1 (1 to 10K connections) for each framework
- [ ] Collect system metrics (CPU, memory, syscalls) for each run
- [ ] Generate flamegraphs for the-framework at various load levels
- [ ] Store all raw data

### Phase 3: Deep analysis

- [ ] Run M3–M5 micro-benchmarks
- [ ] Run A1–A4 application benchmarks
- [ ] Test io_uring tuning knobs (Section 11.2)
- [ ] Compare io_uring backend vs selectors fallback
- [ ] Measure greenlet switching overhead
- [ ] Run sustained load test (1 hour)
- [ ] Profile and identify top-3 optimization opportunities

### Phase 4: Report

- [ ] Compile results into a report with all visualizations
- [ ] Write analysis: bottlenecks, surprises, optimization opportunities
- [ ] Peer review the methodology and results
- [ ] Publish (if appropriate) with full environment documentation

### Phase 5: Continuous

- [ ] Set up CI benchmark job
- [ ] Configure regression detection (5% threshold)
- [ ] Run benchmarks after each significant optimization
- [ ] Update this plan as methodology evolves
