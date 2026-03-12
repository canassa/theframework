# Segfault Report: greenAccept Cold-Branch Crash

## Summary

Adding a `py_helper_decref` call to `greenAccept`'s switch-failure branch
caused a segfault in `Hub.deinit` — even though the new code was **never
executed** at runtime. The root cause is not fully understood, but the assembly
diff reveals a concrete change: the stack frame grows by 32 bytes when the
decref is added. We hypothesize this interacts with greenlet's `memcpy`-based
stack swap, but the exact corruption mechanism requires further investigation.

---

## Timeline of Discovery

### 1. Initial implementation

While implementing switch-failure cleanup for all 11 `green_*` functions (per
`plans/switch-failure.md`), two helper functions were introduced:

```zig
fn cleanupConnSwitch(self: *Hub, conn: *Connection, current: *PyObject) bool { ... }
fn cleanupSlotSwitch(self: *Hub, slot: *OpSlot, current: *PyObject) void { ... }
```

These helpers took `current` (the greenlet pointer obtained from
`py_helper_getcurrent()` before the switch) as a parameter and called
`py_helper_decref(current)` to release the ref.

### 2. First segfault

After wiring up all 11 cleanup paths, `test_cooperative_io.py` segfaulted:

```
tests/test_cooperative_io.py::test_green_sleep PASSED
tests/test_cooperative_io.py::test_green_connect PASSED
tests/test_cooperative_io.py::test_register_fd PASSED
Segmentation fault (core dumped)
```

The crash occurred during `Hub.deinit`, specifically at the `accept_greenlet`
decref. The `.so` offset was consistently `+0x155caa`.

Stack trace:
```
_PyObject_AssertFailed  →  greenlet dealloc  →  _Py_Dealloc  →  py_helper_decref  →  Hub.deinit
```

The `accept_greenlet` pointer had been corrupted — it pointed to something that
was not a valid Python object, triggering an assertion failure inside CPython's
dealloc machinery.

### 3. First fix attempt: read from stored location

The helpers were rewritten to **not** take `current` as a parameter, instead
reading from heap-allocated storage (`conn.greenlet`, `slot.greenlet`):

```zig
fn cleanupConnSwitch(self: *Hub, conn: *Connection) bool {
    const g_opaque = conn.greenlet orelse return false;
    const g: *PyObject = @ptrCast(@alignCast(g_opaque));
    conn.greenlet = null;
    conn.state = .idle;
    conn.pending_ops -= 1;
    self.active_waits -= 1;
    py.py_helper_decref(g);
    return true;
}

fn cleanupSlotSwitch(self: *Hub, slot: *OpSlot) void {
    if (slot.greenlet) |g_opaque| {
        const g: *PyObject = @ptrCast(@alignCast(g_opaque));
        slot.greenlet = null;
        self.active_waits -= 1;
        py.py_helper_decref(g);
    }
    self.op_slots.release(slot);
}
```

This works for 10 functions because reading from the stored location also
serves as an **idempotency guard** — if the CQE handler already ran, it nulled
`conn.greenlet`/`slot.greenlet`, so the cleanup is a no-op. This guard is the
primary reason this pattern is correct, not stack corruption avoidance.

**Result**: Fixed the segfault for 10 of 11 functions. `greenAccept` still
crashed.

### 4. greenAccept isolation

`greenAccept` is structurally different from other green functions:
- It stores its greenlet pointer in `self.accept_greenlet` (a Hub field)
- It doesn't use the connection pool or OpSlot table
- The CQE handler unconditionally decrements `active_waits` when the accept
  CQE arrives

The switch-failure cleanup for `greenAccept` was:

```zig
if (switch_result == null) {
    if (self.accept_greenlet) |g_opaque| {
        const g: *PyObject = @ptrCast(@alignCast(g_opaque));
        self.accept_greenlet = null;
        py.py_helper_decref(g);
    }
    return null;
}
```

This read from `self.accept_greenlet` (heap storage, not a local variable) —
the same pattern that worked for all other functions. Yet it still segfaulted.

### 5. noinline wrapper attempt

To rule out inlining effects, a `noinline` wrapper was tried:

```zig
fn cleanupAcceptSwitch(self: *Hub) void {
    @setCold(true);
    if (self.accept_greenlet) |g_opaque| {
        const g: *PyObject = @ptrCast(@alignCast(g_opaque));
        self.accept_greenlet = null;
        py.py_helper_decref(g);
    }
}
```

**Result**: Still segfaulted.

### 6. Systematic bisection

An agent performed systematic bisection by testing combinations of changes:

