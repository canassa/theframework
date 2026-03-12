# Q5: Greenlet Switch Failure Cleanup

Fix inconsistent connection/slot state when `py_helper_greenlet_switch()` fails.

---

## Prerequisite: SqeStorage Bug (FIXED)

Before switch-failure tests could work, a separate bug had to be fixed: io_uring SQEs
hold *pointers* to caller-owned memory (`kernel_timespec`, `sockaddr`, etc.). Several
green functions stored these as stack-local variables. When the greenlet switched out,
its C stack was saved to heap and the stack memory was reused by the next greenlet. By
the time `submitAndWait` submitted the SQEs, all pointers read from the same reused
stack address — so concurrent sleeps coalesced to whichever duration was written last.

**Fix**: Added `SqeStorage` union to `OpSlot` (`src/op_slot.zig`) and moved all
SQE-referenced values from the stack into `slot.storage`:
- `greenSleep`: `slot.storage.timespec`
- `greenConnect` / `greenConnectFd`: `slot.storage.sockaddr`
- `greenPollFdTimeout`: `slot.storage.timespec`
- `greenPollMulti`: `slots[0].storage.poll_group` (PollGroup) + `slots[n].storage.timespec` (timeout)

**Regression test**: `tests/test_concurrent_sleep.py` — verifies a 50ms sleep wakes
before a 200ms sleep with >100ms gap.

---

## The Problem

Every `green_*` function follows the same pattern:

```
1. Set up state     (conn.greenlet = current, conn.state = .reading, pending_ops++, active_waits++)
2. Submit SQE       (ring.prepRecv/prepSend/etc.)
3. Switch to hub    (py_helper_greenlet_switch → PyGreenlet_Switch)
4. Read result      (conn.result)
```

If step 3 fails (returns null), the function returns null but leaves step 1's mutations
in place. The SQE from step 2 is already submitted to the kernel and will complete
eventually, producing a CQE that references state pointing to a dead greenlet.

### When does `PyGreenlet_Switch` return NULL?

`PyGreenlet_Switch` returns NULL when a Python exception is set. In practice:

- An exception was thrown into the greenlet by another greenlet (e.g., `greenlet.throw()`)
- The greenlet's parent raised during the switch chain
- A `KeyboardInterrupt` arrived between the switch call and the resume
- Memory allocation failure inside the greenlet library

This is rare under normal operation but becomes increasingly likely under load when
handler exceptions propagate through the greenlet hierarchy.

### Consequences

1. **Dead greenlet resume**: The CQE processing loop (`hub.zig:313`) finds
   `conn.greenlet` pointing to a dead greenlet and calls `py_helper_greenlet_switch()`
   on it. Undefined behavior — crash, silent corruption, or refcount leak.

2. **`active_waits` leak**: The hub loop's exit check (`hub.zig:208`) is
   `if (self.active_waits == 0 and self.ready.items.len == 0) break`. A leaked
   `active_waits` means the hub **never exits** — it blocks forever in
   `submitAndWait(1)` waiting for CQEs that will never arrive (or have already
   been processed without decrementing the counter).

3. **`pending_ops` leak** (connection-based functions): `greenClose` checks
   `conn.pending_ops > 0` to decide whether to cancel-before-close. A leaked
   `pending_ops` means the close path submits a cancel SQE for an operation that
   already completed, then waits for a cancel CQE that may never arrive cleanly.

4. **Connection state stuck**: `conn.state = .reading/.writing` prevents the
   connection from being used for new operations or closed properly.

5. **Greenlet refcount leak**: `current` was obtained via `py_helper_greenlet_getcurrent()`
   (which increfs) and stored in `conn.greenlet`/`slot.greenlet`. If we never decref
   it, the greenlet object is leaked.

---

## Test Strategy

All 11 switch-failure paths are covered by `tests/test_switch_failure.py`. Each test:

1. Starts a hub with a listening socket
2. Schedules a **victim** greenlet that calls the target green function (which blocks)
3. Schedules a **killer** greenlet that sleeps 50ms then calls `victim.throw(Exception("killed"))`
4. Schedules a **checker** greenlet that sleeps 200ms, reads `get_active_waits()`, and stops the hub
5. Asserts `active_waits == 0` (no leak)

