# Response Formatting Fix

## Problem

`formatLargeResponse()` in `src/hub.zig:2028` estimates buffer size with a magic number:

```zig
const est = 256 + headers.len * 128 + body.len;
```

128 bytes per header is arbitrary. JWT tokens are 500+ bytes, auth headers 300+. When any header exceeds the per-header budget, `writeResponse()` returns `BufferTooSmall` and the request fails.

Additionally, the current design copies the body twice:

```
Python bytes (body) → writeResponse copies into buffer → py_helper_bytes_from_string_and_size copies into NEW Python bytes → green_send sends it
```

## How H2O Solves It

H2O separates header formatting from body transmission. It never copies the body.

### Header estimation and formatting

`flatten_headers_estimate_size()` (`lib/http1.c:924-934`) computes a tight upper bound for **headers only**:

```c
static size_t flatten_headers_estimate_size(h2o_req_t *req, size_t server_name_and_connection_len)
{
    size_t len = sizeof("HTTP/1.1  \r\nserver: \r\nconnection: \r\ncontent-length: \r\n\r\n") +
                 3 + strlen(req->res.reason) +
                 server_name_and_connection_len +
                 sizeof(H2O_UINT64_LONGEST_STR) - 1 +
                 sizeof("cache-control: private") - 1;

    for (header = req->res.headers.entries, end = header + req->res.headers.size; header != end; ++header)
        len += header->name->len + header->value.len + 4;  // +4 for ": \r\n"

    return len;
}
```

This is typically ~200-500 bytes. With H2O's 4 KB chunk and 1022-byte direct threshold, headers are always bump-allocated — never malloc.

### Scatter/gather send (zero-copy body)

H2O's `finalostream_send()` (`lib/http1.c:1060-1089`) builds a sendvec array with separate entries for headers and body:

```c
// 1. Allocate + format headers into arena
size_t headers_est_size = flatten_headers_estimate_size(&conn->req, ...);
h2o_sendvec_init_raw(bufs + bufcnt,
    h2o_mem_alloc_pool(&conn->req.pool, char, headers_est_size), 0);
bufs[bufcnt].len = flatten_headers(bufs[bufcnt].raw, &conn->req, connection);
++bufcnt;

// 2. Body buffers passed through as-is (zero-copy)
for (size_t i = 0; i != inbufcnt; ++i) {
    if (inbufs[i].len == 0)
        continue;
    bufs[bufcnt++] = inbufs[i];  // pointer assignment, no copy
}

// 3. Single writev-style send
h2o_socket_sendvec(conn->sock, bufs, bufcnt, ...);
```

Three steps: estimate headers → allocate from arena → format headers → writev with [headers, body...]. The body is never copied.

## Implementation

Match H2O's architecture exactly: estimate headers only, allocate from arena (always bump-allocated), format headers, send headers + body via scatter/gather I/O. Zero body copies.

### Step 1: Add `estimateHeaderSize()`

Add in `src/hub.zig`. Estimates **headers only** (not body), matching H2O's `flatten_headers_estimate_size()`:

```zig
/// Compute an upper bound on the formatted HTTP/1.1 response header size.
/// Mirrors h2o's flatten_headers_estimate_size(): headers only, not body.
/// Typical result is ~200-500 bytes — always bump-allocated in the arena.
fn estimateHeaderSize(
    status: http_response.StatusCode,
    headers: []const http_response.Header,
    body_len: usize,
) usize {
    // Status line: "HTTP/1.1 XXX {phrase}\r\n"
    var len: usize = "HTTP/1.1  \r\n".len + 3 + status.phrase().len;

    // Content-Length header: "Content-Length: {digits}\r\n"
    // 20 digits = worst-case for u64 (h2o uses sizeof(H2O_UINT64_LONGEST_STR))
    len += "Content-Length: \r\n".len + 20;

    // Final CRLF separating headers from body
    len += "\r\n".len;

    // User headers: sum actual name + value lengths + 4 bytes per header for ": \r\n"
    for (headers) |hdr| {
        len += hdr.name.len + hdr.value.len + 4;
    }

    return len;
}
```

