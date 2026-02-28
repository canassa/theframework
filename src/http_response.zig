const std = @import("std");

/// HTTP status codes with their reason phrases.
pub const StatusCode = enum(u16) {
    ok = 200,
    created = 201,
    no_content = 204,
    moved_permanently = 301,
    found = 302,
    not_modified = 304,
    bad_request = 400,
    unauthorized = 401,
    forbidden = 403,
    not_found = 404,
    method_not_allowed = 405,
    payload_too_large = 413,
    request_header_fields_too_large = 431,
    internal_server_error = 500,
    bad_gateway = 502,
    service_unavailable = 503,
    gateway_timeout = 504,

    /// Returns the numeric HTTP status code.
    pub fn code(self: StatusCode) u16 {
        return @intFromEnum(self);
    }

    /// Returns the standard HTTP reason phrase for this status code.
    pub fn phrase(self: StatusCode) []const u8 {
        return switch (self) {
            .ok => "OK",
            .created => "Created",
            .no_content => "No Content",
            .moved_permanently => "Moved Permanently",
            .found => "Found",
            .not_modified => "Not Modified",
            .bad_request => "Bad Request",
            .unauthorized => "Unauthorized",
            .forbidden => "Forbidden",
            .not_found => "Not Found",
            .method_not_allowed => "Method Not Allowed",
            .payload_too_large => "Payload Too Large",
            .request_header_fields_too_large => "Request Header Fields Too Large",
            .internal_server_error => "Internal Server Error",
            .bad_gateway => "Bad Gateway",
            .service_unavailable => "Service Unavailable",
            .gateway_timeout => "Gateway Timeout",
        };
    }
};

/// A single HTTP header name-value pair.
pub const Header = struct {
    name: []const u8,
    value: []const u8,
};

/// Error returned when the output buffer is too small to hold the response.
pub const WriteError = error{BufferTooSmall};

/// Appends `str` to `buf` starting at `pos`, advancing `pos`.
/// Returns `error.BufferTooSmall` if the buffer cannot hold the string.
inline fn append(buf: []u8, pos: *usize, str: []const u8) WriteError!void {
    if (pos.* + str.len > buf.len) return error.BufferTooSmall;
    @memcpy(buf[pos.*..][0..str.len], str);
    pos.* += str.len;
}

/// Writes the status line ("HTTP/1.1 {code} {phrase}\r\n") into `buf` at `pos`.
fn writeStatusLine(buf: []u8, pos: *usize, status: StatusCode) WriteError!void {
    try append(buf, pos, "HTTP/1.1 ");

    // Format the numeric status code into a small stack buffer.
    var code_buf: [8]u8 = undefined;
    const code_str = std.fmt.bufPrint(&code_buf, "{d}", .{status.code()}) catch
        return error.BufferTooSmall;
    try append(buf, pos, code_str);

    try append(buf, pos, " ");
    try append(buf, pos, status.phrase());
    try append(buf, pos, "\r\n");
}

/// Writes a single header line ("Name: Value\r\n") into `buf` at `pos`.
fn writeHeaderLine(buf: []u8, pos: *usize, header: Header) WriteError!void {
    try append(buf, pos, header.name);
    try append(buf, pos, ": ");
    try append(buf, pos, header.value);
    try append(buf, pos, "\r\n");
}

/// Writes a complete HTTP/1.1 response into the caller-provided buffer.
///
/// The output consists of: status line, provided headers, an auto-generated
/// `Content-Length` header, the blank line terminator, and the body.
///
/// Returns the total number of bytes written.
pub fn writeResponse(buf: []u8, status: StatusCode, headers: []const Header, body: []const u8) WriteError!usize {
    var pos: usize = 0;

    try writeStatusLine(buf, &pos, status);

    for (headers) |hdr| {
        try writeHeaderLine(buf, &pos, hdr);
    }

    // Auto-generate Content-Length header.
    var cl_buf: [32]u8 = undefined;
    const cl_str = std.fmt.bufPrint(&cl_buf, "{d}", .{body.len}) catch
        return error.BufferTooSmall;
    try append(buf, &pos, "Content-Length: ");
    try append(buf, &pos, cl_str);
    try append(buf, &pos, "\r\n");

    // Blank line separating headers from body.
    try append(buf, &pos, "\r\n");

    // Body.
    try append(buf, &pos, body);

    return pos;
}

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

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test "writeResponse 200 with body" {
    var buf: [4096]u8 = undefined;
    const n = try writeResponse(&buf, .ok, &.{}, "hello world");
    const expected = "HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\nhello world";
    try std.testing.expectEqualStrings(expected, buf[0..n]);
}