**Current baseline**: 10 tests FAIL (active_waits > 0), 1 test PASSES (greenAccept —
its CQE handler unconditionally decrements active_waits, so the leak self-corrects).

Each fix should make exactly one test pass.

**greenAccept test note**: The accept test asserts `active_waits <= 1` rather than
`== 0`, because the accept CQE may not have arrived by the time the checker runs
(200ms). The accept CQE arrives when a connection is made or the listen socket is
closed — neither may happen within the test window. The key invariant is that
`active_waits` must not grow unboundedly; it will settle to 0 once the accept CQE
is processed.

---

## Audit: Every Switch Failure Path in hub.zig

### Category A: No cleanup at all

These functions return null without undoing any state mutations.

| Function | Line | State leaked |
|---|---|---|
| **greenRecv** | 500 | `conn.{greenlet, state=.reading, pending_ops+=1}`, `active_waits+=1`, refcount(current) |
| **greenSend** | 594 | `conn.{greenlet, state=.writing, pending_ops+=1}`, `active_waits+=1`, refcount(current). Note: py_bytes IS decreffed. |
| **greenSendResponse** | 720 | `conn.{greenlet, state=.writing, pending_ops+=1}`, `active_waits+=1`, refcount(current). Note: body_obj IS decreffed. |
| **doRecv** | 831 | `conn.{greenlet, state=.reading, pending_ops+=1}`, `active_waits+=1`, refcount(current) |
| **greenAccept** | 402 | `accept_greenlet`, `accept_pending=true`, `active_waits+=1`, refcount(current) |

### Category B: Partial cleanup (slot released, but active_waits and greenlet ref leaked)

These functions release the op slot (which bumps generation and resets the slot),
but forget to decrement `active_waits` and decref the greenlet. The slot's `reset()`
sets `slot.greenlet = null` without decref — its doc comment says "caller must decref
greenlet before calling reset".

| Function | Line | What's cleaned up | What's leaked |
|---|---|---|---|
| **greenSleep** | 897-899 | `op_slots.release(slot)` | `active_waits+=1`, refcount(current) |
| **greenConnect** | 1024-1029 | `op_slots.release(slot)`, `fd_to_conn=null`, `pool.release(conn)`, `posix.close(fd)` | `active_waits+=1`, refcount(current) |
| **greenConnectFd** | 1117-1119 | `op_slots.release(slot)` | `active_waits+=1`, refcount(current) |
| **greenPollFd** | 1185-1187 | `op_slots.release(slot)` | `active_waits+=1`, refcount(current) |
| **greenPollFdTimeout** | 1507-1509 | `op_slots.release(slot)` | `active_waits+=1`, refcount(current) |
| **greenPollMulti** | 1396-1398 | `cancelAndReleasePollSlots()` | `active_waits+=total_slots`, refcount(current) |

### Category C: Correct (for reference)

The `hub_greenlet orelse` error paths in every function DO clean up correctly.
They serve as the template for the fix.

Example from `greenRecv` lines 492-497:
```zig
conn.greenlet = null;
conn.state = .idle;
conn.pending_ops -= 1;
self.active_waits -= 1;
py.py_helper_decref(current);
return null;
```

---

## How gevent Solves This

gevent faces the exact same problem: greenlets can die while I/O watchers are
registered with the event loop. gevent's solution has three layers:

### Layer 1: Waiter indirection

gevent never stores a greenlet reference directly in the I/O machinery. Instead,
it uses a `Waiter` object that decouples the I/O completion from the greenlet:

```python
class Waiter:
    def get(self):
        self.greenlet = getcurrent()
        try:
            return self.hub.switch()
        finally:
            self.greenlet = None    # ← always unlink, even on exception

    def switch(self, value):
        if self.greenlet is None:
            self.value = value      # ← store for later (greenlet already gone)
        else:
            try:
                self.greenlet.switch(value)
            except:
                self.hub.handle_error(...)  # ← catch dead-greenlet errors
```

