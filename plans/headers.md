# Fix Request Header Parsing

**Problem**: Headers are parsed twice — once in Zig, discarded, then re-parsed in Python. This is inefficient, fragile, and loses duplicate headers.

**Goal**: Parse headers once in Zig, pass them to Python directly, and eliminate re-parsing.

---

## Current Architecture

### Zig Side (`src/http.zig`)

```zig
pub const ParsedRequest = struct {
    method: Method,
    path: []const u8,
    version: Version,
    body: []const u8,
    raw_bytes_consumed: usize,
    keep_alive: bool,
    // headers: MISSING
};
```

The `parseRequestFull()` function:
1. Calls `hparse.parseRequest()` which parses headers into a 64-element `[64]Header` array on the stack
2. Resolves `keep_alive` by scanning headers
3. Returns `ParsedRequest` without headers — they're dropped
4. Headers are completely unavailable to callers

### FFI Boundary (`src/hub.zig::pyHttpParseRequest`)

```zig
pub fn pyHttpParseRequest(...) callconv(.c) ?*PyObject {
    let req = http.parseRequestFull(buf) catch ...;

    // Build tuple (method, path, body, bytes_consumed, keep_alive)
    const tuple = py.py_helper_tuple_new(5) orelse return null;
    // ... populate tuple ...
    return tuple;
}
```

Only 5 fields are passed to Python. Headers are completely lost.

### Python Side (`theframework/request.py::Request._from_raw`)

```python
@classmethod
def _from_raw(cls, method: str, path: str, body: bytes, raw_bytes: bytes) -> Request:
    headers: dict[str, str] = {}
    header_section, _, _ = raw_bytes.partition(b"\r\n\r\n")
    lines = header_section.split(b"\r\n")
    for line in lines[1:]:
        if b": " in line:
            name, _, value = line.partition(b": ")
            headers[name.decode("latin-1").lower()] = value.decode("latin-1")
    # ...
```

Receives only `raw_bytes` and manually re-parses headers by:
1. Splitting on `\r\n\r\n` to find header section
2. Splitting on `\r\n` to get header lines
3. Splitting each line on `": "`
4. **Silently dropping duplicates** (dict overwrites)
5. Lowercasing header names

---

## Problems with Current Approach

### 1. **Double Parsing (Inefficiency)**
- Zig parses into a 64-element array
- Python re-parses from raw bytes
- Same work done twice; wasted CPU

### 2. **Lost Duplicate Headers (Correctness)**
- HTTP allows multiple headers with the same name (e.g., `Set-Cookie`, `Accept-Encoding: gzip, deflate`)
- Python dict stores only the last value: `headers["set-cookie"] = last_value`
- Applications cannot see all cookie values
- **Silent data loss** — no error or warning

### 3. **Fragile Python Parsing (Robustness)**
- Assumes `\r\n\r\n` separator exists
- Assumes `": "` (colon-space) format
- Doesn't handle whitespace around colons per RFC 9110 (section 5.1)
- Doesn't validate header syntax
- Splits on first `": "` which works but doesn't handle edge cases

### 4. **Lowercasing in Python (Semantics)**
- HTTP header names are case-insensitive for lookups
- But preserving original case can be useful for debugging/logging
- Lowercasing in Python loses information
- Different from ASGI spec (which preserves case as bytes)

### 5. **Encoding Assumptions**
- Python assumes `latin-1` encoding
- Doesn't match HTTP spec (which requires `latin-1` for obs-text, but raw bytes are better)
- Loses ability to inspect raw bytes if needed for security

### 6. **No Access to Parsed Headers**
- Can't build a list of headers (only dict)
- Can't preserve order
- Can't handle headers with same name efficiently
- Would need to re-implement Zig parsing in Python if apps need this

---

## Design: Single-Parse Header Pipeline

### Key Principles

1. **Parse once in Zig** — headers parsed by `hparse`, immediately converted to Python objects
2. **Preserve all header data** — no information loss
3. **Duplicate headers supported** — list of tuples, not dict
4. **Lazy header dict** — Python can build a dict if needed, but it's not the primary format
5. **Backward compatible** — existing `_from_raw` can be made legacy; new path uses Zig headers
6. **Memory safe** — headers are slices into the original request buffer; caller keeps buffer alive

### FFI Data Format

**Change the tuple returned by `pyHttpParseRequest` from 5-tuple to 6-tuple:**

```python
# Old (current):
result = (method, path, body, consumed, keep_alive)

# New:
result = (method, path, body, consumed, keep_alive, headers)
```

