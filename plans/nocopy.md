# Zero-Copy Lazy Request Object

## Context

Currently, `pyHttpReadRequest()` in Zig eagerly copies **every** parsed field into
Python objects (`buildPyResult` → 5-tuple), then Python's `Request._from_parsed()`
does additional work: decodes all header bytes to latin-1 strings, lowercases keys,
builds a dict, and parses the query string.  This creates **2N+3** Python objects for
headers alone (N key-bytes + N value-bytes + N tuples + 1 list) plus the body memcpy,
even when the handler never reads most of them.

**Goal**: Replace the eager 5-tuple with a C-level Python type (`Request`) that holds
raw pointers into Zig-owned memory and materializes Python objects only on first
property access.  Inspired by Granian's RSGI `#[pyclass]` frozen struct approach.

### Why this is safe

Between `pyHttpReadRequest()` returning and the **next** call to `pyHttpReadRequest()`
(or connection close), the InputBuffer bytes and arena-allocated header array are
guaranteed stable:

- `consume()` only advances `read_pos` — bytes remain in place.
- `resetForNewRequest()`, `reserveFree()`, `arena.reset()` only happen inside the
  next `pyHttpReadRequest()` call.
- The handler executes entirely within this safe window.

---

## Design: Hybrid Eager/Lazy

**Eagerly set** (always accessed by the router, tiny strings):
- `method` — `str`, from enum literal (no InputBuffer reference)
- `path` — `str`, path portion before `?` (always needed for routing)
- `full_path` — `str`, original path including query string
- `keep_alive` — `bool`, internal flag, not exposed as public API

**Lazily materialized on first access** (cached after first build):
- `body` — `bytes`, potentially large, some handlers ignore it
- `headers` — `dict[str, str]`, requires decoding + lowercasing all keys
- `query_params` — `dict[str, str]`, requires URL-decoding

**Framework-managed** (no Zig involvement):
- `params` — `dict[str, str]`, populated by Python router before handler runs
- `json()` — method, calls `json.loads(self.body)` (implemented in Python, not in Zig)

---

## Design Decisions

### 1. Always lowercase header keys

HTTP/1.1 (RFC 7230) declares header field names case-insensitive.  HTTP/2 (RFC 7540)
goes further and **requires** lowercase.  We always lowercase keys in the `headers`
dict — this is spec-compliant and matches the behavior of every major Python framework
(Flask, Django, Starlette, aiohttp).

### 2. No `raw_headers` property

The original plan included `raw_headers` (`list[tuple[bytes, bytes]]`) preserving
original wire bytes.  We drop it because:

- **Nothing in the codebase uses it**, and YAGNI applies.
- The main use case (preserving original header casing) is moot — the spec says casing
  doesn't matter, and HTTP/2 mandates lowercase anyway.
- It can be added later if a real need arises (proxy forwarding, fingerprinting, etc.).
- Removing it simplifies the implementation: one fewer lazy property, no risk of
  interaction bugs between `headers` and `raw_headers` sharing backing memory.

### 3. Comma-join duplicate headers (not last-value-wins)

RFC 9110 §5.2 explicitly allows recipients to combine multiple header fields with the
same name by joining their values with `, `.  This is what WSGI servers (Gunicorn,
uWSGI) have done for decades, and it's what the entire Flask/Django ecosystem relies on.