Key property: `Waiter.get()` has a `finally` block that sets `self.greenlet = None`.
If the greenlet dies or throws, the waiter is unlinked and the I/O completion
becomes a no-op.

### Layer 2: try/finally around every hub switch

Every I/O wait in gevent is wrapped in try/finally that stops the watcher:

```python
def wait(self, watcher):
    waiter = Waiter(self)
    watcher.start(waiter.switch, waiter)
    try:
        result = waiter.get()
    finally:
        watcher.stop()    # ← always stop watcher, even on exception/kill
```

This ensures the event loop never fires a callback for a dead greenlet because
the watcher is stopped before the greenlet is considered dead.

### Layer 3: Exception-driven cleanup on kill

When `greenlet.kill()` is called, gevent throws `GreenletExit` into the greenlet.
This unwinds the stack, executing all `finally` blocks, which stop all watchers.
The greenlet dies cleanly with no dangling I/O registrations.

### Why we can't copy gevent's architecture directly

gevent uses libev/libuv, which are **readiness-based** (epoll). Watchers can be
started and stopped at any time — stopping a watcher simply removes it from the
epoll interest set. No I/O is in flight; the kernel just stops notifying.

theframework uses io_uring, which is **completion-based**. Once an SQE is submitted,
the kernel **will** produce a CQE. You can cancel it (`IORING_OP_ASYNC_CANCEL`),
but the cancel itself is asynchronous — it produces its own CQE. There is no
synchronous "stop watching" equivalent.

This means:

| | gevent (epoll) | theframework (io_uring) |
|---|---|---|
| Deregister I/O | Synchronous (`watcher.stop()`) | Asynchronous (submit cancel SQE, wait for CQE) |
| Stale completions | Impossible (watcher stopped) | Inevitable (CQE arrives after cleanup) |
| Cleanup model | `finally` block stops watcher | Must handle stale CQEs gracefully |

### What we take from gevent

1. **The `finally` pattern**: Every switch must have a cleanup path. In gevent this is
   Python's `finally`. In Zig, we do it explicitly on the `switch_result == null` path.

2. **Generation-based stale detection**: We already have this — `pool.lookup()` and
   `op_slots.lookup()` validate generations. This is our equivalent of gevent's
   "watcher is stopped, callback is a no-op".

3. **Catch errors on resume, don't crash**: gevent's `Waiter.switch()` wraps
   `greenlet.switch()` in try/except and calls `hub.handle_error()`. Our CQE loop
   already handles this somewhat — if a greenlet is gone, `conn.greenlet` should be
   null and the CQE is silently consumed. The fix ensures we always null it out.

4. **`active_waits` is a hard invariant**: Like gevent's watcher keepalive set, our
   `active_waits` counter determines whether the hub loop continues. A leak here is
   a deadlock. Every increment must have a guaranteed matching decrement.

---

## The Fix

### Principle: Idempotent Cleanup with State Guards

Every `switch_result == null` path must be **idempotent** — correct regardless of
whether the CQE handler already ran before the switch failure code executes. This
is necessary because the CQE can race the `greenlet.throw()` that causes the switch
failure (see "CQE-races-throw" below).

The cleanup checks the greenlet pointer to determine whether the CQE handler already
did its work. If the pointer is null, the CQE handler already cleaned up; if non-null,
the cleanup code is responsible.

Steps (when the guard passes):

1. Reset `conn.greenlet = null` / `slot.greenlet = null`
2. Reset `conn.state = .idle` (for connection-based ops)
3. Decrement `conn.pending_ops` (for connection-based ops)
4. Decrement `self.active_waits`
5. `py.py_helper_decref(current)` — but only if we still own the ref (see loop caveat)
6. Release op slot if applicable (always — even when guard fails)
7. Decref any incref'd Python objects (`py_bytes`, `body_obj`) (always)
8. Release connection/fd resources if applicable (`greenConnect` only) (always)

### The CQE-races-throw problem

The plan previously assumed the CQE had not been processed when the switch failure
cleanup runs. This is NOT guaranteed.

**Scenario** (easiest to see with greenRecv):