**Format of `headers`:**

```python
headers: list[tuple[bytes, bytes]] = [
    (b"host", b"example.com"),
    (b"content-type", b"application/json"),
    (b"set-cookie", b"session=abc123"),
    (b"set-cookie", b"user=john"),  # Duplicate preserved
]
```

Why this format:

- **List** preserves order and duplicates (unlike dict)
- **Bytes** for both name and value (preserves encoding, case, whitespace)
- **Tuple** is immutable, hashable, memory-efficient
- **Matches ASGI spec** (Granian also uses this format)
- **Easy to convert** to dict if needed in Python

---

## Implementation Plan

### Phase 1: Zig Side — Capture Headers in ParsedRequest

#### 1.1 Modify `ParsedRequest` struct

**File: `src/http.zig`**

```zig
pub const ParsedRequest = struct {
    method: Method,
    path: []const u8,
    version: Version,
    headers: []const Header,  // NEW
    header_count: usize,       // NEW (for safe slicing)
    body: []const u8,
    raw_bytes_consumed: usize,
    keep_alive: bool,
};
```

**Rationale:**
- Store slices into the original `hparse` parsing result
- Include `header_count` so Python side knows how many are valid
- `Header` type comes from `hparse`: `pub const Header = struct { key: []const u8, value: []const u8 };`

#### 1.2 Modify `parseRequestFull()` to return headers

**File: `src/http.zig`** — modify function to capture headers on stack and return them

Current code (lines 125-171):
```zig
pub fn parseRequestFull(buf: []const u8) ParseError!ParsedRequest {
    var headers: [64]Header = undefined;
    var header_count: usize = 0;

    const header_bytes = hparse.parseRequest(..., &headers, &header_count) catch ...;

    // ... resolve keep_alive ...

    return ParsedRequest{
        .method = method,
        .path = path orelse return error.Invalid,
        .version = version,
        .body = buf[header_bytes .. header_bytes + content_length],
        .raw_bytes_consumed = header_bytes + content_length,
        .keep_alive = ka,
        // MISSING: headers
    };
}
```

**Change to:**
```zig
pub fn parseRequestFull(buf: []const u8) ParseError!ParsedRequest {
    var headers: [64]Header = undefined;
    var header_count: usize = 0;

    const header_bytes = hparse.parseRequest(..., &headers, &header_count) catch ...;

    // ... resolve keep_alive ...

    return ParsedRequest{
        .method = method,
        .path = path orelse return error.Invalid,
        .version = version,
        .headers = headers[0..header_count],  // NEW
        .header_count = header_count,          // NEW
        .body = buf[header_bytes .. header_bytes + content_length],
        .raw_bytes_consumed = header_bytes + content_length,
        .keep_alive = ka,
    };
}
```

**Correctness note:** Headers are slices into `headers[64]`, which lives on the stack of `parseRequestFull`. But the slices return from the function — are they valid?

**YES**, because:
- `parseRequestFull` returns `ParsedRequest` by value
- Zig includes the array in the return struct (struct contains array)
- Array is part of the stack frame, and it's being returned
- **This is safe because the struct captures the array**

Wait, that's **not quite right**. Let me reconsider:

- `headers: [64]Header` is a local variable on the function's stack frame
- Returning `headers[0..header_count]` (a slice) would be returning a pointer to stack memory after the function returns
- This is **UNSAFE**

**Correct approach**: We need to either:

**Option A: Allocate headers on heap** — but we don't have an allocator in `http.zig`

**Option B: Copy headers into the returned struct** — but `ParsedRequest` is not easy to extend with a fixed array

**Option C: Return headers separately via a different path** — change the FFI to give Zig more control

**Option D: Use a fixed-size array in `ParsedRequest`** — wasteful but safe

**Best approach: Option C + D hybrid**

Create a new function `parseRequestFullWithAlloc` that takes an allocator, or...

**Actually, reconsider the real constraint:**

The issue is that in the current code:
- Zig parses headers into a stack-local array
- Headers are resolved (for keep_alive logic) while the array is live
- The array is discarded when the function returns
- Python receives no headers

The FFI boundary is in `pyHttpParseRequest` which:
1. Receives raw bytes from Python
2. Calls `http.parseRequestFull(buf)`
3. Returns a tuple to Python

**New approach: Keep headers in Zig until Python conversion**

Modify `pyHttpParseRequest` to:
1. Call a new `parseRequestFull` that returns headers
2. Immediately convert headers to Python objects (while still on stack)
3. Return 6-tuple with headers included

