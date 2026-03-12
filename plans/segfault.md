# Segfault Report: greenAccept Cold-Branch Crash

## Summary — DEFINITIVE ROOT CAUSE FOUND

Adding a `py_helper_decref` call to `greenAccept`'s switch-failure branch
caused a segfault in `Hub.deinit`. The cold branch was originally believed to
be dead code, but **it IS executed** — during the greenlet dealloc switch.

The full causal chain:

1. `Hub.deinit` calls `py_helper_decref(accept_greenlet)` → refcount hits 0
2. `green_dealloc` switches into the acceptor to throw `GreenletExit`
3. The acceptor resumes from `py_helper_greenlet_switch`, which returns NULL
4. **The cold branch executes**: `py_helper_decref(g)` decrefs the greenlet
   that is already being deallocated (resurrected with refcnt=1) → refcnt
   hits 0 again → **nested `green_dealloc`**
5. The nested dealloc corrupts the greenlet's GC tracking state
6. When the first dealloc resumes, `PyObject_GC_Track` fires:
   **"object already tracked by the garbage collector"**

### Proof — substitution experiments (all with identical assembly except PLT target)

| Cold branch action | Result | Why |
|---|---|---|
| `py_helper_decref(g)` | **CRASH 5/5** | Nested dealloc on resurrected greenlet |
| `py_helper_noop(g)` | PASS 5/5 | Does nothing harmful |
| `py_helper_incref(g)` | PASS 5/5 | Makes greenlet look "resurrected" → handled gracefully |
| `return null` (nothing) | PASS | Cold branch exits without touching greenlet |

The frame-size theory from the earlier investigation was a **red herring**.
Assembly comparison confirmed that noop and decref produce byte-identical code
(same frame size `0x1e8`, same stack offsets, same register usage) — only the
PLT call target differs. The crash is caused by what `py_helper_decref` DOES
when executed during the dealloc switch, not by its effect on the stack frame.

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

## GDB Investigation (2026-03-12)

### Setup

- **Python**: CPython 3.14.3 (from uv, `/home/canassa/.local/share/uv/python/cpython-3.14.3-linux-x86_64-gnu/`)
- **Greenlet**: 3.3.2 (`.venv/lib/python3.14/site-packages/greenlet/`)
- **GDB**: 17.1 (via `nix-shell -p gdb`)
- **Test**: `tests/test_cooperative_io.py::test_green_sleep`
- **Reproducer**: Add the decref to greenAccept's cold branch, build with `zig build`, run with `uv run pytest`

### How to reproduce

In `src/hub.zig`, in `greenAccept`, change the switch-failure branch from:

```zig
if (switch_result == null) {
    return null;
}
```

to:

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

Then: `zig build && uv run pytest tests/test_cooperative_io.py::test_green_sleep -v -s`

### GDB crash analysis

Running under GDB with `handle SIGSEGV stop nopass`:

```
Thread 2 "Thread-1 (run)" received signal SIGSEGV, Segmentation fault.
```

#### First crash pattern (without debug instrumentation)

The crash RIP was inside `_framework_core.so` at `+0xa33d8`. The stack at
`$rsp` was filled with `0xaaaaaaaaaaaaaaaa`:

```
Stack at $rsp:
0x7fffe7e5fca0: 0xaaaaaaaaaaaaaaaa  0x0000000000000000
0x7fffe7e5fcb0: 0xaaaaaaaaaaaaaaaa  0xaaaaaaaaaaaaaaaa
0x7fffe7e5fcc0: 0x0000000000000000  0xaaaaaaaaaaaaaaaa
0x7fffe7e5fcd0: 0xaaaaaaaaaaaaaaaa  0x0000000000000000
```

`0xAA` is CPython's `PYMEM_DEADBYTE` — used to fill freed memory. The return
address on the stack was `0xaaaaaaaaaaaaaaaa`, meaning greenlet restored a
portion of the C stack that had since been freed by Python's memory allocator.

The backtrace was fully corrupted:
```
#0  0x00007fffe7f163d8 in ?? ()
#1  0xaaaaaaaaaaaaaaaa in ?? ()
#2  0x0000000000000000 in ?? ()
```