1. Victim calls `greenRecv`, switches to hub
2. Hub CQE batch contains both the recv CQE and the killer's sleep CQE
3. CQE processing order: sleep CQE first → killer added to `ready[0]`;
   recv CQE second → `conn.greenlet = null`, `active_waits -= 1`,
   victim added to `ready[1]`
4. Hub drains ready queue: killer (index 0) runs first, calls `victim.throw()`
5. Victim resumes at `py_helper_greenlet_switch` → returns NULL
6. Switch failure cleanup runs — but the CQE handler already did everything

Without a guard, the cleanup would double-decrement `active_waits`, double-decrement
`pending_ops`, and double-decref `current`. With the guard:

```zig
if (conn.greenlet != null) {  // CQE handler nulls this — if null, it already ran
    conn.greenlet = null;
    conn.state = .idle;
    conn.pending_ops -= 1;
    self.active_waits -= 1;
    py.py_helper_decref(current);
}
```

When the guard fails (CQE already processed): the greenlet ref is in the ready queue
at a later index. When the hub drains that index, it calls
`py_helper_greenlet_switch(g, ...)` on the dead greenlet, gets NULL, decrefs g
(line 188), and continues. No leak, no crash.

**Summary of guard variables by op type**:

| Op type | Guard | Why |
|---|---|---|
| Connection (recv/send/doRecv) | `conn.greenlet != null` | CQE handler nulls `conn.greenlet` at line 315 |
| Op slot (sleep/connect/poll) | `slot.greenlet != null` | CQE handler nulls `slot.greenlet` at line 278 |
| Accept | `self.accept_greenlet != null` | CQE handler checks `accept_greenlet` at line 248 |
| Poll multi | `!group_ptr.resumed` | CQE handler sets `group.resumed = true` at line 265 |

### Cleanup helpers

The guard pattern is uniform enough to extract two helpers, reducing copy-paste
cleanups to one-liners plus per-function extras. greenSend, greenSendResponse,
greenAccept, and greenPollMulti have per-function variations and cannot use these
helpers directly — they inline the guard pattern instead.

```zig
/// Connection-based switch failure cleanup. Idempotent with the CQE handler:
/// if conn.greenlet is null, the CQE handler already did everything.
/// Returns true if cleanup was performed (CQE not yet processed).
/// Note: does NOT clear the Python exception state — caller returns null
/// to propagate whatever exception caused the switch failure.
fn cleanupConnSwitch(self: *Hub, conn: *Connection, current: *PyObject) bool {
    if (conn.greenlet == null) return false;
    conn.greenlet = null;
    conn.state = .idle;
    conn.pending_ops -= 1;
    self.active_waits -= 1;
    py.py_helper_decref(current);
    return true;
}

/// Op-slot-based switch failure cleanup. Always releases the slot.
/// Idempotent with the CQE handler: if slot.greenlet is null, the CQE
/// handler already decremented active_waits and consumed the greenlet ref.
/// Note: does NOT clear the Python exception state — caller returns null
/// to propagate whatever exception caused the switch failure.
fn cleanupSlotSwitch(self: *Hub, slot: *OpSlot, current: *PyObject) void {
    if (slot.greenlet != null) {
        slot.greenlet = null;
        self.active_waits -= 1;
        py.py_helper_decref(current);
    }
    self.op_slots.release(slot);
}
```

### Refcount ownership in partial-send loops (greenSend, greenSendResponse)

These two functions loop for partial sends. `current` is obtained once via
`getcurrent()` (refcount +1) before the loop. On each iteration, `conn.greenlet`
borrows the pointer. After a successful switch, the CQE handler moves
`conn.greenlet` to the ready queue, and the ready queue drain decrefs it —
**consuming the original ref**.

On iteration 2+, the function no longer owns a ref to `current`. The cleanup
must NOT decref it again:
- **greenSend**: guard with `if (total_sent == 0)`
- **greenSendResponse**: guard with `if (!first_switch_done)`

Note: the `conn.greenlet != null` guard does NOT replace these guards. On
iteration 2+, the green function re-sets `conn.greenlet = current` before each
switch (line 574 / line 700), so `conn.greenlet` is non-null even though the
function no longer owns the ref from `getcurrent()`.