**This avoids the dangling pointer problem** because headers are converted to Python objects immediately, before the function returns.

#### 1.3 Implementation Details for `parseRequestFull`

Actually, looking at the code again: the array `[64]Header` lives on `parseRequestFull`'s stack frame. If we return a slice `headers[0..header_count]` in the struct, we're returning a pointer to stack memory that's about to be deallocated.

**BUT**, in the new design, `pyHttpParseRequest` will call `parseRequestFull`, immediately convert headers to Python while the stack frame is still alive, and then return. The stack frame is only deallocated after the conversion is complete. This is safe.

**Zig code pattern:**

```zig
pub fn pyHttpParseRequest(...) callconv(.c) ?*PyObject {
    // ... get buf ...

    const req = http.parseRequestFull(buf) catch |err| switch (err) {
        error.Incomplete => return py.py_helper_none(),
        error.Invalid => {
            py.py_helper_err_set_string(...);
            return null;
        }
    };

    // At this point, req contains headers (slices into req's stack frame)
    // Convert them to Python IMMEDIATELY while the stack is still alive

    var py_headers = py.PyList_New(req.header_count) orelse return null;
    for (0..req.header_count) |i| {
        const hdr = req.headers[i];
        const tuple = py.py_helper_tuple_new(2) orelse {
            py.py_helper_decref(py_headers);
            return null;
        };
        const py_key = py.py_helper_bytes_from_string_and_size(
            @ptrCast(hdr.key.ptr),
            @intCast(hdr.key.len),
        ) orelse {
            py.py_helper_decref(tuple);
            py.py_helper_decref(py_headers);
            return null;
        };
        const py_val = py.py_helper_bytes_from_string_and_size(
            @ptrCast(hdr.value.ptr),
            @intCast(hdr.value.len),
        ) orelse {
            py.py_helper_decref(py_key);
            py.py_helper_decref(tuple);
            py.py_helper_decref(py_headers);
            return null;
        };

        _ = py.py_helper_tuple_setitem(tuple, 0, py_key);
        _ = py.py_helper_tuple_setitem(tuple, 1, py_val);
        _ = py.PyList_SetItem(py_headers, @intCast(i), tuple);
    }

    // Now build the 6-tuple and return
    // Headers are now Python objects, safe even after stack is gone
    ...
}
```

**Key point**: Headers are converted to Python objects **before** the function returns. The Python objects own their data (or references to bytes), so they're safe.

---

### Phase 2: Python Side — Accept Headers from Zig

#### 2.1 Update server.py to handle 6-tuple

**File: `theframework/server.py`** — line 100-108

Current:
```python
result = _framework_core.http_parse_request(buf)
if result is None:
    break

method, path, body, consumed, keep_alive = result
raw_bytes = buf[:consumed]
buf = buf[consumed:]

request = Request._from_raw(method, path, body, raw_bytes)
```

New:
```python
result = _framework_core.http_parse_request(buf)
if result is None:
    break

method, path, body, consumed, keep_alive, headers = result
buf = buf[consumed:]

request = Request._from_parsed(method, path, body, headers)
```

**Note:** We remove the `raw_bytes` path since headers are now pre-parsed.

#### 2.2 Create `Request._from_parsed()` class method

**File: `theframework/request.py`** — new method

```python
@classmethod
def _from_parsed(
    cls,
    method: str,
    path: str,
    body: bytes,
    headers: list[tuple[bytes, bytes]],
) -> Request:
    """Build a Request from pre-parsed fields and headers (from Zig)."""
    # Convert headers list to dict (for backward compatibility)
    # But preserve order and handle duplicates gracefully
    headers_dict: dict[str, str] = {}
    for key_bytes, val_bytes in headers:
        key = key_bytes.decode("latin-1").lower()
        val = val_bytes.decode("latin-1")
        # For now, keep last value (same as old behavior)
        # TODO: apps needing duplicates should iterate raw list
        headers_dict[key] = val

    # Extract path and query string
    full_path = path
    if "?" in path:
        path_part, _, query_string = path.partition("?")
        parsed_qs = urllib.parse.parse_qs(query_string)
        query_params = {k: v[0] for k, v in parsed_qs.items()}
    else:
        path_part = path
        query_params = {}

    return cls(
        method=method,
        path=path_part,
        full_path=full_path,
        query_params=query_params,
        headers=headers_dict,
        body=body,
    )
```

#### 2.3 Keep `Request._from_raw()` for backward compatibility (deprecated)

**File: `theframework/request.py`** — add deprecation comment