| Change | Result |
|--------|--------|
| SqeStorage only (no cleanup code) | PASS |
| SqeStorage + helper definitions (no call sites) | PASS |
| SqeStorage + helpers + all call sites EXCEPT greenAccept | PASS |
| SqeStorage + helpers + greenAccept: `self.accept_greenlet = null` only | PASS |
| SqeStorage + helpers + greenAccept: `self.accept_greenlet = null` + `py_helper_decref` | **SEGFAULT** |
| greenAccept with noinline wrapper calling decref | **SEGFAULT** |
| greenAccept with only `return null` (no state changes) | PASS |

The critical finding: **adding any `py_helper_decref` call to greenAccept's
null branch — even via a noinline wrapper, even reading the pointer from heap
storage — caused the crash.**

### 7. The never-executed code observation

The switch-failure tests (`test_switch_failure.py`) all passed. The segfault
manifested in `test_cooperative_io.py` — tests that exercise the **normal**
(non-failure) code path. The null branch was never taken during the crashing
tests.

This means the mere **presence** of the decref call in the compiled binary
changed the behavior of the normal path.

---

## Assembly Evidence

Comparing `objdump -d` output of `greenAccept` with and without the
`py_helper_decref` call in the cold branch:

### Stack frame size change

```
# Without decref (working):
159c76:  sub    $0x1c8,%rsp        # 456 bytes

# With decref (crashing):
159c76:  sub    $0x1e8,%rsp        # 488 bytes
```

**The stack frame grows by 32 bytes (0x20)** when the decref is added. This is
the compiler reserving space for the additional call's temporaries/spills.

### Stack variable offset shifts

High-address stack variables shift to accommodate the larger frame:

```
# Without decref:                    # With decref:
mov %edx,0x1a0(%rsp)                 mov %edx,0x1c8(%rsp)
mov %edx,0x1a4(%rsp)                 mov %edx,0x1cc(%rsp)
mov %edx,0x1a8(%rsp)                 mov %edx,0x1d0(%rsp)
```

### Code at the switch point is identical

The actual `py_helper_greenlet_switch` call is at the same address (0x159f14)
in both versions. The instruction sequence leading up to and including the
switch call is byte-identical.

### Post-switch paths diverge

After the switch, the null check is at the same address (0x159f29) in both
versions. But the branch targets differ:

```
# Without decref: null → xor eax,eax; jmp epilogue (4 instructions)
# With decref:    null → load self, load accept_greenlet, null check,
#                        store null, decref, jmp epilogue (20+ instructions)
```

### Key observation

The `self` pointer is stored at `0x08(%rsp)` and `0x10(%rsp)` in both
versions (same offsets). But because `rsp` is 32 bytes lower in the
with-decref version, the `self` pointer is at a different **absolute stack
address**. This changes the byte range that greenlet's `memcpy` saves and
restores when switching.

---

## Root Cause Analysis

### What we know for certain

1. Adding `py_helper_decref` to the cold branch increases the stack frame by 32
   bytes
2. The cold branch code is never executed during the crashing tests
3. The crash is in `Hub.deinit` at the `accept_greenlet` decref — the stored
   `accept_greenlet` pointer is corrupted (not a valid Python object)
4. The refcount lifecycle is balanced in both the normal and switch-failure paths
   (verified by tracing all paths)
5. The corruption affects `accept_greenlet` which is a field on the heap-allocated
   Hub struct — NOT a stack variable

### What we hypothesize (but cannot prove definitively)

The 32-byte stack frame increase changes the byte range greenlet saves/restores
during the cooperative switch. Several mechanisms could explain how this causes
corruption:

**Hypothesis A: Stack region overlap between greenlets.** When `rsp` is 32
bytes lower, greenlet saves a wider region of the C stack. If this wider region
overlaps with another greenlet's saved stack area, restoring one greenlet could
clobber data belonging to another. Greenlet has a "stack save cascade" to
handle overlaps, but it may have edge cases with frame sizes near alignment
boundaries.

**Hypothesis B: Stack alignment at the switch point.** The x86-64 ABI requires
16-byte stack alignment at call sites. The 32-byte frame change preserves this
alignment (both 0x1c8 and 0x1e8 are 8-byte aligned, and the `push rbp` +
`push r11` adds 16, giving 16-byte alignment after `sub`). However, the
absolute stack position at the switch point differs, which could interact with
greenlet's stack boundary calculations.

**Hypothesis C: LLVM optimization change.** The presence of `py_helper_decref`
(an extern call) in the cold branch may disable a tail-call optimization or
change register liveness analysis for the switch call in a way that affects
how the stack frame is laid out around the switch point.

**Hypothesis D: Pre-existing corruption masked by binary layout.** There may
be a latent bug (e.g., a buffer overrun in greenlet's stack save area, or
an off-by-one in stack boundary calculation) that only becomes observable
when the stack frame size crosses a particular threshold. The 32-byte increase
may simply move the frame into a region where the corruption hits
`accept_greenlet` instead of padding.