Note: the existing `hub_greenlet orelse` error path inside greenSend's loop
(line 589) unconditionally decrefs `current` — this is a latent bug, but
harmless because `hub_greenlet` is always set if we reached iteration 2+.

### Existing bug: unmatched decref in partial-send loops (out of scope)

The refcount issue above is actually worse than just the switch-failure cleanup.
On the **normal success path**, iteration 2+ of greenSend/greenSendResponse stores
`current` in `conn.greenlet` without incref (line 574 / 700). The CQE handler takes
this unowned pointer, adds it to the ready queue, and the drain decrefs it — an
unmatched decref on each iteration after the first.

This doesn't crash because the live greenlet has other strong references (parent
chain, Python frame), but it IS real refcount corruption that accumulates.

**Fix** (separate from this PR): incref `current` before each `conn.greenlet = current`
on iteration 2+, e.g. `if (total_sent > 0) py.py_helper_incref(current);` for
greenSend. This is orthogonal to the switch-failure fix and should be filed separately.

### What about the in-flight SQE?

After the fix, the SQE is still submitted and the kernel will produce a CQE. The
cleanup is idempotent with respect to the CQE handler — regardless of whether the
CQE arrives before or after the cleanup:

- **CQE arrives AFTER cleanup** (common case):
  - Connection ops: `conn.greenlet` is null, `conn.state` is `.idle` → CQE
    processing is a no-op (line 312-320).
  - Op-slot ops: `release()` bumped generation → `lookup()` returns null, CQE
    discarded.
  - Accept: `accept_greenlet` is null → CQE handler decrements `active_waits`
    (line 247, unconditional) and skips resume (line 248).

- **CQE arrives BEFORE cleanup** (the race):
  - Guard variable is null/true → cleanup skips all state mutations. No
    double-decrement, no double-decref.

### The active_waits ownership model

| Op type | Cleanup decrements? | CQE handler decrements? | Guard |
|---|---|---|---|
| Connection (recv/send) | Only if `conn.greenlet != null` | Only if `conn.greenlet != null` (line 316) | Mutually exclusive — whoever nulls `conn.greenlet` first owns the decrement |
| Op slot (sleep/connect/poll) | Only if `slot.greenlet != null` | Only if `slot.greenlet != null` (line 279) | Same mutual exclusion. Plus, `release()` invalidates stale CQEs via generation bump |
| Accept | **Never** | Always (line 247, unconditional) | CQE handler is always the sole owner |
| Poll multi | Only if `!group_ptr.resumed` | Only if `!group.resumed` (line 264) | Same mutual exclusion via `group.resumed` flag |

### Accept: special handling

Accept does NOT decrement `active_waits` in the cleanup. The accept CQE handler
at line 247 unconditionally decrements it, regardless of `accept_greenlet`. So
the CQE handler is always the sole owner of the `active_waits` decrement for accept.

The cleanup also leaves `accept_pending = true`. Since accept CQEs all use the same
`ACCEPT_SENTINEL` user_data (no generation-based protection), setting
`accept_pending = false` would allow a new `greenAccept` call to submit a second
accept SQE while the old one is still in flight. When the old CQE arrives, it would
resume the new accept's greenlet with the wrong result. Leaving `accept_pending = true`
prevents this — the CQE handler will clear it when the CQE arrives.

Note: `greenAccept` does not currently check `accept_pending` as a guard. Adding
such a check is a future hardening item.

### Detailed fix for each function

#### greenRecv (line 500-502)

**Before:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) {
    return null;
}
```

**After:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) {
    _ = self.cleanupConnSwitch(conn, current);
    return null;
}
```

#### greenSend (line 594-596)

**Before:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) {
    py.py_helper_decref(py_bytes);
    return null;
}
```

**After:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) {
    if (conn.greenlet != null) {
        conn.greenlet = null;
        conn.state = .idle;
        conn.pending_ops -= 1;
        self.active_waits -= 1;
        if (total_sent == 0) py.py_helper_decref(current);
    }
    py.py_helper_decref(py_bytes);
    return null;
}
```

Note: cannot use `cleanupConnSwitch` because the decref is conditional on
`total_sent == 0` (see "Refcount ownership in partial-send loops" above).