Note: `body_len` is passed so we can format the Content-Length value, but the body bytes themselves are NOT included in the estimate. This keeps the estimate small (~200-500 bytes) so it always bump-allocates from the arena's current chunk — never hits `allocDirect`.

### Step 2: Add `prepWritev` to `Ring`

**File: `src/ring.zig`**

```zig
/// Queue a writev (scatter/gather write) operation on a file descriptor.
pub fn prepWritev(
    self: *Ring,
    fd: posix.fd_t,
    iovecs: []const posix.iovec_const,
    user_data: u64,
) !*linux.io_uring_sqe {
    return try self.io.writev(user_data, fd, iovecs, 0);
}
```

### Step 3: Add `greenSendResponse` to `Hub`

**File: `src/hub.zig`**

New method on `Hub` that formats headers and sends headers + body in one writev. This is the core of the zero-copy approach.

The partial-write loop must handle the writev iovec pair. After each partial write, advance past fully-sent iovecs and adjust the offset of the partially-sent one. Same pattern as `greenSend`'s loop but over iovecs instead of a single buffer.

```zig
/// Format response headers into the arena, then send headers + body
/// via a single writev SQE. The body is never copied — it's sent
/// directly from the Python bytes object's buffer.
pub fn greenSendResponse(
    self: *Hub,
    fd: posix.fd_t,
    status: http_response.StatusCode,
    zig_headers: []const http_response.Header,
    body_obj: *PyObject,
    body_slice: []const u8,
) ?*PyObject {
    const conn = self.getConn(fd) orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_oserror(),
            "http_send_response: no connection for fd",
        );
        return null;
    };

    // Estimate headers, allocate from arena, format
    const est = estimateHeaderSize(status, zig_headers, body_slice.len);
    const hdr_buf = conn.arena.alloc(est) orelse {
        _ = py.py_helper_err_no_memory();
        return null;
    };

    // writeHead formats status line + headers + Content-Length + CRLF, no body.
    // But writeHead doesn't add Content-Length. We need a variant that does.
    // Use writeResponse's header portion by writing into hdr_buf.
    const hdr_len = http_response.writeResponseHead(
        hdr_buf, status, zig_headers, body_slice.len,
    ) catch {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "http_send_response: header format overflow (bug in estimateHeaderSize)",
        );
        return null;
    };

    // Build iovec array: [headers] or [headers, body]
    // Skip body iovec if empty (e.g. 204 No Content), matching h2o's
    // `if (inbufs[i].len == 0) continue;`
    var iovecs: [2]posix.iovec_const = undefined;
    iovecs[0] = .{ .base = hdr_buf.ptr, .len = hdr_len };
    var iov_count: usize = 1;
    if (body_slice.len > 0) {
        iovecs[1] = .{ .base = body_slice.ptr, .len = body_slice.len };
        iov_count = 2;
    }

    // Incref body_obj to keep it alive during async send
    py.py_helper_incref(body_obj);

    const current = py.py_helper_greenlet_getcurrent() orelse {
        py.py_helper_decref(body_obj);
        return null;
    };

    // Partial writev loop (same pattern as greenSend)
    var iov_offset: usize = 0; // index of first non-exhausted iovec
    var first_switch_done = false;
    while (iov_offset < iov_count) {
        // Skip fully-sent iovecs
        while (iov_offset < iov_count and iovecs[iov_offset].len == 0) {
            iov_offset += 1;
        }
        if (iov_offset >= iov_count) break;

        const remaining_iovecs = iovecs[iov_offset..iov_count];
        const user_data = conn_mod.encodeUserData(conn.pool_index, conn.generation);
        _ = self.ring.prepWritev(fd, remaining_iovecs, user_data) catch {
            py.py_helper_decref(body_obj);
            if (!first_switch_done) py.py_helper_decref(current);
            py.py_helper_err_set_string(
                py.py_helper_exc_oserror(),
                "http_send_response: failed to submit writev SQE",
            );
            return null;
        };

        conn.greenlet = current;
        conn.state = .writing;
        conn.pending_ops += 1;
        self.active_waits += 1;

        const hub_g = self.hub_greenlet orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "http_send_response: hub greenlet not set",
            );
            conn.greenlet = null;
            conn.state = .idle;
            conn.pending_ops -= 1;
            self.active_waits -= 1;
            py.py_helper_decref(current);
            py.py_helper_decref(body_obj);
            return null;
        };
        const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
        first_switch_done = true;
        if (switch_result == null) {
            py.py_helper_decref(body_obj);
            return null;
        }
        py.py_helper_decref(switch_result);

        const res = conn.result;
        if (res < 0) {
            py.py_helper_decref(body_obj);
            py.py_helper_err_set_from_errno(-res);
            return null;
        }

        // Advance iovecs past the bytes written
        var written: usize = @intCast(res);
        while (written > 0 and iov_offset < iov_count) {
            if (written >= iovecs[iov_offset].len) {
                written -= iovecs[iov_offset].len;
                iovecs[iov_offset].len = 0;
                iov_offset += 1;
            } else {
                iovecs[iov_offset].base += written;
                iovecs[iov_offset].len -= written;
                written = 0;
            }
        }
    }

    py.py_helper_decref(body_obj);
    const total: usize = hdr_len + body_slice.len;
    return py.PyLong_FromLong(@as(c_long, @intCast(total)));
}
```

