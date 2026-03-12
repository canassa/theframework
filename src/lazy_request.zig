const std = @import("std");
const http = @import("http.zig");

const py = @cImport({
    @cInclude("py_helpers.h");
});

const PyObject = py.PyObject;

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/// Maximum header key length. Enforced at parse time in hub.zig (tryParse).
/// The headers getter uses a stack buffer of this size for lowercasing.
pub const MAX_HEADER_KEY_LEN: usize = 256;

// ---------------------------------------------------------------------------
// Interned method strings
// ---------------------------------------------------------------------------

var interned_methods: [@typeInfo(http.Method).@"enum".fields.len]?*PyObject =
    .{null} ** @typeInfo(http.Method).@"enum".fields.len;

pub fn initInternedMethods() bool {
    const entries = .{
        .{ http.Method.get, "GET" },
        .{ http.Method.post, "POST" },
        .{ http.Method.put, "PUT" },
        .{ http.Method.delete, "DELETE" },
        .{ http.Method.head, "HEAD" },
        .{ http.Method.options, "OPTIONS" },
        .{ http.Method.patch, "PATCH" },
        .{ http.Method.connect, "CONNECT" },
        .{ http.Method.trace, "TRACE" },
    };
    inline for (entries) |entry| {
        interned_methods[@intFromEnum(entry[0])] =
            py.PyUnicode_InternFromString(entry[1]) orelse return false;
    }
    return true;
}

pub fn methodToPyStr(method: http.Method) ?*PyObject {
    const idx = @intFromEnum(method);
    if (idx < interned_methods.len) {
        if (interned_methods[idx]) |s| {
            py.py_helper_incref(s);
            return s;
        }
    }
    // .unknown — not interned, fall back to dynamic string.
    return py.PyUnicode_FromStringAndSize("UNKNOWN", 7);
}

// ---------------------------------------------------------------------------
// Type definition
// ---------------------------------------------------------------------------

const LazyRequestObject = py.LazyRequestObject;

// --- tp_new: prevent direct construction ---
fn lazy_request_tp_new(
    _: ?*py.PyTypeObject,
    _: ?*PyObject,
    _: ?*PyObject,
) callconv(.c) ?*PyObject {
    py.py_helper_err_set_string(
        py.py_helper_exc_type_error(),
        "Request objects cannot be created directly",
    );
    return null;
}

// --- tp_dealloc ---
pub fn lazy_request_dealloc(self_raw: ?*PyObject) callconv(.c) void {
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

// --- Eager getters ---
fn lazy_request_get_method(self_raw: ?*PyObject, _: ?*anyopaque) callconv(.c) ?*PyObject {
    const self: *LazyRequestObject = @ptrCast(@alignCast(self_raw));
    if (self.method) |m| {
        py.py_helper_incref(m);
        return m;
    }
    return py.py_helper_none();
}

fn lazy_request_get_path(self_raw: ?*PyObject, _: ?*anyopaque) callconv(.c) ?*PyObject {
    const self: *LazyRequestObject = @ptrCast(@alignCast(self_raw));
    if (self.path) |p| {
        py.py_helper_incref(p);
        return p;
    }
    return py.py_helper_none();
}

fn lazy_request_get_full_path(self_raw: ?*PyObject, _: ?*anyopaque) callconv(.c) ?*PyObject {
    const self: *LazyRequestObject = @ptrCast(@alignCast(self_raw));
    if (self.full_path) |fp| {
        py.py_helper_incref(fp);
        return fp;
    }
    return py.py_helper_none();
}

fn lazy_request_get_keep_alive(self_raw: ?*PyObject, _: ?*anyopaque) callconv(.c) ?*PyObject {
    const self: *LazyRequestObject = @ptrCast(@alignCast(self_raw));
    const result = if (self.keep_alive != 0) py.py_helper_true() else py.py_helper_false();
    py.py_helper_incref(result);
    return result;
}

// --- Lazy getters ---

fn raiseInvalidatedError() ?*PyObject {
    py.py_helper_err_set_string(
        py.py_helper_exc_runtime_error(),
        "Request accessed after handler returned",
    );
    return null;
}

fn lazy_request_get_body(self_raw: ?*PyObject, _: ?*anyopaque) callconv(.c) ?*PyObject {
    const self: *LazyRequestObject = @ptrCast(@alignCast(self_raw));

    // Return cached if available
    if (self.cached_body) |b| {
        py.py_helper_incref(b);
        return b;
    }

    // Check validity
    if (self.valid == 0) return raiseInvalidatedError();

    // Build from raw pointers
    const body = py.py_helper_bytes_from_string_and_size(
        self.raw_body_ptr,
        self.raw_body_len,
    ) orelse return null;

    // Cache it (incref for cache ownership, body already has refcount 1 for caller)
    py.py_helper_incref(body);
    self.cached_body = body;
    return body;
}

fn lazy_request_get_headers(self_raw: ?*PyObject, _: ?*anyopaque) callconv(.c) ?*PyObject {
    const self: *LazyRequestObject = @ptrCast(@alignCast(self_raw));

    // Return cached if available
    if (self.cached_headers) |h| {
        py.py_helper_incref(h);
        return h;
    }

    // Check validity
    if (self.valid == 0) return raiseInvalidatedError();

    // Build dict from raw header pointers
    const dict = py.PyDict_New() orelse return null;

    const header_count: usize = @intCast(self.raw_headers_count);
    const headers_ptr: [*]const http.Header = @ptrCast(@alignCast(self.raw_headers_ptr));
    const headers = headers_ptr[0..header_count];

    for (headers) |hdr| {
        // Lowercase key into stack buffer
        if (hdr.key.len > MAX_HEADER_KEY_LEN) {
            // Should never happen — tryParse rejects keys > MAX_HEADER_KEY_LEN
            py.py_helper_decref(dict);
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "header key too long",
            );
            return null;
        }
        var lower_buf: [MAX_HEADER_KEY_LEN]u8 = undefined;
        @memcpy(lower_buf[0..hdr.key.len], hdr.key);
        toLowerAscii(lower_buf[0..hdr.key.len]);

        const py_key = py.PyUnicode_FromStringAndSize(
            @ptrCast(&lower_buf),
            @intCast(hdr.key.len),
        ) orelse {
            py.py_helper_decref(dict);
            return null;
        };

        // Value: decode as Latin-1 (RFC 7230 obs-text)
        const py_val = py.PyUnicode_DecodeLatin1(
            @ptrCast(hdr.value.ptr),
            @intCast(hdr.value.len),
            null,
        ) orelse {
            py.py_helper_decref(py_key);
            py.py_helper_decref(dict);
            return null;
        };

        // Check for duplicate key — comma-join per RFC 9110 §5.2
        const existing = py.PyDict_GetItem(dict, py_key); // borrowed ref
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
    }

    // Cache (incref for cache ownership)
    py.py_helper_incref(dict);
    self.cached_headers = dict;
    return dict;
}