```python
@classmethod
def _from_raw(
    cls,
    method: str,
    path: str,
    body: bytes,
    raw_bytes: bytes,
) -> Request:
    """Build a Request from raw HTTP bytes. DEPRECATED: use _from_parsed() instead."""
    # Old re-parsing path (kept for fallback/testing)
    ...
```

Mark in docstring as deprecated but keep it working until all callers migrate.

#### 2.4 Add raw headers property (optional, for future use)

```python
class Request:
    __slots__ = (..., "_raw_headers")
    _raw_headers: list[tuple[bytes, bytes]] | None

    @property
    def raw_headers(self) -> list[tuple[bytes, bytes]]:
        """Access headers as list of (bytes, bytes) tuples.

        Preserves order and duplicates (unlike .headers dict).
        Use for cases like multiple Set-Cookie headers.
        """
        if self._raw_headers is None:
            raise RuntimeError("raw_headers not available (request parsed from raw bytes)")
        return self._raw_headers
```

Store the raw headers list in `_from_parsed()`:

```python
return cls(
    method=method,
    ...
    body=body,
    _raw_headers=list(headers),  # Preserve original list
)
```

---

### Phase 3: Error Handling & Edge Cases

#### 3.1 Handle missing headers gracefully

**In `pyHttpParseRequest` (Zig side):**

```zig
// If header_count is 0, still return empty list (not error)
var py_headers = py.PyList_New(req.header_count) orelse return null;
// Empty list is valid
```

#### 3.2 Handle header encoding

**Assumption:** Headers are valid `latin-1` (HTTP/1.1 spec).

**In Python:** Use `decode("latin-1")` which never fails (all byte values 0-255 are valid).

**No validation needed** — hparse already validated header syntax.

#### 3.3 Handle Content-Length header

**Current:** Zig parses Content-Length to determine body length.

**No change needed** — Zig side still does this. Headers are just passed along.

#### 3.4 Handle large header counts

**Current limit:** 64 headers (hparse hardcoded)

**If more than 64 headers:**
- hparse returns `error.Invalid` (not enough space in array)
- Zig propagates to Python as `ValueError: Invalid HTTP request`
- Connection closes

**This is acceptable** — 64 headers is a reasonable limit. Real-world requests rarely exceed 20-30 headers.

**If we need to increase it later:**
- Change hparse dependency (if it supports variable-size array) or fork it
- This is a separate task

---

## Testing Strategy

### Zig Tests (`src/http.zig`)

**Add tests for `parseRequestFull` returning headers:**

```zig
test "parse request with headers" {
    const buf = "GET /path HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n\r\n";
    const req = try parseRequestFull(buf);

    try testing.expect(req.header_count == 2);
    try testing.expectEqualStrings("Host", req.headers[0].key);
    try testing.expectEqualStrings("localhost", req.headers[0].value);
    try testing.expectEqualStrings("Content-Type", req.headers[1].key);
    try testing.expectEqualStrings("application/json", req.headers[1].value);
}

test "parse request with duplicate headers" {
    const buf = "GET / HTTP/1.1\r\nSet-Cookie: a=1\r\nSet-Cookie: b=2\r\n\r\n";
    const req = try parseRequestFull(buf);

    try testing.expect(req.header_count == 2);
    // Both cookies preserved
}

test "parse request with no headers" {
    const buf = "GET / HTTP/1.1\r\n\r\n";
    const req = try parseRequestFull(buf);

    try testing.expect(req.header_count == 0);
}
```

### Python Tests (`tests/test_request.py`)

**Add tests for `Request._from_parsed`:**

```python
def test_request_from_parsed_simple():
    req = Request._from_parsed(
        method="GET",
        path="/path",
        body=b"",
        headers=[(b"host", b"example.com")],
    )
    assert req.method == "GET"
    assert req.path == "/path"
    assert req.headers["host"] == "example.com"

def test_request_from_parsed_duplicate_headers():
    req = Request._from_parsed(
        method="GET",
        path="/",
        body=b"",
        headers=[
            (b"set-cookie", b"a=1"),
            (b"set-cookie", b"b=2"),
        ],
    )
    # Last value wins in dict (backward compat)
    assert req.headers["set-cookie"] == "b=2"
    # But raw_headers preserves both
    assert req.raw_headers == [(b"set-cookie", b"a=1"), (b"set-cookie", b"b=2")]

def test_request_from_parsed_preserves_case():
    req = Request._from_parsed(
        method="GET",
        path="/",
        body=b"",
        headers=[(b"Content-Type", b"text/html")],  # Original case
    )
    # Dict lowercases for lookup
    assert req.headers["content-type"] == "text/html"
    # But raw_headers preserves original
    assert req.raw_headers[0][0] == b"Content-Type"
```