#### With debug instrumentation (std.debug.print)

Adding `std.debug.print` calls to greenAccept changed the crash location
because the format strings themselves were placed on the C stack, altering
the frame layout further. The stack dump showed our debug strings:

```
0x7fffe7e5fd80: 0x0000aaaa0000aaaa  0x47554245445b0000  # "[DEBUG"
0x7fffe7e5fd90: 0x63416e6565726720  0x6573205d74706563  # " greenAccept] se"
0x7fffe7e5fda0: 0x66663778303d666c  ...                 # "lf=0x7ff..."
```

This confirmed that stack contents (including string literals referenced by
Zig's formatting) are part of the saved/restored region, and any change to
the frame layout changes what greenlet saves.

### Breaking at `_PyObject_AssertFailed`

Setting a breakpoint on `_PyObject_AssertFailed` caught the crash before the
secondary SIGSEGV:

```
obj = 0x7fffe8526040
ob_refcnt = 0x00000000ffffffff    (4294967295 = unsigned interpretation of -1)
ob_type   = 0x00007ffff7907c40
ob_type->tp_name = greenlet.greenlet
weakreflist = (nil)
dict = (nil)
pimpl = (nil)
```

Key findings:
- **Object IS a greenlet** — `ob_type->tp_name` confirmed
- **Refcount = 0xFFFFFFFF** — this is the result of `Py_DECREF` reducing
  refcount from 0 to -1 (wrapping as unsigned 32-bit in a 64-bit field)
- **pimpl = NULL** — the greenlet's C++ implementation was already destroyed

The backtrace from `_PyObject_AssertFailed`:
```
#0  _PyObject_AssertFailed
#1  PyObject_GC_Track.cold
#2  (greenlet code at +0xfdc20)
```

The assertion message was:
```
Python/gc.c:2210: PyObject_GC_Track: Assertion failed: object already tracked
by the garbage collector
```

### C-level instrumentation approach

Added `py_helper_debug_checkpoint()` to `py_helpers.c` — a C function that
dumps a PyObject's refcount, type, pimpl, greenlet active/started/main status,
and GC tracking state. This function does not alter the Zig stack frame since
it's an extern C call (Zig already accounts for extern calls in its frame
layout).

Called from `Hub.deinit`, CQE handler, and ready queue drain.

### Critical finding: accept_greenlet is NOT corrupted

Both versions show **identical metadata** at `Hub.deinit`:

```
[CP] deinit:accept_greenlet: 0x... refcnt=1 type=0x... pimpl=0x... active=1 started=1 main=0
```

- **Working version**: PASSED — decref succeeds, greenlet is deallocated normally
- **Crashing version**: SEGFAULT — decref triggers assertion in greenlet dealloc

**The `accept_greenlet` pointer itself is NOT corrupted.** It points to a valid
greenlet object with refcnt=1 and valid pimpl. The corruption is in the
greenlet's **saved stack state**, which is only exercised during the dealloc
switch.

### Full lifecycle trace

The checkpoint trace for both versions shows identical event sequences:

```
1. drain:switch_to accept_g   refcnt=1 active=0 started=0  (first switch into acceptor)
2. drain:after_switch          refcnt=2 active=1 started=1  (acceptor ran, switched back)
3. cqe:accept_g:before         refcnt=1                     (first accept CQE arrives)
4. cqe:accept_g:sched          refcnt=1                     (schedule to ready queue)
5. drain:switch_to accept_g   refcnt=1                     (switch to acceptor again)
6. drain:after_switch          refcnt=2                     (acceptor ran greenAccept again)
   ... handler greenlets run ...
7. cqe:accept_g:before         refcnt=1                     (second accept CQE)
8. cqe:accept_g:sched          refcnt=1                     (schedule again)
9. drain:switch_to accept_g   refcnt=1                     (switch to acceptor)
10. drain:after_switch         refcnt=2                     (acceptor called greenAccept)
    ... more handlers ...
11. deinit:accept_greenlet    refcnt=1 active=1 started=1  (hub exiting, greenlet still suspended)
```