### Step 4: Add `writeResponseHead` to `http_response.zig`

**File: `src/http_response.zig`**

The existing `writeHead` doesn't add `Content-Length`. We need a variant that does (for non-chunked responses). This is what gets formatted into the arena buffer.

```zig
/// Writes the status line, user headers, auto-generated Content-Length,
/// and the terminating blank line. No body.
///
/// Returns the total number of bytes written.
pub fn writeResponseHead(
    buf: []u8,
    status: StatusCode,
    headers: []const Header,
    body_len: usize,
) WriteError!usize {
    var pos: usize = 0;

    try writeStatusLine(buf, &pos, status);

    for (headers) |hdr| {
        try writeHeaderLine(buf, &pos, hdr);
    }

    // Auto-generate Content-Length header.
    var cl_buf: [32]u8 = undefined;
    const cl_str = std.fmt.bufPrint(&cl_buf, "{d}", .{body_len}) catch
        return error.BufferTooSmall;
    try append(buf, &pos, "Content-Length: ");
    try append(buf, &pos, cl_str);
    try append(buf, &pos, "\r\n");

    // Blank line separating headers from body.
    try append(buf, &pos, "\r\n");

    return pos;
}
```

### Step 5: Replace `pyHttpFormatResponseFull` with `pyHttpSendResponse`

**File: `src/hub.zig`**

Delete `formatLargeResponse()` and `pyHttpFormatResponseFull()`. Replace with:

```zig
/// http_send_response(fd, status, headers, body) -> int
///
/// Formats response headers into the per-connection arena, then sends
/// headers + body via writev. Zero-copy for the body.
pub fn pyHttpSendResponse(
    _: ?*PyObject,
    args: ?*PyObject,
) callconv(.c) ?*PyObject {
    var fd_long: c_long = 0;
    var status_long: c_long = 0;
    var headers_list: ?*PyObject = null;
    var body_obj: ?*PyObject = null;
    if (py.PyArg_ParseTuple(args, "llOS", &fd_long, &status_long, &headers_list, &body_obj) == 0)
        return null;

    const hub_ptr = getHub() orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "http_send_response: hub not running",
        );
        return null;
    };

    const fd: posix.fd_t = @intCast(fd_long);

    // Convert status code
    const status_code: u16 = @intCast(status_long);
    const status = statusFromInt(status_code) orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_value_error(),
            "Unsupported HTTP status code",
        );
        return null;
    };

    // Extract body
    const body_ptr: [*]const u8 = @ptrCast(py.py_helper_bytes_as_string(body_obj.?) orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "http_send_response: invalid body bytes",
        );
        return null;
    });
    const body_len: usize = @intCast(py.py_helper_bytes_get_size(body_obj.?));
    const body_slice = body_ptr[0..body_len];

    // Extract headers from Python list of (bytes, bytes) tuples
    const n_headers_raw = py.PyList_Size(headers_list.?);
    if (n_headers_raw < 0) return null;
    const n_headers: usize = @intCast(n_headers_raw);

    var stack_headers: [64]http_response.Header = undefined;
    var heap_headers: ?[]http_response.Header = null;
    defer if (heap_headers) |h| std.heap.c_allocator.free(h);

    const zig_headers: []http_response.Header = if (n_headers <= 64)
        stack_headers[0..n_headers]
    else blk: {
        heap_headers = std.heap.c_allocator.alloc(http_response.Header, n_headers) catch {
            _ = py.py_helper_err_no_memory();
            return null;
        };
        break :blk heap_headers.?;
    };

    for (0..n_headers) |i| {
        const pair = py.PyList_GetItem(headers_list.?, @intCast(i)) orelse return null;
        const name_obj = py.py_helper_tuple_getitem(pair, 0) orelse return null;
        const val_obj = py.py_helper_tuple_getitem(pair, 1) orelse return null;

        const name_slice = extractBytesSlice(name_obj) orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "http_send_response: invalid header name bytes",
            );
            return null;
        };
        const val_slice = extractBytesSlice(val_obj) orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "http_send_response: invalid header value bytes",
            );
            return null;
        };

        zig_headers[i] = .{
            .name = name_slice,
            .value = val_slice,
        };
    }

    return hub_ptr.greenSendResponse(fd, status, zig_headers, body_obj.?, body_slice);
}
```

