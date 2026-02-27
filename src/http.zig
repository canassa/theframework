const std = @import("std");
const hparse = @import("hparse");

// ---------------------------------------------------------------------------
// Re-exported hparse types
// ---------------------------------------------------------------------------
pub const Method = hparse.Method;
pub const Version = hparse.Version;
pub const Header = hparse.Header;

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------
pub const ParseError = error{ Incomplete, Invalid };

// ---------------------------------------------------------------------------
// Request
// ---------------------------------------------------------------------------
pub const Request = struct {
    method: Method,
    path: []const u8,
    version: Version,
    headers: []const Header,
    header_count: usize,
    body: []const u8,
    raw_bytes_consumed: usize, // total bytes consumed including body
};

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// Parse an HTTP/1.x request from `buf`.
///
/// On success the returned `Request` borrows slices directly into `buf`, so
/// the caller must keep `buf` alive for as long as the `Request` is used.
///
/// Returns `error.Incomplete` when the buffer does not yet contain a full
/// request (headers or body), and `error.Invalid` when the request is
/// malformed.
pub fn parseRequest(buf: []const u8) ParseError!Request {
    var method: Method = .unknown;
    var path: ?[]const u8 = null;
    var version: Version = .@"1.0";
    var headers: [64]Header = undefined;
    var header_count: usize = 0;

    const header_bytes = hparse.parseRequest(
        buf,
        &method,
        &path,
        &version,
        &headers,
        &header_count,
    ) catch |err| switch (err) {
        error.Incomplete => return error.Incomplete,
        error.Invalid => return error.Invalid,
    };

    // Determine body length from Content-Length header (default 0).
    const content_length: usize = blk: {
        const cl_value = findHeader(headers[0..header_count], "content-length") orelse break :blk 0;
        const trimmed = std.mem.trim(u8, cl_value, &std.ascii.whitespace);
        break :blk std.fmt.parseInt(usize, trimmed, 10) catch return error.Invalid;
    };

    // Ensure the full body is present in the buffer.
    if (buf.len < header_bytes + content_length) {
        return error.Incomplete;
    }

    return Request{
        .method = method,
        .path = path orelse return error.Invalid,
        .version = version,
        .headers = headers[0..header_count],
        .header_count = header_count,
        .body = buf[header_bytes .. header_bytes + content_length],
        .raw_bytes_consumed = header_bytes + content_length,
    };
}

/// Find the first header whose name matches `name` (case-insensitive).
/// Returns the header value, or null if not found.
pub fn findHeader(headers: []const Header, name: []const u8) ?[]const u8 {
    for (headers) |h| {
        if (std.ascii.eqlIgnoreCase(h.key, name)) {
            return h.value;
        }
    }
    return null;
}

/// Returns whether the connection should be kept alive after this request.
///
/// Checks the `Connection` header first; if absent, defaults to keep-alive
/// for HTTP/1.1 and close for HTTP/1.0.
pub fn isKeepAlive(req: Request) bool {
    if (findHeader(req.headers[0..req.header_count], "connection")) |conn| {
        if (std.ascii.eqlIgnoreCase(conn, "close")) return false;
        if (std.ascii.eqlIgnoreCase(conn, "keep-alive")) return true;
    }
    return req.version == .@"1.1";
}

// ---------------------------------------------------------------------------
// ParsedRequest — safe variant that resolves keep-alive inline
// ---------------------------------------------------------------------------

/// Like `Request` but with `keep_alive` resolved and no dangling header slice.
/// All slices point into the original `buf`.
pub const ParsedRequest = struct {
    method: Method,
    path: []const u8,
    version: Version,
    body: []const u8,
    raw_bytes_consumed: usize,
    keep_alive: bool,
};