At step 11, `Hub.deinit` calls `Py_DECREF(accept_greenlet)`. Refcount goes
from 1 to 0, triggering `green_dealloc`. Because the greenlet is
`active && started && !main`, greenlet calls
`_green_dealloc_kill_started_non_main_greenlet`, which:

1. `PyObject_GC_UnTrack(self)` — untracks from GC
2. Resurrects the greenlet (sets refcnt to 1)
3. Calls `throw_GreenletExit_during_dealloc` → `g_switch()` → `g_switchstack()` → `slp_switch()`
4. `slp_switch()` restores the acceptor's saved C stack
5. During/after this switch, something goes wrong
6. When control returns, if refcnt != 1 (resurrected), calls `PyObject_GC_Track(self)`
7. **ASSERTION**: "object already tracked by the garbage collector"

The assertion means that between the `GC_UnTrack` at step 1 and the
`GC_Track` at step 6, something **re-tracked** the object. This happens
during the stack switch in step 4-5, when the corrupted saved stack is
restored.

### Greenlet StackState comparison

Dumped the raw bytes of the Greenlet C++ pimpl object (which contains the
StackState) at deinit for both versions. The Greenlet pimpl layout (x86_64):

```
+0x00: vtable pointer (8 bytes)
+0x08: PyGreenlet* _self (8 bytes)
+0x10: ExceptionState (variable)
+0x??: SwitchingArgs (variable)
+0x38: StackState._stack_start (char*)
+0x40: StackState.stack_stop (char*)
+0x48: StackState.stack_copy (char*)  — heap copy of saved stack
+0x50: StackState._stack_saved (intptr_t)  — bytes saved
+0x58: StackState.stack_prev (StackState*)
```

**Note**: The exact offsets depend on ExceptionState and SwitchingArgs sizes,
which vary by Python version. The offsets below are empirical from the dumps.

#### Hub greenlet (main greenlet) — both versions identical

```
_stack_start = 0x...2530      (current rsp of the hub)
stack_stop   = 0xFFFFFFFFFFFFFFFF  (-1, marks main greenlet)
stack_copy   = NULL
_stack_saved = 0
stack_prev   = NULL
```

The hub greenlet is the main greenlet (stack_stop = -1), so it lives on the
real C stack and never gets saved/restored.

#### Acceptor greenlet StackState