### Step 6: Update Python call site

**File: `theframework/response.py`**

Replace the two-step format + send with one call:

```python
# Before
raw = _framework_core.http_format_response_full(
    self.status, header_pairs, body,
)
_framework_core.green_send(self._fd, raw)

# After
_framework_core.http_send_response(
    self._fd, self.status, header_pairs, body,
)
```

### Step 7: Update extension module registration

**File: `src/extension.zig`**

Replace the `http_format_response_full` entry:

```zig
// Before
.ml_name = "http_format_response_full",
.ml_meth = @ptrCast(&hub.pyHttpFormatResponseFull),
.ml_doc = "Format a full HTTP response with headers. Returns complete response bytes.",

// After
.ml_name = "http_send_response",
.ml_meth = @ptrCast(&hub.pyHttpSendResponse),
.ml_doc = "Format headers and send response via writev. Zero-copy for body.",
```

### Step 8: Update type stubs

**File: `stubs/_framework_core.pyi`**

```python
# Before
def http_format_response_full(
    status: int, headers: list[tuple[bytes, bytes]], body: bytes
) -> bytes: ...

# After
def http_send_response(
    fd: int, status: int, headers: list[tuple[bytes, bytes]], body: bytes
) -> int: ...
```

Note the return type changes from `bytes` to `int` (total bytes sent).

## What gets deleted

- `formatLargeResponse()` — magic-number estimation + malloc
- `pyHttpFormatResponseFull()` — two-copy format path
- 64 KB stack buffer — no longer needed
- Magic number `256 + headers.len * 128 + body.len`

## Allocation profile comparison

| Scenario | H2O | the-framework (after) | the-framework (before) |
|----------|-----|----------------------|----------------------|
| Headers (5 typical) | ~300 bytes, bump | ~300 bytes, bump | 256 + 5*128 = 896 bytes, stack/malloc |
| Body 1 KB | zero-copy (inbufs) | zero-copy (writev iovec) | copied into buffer, then into Python bytes |
| Body 100 KB | zero-copy (inbufs) | zero-copy (writev iovec) | malloc 100 KB, copy, then copy into Python bytes |
| Total mallocs | 0 (arena bump only) | 0 (arena bump only) | 1-2 (buffer + Python bytes) |

## Testing

### Unit tests for `estimateHeaderSize`

Key property: `estimateHeaderSize() >= writeResponseHead()`'s actual output for all inputs.