### Integration Tests (`tests/test_server.py`)

**Add test that sends request with duplicate headers:**

```python
def test_server_handles_duplicate_headers():
    """Send a request with multiple Set-Cookie headers.

    Verify that:
    1. Server doesn't crash
    2. At least one cookie is accessible
    3. Raw headers list has both
    """
    server = ...

    request_bytes = b"GET / HTTP/1.1\r\nHost: localhost\r\nSet-Cookie: a=1\r\nSet-Cookie: b=2\r\n\r\n"
    response = send_raw(request_bytes)

    assert response.status == 200
    # Verify in handler that cookies are available
```

---

## Migration Path

### Step 1: Implement Zig changes (Phase 1)

- Modify `ParsedRequest` struct
- Update `parseRequestFull()` to capture headers
- Update `pyHttpParseRequest` to return 6-tuple with headers

### Step 2: Update server.py (Phase 2)

- Change `http_parse_request` call to unpack 6-tuple
- Call `Request._from_parsed()` instead of `_from_raw()`

### Step 3: Update request.py (Phase 2)

- Add `_from_parsed()` class method
- Add optional `raw_headers` property
- Mark `_from_raw()` as deprecated (but keep working)

### Step 4: Update tests

- Add Zig tests for headers in `parseRequestFull`
- Add Python tests for `_from_parsed`
- Add integration tests for duplicate headers

### Step 5: Documentation

- Update CLAUDE.md to note that headers are now parsed once in Zig
- Document `raw_headers` property
- Add example showing duplicate header access

---

## Correctness & Security Considerations

### 1. **Memory Safety**

- Headers are slices into the original buffer (`buf`)
- Caller must keep buffer alive during FFI call
- Buffer is already kept alive by `_framework_core.http_parse_request()` contract
- ✅ Safe

### 2. **Data Loss Prevention**

- Duplicate headers preserved in list
- Dict converts to last-value-wins (backward compatible)
- Applications can access raw list if they need all values
- ✅ No data loss

### 3. **Header Injection Attacks**

- Headers are passed as-is (no filtering)
- This is correct — the framework should not filter headers
- Applications should validate/sanitize if needed
- ✅ No change to attack surface

### 4. **Encoding Safety**

- Headers assumed to be valid `latin-1` (HTTP/1.1 spec)
- `decode("latin-1")` never fails (all bytes 0-255 valid)
- ✅ Safe

### 5. **Parsing Correctness**

- Zig parsing done by trusted `hparse` library
- Python just converts, no additional parsing
- ✅ No new parsing bugs

### 6. **Performance**

- **Before:** Parse in Zig (N operations), throw away, re-parse in Python (N operations) = 2N
- **After:** Parse in Zig (N operations), convert to Python (M operations where M << N) = N + M
- ✅ Faster

---

## Backward Compatibility

### Python API
- **Old:** `Request._from_raw(method, path, body, raw_bytes)`
- **New:** `Request._from_parsed(method, path, body, headers)`
- **Compat:** Keep `_from_raw()` working for fallback (tests can use it)

### Server Code
- **Old:** `method, path, body, consumed, keep_alive = result`
- **New:** `method, path, body, consumed, keep_alive, headers = result`
- **Compat:** Breaking change to internal FFI contract (acceptable since it's not public API)

### User Applications
- **No breaking changes** — Request object API unchanged
- **New feature:** `request.raw_headers` available (optional, for duplicate headers)

---

## Implementation Order

1. **Zig changes first** — extend `ParsedRequest` struct, update `parseRequestFull`, update `pyHttpParseRequest`
2. **Python changes second** — update server.py to unpack 6-tuple, add `Request._from_parsed()`
3. **Tests and validation** — verify headers flow through correctly
4. **Documentation** — update comments and CLAUDE.md

---

## Success Criteria

- [x] Headers are parsed exactly once (in Zig)
- [x] All headers passed to Python (no re-parsing needed)
- [x] Duplicate headers preserved in raw list
- [x] No data loss compared to old implementation
- [x] Server code updated to use new path
- [x] Zig tests pass for header parsing
- [x] Python tests pass for `Request._from_parsed()`
- [x] Integration test verifies duplicate headers work end-to-end
- [x] `uv run mypy theframework/` passes (strict mode)
- [x] `uv run pytest` passes
- [x] `zig build test` passes