#### greenSendResponse (line 720-722)

**Before:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
first_switch_done = true;
if (switch_result == null) {
    py.py_helper_decref(body_obj);
    return null;
}
```

**After:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) {
    if (conn.greenlet != null) {
        conn.greenlet = null;
        conn.state = .idle;
        conn.pending_ops -= 1;
        self.active_waits -= 1;
        if (!first_switch_done) py.py_helper_decref(current);
    }
    py.py_helper_decref(body_obj);
    return null;
}
// Must be AFTER the null check: on switch failure during iteration 1,
// first_switch_done must still be false so we decref current (we own the ref).
// On iteration 2+, the previous iteration set this to true, so the decref
// is correctly skipped (the ref was consumed by the CQE handler cycle).
first_switch_done = true;
```

Note: `first_switch_done = true` is moved AFTER the null check. On the first
iteration it's still `false`, so we decref `current` (we own the ref from
`getcurrent()`). On iteration 2+, the ref was already consumed by the CQE
handler → ready queue drain → `decref(g)` cycle, so we must NOT decref again.
This matches the existing guard on the `prepWritev catch` error path (line 692).

The move also preserves the `prepWritev catch` guard: on iteration 2+,
`first_switch_done` is `true` (set by the previous iteration's successful
switch), so the `prepWritev catch` path correctly skips the decref.

Note: cannot use `cleanupConnSwitch` because the decref is conditional on
`!first_switch_done` (see "Refcount ownership in partial-send loops" above).

#### doRecv (line 831)

**Before:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) return null;
```

**After:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) {
    _ = self.cleanupConnSwitch(conn, current);
    return null;
}
```

#### greenAccept (line 402)

**Before:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) return null;
```

**After:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) {
    // Do NOT decrement active_waits — the accept CQE handler (line 247)
    // unconditionally decrements it when the CQE arrives.
    // Leave accept_pending = true — prevents overlapping accepts with
    // the same ACCEPT_SENTINEL user_data. CQE handler will clear it.
    if (self.accept_greenlet != null) {
        self.accept_greenlet = null;
        py.py_helper_decref(current);
    }
    return null;
}
```

#### greenSleep (line 897-899)

**Before:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) {
    self.op_slots.release(slot);
    return null;
}
```

**After:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) {
    self.cleanupSlotSwitch(slot, current);
    return null;
}
```

#### greenConnect (line 1024-1029)

**Before:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) {
    self.op_slots.release(slot);
    self.fd_to_conn[fd_usize] = null;
    self.pool.release(conn);
    posix.close(fd);
    return null;
}
```

**After:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) {
    self.cleanupSlotSwitch(slot, current);
    self.fd_to_conn[fd_usize] = null;
    self.pool.release(conn);
    posix.close(fd);
    return null;
}
```

#### greenConnectFd (line 1117-1119)

**Before:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) {
    self.op_slots.release(slot);
    return null;
}
```

**After:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) {
    self.cleanupSlotSwitch(slot, current);
    return null;
}
```

#### greenPollFd (line 1185-1187)

**Before:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) {
    self.op_slots.release(slot);
    return null;
}
```

**After:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) {
    self.cleanupSlotSwitch(slot, current);
    return null;
}
```

#### greenPollFdTimeout (line 1507-1509)

**Before:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) {
    self.op_slots.release(slot);
    return null;
}
```

**After:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) {
    self.cleanupSlotSwitch(slot, current);
    return null;
}
```

#### greenPollMulti (line 1396-1398)