**Why not last-value-wins?** Last-value-wins silently drops data.  Duplicate headers
are uncommon but legitimate — `Cache-Control`, `Via`, `X-Forwarded-For`, `Cookie`
(HTTP/2) can all appear multiple times.  Losing all but the last value for
`X-Forwarded-For` has caused real security vulnerabilities (IP spoofing in rate
limiters, documented in Go issue #51493).  Comma-joining preserves all data at
negligible cost.

**What about `Set-Cookie`?** `Set-Cookie` is the one header that cannot be
comma-folded (RFC 6265).  However, `Set-Cookie` is a **response** header — it does
not appear in incoming requests, so it does not affect our request parsing.

**Implementation cost:** One `PyDict_GetItem` check per header during dict
construction.  When a duplicate is found (rare), `PyUnicode_FromFormat("%U, %U", ...)`
concatenates the values.  The check is a hash table lookup — essentially free compared
to the Python object allocations already happening per header.

### 4. Stack-buffer lowercasing (not in-place mutation)

The original plan proposed lowercasing header keys in-place in the InputBuffer.  We
instead lowercase into a small stack buffer during `headers` getter construction:

```zig
var lower_buf: [256]u8 = undefined;
@memcpy(lower_buf[0..key.len], key);
toLowerAscii(lower_buf[0..key.len]);
```

**Why?** In-place mutation of shared backing memory is fragile.  Even though we dropped
`raw_headers` now, mutating the InputBuffer creates a hidden invariant that would
become a bug if `raw_headers` is ever added back.  The cost of a stack memcpy for
header keys (typically <64 bytes) is negligible compared to the Python string allocation
on the same line.

---

## Implementation

### Phase 1: C helpers for PyTypeObject infrastructure

Most CPython APIs (`PyDict_New`, `PyDict_SetItem`, `PyDict_GetItem`,
`PyType_Ready`, `PyModule_AddObject`, `PyUnicode_DecodeLatin1`,
`PyUnicode_FromString`, `PyUnicode_FromFormat`, `PyUnicode_FromStringAndSize`,
`PyList_New`, `PyList_SetItem`) are actual C functions — Zig can call them
directly via `@cImport`.  Only add C wrappers for things that are macros,
inline functions, or macro-defined constants.

**File: `src/py_helpers.h`** — add only the wrappers that are genuinely needed:

```c
/* Type infrastructure — Py_TYPE() is a macro, tp_free is a function pointer */
void py_helper_type_free(PyObject *self);               /* Py_TYPE(self)->tp_free(self) */

/* Exception type accessor — PyExc_TypeError is a macro-defined global */
PyObject *py_helper_exc_type_error(void);               /* PyExc_TypeError */
```

**File: `src/py_helpers.c`** — implement:

```c
void py_helper_type_free(PyObject *self) {
    Py_TYPE(self)->tp_free(self);
}

PyObject *py_helper_exc_type_error(void) {
    return PyExc_TypeError;
}
```

Everything else (`PyDict_New`, `PyDict_SetItem`, `PyType_Ready`, etc.) is called
directly from Zig via `@cImport` — no wrapper needed.  The existing helpers in
`py_helpers.h` (incref/decref, true/false, none, exc_runtime_error, etc.) remain
as they are since those wrap genuine macros.

### Phase 2: LazyRequest C struct (defined in C for layout agreement)

**File: `src/py_helpers.h`** — add the struct definition so both C and Zig see the
same layout:

```c
typedef struct {
    PyObject_HEAD

    /* Eagerly set (always valid, owned Python objects) */
    PyObject *method;          /* str */
    PyObject *path;            /* str (path only, no query) */
    PyObject *full_path;       /* str (original with query) */
    int keep_alive;            /* C bool */

    /* Raw pointers into Zig memory (valid while valid==1) */
    const char *raw_body_ptr;
    Py_ssize_t  raw_body_len;
    void       *raw_headers_ptr;   /* Header* in Zig, opaque in C */
    Py_ssize_t  raw_headers_count;
    const char *raw_qs_ptr;        /* query string after '?' */
    Py_ssize_t  raw_qs_len;

    /* Lazily cached Python objects (NULL until first access) */
    PyObject *cached_body;         /* bytes */
    PyObject *cached_headers;      /* dict[str, str], keys lowercased, dupes comma-joined */
    PyObject *cached_query_params; /* dict[str, str] */
    PyObject *cached_params;       /* dict[str, str] */

    /* Validity flag */
    int valid;  /* 1 while handler runs, 0 after _invalidate() */
} LazyRequestObject;
```

The `PyTypeObject`, `PyGetSetDef` table, and `PyMethodDef` table are **not** defined
in C.  They are defined in Zig (in `lazy_request.zig`) — these are just arrays of
C-compatible structs, and Zig can populate them directly with function pointers to
its own `export fn` callbacks.  This avoids a cross-language dependency maze where C
declares `extern` for Zig functions and Zig references C-defined vtables.

Only `LazyRequestObject` (the struct) stays in C because `PyObject_HEAD` is a macro.
The C file just needs:
- The struct definition (above)
- `py_helper_type_free()` (wraps `Py_TYPE(self)->tp_free(self)`)
- `py_helper_exc_type_error()` (wraps `PyExc_TypeError`)

### Phase 3: Zig getter/setter implementations

**File: `src/lazy_request.zig`** (new file)

Import `py_helpers.h` via `@cImport`, alias `LazyRequestObject` from C.

#### Type definition (PyTypeObject + tables, defined in Zig)

Define `PyGetSetDef`, `PyMethodDef`, and `PyTypeObject` as Zig-level constants:

```zig
const getset_table = [_]py.PyGetSetDef{
    .{ .name = "method",       .get = lazy_request_get_method,       .set = null, ... },
    .{ .name = "path",         .get = lazy_request_get_path,         .set = null, ... },
    .{ .name = "full_path",    .get = lazy_request_get_full_path,    .set = null, ... },
    .{ .name = "body",         .get = lazy_request_get_body,         .set = null, ... },
    .{ .name = "headers",      .get = lazy_request_get_headers,      .set = null, ... },
    .{ .name = "query_params", .get = lazy_request_get_query_params, .set = null, ... },
    .{ .name = "_keep_alive",  .get = lazy_request_get_keep_alive,   .set = null, ... },
    .{ .name = null, .get = null, .set = null, ... },  // sentinel
};

const methods_table = [_]py.PyMethodDef{
    .{ .name = "_invalidate", .method = lazy_request_invalidate, .flags = py.METH_NOARGS, ... },
    py.py_helper_method_sentinel(),  // sentinel
};

const members_table = [_]py.PyMemberDef{
    .{ .name = "params", .type = py.T_OBJECT, .offset = @offsetOf(py.LazyRequestObject, "cached_params"), ... },
    .{ .name = null, ... },  // sentinel
};

pub export var LazyRequestType: py.PyTypeObject = .{
    // ... zero-init most fields ...
    .tp_name = "_framework_core.Request",
    .tp_basicsize = @sizeOf(py.LazyRequestObject),
    .tp_flags = py.Py_TPFLAGS_DEFAULT,  // required — PyType_Ready needs this
    .tp_dealloc = lazy_request_dealloc,
    .tp_getset = &getset_table,
    .tp_methods = &methods_table,
    .tp_members = &members_table,
    .tp_new = lazy_request_tp_new,  // raises TypeError
};
```

`lazy_request_tp_new` raises `TypeError("Request objects cannot be created directly")`
to prevent Python code from calling `Request()` and getting an uninitialized struct.

**No GC traversal (`tp_traverse`/`tp_clear`):** The type does not set `Py_TPFLAGS_HAVE_GC`.
All held Python objects are `str`, `bytes`, or `dict[str, str]` — none can form reference
cycles.  The `params` slot (`T_OBJECT`) technically allows arbitrary assignment, but in
practice it's only set by the router to a `dict[str, str]`, and the request's short
lifetime (`_invalidate()` in the `finally` block) makes cycles impossible to sustain.
Adding GC support would require `PyObject_GC_New`/`PyObject_GC_Del` and per-request
tracking overhead for a scenario no reasonable code would create.  If `params` ever
needs to hold complex objects, revisit this decision.

#### Interned method strings

HTTP methods are a tiny fixed enum (GET, POST, PUT, DELETE, etc.).  Instead of
allocating a new Python string per request, intern them once at module init and
reuse via incref:

```zig
var interned_methods: [@typeInfo(http.Method).@"enum".fields.len]?*py.PyObject = .{null} ** @typeInfo(http.Method).@"enum".fields.len;

pub fn initInternedMethods() bool {
    // Must cover ALL enum variants — if a variant is missing, methodToPyStr
    // will index into a null slot and return null (treated as error).
    const entries = .{
        .{ http.Method.get,     "GET" },
        .{ http.Method.post,    "POST" },
        .{ http.Method.put,     "PUT" },
        .{ http.Method.delete,  "DELETE" },
        .{ http.Method.head,    "HEAD" },
        .{ http.Method.options, "OPTIONS" },
        .{ http.Method.patch,   "PATCH" },
        .{ http.Method.connect, "CONNECT" },
        .{ http.Method.trace,   "TRACE" },
    };
    inline for (entries) |entry| {
        interned_methods[@intFromEnum(entry[0])] =
            py.PyUnicode_InternFromString(entry[1]) orelse return false;
    }
    return true;
}

fn methodToPyStr(method: http.Method) ?*py.PyObject {
    const idx = @intFromEnum(method);
    if (idx < interned_methods.len) {
        if (interned_methods[idx]) |s| {
            py.py_helper_incref(s);
            return s;
        }
    }
    // .unknown — not interned, fall back to dynamic string.
    // The current parser allows unknown methods through (methodToStr returns
    // "UNKNOWN"), so this path is reachable.
    return py.PyUnicode_FromStringAndSize("UNKNOWN", 7);
}
```

The `.unknown` variant is not interned but is handled via fallback to
`PyUnicode_FromStringAndSize`.  This avoids returning null without setting a Python
exception, which would be a CPython protocol violation.

Call `initInternedMethods()` during module init (Phase 5).  This is what Granian
and uvicorn do — saves a `PyUnicode` allocation on every request.

#### Eager getters (method, path, full_path)

Pattern — return cached Python object with incref:

```zig
pub export fn lazy_request_get_method(self_raw: ?*PyObject, _: ?*anyopaque) callconv(.c) ?*PyObject {
    const self: *LazyRequestObject = @ptrCast(@alignCast(self_raw));
    py.py_helper_incref(self.method);
    return self.method;
}
```

Same for `path`, `full_path`.

#### `_keep_alive` getter (eager, internal)

Returns `Py_True` / `Py_False` from the `keep_alive` int field.  Always safe
regardless of `valid` flag — it's eagerly set and doesn't reference Zig memory.
Added to `tp_getset` as `{"_keep_alive", lazy_request_get_keep_alive, NULL, ...}`.

#### Lazy getter pattern (body, headers, query_params)

Each follows the same template:

```
1. If cached_X != null → incref + return cached_X
2. If !valid → raise RuntimeError("Request accessed after handler returned")
3. Build Python object from raw pointers
4. Store in cached_X (incref for cache ownership)
5. Return with incref for caller
```

**body getter**: `py_helper_bytes_from_string_and_size(raw_body_ptr, raw_body_len)`

**headers getter**: Build a `dict[str, str]` with lowercased keys and comma-joined
duplicates.  Loop over raw headers, for each:
- Copy key bytes to a stack buffer and lowercase there (not in-place — see Design
  Decisions §4):

  ```zig
  var lower_buf: [MAX_HEADER_KEY_LEN]u8 = undefined;
  // Safe: tryParse() rejects header keys > MAX_HEADER_KEY_LEN before
  // the request reaches the handler, so this can never overflow.
  @memcpy(lower_buf[0..key.len], key);
  toLowerAscii(lower_buf[0..key.len]);
  const py_key = py.PyUnicode_FromStringAndSize(&lower_buf, @intCast(key.len));
  ```

- For the value: use `PyUnicode_DecodeLatin1()` unconditionally.  HTTP header
  values are Latin-1 per RFC 7230 (obs-text), and CPython's Latin-1 decoder
  already detects pure-ASCII input internally and creates a compact ASCII
  representation — a Zig-side `isAscii()` pre-scan would just duplicate that
  work.  This is simpler and avoids a double-scan for the >99% ASCII case.
- Check for existing key with `PyDict_GetItem()` (borrowed ref).  If found,
  comma-join via `PyUnicode_FromFormat("%U, %U", existing, py_val)` and replace.
  Otherwise, simple `PyDict_SetItem()`.

  ```zig
  const existing = py.PyDict_GetItem(dict, py_key);  // borrowed ref
  if (existing != null) {
      const joined = py.PyUnicode_FromFormat("%U, %U", existing, py_val) orelse {
          py.py_helper_decref(py_key);
          py.py_helper_decref(py_val);
          py.py_helper_decref(dict);
          return null;
      };
      _ = py.PyDict_SetItem(dict, py_key, joined);
      py.py_helper_decref(joined);
  } else {
      _ = py.PyDict_SetItem(dict, py_key, py_val);
  }
  py.py_helper_decref(py_key);
  py.py_helper_decref(py_val);
  ```

  Note: `py_key` and `py_val` are decref'd after each iteration regardless of
  success — `PyDict_SetItem` increfs internally.  `PyDict_GetItem` returns a
  borrowed ref so `existing` does not need decref.

This is spec-compliant per RFC 9110 §5.2 (see Design Decisions §3).  The
`PyDict_GetItem` check is a hash table lookup — negligible compared to the Python
object allocations already happening per header.  Duplicates are rare so the
`PyUnicode_FromFormat` branch is almost never taken.

**query_params getter**: Parse entirely in Zig using `std.Uri` and `std.mem`
primitives — no Python callback:

```zig
fn buildQueryParams(raw_qs: [*]const u8, qs_len: usize) ?*PyObject {
    const dict = py.PyDict_New() orelse return null;

    // Copy the query string so we can safely mutate it (plusToSpace +
    // percentDecodeInPlace) without touching the InputBuffer.  This prevents
    // a double-decode bug if the build fails partway (e.g., OOM) and the
    // getter retries on cached null.
    //
    // We use PyMem_Malloc (not the connection arena) because the getter
    // callback has no access to the arena — it only receives (self, closure).
    const qs_buf = @as(?[*]u8, @ptrCast(py.PyMem_Malloc(qs_len))) orelse {
        py.py_helper_decref(dict);
        return null;
    };
    defer py.PyMem_Free(qs_buf);
    const qs = qs_buf[0..qs_len];
    @memcpy(qs, raw_qs[0..qs_len]);

    var pairs = std.mem.splitScalar(u8, qs, '&');
    while (pairs.next()) |pair| {
        if (pair.len == 0) continue;
        const eq_pos = std.mem.indexOfScalar(u8, pair, '=') orelse pair.len;
        var key_buf = pair[0..eq_pos];
        var val_buf: []u8 = if (eq_pos < pair.len) pair[eq_pos + 1 ..] else pair[0..0];

        // Replace '+' → ' ' and percent-decode (mutates the arena copy, not InputBuffer)
        plusToSpace(key_buf);
        plusToSpace(val_buf);
        const key = std.Uri.percentDecodeInPlace(key_buf);
        const val = std.Uri.percentDecodeInPlace(val_buf);

        // Build Python str objects, insert into dict (last-value-wins)
        const py_key = py.PyUnicode_FromStringAndSize(key.ptr, @intCast(key.len))
            orelse { py.py_helper_decref(dict); return null; };
        const py_val = py.PyUnicode_FromStringAndSize(val.ptr, @intCast(val.len))
            orelse { py.py_helper_decref(py_key); py.py_helper_decref(dict); return null; };
        _ = py.PyDict_SetItem(dict, py_key, py_val);
        py.py_helper_decref(py_key);
        py.py_helper_decref(py_val);
    }
    return dict;
}
```

The query string is copied into the arena before mutation (`plusToSpace` +
`percentDecodeInPlace`).  This avoids mutating the InputBuffer, which would cause
a double-decode bug if the build fails partway (e.g., OOM) and the getter retries
on a still-null cache.  The arena copy is cheap — query strings are typically short,
and the arena is reset on the next request anyway.
Last-value-wins on duplicate keys; no multi-value support.

#### params (via `tp_members`, no custom getter/setter)

`params` is purely Python-managed — set by the router in `app.py` before the handler
runs, never touches Zig memory.  Instead of a custom getter/setter, it's exposed as
a `PyMemberDef` with `T_OBJECT` type pointing to the `cached_params` slot.  This
makes `request.params = {...}` and `request.params` work as normal attribute
get/set with zero Zig code.  Accessing before the router sets it returns `None`.

#### _invalidate method

Set `self.valid = 0` and null out raw pointers (`raw_body_ptr`, `raw_headers_ptr`,
`raw_qs_ptr`) as a defensive measure — if a bug bypasses the validity check, a null
dereference is easier to diagnose than a use-after-free.  Does **not** free cached
objects (those live until `tp_dealloc`).

#### tp_dealloc

**Every `PyObject*` field must be null-checked before decref** — including the eager
fields (`method`, `path`, `full_path`).  `buildLazyRequest` initializes all fields to
null before populating them, and uses `defer if (!success) lazy_request_dealloc(obj)`
to clean up on partial failure.  If, say, `path` allocation fails after `method`
succeeds, dealloc runs with `path == null`.  The "always valid" framing in the getter
descriptions applies only to fully-constructed requests — dealloc must handle the
partially-constructed case.

```zig
fn lazy_request_dealloc(self_raw: ?*PyObject) callconv(.c) void {
    const self: *LazyRequestObject = @ptrCast(@alignCast(self_raw));

    // Eager fields — may be null if buildLazyRequest failed partway
    if (self.method) |m| py.py_helper_decref(m);
    if (self.path) |p| py.py_helper_decref(p);
    if (self.full_path) |fp| py.py_helper_decref(fp);

    // Lazy cached fields — null until first access
    if (self.cached_body) |b| py.py_helper_decref(b);
    if (self.cached_headers) |h| py.py_helper_decref(h);
    if (self.cached_query_params) |q| py.py_helper_decref(q);
    if (self.cached_params) |p| py.py_helper_decref(p);

    py.py_helper_type_free(@ptrCast(self));
}
```

### Phase 4: Wire into hub.zig

**File: `src/hub.zig`**

1. Add `MAX_HEADER_KEY_LEN` constant and per-key validation in `tryParse()`:

Import `MAX_HEADER_KEY_LEN` from `lazy_request.zig` (where the stack buffer is defined)
so the parse-time check and the getter buffer size are always in sync:

```zig
// In lazy_request.zig (single source of truth):
pub const MAX_HEADER_KEY_LEN = 256;

// In hub.zig:
const lazy_request = @import("lazy_request.zig");
// ... use lazy_request.MAX_HEADER_KEY_LEN
```

After `hparse.parseRequest()` succeeds, before the existing `header_bytes > max_header_size`
check, add:

```zig
// Reject header keys that exceed the stack buffer size used by the headers
// getter for lowercasing.  Without this check, a pathologically long header
// key would overflow the getter's 256-byte stack buffer.  We enforce this
// here (at parse time) rather than in the getter because:
//   - The getter runs inside a tp_getset callback with no access to the
//     connection or configuration — it can't know max_header_size.
//   - A fixed stack buffer is the only safe option in a greenlet context
//     (greenlet C stacks are smaller than thread stacks).
//   - Rejecting early gives the client a clean 400, not a mid-handler crash.
// 256 bytes is generous — standard header names are 10-40 bytes.
for (headers[0..header_count]) |hdr| {
    if (hdr.key.len > MAX_HEADER_KEY_LEN) return .invalid;
}
```

2. Add `query_offset` to `ParsedRequest`:

```zig
const ParsedRequest = struct {
    // ... existing fields ...
    query_offset: usize,  // offset of '?' in path, or path.len if none
};
```

2. Compute `query_offset` in `tryParse()`:

```zig
const query_offset = std.mem.indexOfScalar(u8, path.?, '?') orelse path.?.len;
```

3. Replace `buildPyResult()` with `buildLazyRequest()`:

```zig
fn buildLazyRequest(req: ParsedRequest) ?*PyObject {
    // tp_alloc is filled in by PyType_Ready (called during module init),
    // so it is always valid by the time requests are processed.
    const request_type: *py.PyTypeObject = @ptrCast(&lazy_request.LazyRequestType);
    const obj = request_type.tp_alloc.?(request_type, 0) orelse return null;
    const self: *py.LazyRequestObject = @ptrCast(@alignCast(obj));

    // On any error below, tp_dealloc will decref all non-null fields.
    // Initialize everything to null/zero first so dealloc is safe.
    self.method = null;
    self.path = null;
    self.full_path = null;
    self.cached_body = null;
    self.cached_headers = null;
    self.cached_query_params = null;
    self.cached_params = null;
    self.valid = 0;

    // errdefer won't work here — buildLazyRequest returns ?*PyObject (optional),
    // not an error union.  Use defer + success flag instead.
    var success = false;
    defer if (!success) lazy_request_dealloc(obj);

    // Eager: method (interned string, just incref)
    self.method = lazy_request.methodToPyStr(req.method) orelse return null;

    // Eager: path (portion before '?')
    self.path = py.PyUnicode_FromStringAndSize(req.path.ptr, @intCast(req.query_offset))
        orelse return null;

    // Eager: full_path
    if (req.query_offset < req.path.len) {
        self.full_path = py.PyUnicode_FromStringAndSize(req.path.ptr, @intCast(req.path.len))
            orelse return null;
        const qs_start = req.query_offset + 1;
        self.raw_qs_ptr = @ptrCast(req.path[qs_start..].ptr);
        self.raw_qs_len = @intCast(req.path.len - qs_start);
    } else {
        py.py_helper_incref(self.path);
        self.full_path = self.path;  // same object, no query
        self.raw_qs_ptr = null;
        self.raw_qs_len = 0;
    }

    // Raw pointers (zero-copy)
    self.raw_body_ptr = @ptrCast(req.body.ptr);
    self.raw_body_len = @intCast(req.body.len);
    self.raw_headers_ptr = @ptrCast(req.headers.ptr);
    self.raw_headers_count = @intCast(req.headers.len);

    self.keep_alive = @intFromBool(req.keep_alive);
    self.valid = 1;
    success = true;
    return obj;
}
```

4. In `pyHttpReadRequest()`, replace both `buildPyResult(req)` call sites
   with `buildLazyRequest(req)`.  The return value is now a `Request` object
   (or `None` on EOF), no longer a 5-tuple.

   **Safety note (add as comment in `buildLazyRequest`):** `raw_headers_ptr` points
   into arena-allocated memory (`conn.arena`), and `raw_body_ptr`/`raw_qs_ptr` point
   into the InputBuffer's consumed region.  Both become invalid on the next
   `pyHttpReadRequest()` call: `arena.reset()` frees the header array, and
   `resetForNewRequest()` → `compact()` may memmove data over the consumed region
   where body/query bytes lived.  `consume()` after `buildLazyRequest()` only
   advances `read_pos` (bytes stay in place), so pointers remain valid during the
   handler.  This is safe because the handler runs synchronously within the
   keep-alive loop, and `_invalidate()` is called in the `finally` block before the
   next `pyHttpReadRequest()` call.  Each connection has its own arena and
   InputBuffer, so handlers calling `http_read_request` on a different fd cannot
   invalidate this connection's memory.  Users must never store `Request` objects
   across requests — `_invalidate()` enforces this by blocking lazy access after
   the handler returns.

### Phase 5: Register type in module init

**File: `src/extension.zig`**

In `PyInit__framework_core()`, after creating the module:

```zig
const module = py.py_helper_module_create(&module_def) orelse return null;

// Intern method strings (GET, POST, etc.)
if (!lazy_request.initInternedMethods()) return null;

// Register LazyRequest type (LazyRequestType is defined in lazy_request.zig)
const request_type: *py.PyTypeObject = @ptrCast(&lazy_request.LazyRequestType);
if (py.PyType_Ready(request_type) < 0) return null;
py.py_helper_incref(@ptrCast(request_type));
if (py.PyModule_AddObject(module, "Request", @ptrCast(request_type)) < 0) return null;

return module;
```

### Phase 6: Python-side changes

**File: `theframework/server.py`** — update `_handle_connection()`:

```python
def _handle_connection(fd, handler, config=None):
    cfg = config or {}
    max_header_size = cfg.get("max_header_size", 32768)
    max_body_size = cfg.get("max_body_size", 1_048_576)
    try:
        while True:
            request = _framework_core.http_read_request(
                fd, max_header_size, max_body_size,
            )
            if request is None:
                return

            response = Response(fd)
            try:
                handler(request, response)
                response._finalize()
            except Exception:
                if not response._finalized:
                    _try_send_error(fd, 500)
                return
            finally:
                request._invalidate()

            if not request._keep_alive:
                return
    except ValueError:
        ...  # unchanged
```

Key changes:
- `http_read_request` returns a `Request` object directly (not a 5-tuple)
- `response._finalize()` inside the try block — both handler and finalization can
  access the request while it's still valid
- `request._invalidate()` in `finally` block ensures invalidation always happens last,
  even on exception
- `request._keep_alive` replaces the unpacked `keep_alive` variable (safe to read
  after invalidation — it's an eagerly-set field)

**File: `theframework/request.py`** — replace class with re-export:

```python
from _framework_core import Request
```

**File: `theframework/app.py`** — `request.params = {...}` already works because
the C type has a `params` setter.  `request.path` and `request.method` are
properties on the C type.  No changes needed.

**File: `stubs/_framework_core.pyi`** — add:

```python
class Request:
    @property
    def method(self) -> str: ...
    @property
    def path(self) -> str: ...
    @property
    def full_path(self) -> str: ...
    @property
    def query_params(self) -> dict[str, str]: ...
    @property
    def headers(self) -> dict[str, str]: ...
    @property
    def body(self) -> bytes: ...
    @property
    def params(self) -> dict[str, str]: ...
    @params.setter
    def params(self, value: dict[str, str]) -> None: ...
    @property
    def _keep_alive(self) -> bool: ...
    def _invalidate(self) -> None: ...

def http_read_request(
    fd: int, max_header_size: int, max_body_size: int,
) -> Request | None: ...
```

Remove the old tuple return type for `http_read_request`.

---

## Safety: Post-Handler Access

When `valid == 0`:
- **Cached fields** (already materialized) → returned normally; they are Python-owned
  copies that don't reference Zig memory
- **Uncached lazy fields** → `RuntimeError("Request accessed after handler returned")`
- **Eager fields** (method, path, full_path) → always safe, they are Python strings

This prevents use-after-free while giving a clear error message instead of a segfault.

---

## Files Changed

| File | Action |
|------|--------|
| `src/py_helpers.h` | Add C helpers (`py_helper_type_free`, `py_helper_exc_type_error`) + `LazyRequestObject` struct |
| `src/py_helpers.c` | Implement C helpers (`py_helper_type_free`, `py_helper_exc_type_error`) |
| `src/lazy_request.zig` | **New** — `PyTypeObject`, `PyGetSetDef`/`PyMethodDef`/`PyMemberDef` tables, all getter/setter/dealloc/method implementations |
| `src/hub.zig` | Add `query_offset` to `ParsedRequest`, replace `buildPyResult` → `buildLazyRequest` |
| `src/extension.zig` | Register `Request` type in module init, import lazy_request.zig |
| `build.zig` | Add `lazy_request.zig` as import if needed |
| `theframework/request.py` | Replace class with re-export from C |
| `theframework/server.py` | Update `_handle_connection` for new return type + invalidation |
| `stubs/_framework_core.pyi` | Add `Request` class, update `http_read_request` signature |

---

## Verification

**Build & existing tests:**

1. `zig build` — compiles the new C struct, type registration, and Zig getters
2. `zig build test` — existing Zig-level `tryParse` tests still pass
3. `uv run pytest tests/` — all existing Python tests pass (Request API unchanged)
4. `uv run mypy` — type stubs match the new C type's interface
5. `uv run ruff check` — linting passes

**New automated tests** (add to `tests/test_request_response.py`):

6. **Property values match** — send a request with known method, path, query string,
   headers, and body; verify every property returns the expected value
7. **Duplicate headers comma-joined** — send a request with duplicate `Cache-Control`
   headers (e.g., `no-cache` and `no-store`); verify `request.headers["cache-control"]`
   returns `"no-cache, no-store"`
8. **Header keys lowercased** — send headers with mixed casing (`Content-Type`,
   `X-Request-ID`); verify dict keys are all lowercase
9. **Invalidation blocks uncached lazy fields** — in a handler, do NOT access `body`
   or `headers`; after `_invalidate()`, access them → expect `RuntimeError`
10. **Invalidation allows cached fields** — in a handler, access `body` and `headers`
    (triggers materialization); after `_invalidate()`, access them again → succeeds
    with correct values
11. **Direct construction blocked** — `_framework_core.Request()` raises `TypeError`
12. **Eager fields survive invalidation** — after `_invalidate()`, `method`, `path`,
    `full_path` still return correct values