test "writeResponse 404 with no body" {
    var buf: [4096]u8 = undefined;
    const n = try writeResponse(&buf, .not_found, &.{}, "");
    const expected = "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n";
    try std.testing.expectEqualStrings(expected, buf[0..n]);
}

test "writeResponse with custom headers" {
    var buf: [4096]u8 = undefined;
    const hdrs = [_]Header{
        .{ .name = "Content-Type", .value = "text/plain" },
        .{ .name = "X-Custom", .value = "test" },
    };
    const n = try writeResponse(&buf, .ok, &hdrs, "hi");
    const expected = "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nX-Custom: test\r\nContent-Length: 2\r\n\r\nhi";
    try std.testing.expectEqualStrings(expected, buf[0..n]);
}

test "writeResponse buffer too small" {
    var buf: [10]u8 = undefined;
    const result = writeResponse(&buf, .ok, &.{}, "hello");
    try std.testing.expectError(error.BufferTooSmall, result);
}

test "writeResponse 500 Internal Server Error" {
    var buf: [4096]u8 = undefined;
    const n = try writeResponse(&buf, .internal_server_error, &.{}, "error");
    // Just verify it starts with the right status line
    try std.testing.expect(std.mem.startsWith(u8, buf[0..n], "HTTP/1.1 500 Internal Server Error\r\n"));
}

test "StatusCode phrase and code" {
    try std.testing.expectEqualStrings("OK", StatusCode.ok.phrase());
    try std.testing.expectEqual(@as(u16, 200), StatusCode.ok.code());
    try std.testing.expectEqualStrings("Not Found", StatusCode.not_found.phrase());
    try std.testing.expectEqual(@as(u16, 404), StatusCode.not_found.code());
    try std.testing.expectEqualStrings("Service Unavailable", StatusCode.service_unavailable.phrase());
    try std.testing.expectEqual(@as(u16, 503), StatusCode.service_unavailable.code());
}

test "writeResponse 201 Created" {
    var buf: [4096]u8 = undefined;
    const n = try writeResponse(&buf, .created, &.{}, "{\"id\":1}");
    const expected = "HTTP/1.1 201 Created\r\nContent-Length: 8\r\n\r\n{\"id\":1}";
    try std.testing.expectEqualStrings(expected, buf[0..n]);
}

test "writeResponse 204 No Content" {
    var buf: [4096]u8 = undefined;
    const n = try writeResponse(&buf, .no_content, &.{}, "");
    const expected = "HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n";
    try std.testing.expectEqualStrings(expected, buf[0..n]);
}

test "writeResponseHead produces correct output" {
    var buf: [4096]u8 = undefined;
    const headers = [_]Header{
        .{ .name = "Content-Type", .value = "text/plain" },
    };
    const n = try writeResponseHead(&buf, .ok, &headers, 11);
    const expected = "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 11\r\n\r\n";
    try std.testing.expectEqualStrings(expected, buf[0..n]);
}

test "writeResponseHead no headers" {
    var buf: [4096]u8 = undefined;
    const n = try writeResponseHead(&buf, .not_found, &.{}, 0);
    const expected = "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n";
    try std.testing.expectEqualStrings(expected, buf[0..n]);
}

test "writeResponseHead buffer too small" {
    var buf: [10]u8 = undefined;
    const result = writeResponseHead(&buf, .ok, &.{}, 0);
    try std.testing.expectError(error.BufferTooSmall, result);
}
