const std = @import("std");

/// HTTP status codes with their reason phrases.
pub const StatusCode = enum(u16) {
    ok = 200,
    created = 201,
    no_content = 204,
    moved_permanently = 301,
    bad_request = 400,
    not_found = 404,
    method_not_allowed = 405,
    request_header_fields_too_large = 431,
    internal_server_error = 500,
    service_unavailable = 503,

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
            .bad_request => "Bad Request",
            .not_found => "Not Found",
            .method_not_allowed => "Method Not Allowed",
            .request_header_fields_too_large => "Request Header Fields Too Large",
            .internal_server_error => "Internal Server Error",
            .service_unavailable => "Service Unavailable",
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

/// Writes the status line and headers (with terminating blank line) but no body.
///
/// Does NOT add a Content-Length header automatically. This is intended for
/// chunked or streaming responses where the caller manages transfer encoding.
///
/// Returns the total number of bytes written.
pub fn writeHead(buf: []u8, status: StatusCode, headers: []const Header) WriteError!usize {
    var pos: usize = 0;

    try writeStatusLine(buf, &pos, status);

    for (headers) |hdr| {
        try writeHeaderLine(buf, &pos, hdr);
    }

    // Blank line terminating the header section.
    try append(buf, &pos, "\r\n");

    return pos;
}

/// Writes a single chunked transfer encoding chunk.
///
/// Output format: `{hex_length}\r\n{data}\r\n`
///
/// Returns the total number of bytes written.
pub fn writeChunk(buf: []u8, data: []const u8) WriteError!usize {
    var pos: usize = 0;

    // Format the chunk size as lowercase hex followed by CRLF.
    var hex_buf: [32]u8 = undefined;
    const hex_str = std.fmt.bufPrint(&hex_buf, "{x}\r\n", .{data.len}) catch
        return error.BufferTooSmall;
    try append(buf, &pos, hex_str);

    // Chunk data.
    try append(buf, &pos, data);

    // Trailing CRLF after the chunk data.
    try append(buf, &pos, "\r\n");

    return pos;
}

/// Writes the terminating zero-length chunk that signals end of chunked data.
///
/// Output: `0\r\n\r\n`
///
/// Returns the total number of bytes written (always 5).
pub fn writeEnd(buf: []u8) WriteError!usize {
    var pos: usize = 0;
    try append(buf, &pos, "0\r\n\r\n");
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

test "writeHead for chunked streaming" {
    var buf: [4096]u8 = undefined;
    const hdrs = [_]Header{
        .{ .name = "Transfer-Encoding", .value = "chunked" },
    };
    const n = try writeHead(&buf, .ok, &hdrs);
    const expected = "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n";
    try std.testing.expectEqualStrings(expected, buf[0..n]);
}

test "writeChunk and writeEnd" {
    var buf: [4096]u8 = undefined;
    var pos: usize = 0;
    pos += try writeChunk(buf[pos..], "hello");
    pos += try writeChunk(buf[pos..], " world");
    pos += try writeEnd(buf[pos..]);
    const expected = "5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n";
    try std.testing.expectEqualStrings(expected, buf[0..pos]);
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

test "writeHead buffer too small" {
    var buf: [5]u8 = undefined;
    const result = writeHead(&buf, .ok, &.{});
    try std.testing.expectError(error.BufferTooSmall, result);
}

test "writeChunk buffer too small" {
    var buf: [3]u8 = undefined;
    const result = writeChunk(&buf, "hello world");
    try std.testing.expectError(error.BufferTooSmall, result);
}

test "writeEnd buffer too small" {
    var buf: [2]u8 = undefined;
    const result = writeEnd(&buf);
    try std.testing.expectError(error.BufferTooSmall, result);
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
