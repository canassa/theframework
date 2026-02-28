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
// Public API
// ---------------------------------------------------------------------------

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