fn lazy_request_get_query_params(self_raw: ?*PyObject, _: ?*anyopaque) callconv(.c) ?*PyObject {
    const self: *LazyRequestObject = @ptrCast(@alignCast(self_raw));

    // Return cached if available
    if (self.cached_query_params) |q| {
        py.py_helper_incref(q);
        return q;
    }

    // Check validity
    if (self.valid == 0) return raiseInvalidatedError();

    // If no query string, return empty dict
    if (self.raw_qs_ptr == null or self.raw_qs_len == 0) {
        const empty = py.PyDict_New() orelse return null;
        py.py_helper_incref(empty);
        self.cached_query_params = empty;
        return empty;
    }

    const qs_len: usize = @intCast(self.raw_qs_len);

    // Copy query string so we can mutate it (plusToSpace + percentDecodeInPlace)
    const qs_buf: ?[*]u8 = @ptrCast(py.PyMem_Malloc(qs_len));
    if (qs_buf == null) {
        _ = py.py_helper_err_no_memory();
        return null;
    }
    defer py.PyMem_Free(qs_buf);
    const qs = qs_buf.?[0..qs_len];
    const raw_qs: [*]const u8 = @ptrCast(self.raw_qs_ptr.?);
    @memcpy(qs, raw_qs[0..qs_len]);

    const dict = buildQueryParams(qs) orelse return null;

    // Cache (incref for cache ownership)
    py.py_helper_incref(dict);
    self.cached_query_params = dict;
    return dict;
}

fn buildQueryParams(qs: []u8) ?*PyObject {
    const dict = py.PyDict_New() orelse return null;

    var pairs = std.mem.splitScalar(u8, qs, '&');
    while (pairs.next()) |pair| {
        if (pair.len == 0) continue;
        const eq_pos = std.mem.indexOfScalar(u8, pair, '=') orelse pair.len;

        // We need mutable slices for plusToSpace and percentDecodeInPlace.
        // pair is already a slice into our mutable qs buffer, but Zig's
        // splitScalar returns const slices. We need to reconstruct mutable
        // pointers from the known offsets.
        const pair_offset = @intFromPtr(pair.ptr) - @intFromPtr(qs.ptr);
        const key_buf = qs[pair_offset .. pair_offset + eq_pos];
        const val_buf: []u8 = if (eq_pos < pair.len)
            qs[pair_offset + eq_pos + 1 .. pair_offset + pair.len]
        else
            qs[0..0];

        plusToSpace(key_buf);
        plusToSpace(val_buf);
        const key = std.Uri.percentDecodeInPlace(key_buf);
        const val = std.Uri.percentDecodeInPlace(val_buf);

        const py_key = py.PyUnicode_FromStringAndSize(
            @ptrCast(key.ptr),
            @intCast(key.len),
        ) orelse {
            py.py_helper_decref(dict);
            return null;
        };
        const py_val = py.PyUnicode_FromStringAndSize(
            @ptrCast(val.ptr),
            @intCast(val.len),
        ) orelse {
            py.py_helper_decref(py_key);
            py.py_helper_decref(dict);
            return null;
        };
        _ = py.PyDict_SetItem(dict, py_key, py_val); // last-value-wins
        py.py_helper_decref(py_key);
        py.py_helper_decref(py_val);
    }
    return dict;
}