**Before:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) {
    self.cancelAndReleasePollSlots(slots[0..total_slots]);
    return null;
}
```

**After:**
```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) {
    // IMPORTANT: read group_ptr BEFORE cancelAndReleasePollSlots, which
    // releases slots[0] (where the PollGroup storage lives).
    if (!group_ptr.resumed) {
        self.active_waits -= total_slots;
        py.py_helper_decref(current);
    }
    self.cancelAndReleasePollSlots(slots[0..total_slots]);
    return null;
}
```

Note: cannot use `cleanupSlotSwitch` because poll multi uses a `PollGroup` shared
across multiple slots. The guard is `!group_ptr.resumed` (CQE handler sets
`group.resumed = true` at line 265). `cancelAndReleasePollSlots` must always run
to cancel in-flight SQEs and release all slots. The ordering is critical —
`group_ptr` points into `slots[0].storage.poll_group`, which becomes invalid
after `cancelAndReleasePollSlots` releases `slots[0]`.

---

## Edge Cases and Invariants

### 1. What if the CQE arrives before the cleanup? (The race)

Handled by the guard pattern. If the CQE handler already ran:
- `conn.greenlet` / `slot.greenlet` / `accept_greenlet` is null, or
  `group.resumed` is true
- The guard fails, cleanup is a no-op for state mutations
- The greenlet ref is in the ready queue, will be decreffed when the hub tries
  to switch to the (now dead) greenlet at line 188

The guard makes every cleanup path idempotent with the CQE handler.

### 2. What if the connection is reused before the stale CQE arrives?

For connection-based ops: we do NOT release the connection or bump its generation.
The stale CQE will match the generation and be processed. But since
`conn.greenlet == null` and `conn.state == .idle`, the CQE processing is a no-op
(sets state to `.idle`, skips greenlet resume). The connection remains valid.

This cannot happen in practice anyway — the switch failure means the greenlet is
dead or errored. The Python handler catches the exception and returns (or the
connection is closed). There is no retry path that would reuse the same connection
with a new greenlet before the stale CQE arrives.

### 3. What if multiple switch failures happen on the same connection?

Impossible. A switch failure means the greenlet returned to the caller with an error.
The caller (Python handler) cannot call another green function on the same connection
because the greenlet is dead. The connection will be closed by the `finally` block
in `_handle_connection`.

### 4. What if the hub loop exits before the stale CQE arrives?

The hub's `deinit()` decrefs all greenlets in connections and op slots (lines
111-136). Since we've already set `conn.greenlet = null` / `slot.greenlet = null`,
there's nothing to double-decref. The stale CQE is never processed (the hub is gone),
and the ring's `deinit()` cleans up the io_uring instance. No leak.

### 5. What about greenSend/greenSendResponse's partial send loop?

These functions loop, submitting multiple SQEs for partial sends. If a switch failure
happens mid-loop (some bytes already sent), we clean up and return null. The
partially-sent response is truncated. This is acceptable — the connection will be
closed by the Python-side error handler, and the client will see a broken response.

### 6. The accept CQE handler's unconditional `active_waits -= 1`

The accept CQE handler at line 247 always decrements `active_waits`, regardless of
whether `accept_greenlet` is set. This is different from the connection CQE handler,
which only decrements inside `if (conn.greenlet)`. For this reason, the accept
cleanup must NOT decrement `active_waits` — the CQE handler will do it.

`active_waits` stays +1 until the accept CQE arrives, keeping the hub running.
The accept CQE will eventually arrive (either a new connection or a cancellation
from the listen socket close), decrement `active_waits`, and allow the hub to exit.
The shutdown path handles this via `hub_stop()` / stop pipe.

---

## Implementation Order

1. Add `cleanupConnSwitch` and `cleanupSlotSwitch` helpers to `Hub`
2. Fix all 10 switch failure paths (greenAccept already passes)
3. Run `zig build test` to verify compilation
4. Run `uv run pytest tests/` — all 11 switch-failure tests should pass
5. Run `uv run mypy` per CLAUDE.md rules

---

## Future Hardening (Not in Scope)

These are improvements that would make the system more robust but are not required
for the immediate fix:

- **`accept_pending` guard in `greenAccept`**: Add an
  `if (self.accept_pending) { return error; }` check at the top of `greenAccept`
  to prevent overlapping accepts. Currently `greenAccept` does not check this flag.

- **Debug assertion**: Add `std.debug.assert(self.active_waits > 0)` before every
  decrement to catch underflow during development.

- **Greenlet liveness check before resume**: In the CQE processing loop, check
  `py_helper_greenlet_active(g)` before switching. This would catch bugs where
  a greenlet reference is stale but not null. Low priority — the null check is
  sufficient if the cleanup is correct.