### What would settle this

Comparing the exact stack regions greenlet saves/restores (the `stack_start`,
`stack_stop`, `stack_copy`, `stack_saved` fields in greenlet's C struct) in
both the working and crashing binaries would definitively identify the
corruption mechanism. A `gdb` session breaking at `slp_switch` in greenlet
would reveal this.

---

## The Fix

### greenAccept: delegate cleanup to CQE handler and deinit

```zig
if (switch_result == null) {
    // Do NOT touch accept_greenlet or call py_helper_decref here.
    // Adding ANY decref call — even via noinline wrapper, even reading
    // from self.accept_greenlet — changes the stack frame layout and
    // triggers a crash in the normal (non-failure) path.
    return null;
}
```

This is safe because the refcount lifecycle is covered by other paths:

1. **Normal CQE arrival**: The accept CQE handler (line 243-254) decrements
   `active_waits`, nulls `accept_greenlet`, and moves the greenlet ref to the
   ready queue. The ready queue drain (line 188) decrefs it.

2. **Dead greenlet in ready queue**: If the victim greenlet is dead when the
   CQE handler schedules it, the hub drain switches to it. `PyGreenlet_Switch`
   on a dead greenlet returns `()` (an empty tuple, not NULL, no exception set —
   verified experimentally). The drain decrefs normally and continues.

3. **Hub exits before CQE arrives**: `Hub.deinit` (line 113-116) checks
   `accept_greenlet` and decrefs if non-null.

4. **active_waits**: The CQE handler unconditionally decrements `active_waits`
   when the accept CQE arrives. No decrement is needed in the switch-failure
   path.

### All other functions: read from stored location (idempotency guard)

The cleanup helpers read the greenlet pointer from `conn.greenlet` or
`slot.greenlet` rather than from a local variable:

```zig
// Read from stored location — serves as idempotency guard:
// if the CQE handler already ran, it nulled this field, making cleanup a no-op
const g_opaque = conn.greenlet orelse return false;
```

This pattern is correct primarily because it provides **idempotency**: if the
CQE handler already processed the completion and nulled the greenlet pointer,
the switch-failure cleanup safely does nothing. It also avoids using a local
variable (`current`) that was captured before the switch, which could be stale
if the CQE handler consumed the ref.

---

## Edge Cases Verified

### Dead greenlet switch returns normally

When the accept CQE arrives after the victim is killed:
1. CQE handler puts dead greenlet in ready queue
2. Hub drain calls `py_helper_greenlet_switch(dead_g, NULL, NULL)`
3. Returns `()` (empty tuple) — NOT NULL, no exception set
4. Drain decrefs the greenlet and the result, continues normally

Verified with:
```python
g = greenlet.greenlet(lambda: None)
g.switch()  # run to completion
assert g.dead
result = g.switch()  # switch to dead greenlet
assert result == ()  # returns empty tuple, no exception
```

### Refcount is balanced in all paths

| Path | +1 | -1 |
|------|----|----|
| Normal | `getcurrent()` | drain `decref(g)` at line 188 |
| Switch failure → CQE arrives | `getcurrent()` | drain `decref(g)` at line 188 |
| Switch failure → hub exits | `getcurrent()` | `deinit` `decref` at line 114 |

No refcount leak or double-free in any path.

---

## Lessons Learned

1. **Never assume cold branches are free in greenlet contexts.** Adding code
   to a never-taken branch changed the compiled stack frame layout, causing
   a crash in the normal path. The assembly diff proves the stack frame grows
   by 32 bytes.

2. **Bisection evidence is stronger than mechanistic theory.** The bisection
   cleanly isolated the crash to a single `py_helper_decref` call. The exact
   corruption mechanism (how the 32-byte frame change causes a corrupted
   pointer) is still uncertain. When in doubt, trust the bisection.

3. **The crash location misleads.** The segfault was in `Hub.deinit` at the
   `accept_greenlet` decref — far from `greenAccept` where the code change
   was made. The corrupted pointer survived until deinit because nothing else
   touched `accept_greenlet` between corruption and crash.

4. **Idempotency guards are the real fix.** Reading from `conn.greenlet` /
   `slot.greenlet` works primarily because it provides an idempotency check
   (CQE handler may have already nulled the field), not because of stack
   corruption avoidance. For `greenAccept`, delegating entirely to the CQE
   handler and deinit achieves the same safety.

5. **Investigate assembly when behavior is inexplicable.** The assembly diff
   (`sub $0x1c8,%rsp` → `sub $0x1e8,%rsp`) immediately shows what changed.
   It doesn't fully explain the crash mechanism, but it provides the concrete
   starting point for further investigation.