// --- _invalidate method ---
fn lazy_request_invalidate(self_raw: ?*PyObject, _: ?*PyObject) callconv(.c) ?*PyObject {
    const self: *LazyRequestObject = @ptrCast(@alignCast(self_raw));
    self.valid = 0;
    self.raw_body_ptr = null;
    self.raw_headers_ptr = null;
    self.raw_qs_ptr = null;
    const none = py.py_helper_none();
    py.py_helper_incref(none);
    return none;
}

// --- params getter (custom getter for cached_params) ---
fn lazy_request_get_params(self_raw: ?*PyObject, _: ?*anyopaque) callconv(.c) ?*PyObject {
    const self: *LazyRequestObject = @ptrCast(@alignCast(self_raw));
    if (self.cached_params) |p| {
        py.py_helper_incref(p);
        return p;
    }
    // Return None if not set yet
    const none = py.py_helper_none();
    py.py_helper_incref(none);
    return none;
}

fn lazy_request_set_params(self_raw: ?*PyObject, value: ?*PyObject, _: ?*anyopaque) callconv(.c) c_int {
    const self: *LazyRequestObject = @ptrCast(@alignCast(self_raw));
    // Decref old value if any
    if (self.cached_params) |old| {
        py.py_helper_decref(old);
    }
    if (value) |v| {
        py.py_helper_incref(v);
        self.cached_params = v;
    } else {
        self.cached_params = null;
    }
    return 0;
}


// ---------------------------------------------------------------------------
// PyGetSetDef / PyMethodDef / PyTypeObject tables
// ---------------------------------------------------------------------------

const getset_table = [_]py.PyGetSetDef{
    .{ .name = "method", .get = &lazy_request_get_method, .set = null, .doc = "HTTP method", .closure = null },
    .{ .name = "path", .get = &lazy_request_get_path, .set = null, .doc = "Request path (without query string)", .closure = null },
    .{ .name = "full_path", .get = &lazy_request_get_full_path, .set = null, .doc = "Full request path (with query string)", .closure = null },
    .{ .name = "body", .get = &lazy_request_get_body, .set = null, .doc = "Request body bytes", .closure = null },
    .{ .name = "headers", .get = &lazy_request_get_headers, .set = null, .doc = "Request headers dict", .closure = null },
    .{ .name = "query_params", .get = &lazy_request_get_query_params, .set = null, .doc = "Query parameters dict", .closure = null },
    .{ .name = "_keep_alive", .get = &lazy_request_get_keep_alive, .set = null, .doc = "Keep-alive flag (internal)", .closure = null },
    .{ .name = "params", .get = &lazy_request_get_params, .set = @ptrCast(&lazy_request_set_params), .doc = "Route parameters dict", .closure = null },
    // Sentinel
    .{ .name = null, .get = null, .set = null, .doc = null, .closure = null },
};

// Methods table — ml_flags requires extern call, so initialized at runtime.
var methods_table: [2]py.PyMethodDef = undefined;

// The PyTypeObject for LazyRequest.
// We use a var because PyType_Ready modifies it.
// tp_flags cannot be set at comptime (py_helper_tpflags_default is extern),
// so it is set in initLazyRequestType() which must be called before PyType_Ready.
pub var LazyRequestType: py.PyTypeObject = blk: {
    var t: py.PyTypeObject = std.mem.zeroes(py.PyTypeObject);
    t.tp_name = "_framework_core.Request";
    t.tp_basicsize = @sizeOf(LazyRequestObject);
    t.tp_dealloc = @ptrCast(&lazy_request_dealloc);
    t.tp_doc = "HTTP Request object (zero-copy lazy)";
    t.tp_getset = @constCast(@ptrCast(&getset_table));
    // tp_methods is set at runtime in initLazyRequestType() (needs extern call for ml_flags)
    t.tp_new = @ptrCast(&lazy_request_tp_new);
    break :blk t;
};

/// Must be called once before PyType_Ready to set tp_flags and methods table
/// (extern calls, not comptime).
pub fn initLazyRequestType() void {
    // Initialize methods table (ml_flags needs extern call)
    const noargs = py.py_helper_meth_noargs();
    methods_table[0] = .{
        .ml_name = "_invalidate",
        .ml_meth = @ptrCast(&lazy_request_invalidate),
        .ml_flags = noargs,
        .ml_doc = "Invalidate request (called after handler returns)",
    };
    methods_table[1] = py.py_helper_method_sentinel();

    LazyRequestType.tp_flags = py.py_helper_tpflags_default();
    LazyRequestType.tp_methods = @ptrCast(&methods_table);
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn toLowerAscii(buf: []u8) void {
    for (buf) |*c| {
        if (c.* >= 'A' and c.* <= 'Z') {
            c.* = c.* + 32;
        }
    }
}

fn plusToSpace(buf: []u8) void {
    for (buf) |*c| {
        if (c.* == '+') c.* = ' ';
    }
}