| Field | Working | Crashing | Difference |
|-------|---------|----------|------------|
| `_stack_start` | `0x...1cd0` | `0x...1cb0` | **-0x20 (32 bytes lower)** |
| `stack_stop` | `0x...2608` | `0x...2608` | Same |
| `stack_copy` | `0x...b090` | `0x...b090` | Same (heap addr, coincidence) |
| `_stack_saved` | `0x938` (2360) | `0x958` (2392) | **+0x20 (32 more bytes)** |
| `stack_prev` | `0x...c968` | `0x...c968` | Same (points to hub's StackState) |

The 32-byte difference is exactly the stack frame size change measured in
assembly. `_stack_start` is captured from `rsp` during `slp_switch()`. The
acceptor's rsp is 32 bytes lower because `greenAccept`'s frame is larger.

#### Full pimpl dumps (for reference)

**Working version — Hub greenlet pimpl:**
```
+  0: 0x7f8ceaed99f8    (vtable)
+  8: 0x7f8cdbda4700    (_self)
+ 16: (nil)              (ExceptionState)
+ 24: (nil)
+ 32: (nil)
+ 40: (nil)
+ 48: (nil)
+ 56: 0x7f8cdb462530    (StackState._stack_start)
+ 64: 0xffffffffffffffff (StackState.stack_stop = -1, main greenlet)
+ 72: (nil)              (StackState.stack_copy)
+ 80: (nil)              (StackState._stack_saved = 0)
+ 88: (nil)              (StackState.stack_prev)
```

**Working version — Acceptor greenlet pimpl:**
```
+  0: 0x7f8ceaed9aa0    (vtable)
+  8: 0x7f8cdbb21ac0    (_self)
+ 16: 0x2ff4e7f0        (ExceptionState)
+ 24: (nil)
+ 32: (nil)
+ 40: (nil)
+ 48: (nil)
+ 56: 0x7f8cdb461cd0    (StackState._stack_start)
+ 64: 0x7f8cdb462608    (StackState.stack_stop)
+ 72: 0x7f8cd400b090    (StackState.stack_copy — heap)
+ 80: 0x938              (StackState._stack_saved = 2360 bytes)
+ 88: 0x7f8cdbcdc968    (StackState.stack_prev)
```

**Crashing version — Hub greenlet pimpl:**
```
+  0: 0x7fb4567349f8    (vtable)
+  8: 0x7fb446d21980    (_self)
+ 16: (nil)              (ExceptionState)
+ 24: (nil)
+ 32: (nil)
+ 40: (nil)
+ 48: (nil)
+ 56: 0x7fb446662530    (StackState._stack_start)
+ 64: 0xffffffffffffffff (StackState.stack_stop = -1, main greenlet)
+ 72: (nil)              (StackState.stack_copy)
+ 80: (nil)              (StackState._stack_saved = 0)
+ 88: (nil)              (StackState.stack_prev)
```

**Crashing version — Acceptor greenlet pimpl:**
```
+  0: 0x7fb456734aa0    (vtable)
+  8: 0x7fb446d21c00    (_self)
+ 16: 0x2b1b7fb0        (ExceptionState)
+ 24: (nil)
+ 32: (nil)
+ 40: (nil)
+ 48: (nil)
+ 56: 0x7fb446661cb0    (StackState._stack_start)   ← 0x20 lower than working
+ 64: 0x7fb446662608    (StackState.stack_stop)      ← same
+ 72: 0x7fb44000b090    (StackState.stack_copy — heap)
+ 80: 0x958              (StackState._stack_saved = 2392 bytes)  ← 0x20 more
+ 88: 0x7fb446edc968    (StackState.stack_prev)
```

### Stack dump at crash site

When the crash manifests as a SIGSEGV (without the assertion breakpoint), the
stack contains `0xaaaaaaaaaaaaaaaa` (PYMEM_DEADBYTE) patterns:

```
$rsp:
0x7fffe7e5fca0: 0xaaaaaaaaaaaaaaaa  0x0000000000000000
0x7fffe7e5fcb0: 0xaaaaaaaaaaaaaaaa  0xaaaaaaaaaaaaaaaa
0x7fffe7e5fcc0: 0x0000000000000000  0xaaaaaaaaaaaaaaaa
0x7fffe7e5fcd0: 0xaaaaaaaaaaaaaaaa  0x0000000000000000
```

This means greenlet's `memcpy` restored stack data that had been overwritten
with DEADBYTE fill by Python's allocator (freed memory).

---

## Root Cause Analysis — SOLVED

### Why the cold branch IS executed

The cold branch (`if (switch_result == null)`) in `greenAccept` was assumed
to be dead code during normal operation. This is true during NORMAL switches.
But it IS reached during the **dealloc switch**:

1. `Hub.deinit` calls `py_helper_decref(accept_greenlet)` → refcount hits 0
2. `green_dealloc` → `_green_dealloc_kill_started_non_main_greenlet`:
   a. `PyObject_GC_UnTrack(self)`
   b. Resurrect: `Py_SET_REFCNT(self, 1)`
   c. `throw_GreenletExit_during_dealloc()` → `g_switch()` → `slp_switch()`
3. `slp_switch()` saves the hub/deinit stack, restores the acceptor's stack
4. Acceptor resumes from where it was suspended: inside `slp_switch()`
5. `slp_switch()` returns → `g_switchstack()` → `g_switch_finish()`
6. GreenletExit exception is pending
7. `green_switch()` catches the C++ exception → returns NULL
8. `py_helper_greenlet_switch()` returns NULL to `greenAccept`
9. **`switch_result == null` → cold branch executes**

At step 9, `self.accept_greenlet` is still non-null (deinit hasn't nulled it
yet — the `self.accept_greenlet = null` in deinit runs AFTER the decref
returns). So the `if (self.accept_greenlet)` check succeeds.

### What the decref does during the dealloc switch

When `py_helper_decref(g)` runs in the cold branch:

1. The greenlet was resurrected with refcnt=1 (step 2b above)
2. `Py_DECREF` reduces it from 1 to 0
3. `_Py_Dealloc` → `green_dealloc` → NESTED dealloc
4. The nested `green_dealloc` sees `active && started && !main` → enters
   `_green_dealloc_kill_started_non_main_greenlet` AGAIN
5. The nested dealloc corrupts the GC tracking state (the object was
   untracked by the first dealloc, but the nested dealloc may re-track it
   or free the underlying pimpl)
6. When control eventually returns to the first dealloc, it checks
   `Py_REFCNT(self) != 1` and calls `PyObject_GC_Track(self)`
7. **ASSERTION: "object already tracked by the garbage collector"**

### The DEADBYTE on the stack

The `0xAA` (PYMEM_DEADBYTE) patterns found on the stack during the crash
are a CONSEQUENCE of the nested dealloc, not a cause:

1. The nested dealloc frees the greenlet's pimpl (C++ implementation object)
2. CPython's debug allocator fills freed memory with `0xAA`
3. When the dying greenlet tries to switch back to its parent, greenlet reads
   StackState fields from the freed pimpl → reads `0xAA` garbage
4. `memcpy` copies `0xAA`-filled data to the C stack → corrupted stack

### Why the frame-size theory was wrong

The earlier investigation compared the "no cold branch" version (frame=0x1c8)
with the "decref cold branch" version (frame=0x1e8) and attributed the crash
to the 32-byte frame size difference. This was disproved by:

1. **Noop experiment**: `py_helper_noop(g)` in cold branch → same frame size
   (0x1e8) → **PASSES** 5/5
2. **Assembly diff**: noop and decref produce byte-identical code except for
   the PLT call target. Same frame size, same offsets, same registers.
3. **Stack_copy dump**: Both versions have identical stack_copy contents at
   deinit time (`saved=2392`, similar 0xAA byte counts ~35-39).
4. **Incref experiment**: `py_helper_incref(g)` in cold branch → same frame
   size → **PASSES** 5/5. Incref increases refcnt from 1 to 2, making the
   greenlet look "resurrected" → `_green_dealloc_kill_started_non_main_greenlet`
   handles this gracefully by re-tracking and returning false.

The frame size increase was real but irrelevant. The crash is determined by
what the function in the cold branch DOES when executed during the dealloc
switch.

### The causal chain (corrected)

```
Hub.deinit: py_helper_decref(accept_greenlet)
  → refcount 0 → green_dealloc
    → GC_UnTrack, resurrect(refcnt=1), switch to acceptor
      → acceptor resumes from py_helper_greenlet_switch
        → GreenletExit pending → returns NULL
          → cold branch executes
            → py_helper_decref(g): refcnt 1→0 → NESTED green_dealloc
              → nested dealloc corrupts GC state / frees pimpl
                → first dealloc resumes
                  → PyObject_GC_Track: "object already tracked"
                    → _PyObject_AssertFailed → SIGSEGV
```

---

## The Fix

### greenAccept: do NOT decref in the cold branch

```zig
if (switch_result == null) {
    // Do NOT call py_helper_decref here!
    //
    // This branch runs in TWO scenarios:
    //   1. Normal switch failure (runtime error during greenlet_switch)
    //   2. Dealloc switch (GreenletExit thrown during green_dealloc)
    //
    // In scenario 2, the greenlet is already being deallocated. Calling
    // decref here would trigger a nested green_dealloc, corrupting the
    // GC tracking state and causing a segfault.
    //
    // In both scenarios, the greenlet ref is owned by deinit or the CQE
    // handler — the cold branch must NOT consume it.
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

### All other functions: no decref in switch-failure branches (2026-03-13 update)

The cleanup helpers (`cleanupConnSwitch`, `cleanupSlotSwitch`) and manual
cleanup in `greenSend`, `greenSendResponse`, and `greenPollMulti` were also
found to have the same nested-dealloc vulnerability. The decref was removed
from all of them. See "Follow-up: Latent vulnerability" section below.

The helpers still read from the stored location (`conn.greenlet`/
`slot.greenlet`) which provides an idempotency guard against the CQE handler
having already run, but they no longer decref the greenlet. The ref is owned
by `Hub.deinit` which decrefs all stored greenlets during shutdown.

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
|------|----|---|
| Normal | `getcurrent()` | drain `decref(g)` at line 188 |
| Switch failure → CQE arrives | `getcurrent()` | drain `decref(g)` at line 188 |
| Switch failure → hub exits | `getcurrent()` | `deinit` `decref` at line 114 |

No refcount leak or double-free in any path.

---

## Investigation Approaches Used

### Approach 4 (from original plan): Replace decref with noop — DECISIVE

Replaced `py_helper_decref(g)` with `py_helper_noop(g)` (a no-op C function
with the same signature). This produced **byte-identical assembly** (same frame
size `0x1e8`, same stack offsets) except for the PLT call target. Result: PASS.

This disproved the frame-size theory and showed the crash depends on what the
function DOES, not its effect on the compiled binary.

### Incref substitution — CONFIRMS THEORY

Replaced `py_helper_decref(g)` with `py_helper_incref(g)`. Result: PASS.

During the dealloc switch, incref increases refcnt from 1→2. When
`_green_dealloc_kill_started_non_main_greenlet` checks after the switch, it
sees `refcnt != 1` → assumes the greenlet was "resurrected" → calls
`PyObject_GC_Track(self)` and returns false (keeping the greenlet alive).
This is greenlet's intended codepath for resurrected greenlets.

### Stack_copy dump — DISPROVES HEAP CORRUPTION

Dumped the full stack_copy contents at deinit time for both working and
crashing versions. Both had `saved=2392`, structurally identical contents,
and similar 0xAA byte counts (~35 vs ~39). The heap copy is NOT corrupted
before the dealloc switch — the DEADBYTE appears DURING the switch as a
consequence of the nested dealloc freeing the pimpl.

### Determinism testing — CONFIRMS RELIABILITY

Each variant tested 5 times:
- decref: CRASH 5/5
- noop: PASS 5/5
- incref: PASS 5/5

### Disproven theories

1. **Frame-size theory** (the original theory): Adding `py_helper_decref` to
   the cold branch increases the stack frame by 32 bytes, causing greenlet to
   save/restore 32 extra bytes that get corrupted with DEADBYTE.
   **Disproved**: `py_helper_noop` produces the same frame size (0x1e8) and
   passes. The frame size change is real but irrelevant to the crash.

2. **Stack corruption theory**: The saved stack copy is corrupted (contains
   DEADBYTE) before the dealloc switch.
   **Disproved**: Stack_copy dumps at deinit time show valid data in both
   versions. The DEADBYTE appears DURING the dealloc switch as a consequence
   of the nested dealloc freeing the pimpl.

3. **Binary layout theory**: The presence of different PLT entries changes
   the binary layout, shifting return addresses on the stack.
   **Disproved**: Assembly comparison shows byte-identical code for noop vs
   decref. The binary layout differs slightly (due to PLT entries), but the
   noop version passes while the decref version crashes. The crash is
   determined by runtime behavior, not binary layout.

### Key greenlet source files (in .venv/lib/python3.14/site-packages/greenlet/)

| File | Purpose |
|------|---------|
| `TStackState.cpp` | Stack save/restore: `copy_stack_to_heap`, `copy_heap_to_stack`, `copy_stack_to_heap_up_to` |
| `TGreenlet.cpp` | `slp_save_state`, `slp_restore_state`, `g_switchstack`, `deallocing_greenlet_in_thread` |
| `TGreenlet.hpp` | Class declarations, `StackState` class definition (fields: `_stack_start`, `stack_stop`, `stack_copy`, `_stack_saved`, `stack_prev`) |
| `greenlet_slp_switch.hpp` | `slp_switch` function, `SLP_SAVE_STATE`/`SLP_RESTORE_STATE` macros, `switching_thread_state` global |
| `platform/switch_amd64_unix.h` | x86_64 `slp_switch()` implementation (inline asm, saves rbp/rbx/csr/cw, adjusts rsp/rbp) |
| `PyGreenlet.cpp` | `green_dealloc`, `_green_dealloc_kill_started_non_main_greenlet` (the dealloc switch logic) |
| `greenlet.h` | `PyGreenlet` struct: `{ PyObject_HEAD, weakreflist, dict, pimpl }` |

### Dealloc switch code path (PyGreenlet.cpp:297-308)

```cpp
static void green_dealloc(PyGreenlet* self) {
    PyObject_GC_UnTrack(self);              // Step 1: untrack from GC
    BorrowedGreenlet me(self);
    if (me->active() && me->started() && !me->main()) {
        if (!_green_dealloc_kill_started_non_main_greenlet(me)) {
            return;  // greenlet was resurrected
        }
    }
    // ... cleanup ...
}
```

`_green_dealloc_kill_started_non_main_greenlet` (line 189-293):
1. Resurrects greenlet (refcnt = 1)
2. Calls `throw_GreenletExit_during_dealloc` → `g_switch()` → stack switch
3. Checks if resurrected (refcnt != 1 after dealloc attempt)
4. If resurrected: `PyObject_GC_Track(self)` ← **THIS IS WHERE THE ASSERT FIRES**

### Debug infrastructure — CLEANED UP (2026-03-13)

All debug helpers have been removed from `py_helpers.h`, `py_helpers.c`, and
`hub.zig`. Debug script files (`debug_*.py`, `debug_*.gdb`, `debug_*.txt`)
have been deleted from the project root.

---

## Lessons Learned

1. **Switch-failure branches are NOT dead code in greenlet contexts.** The
   `if (switch_result == null)` branch runs in TWO scenarios: normal switch
   failures AND GreenletExit during dealloc. Any cleanup code in this branch
   must be safe for BOTH cases — specifically, it must not decref a greenlet
   that might be mid-dealloc.

2. **The "never executed" assumption was wrong and wasted hours.** The crash
   was attributed to binary layout effects (frame size change) because the
   cold branch was believed to be dead code during the crashing tests. A
   simple noop-substitution experiment disproved the frame-size theory in
   minutes — the cold branch code is EXECUTED, just not during normal
   operation.

3. **Substitution experiments are more powerful than observational debugging.**
   Hours of GDB, assembly diffing, and StackState dumps led to the wrong
   conclusion (frame-size theory). Three substitution experiments (noop,
   incref, decref) immediately identified the real cause:
   - Same frame size + noop → PASS → not frame size
   - Same frame size + incref → PASS → not frame size
   - Same frame size + decref → CRASH → it's the decref

4. **Greenlet dealloc-switch is a hidden code path.** When a non-main, active
   greenlet's refcount hits 0, greenlet resurrects it and switches into it to
   deliver GreenletExit. This switch causes `PyGreenlet_Switch` to return NULL
   in the dying greenlet's context. Any code after `PyGreenlet_Switch` that
   handles NULL returns WILL execute during dealloc — it must not assume
   ownership of the greenlet being deallocated.

5. **Nested dealloc = instant corruption.** Decrefing a resurrected greenlet
   (refcnt=1) to 0 inside its own dealloc switch triggers a recursive
   `green_dealloc`. This corrupts GC tracking state and/or frees the pimpl
   while the first dealloc is still in progress.

6. **CPython 3.14 GC assertions catch the bug early.** The
   `PyObject_GC_Track` assertion ("object already tracked") fires before
   worse consequences occur. On older CPythons this might manifest as silent
   corruption, use-after-free, or intermittent crashes.

7. **The fix is to not decref in the cold branch.** The greenlet ref is owned
   by `Hub.deinit` or the CQE handler — the cold branch should not consume
   it. Simply `return null` is correct.

## Broader Lesson for All Green Functions

Every `green_*` function that calls `py_helper_greenlet_switch` must consider
the dealloc-switch scenario in its NULL-return handler. The NULL return could
be:

1. A normal switch failure (exception set, greenlet still alive)
2. A GreenletExit during dealloc (exception set, greenlet mid-destruction)

In case 2, the greenlet pointer obtained from `getcurrent()` before the switch
refers to a greenlet that is being deallocated. Decrefing it would trigger a
nested dealloc. The safe pattern is:

```zig
const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
if (switch_result == null) {
    // Do NOT decref any greenlet here — it might be mid-dealloc.
    // Let deinit or the CQE handler own the cleanup.
    return null;
}
```

---

## Follow-up: Latent vulnerability in ALL switch-failure branches (2026-03-13)

### Discovery

Code review revealed that `cleanupConnSwitch` and `cleanupSlotSwitch` had the
**same nested-dealloc vulnerability** as `greenAccept`. The original analysis
attributed their safety to the "idempotency guard" (reading from the stored
location), but this reasoning was incomplete.

The idempotency guard only protects against the CQE-handler-already-ran case.
It does NOT protect against the dealloc-switch case:

1. `Hub.deinit` decrefs `conn.greenlet` → refcount hits 0
2. `green_dealloc` → dealloc switch into the handler greenlet
3. Handler resumes from `py_helper_greenlet_switch` → NULL → cold branch
4. `cleanupConnSwitch` reads `conn.greenlet` — still non-null (deinit hasn't
   nulled it yet, it's still inside `py_helper_decref`)
5. `py_helper_decref(g)` → refcount 1→0 → **nested `green_dealloc`** → CRASH

The same applies to `cleanupSlotSwitch`, `greenSend` (manual cleanup),
`greenSendResponse` (manual cleanup), and `greenPollMulti` (manual cleanup).

### Why it didn't crash before

Handler greenlets are typically **dead** when `Hub.deinit` runs. Dead greenlets
(`started && !active`) skip the dealloc switch in `green_dealloc` — the check
`me->active() && me->started() && !me->main()` fails, so `green_dealloc` goes
straight to cleanup without switching.

The acceptor greenlet is different: it's always **active** (suspended in an
infinite accept loop) when deinit runs, so the dealloc switch always fires.

However, if a handler greenlet were still suspended (e.g., waiting in
`green_recv` when the hub shuts down) AND its only remaining reference was in
`conn.greenlet` (the Python local `g` in the acceptor was rebound), the same
crash would occur.

### The fix (2026-03-13)

Removed `py_helper_decref` from ALL switch-failure cleanup paths:

| Function | Change |
|---|---|
| `greenAccept` | Replaced incref experiment with plain `return null` |
| `cleanupConnSwitch` | Removed `py_helper_decref(g)` |
| `cleanupSlotSwitch` | Removed `py_helper_decref(g)` |
| `greenSend` | Removed `if (total_sent == 0) py_helper_decref(g)` |
| `greenSendResponse` | Removed `if (!first_switch_done) py_helper_decref(g)` |
| `greenPollMulti` | Removed `py_helper_decref(g)` |

The cleanup helpers still null the stored pointer and clean up state
(`pending_ops`, `active_waits`, `conn.state`, slot release). Only the
decref is removed.

### Refcount ownership after this fix

The greenlet ref from `getcurrent()` is now exclusively owned by:

1. **CQE handler** — when the CQE arrives, moves greenlet to ready queue;
   drain decrefs it (even if the greenlet is dead — switching to a dead
   greenlet returns `()`, not NULL).

2. **Hub.deinit** — decrefs all `conn.greenlet`, `slot.greenlet`, and
   ready queue entries during shutdown.

In the cold branch, the stored pointer (`conn.greenlet`/`slot.greenlet`) is
nulled so the CQE handler won't use a dangling pointer after a potential
dealloc. The ref itself is released by deinit's cleanup loop.

### Also cleaned up

- Removed all debug helpers from `py_helpers.h` and `py_helpers.c`
- Removed debug call sites from `Hub.deinit` and CQE handler
- Deleted debug script files from project root

### Test results

All 320 tests pass (3 SSL skipped). Zig tests pass. mypy clean.