/// Parse an HTTP/1.x request and determine keep-alive in one shot.
///
/// This avoids the dangling-header-slice problem of `parseRequest` + `isKeepAlive`:
/// the header array lives on this function's stack and is accessed while still valid.
pub fn parseRequestFull(buf: []const u8) ParseError!ParsedRequest {
    var method: Method = .unknown;
    var path: ?[]const u8 = null;
    var version: Version = .@"1.0";
    var headers: [64]Header = undefined;
    var header_count: usize = 0;

    const header_bytes = hparse.parseRequest(
        buf,
        &method,
        &path,
        &version,
        &headers,
        &header_count,
    ) catch |err| switch (err) {
        error.Incomplete => return error.Incomplete,
        error.Invalid => return error.Invalid,
    };

    const content_length: usize = blk: {
        const cl_value = findHeader(headers[0..header_count], "content-length") orelse break :blk 0;
        const trimmed = std.mem.trim(u8, cl_value, &std.ascii.whitespace);
        break :blk std.fmt.parseInt(usize, trimmed, 10) catch return error.Invalid;
    };

    if (buf.len < header_bytes + content_length) {
        return error.Incomplete;
    }

    // Resolve keep-alive while headers are still on the stack.
    const ka = blk: {
        if (findHeader(headers[0..header_count], "connection")) |conn| {
            if (std.ascii.eqlIgnoreCase(conn, "close")) break :blk false;
            if (std.ascii.eqlIgnoreCase(conn, "keep-alive")) break :blk true;
        }
        break :blk version == .@"1.1";
    };

    return ParsedRequest{
        .method = method,
        .path = path orelse return error.Invalid,
        .version = version,
        .body = buf[header_bytes .. header_bytes + content_length],
        .raw_bytes_consumed = header_bytes + content_length,
        .keep_alive = ka,
    };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
const testing = std.testing;

test "parse simple GET request" {
    const buf = "GET /path HTTP/1.1\r\nHost: localhost\r\n\r\n";
    const req = try parseRequest(buf);
    try testing.expectEqual(Method.get, req.method);
    try testing.expectEqualStrings("/path", req.path);
    try testing.expectEqual(Version.@"1.1", req.version);
    try testing.expect(req.header_count == 1);
    try testing.expectEqualStrings("Host", req.headers[0].key);
    try testing.expectEqualStrings("localhost", req.headers[0].value);
    try testing.expectEqualStrings("", req.body);
}

test "parse POST with Content-Length body" {
    const buf = "POST /submit HTTP/1.1\r\nHost: localhost\r\nContent-Length: 5\r\n\r\nhello";
    const req = try parseRequest(buf);
    try testing.expectEqual(Method.post, req.method);
    try testing.expectEqualStrings("/submit", req.path);
    try testing.expectEqualStrings("hello", req.body);
    try testing.expectEqual(buf.len, req.raw_bytes_consumed);
}

test "incomplete request returns error.Incomplete" {
    const buf = "GET /path HTTP/1.1\r\nHost: local";
    const result = parseRequest(buf);
    try testing.expectError(error.Incomplete, result);
}

test "invalid request returns error" {
    // hparse treats malformed data without a recognizable request line as
    // Incomplete (not enough valid data to form a request).
    const buf = "GARBAGE\r\n\r\n";
    const result = parseRequest(buf);
    try testing.expectError(error.Incomplete, result);
}

test "pipelined requests" {
    const buf = "GET /a HTTP/1.1\r\nHost: localhost\r\n\r\nGET /b HTTP/1.1\r\nHost: localhost\r\n\r\n";
    const req1 = try parseRequest(buf);
    try testing.expectEqualStrings("/a", req1.path);
    const req2 = try parseRequest(buf[req1.raw_bytes_consumed..]);
    try testing.expectEqualStrings("/b", req2.path);
}

test "POST with incomplete body returns error.Incomplete" {
    const buf = "POST /submit HTTP/1.1\r\nContent-Length: 10\r\n\r\nhello";
    const result = parseRequest(buf);
    try testing.expectError(error.Incomplete, result);
}

test "Connection close means not keep-alive" {
    const buf = "GET / HTTP/1.1\r\nConnection: close\r\n\r\n";
    const req = try parseRequest(buf);
    try testing.expect(!isKeepAlive(req));
}

test "HTTP/1.1 default is keep-alive" {
    const buf = "GET / HTTP/1.1\r\nHost: localhost\r\n\r\n";
    const req = try parseRequest(buf);
    try testing.expect(isKeepAlive(req));
}

test "HTTP/1.0 default is not keep-alive" {
    const buf = "GET / HTTP/1.0\r\nHost: localhost\r\n\r\n";
    const req = try parseRequest(buf);
    try testing.expect(!isKeepAlive(req));
}