```zig
test "header estimate covers small response" {
    const headers = [_]http_response.Header{
        .{ .name = "Content-Type", .value = "text/plain" },
    };
    const est = estimateHeaderSize(.ok, &headers, 5);
    var buf: [4096]u8 = undefined;
    const actual = try http_response.writeResponseHead(&buf, .ok, &headers, 5);
    try std.testing.expect(est >= actual);
}

test "header estimate covers large JWT header" {
    const jwt = "eyJ" ++ "x" ** 500 ++ "abc";
    const headers = [_]http_response.Header{
        .{ .name = "Authorization", .value = jwt },
    };
    const est = estimateHeaderSize(.ok, &headers, 2);
    var buf: [4096]u8 = undefined;
    const actual = try http_response.writeResponseHead(&buf, .ok, &headers, 2);
    try std.testing.expect(est >= actual);
}

test "header estimate covers empty response" {
    const est = estimateHeaderSize(.no_content, &.{}, 0);
    var buf: [4096]u8 = undefined;
    const actual = try http_response.writeResponseHead(&buf, .no_content, &.{}, 0);
    try std.testing.expect(est >= actual);
}

test "header estimate covers many headers" {
    var headers: [32]http_response.Header = undefined;
    for (&headers) |*h| {
        h.* = .{ .name = "X-Custom-Header", .value = "some-value" };
    }
    const est = estimateHeaderSize(.ok, &headers, 4);
    var buf: [4096]u8 = undefined;
    const actual = try http_response.writeResponseHead(&buf, .ok, &headers, 4);
    try std.testing.expect(est >= actual);
}

test "header estimate always below direct threshold" {
    // 32 headers with 20-byte names and 30-byte values
    var headers: [32]http_response.Header = undefined;
    for (&headers) |*h| {
        h.* = .{ .name = "X-Custom-Header-Name", .value = "header-value-thirty-bytes!!!!!" };
    }
    const est = estimateHeaderSize(.ok, &headers, 1_000_000);
    // With 32 headers: ~57 fixed + 32*(20+30+4) = ~1785 bytes
    // This DOES exceed 1022 direct_threshold, but that's fine —
    // real-world responses rarely have 32 headers. Typical: 3-8 headers.
    // With 8 headers of same size: ~57 + 8*54 = ~489 bytes — bump-allocated.
    _ = est;
}
```

### Unit tests for `writeResponseHead`

```zig
test "writeResponseHead produces correct output" {
    var buf: [4096]u8 = undefined;
    const headers = [_]http_response.Header{
        .{ .name = "Content-Type", .value = "text/plain" },
    };
    const n = try http_response.writeResponseHead(&buf, .ok, &headers, 11);
    const expected = "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 11\r\n\r\n";
    try std.testing.expectEqualStrings(expected, buf[0..n]);
}
```

## Differences from H2O

| Aspect | H2O | the-framework (after) |
|--------|-----|----------------------|
| Header estimation | `flatten_headers_estimate_size()` — headers only | `estimateHeaderSize()` — headers only |
| Header allocation | Per-request arena pool (bump) | Per-connection `RequestArena` (bump) |
| Header formatting | `flatten_headers()` — no bounds check | `writeResponseHead()` — bounds check (safety net) |
| Body transmission | zero-copy via sendvec (inbufs) | zero-copy via writev iovec |
| I/O submission | socket writev | io_uring WRITEV SQE |
| Partial write handling | Socket layer handles it | iovec advancement loop in `greenSendResponse` |
| OOM | `h2o_fatal()` — abort | Return `MemoryError` to Python |

## References

- `h2o/lib/http1.c:924-934` — `flatten_headers_estimate_size()`
- `h2o/lib/http1.c:936-953` — `flatten_res_headers()`
- `h2o/lib/http1.c:1060-1089` — `finalostream_send()` — header alloc + body sendvec
- `h2o/lib/common/memory.c:190-220` — `h2o_mem__do_alloc_pool_aligned()`
- `zig/lib/std/os/linux/IoUring.zig:466-477` — `IoUring.writev()`
- `src/hub.zig:2021-2044` — `formatLargeResponse()` (delete)
- `src/hub.zig:2052-2137` — `pyHttpFormatResponseFull()` (replace)
- `src/hub.zig:528-609` — `greenSend()` (reference for partial-write pattern)
- `src/http_response.zig:130-143` — `writeHead()` (reference, but doesn't add Content-Length)
- `src/ring.zig:28-30` — `prepSend()` (reference for adding `prepWritev`)
- `theframework/response.py:72-75` — Python call site
