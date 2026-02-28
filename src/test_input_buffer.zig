const std = @import("std");
const testing = std.testing;
const InputBuffer = @import("input_buffer.zig").InputBuffer;
const INLINE_BUF_SIZE = @import("input_buffer.zig").INLINE_BUF_SIZE;
const BufferRecycler = @import("buffer_recycler.zig").BufferRecycler;
const sizeToPower = @import("buffer_recycler.zig").sizeToPower;
const MIN_POWER = @import("buffer_recycler.zig").MIN_POWER;
const MAX_POWER = @import("buffer_recycler.zig").MAX_POWER;

// ===========================================================================
// InputBuffer tests
// ===========================================================================

test "InputBuffer: init sets correct state" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    var inline_buf: [INLINE_BUF_SIZE]u8 = undefined;
    var buf = InputBuffer.init(&inline_buf, INLINE_BUF_SIZE, &recycler);

    try testing.expectEqual(@as(usize, 0), buf.read_pos);
    try testing.expectEqual(@as(usize, 0), buf.write_pos);
    try testing.expectEqual(INLINE_BUF_SIZE, buf.capacity);
    try testing.expect(buf.is_inline);
    try testing.expectEqual(@as(usize, 0), buf.prev_capacity);
    try testing.expectEqual(@as(usize, 0), buf.unconsumedLen());
}

test "InputBuffer: unconsumed after write and consume" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    var inline_buf: [INLINE_BUF_SIZE]u8 = undefined;
    var buf = InputBuffer.init(&inline_buf, INLINE_BUF_SIZE, &recycler);

    // Simulate recv: write some data
    const free = try buf.reserveFree(100);
    @memcpy(free[0..13], "Hello, World!");
    buf.commitWrite(13);

    // Verify unconsumed
    try testing.expectEqual(@as(usize, 13), buf.unconsumedLen());
    try testing.expectEqualSlices(u8, "Hello, World!", buf.unconsumed());

    // Consume 7 bytes ("Hello, ")
    buf.consume(7);
    try testing.expectEqual(@as(usize, 6), buf.unconsumedLen());
    try testing.expectEqualSlices(u8, "World!", buf.unconsumed());
}

test "InputBuffer: compact moves unconsumed data to front" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    var inline_buf: [INLINE_BUF_SIZE]u8 = undefined;
    var buf = InputBuffer.init(&inline_buf, INLINE_BUF_SIZE, &recycler);

    // Write data
    const free = try buf.reserveFree(100);
    @memcpy(free[0..10], "0123456789");
    buf.commitWrite(10);

    // Consume some
    buf.consume(6); // consumed "012345", unconsumed = "6789"

    try testing.expectEqual(@as(usize, 6), buf.read_pos);
    try testing.expectEqual(@as(usize, 10), buf.write_pos);

    // Compact
    buf.compact();

    try testing.expectEqual(@as(usize, 0), buf.read_pos);
    try testing.expectEqual(@as(usize, 4), buf.write_pos);
    try testing.expectEqualSlices(u8, "6789", buf.unconsumed());
}

test "InputBuffer: compact is no-op when read_pos is 0" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    var inline_buf: [INLINE_BUF_SIZE]u8 = undefined;
    var buf = InputBuffer.init(&inline_buf, INLINE_BUF_SIZE, &recycler);

    const free = try buf.reserveFree(5);
    @memcpy(free[0..5], "ABCDE");
    buf.commitWrite(5);

    // No consumption: compact should be a no-op
    buf.compact();
    try testing.expectEqual(@as(usize, 0), buf.read_pos);
    try testing.expectEqual(@as(usize, 5), buf.write_pos);
    try testing.expectEqualSlices(u8, "ABCDE", buf.unconsumed());
}

test "InputBuffer: grow preserves unconsumed data" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    // Use a small inline buffer to force growth
    var inline_buf: [64]u8 = undefined;
    var buf = InputBuffer.init(&inline_buf, 64, &recycler);

    // Write 50 bytes
    const free1 = try buf.reserveFree(50);
    @memcpy(free1[0..50], "A" ** 50);
    buf.commitWrite(50);

    // Consume 10, so 40 unconsumed
    buf.consume(10);
    try testing.expectEqual(@as(usize, 40), buf.unconsumedLen());

    // Request more free space than available (64 - 50 = 14 free, need 30)
    // After compaction: unconsumed(40) + min_free(30) = 70 > capacity(64) → grow
    const free2 = try buf.reserveFree(30);
    try testing.expect(free2.len >= 30);

    // Buffer should have grown (no longer inline)
    try testing.expect(!buf.is_inline);
    try testing.expect(buf.capacity > 64);

    // Unconsumed data should be preserved at the front
    try testing.expectEqual(@as(usize, 0), buf.read_pos);
    try testing.expectEqual(@as(usize, 40), buf.write_pos);

    // Verify data integrity
    for (buf.unconsumed()) |byte| {
        try testing.expectEqual(@as(u8, 'A'), byte);
    }

    // Cleanup: return grown buffer to recycler
    buf.resetForNewConnection(&inline_buf, 64);
}

test "InputBuffer: reserveFree triggers compact when beneficial" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    // Use a buffer where compaction will help
    var inline_buf: [128]u8 = undefined;
    var buf = InputBuffer.init(&inline_buf, 128, &recycler);

    // Write 100 bytes
    const free1 = try buf.reserveFree(100);
    @memcpy(free1[0..100], "X" ** 100);
    buf.commitWrite(100);

    // Consume 90 bytes: only 10 unconsumed, 90 consumed waste
    buf.consume(90);
    try testing.expectEqual(@as(usize, 10), buf.unconsumedLen());

    // Free space: 128 - 100 = 28. Need 50.
    // unconsumed(10) + min_free(50) = 60 <= capacity(128) → compact, don't grow
    const free2 = try buf.reserveFree(50);
    try testing.expect(free2.len >= 50);

    // Should still be inline (compacted, not grown)
    try testing.expect(buf.is_inline);
    try testing.expectEqual(@as(usize, 0), buf.read_pos);
    try testing.expectEqual(@as(usize, 10), buf.write_pos);
}

test "InputBuffer: reserveFree grows when compact is insufficient" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    var inline_buf: [64]u8 = undefined;
    var buf = InputBuffer.init(&inline_buf, 64, &recycler);

    // Write 60 bytes
    const free1 = try buf.reserveFree(60);
    @memcpy(free1[0..60], "Y" ** 60);
    buf.commitWrite(60);

    // Consume 10: 50 unconsumed
    buf.consume(10);

    // Need 30 free. After compact: unconsumed(50) + min_free(30) = 80 > capacity(64) → grow
    const free2 = try buf.reserveFree(30);
    try testing.expect(free2.len >= 30);
    try testing.expect(!buf.is_inline);
    try testing.expect(buf.capacity >= 80);

    // Cleanup
    buf.resetForNewConnection(&inline_buf, 64);
}

test "InputBuffer: resetForNewConnection returns buffer to recycler" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    var inline_buf: [64]u8 = undefined;
    var buf = InputBuffer.init(&inline_buf, 64, &recycler);

    // Force growth so we have a recycler-owned buffer
    const free = try buf.reserveFree(100);
    _ = free;
    // Buffer has grown to at least 4096 (recycler min)
    try testing.expect(!buf.is_inline);
    const grown_cap = buf.capacity;

    // Reset: should return grown buffer to recycler, revert to inline
    buf.resetForNewConnection(&inline_buf, 64);

    try testing.expect(buf.is_inline);
    try testing.expectEqual(@as(usize, 64), buf.capacity);
    try testing.expectEqual(@as(usize, 0), buf.read_pos);
    try testing.expectEqual(@as(usize, 0), buf.write_pos);
    try testing.expectEqual(grown_cap, buf.prev_capacity);

    // Recycler should have the buffer cached
    try testing.expectEqual(@as(usize, 1), recycler.cachedCount());
}

test "InputBuffer: resetForNewConnection on inline buffer does not release to recycler" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    var inline_buf: [INLINE_BUF_SIZE]u8 = undefined;
    var buf = InputBuffer.init(&inline_buf, INLINE_BUF_SIZE, &recycler);

    // Write some data without growing
    const free = try buf.reserveFree(100);
    @memcpy(free[0..50], "Z" ** 50);
    buf.commitWrite(50);

    // Reset while still inline
    buf.resetForNewConnection(&inline_buf, INLINE_BUF_SIZE);

    // Should not have released anything to recycler
    try testing.expectEqual(@as(usize, 0), recycler.cachedCount());
    try testing.expect(buf.is_inline);
}

test "InputBuffer: capacity memory (prev_capacity preserved)" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    var inline_buf: [64]u8 = undefined;
    var buf = InputBuffer.init(&inline_buf, 64, &recycler);

    // Force growth
    _ = try buf.reserveFree(200);
    try testing.expect(!buf.is_inline);
    const cap1 = buf.capacity;

    // Reset: prev_capacity remembers
    buf.resetForNewConnection(&inline_buf, 64);
    try testing.expectEqual(cap1, buf.prev_capacity);
}

test "InputBuffer: resetForNewRequest with no leftover zeroes positions" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    var inline_buf: [INLINE_BUF_SIZE]u8 = undefined;
    var buf = InputBuffer.init(&inline_buf, INLINE_BUF_SIZE, &recycler);

    // Simulate a complete request: write and fully consume
    const free = try buf.reserveFree(100);
    @memcpy(free[0..80], "R" ** 80);
    buf.commitWrite(80);
    buf.consume(80);

    try testing.expectEqual(@as(usize, 0), buf.unconsumedLen());
    try testing.expectEqual(@as(usize, 80), buf.read_pos);
    try testing.expectEqual(@as(usize, 80), buf.write_pos);

    // Reset for new request: positions zeroed since no leftover
    buf.resetForNewRequest();

    try testing.expectEqual(@as(usize, 0), buf.read_pos);
    try testing.expectEqual(@as(usize, 0), buf.write_pos);
}

test "InputBuffer: resetForNewRequest preserves unconsumed (pipelining)" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    var inline_buf: [INLINE_BUF_SIZE]u8 = undefined;
    var buf = InputBuffer.init(&inline_buf, INLINE_BUF_SIZE, &recycler);

    // Simulate two pipelined requests: write 800 bytes, consume 400
    const free = try buf.reserveFree(800);
    @memcpy(free[0..400], "A" ** 400);
    @memcpy(free[400..800], "B" ** 400);
    buf.commitWrite(800);
    buf.consume(400); // first request consumed

    try testing.expectEqual(@as(usize, 400), buf.unconsumedLen());

    // Reset for new request: unconsumed data preserved
    buf.resetForNewRequest();

    try testing.expectEqual(@as(usize, 400), buf.unconsumedLen());

    // Verify the unconsumed data is the second request's bytes
    for (buf.unconsumed()) |byte| {
        try testing.expectEqual(@as(u8, 'B'), byte);
    }
}

test "InputBuffer: resetForNewRequest compacts when >50% is waste" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    var inline_buf: [INLINE_BUF_SIZE]u8 = undefined;
    var buf = InputBuffer.init(&inline_buf, INLINE_BUF_SIZE, &recycler);

    // Write 3000 bytes, consume 2600 → read_pos=2600 > capacity/2=2048
    const free = try buf.reserveFree(3000);
    @memcpy(free[0..3000], "X" ** 3000);
    buf.commitWrite(3000);
    buf.consume(2600);

    try testing.expect(buf.read_pos > buf.capacity / 2);

    buf.resetForNewRequest();

    // Should have compacted: read_pos=0
    try testing.expectEqual(@as(usize, 0), buf.read_pos);
    try testing.expectEqual(@as(usize, 400), buf.write_pos);
    try testing.expectEqual(@as(usize, 400), buf.unconsumedLen());
}

test "InputBuffer: resetForNewRequest does NOT compact when waste < 50%" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    var inline_buf: [INLINE_BUF_SIZE]u8 = undefined;
    var buf = InputBuffer.init(&inline_buf, INLINE_BUF_SIZE, &recycler);

    // Write 800 bytes, consume 400 → read_pos=400 < capacity/2=2048
    const free = try buf.reserveFree(800);
    @memcpy(free[0..800], "Y" ** 800);
    buf.commitWrite(800);
    buf.consume(400);

    try testing.expect(buf.read_pos <= buf.capacity / 2);

    buf.resetForNewRequest();

    // Should NOT have compacted
    try testing.expectEqual(@as(usize, 400), buf.read_pos);
    try testing.expectEqual(@as(usize, 800), buf.write_pos);
    try testing.expectEqual(@as(usize, 400), buf.unconsumedLen());
}

test "InputBuffer: pipelining simulation with multiple requests" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    var inline_buf: [INLINE_BUF_SIZE]u8 = undefined;
    var buf = InputBuffer.init(&inline_buf, INLINE_BUF_SIZE, &recycler);

    // Simulate recv: two complete HTTP requests arrive in one TCP segment
    const req1 = "GET /a HTTP/1.1\r\nHost: x\r\n\r\n";
    const req2 = "GET /b HTTP/1.1\r\nHost: x\r\n\r\n";
    const both = req1 ++ req2;

    const free = try buf.reserveFree(both.len);
    @memcpy(free[0..both.len], both);
    buf.commitWrite(both.len);

    // Parse first request: consume req1.len bytes
    try testing.expectEqual(@as(usize, both.len), buf.unconsumedLen());
    buf.consume(req1.len);

    // Reset for next request on same connection
    buf.resetForNewRequest();

    // Second request should be available as unconsumed
    try testing.expectEqual(@as(usize, req2.len), buf.unconsumedLen());
    try testing.expectEqualSlices(u8, req2, buf.unconsumed());

    // Consume second request
    buf.consume(req2.len);
    try testing.expectEqual(@as(usize, 0), buf.unconsumedLen());

    // Reset for next request: no leftover, positions zeroed
    buf.resetForNewRequest();
    try testing.expectEqual(@as(usize, 0), buf.read_pos);
    try testing.expectEqual(@as(usize, 0), buf.write_pos);
}

test "InputBuffer: freeSpace reports correct value" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    var inline_buf: [INLINE_BUF_SIZE]u8 = undefined;
    var buf = InputBuffer.init(&inline_buf, INLINE_BUF_SIZE, &recycler);

    try testing.expectEqual(INLINE_BUF_SIZE, buf.freeSpace());

    const free = try buf.reserveFree(100);
    @memcpy(free[0..100], "Z" ** 100);
    buf.commitWrite(100);

    try testing.expectEqual(INLINE_BUF_SIZE - 100, buf.freeSpace());
}

test "InputBuffer: grow returns old buffer to recycler" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    var inline_buf: [64]u8 = undefined;
    var buf = InputBuffer.init(&inline_buf, 64, &recycler);

    // First growth: inline -> recycler buffer (inline not released to recycler)
    _ = try buf.reserveFree(100);
    try testing.expect(!buf.is_inline);
    try testing.expectEqual(@as(usize, 0), recycler.cachedCount());

    // Second growth: recycler buffer -> larger recycler buffer (old released)
    _ = try buf.reserveFree(buf.capacity + 1);
    try testing.expectEqual(@as(usize, 1), recycler.cachedCount());

    // Cleanup
    buf.resetForNewConnection(&inline_buf, 64);
}

test "InputBuffer: multiple grows return old buffers correctly" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    var inline_buf: [64]u8 = undefined;
    var buf = InputBuffer.init(&inline_buf, 64, &recycler);

    // Grow multiple times: 64 -> 4096 -> 8192 -> 16384
    _ = try buf.reserveFree(100); // 64->4096
    try testing.expectEqual(@as(usize, 4096), buf.capacity);

    // Write some data so we have unconsumed bytes to preserve
    const free1 = try buf.reserveFree(100);
    @memcpy(free1[0..10], "0123456789");
    buf.commitWrite(10);

    _ = try buf.reserveFree(4097); // 4096->8192 (10 unconsumed + 4097 = 4107 > 4096)
    try testing.expectEqual(@as(usize, 8192), buf.capacity);
    try testing.expectEqual(@as(usize, 1), recycler.cachedCount()); // 4K returned

    // Verify data survived the growth
    try testing.expectEqualSlices(u8, "0123456789", buf.unconsumed());

    _ = try buf.reserveFree(8193); // 8192->16384
    try testing.expectEqual(@as(usize, 16384), buf.capacity);
    try testing.expectEqual(@as(usize, 2), recycler.cachedCount()); // 4K + 8K returned

    // Data still intact
    try testing.expectEqualSlices(u8, "0123456789", buf.unconsumed());

    // Cleanup
    buf.resetForNewConnection(&inline_buf, 64);
}

// ===========================================================================
// BufferRecycler tests
// ===========================================================================

test "BufferRecycler: sizeToPower maps correctly" {
    // At or below MIN_POWER → MIN_POWER (12)
    try testing.expectEqual(@as(u5, 12), sizeToPower(0));
    try testing.expectEqual(@as(u5, 12), sizeToPower(1));
    try testing.expectEqual(@as(u5, 12), sizeToPower(4095));
    try testing.expectEqual(@as(u5, 12), sizeToPower(4096));

    // One above power-of-2 → next power
    try testing.expectEqual(@as(u5, 13), sizeToPower(4097));
    try testing.expectEqual(@as(u5, 14), sizeToPower(8193));
    try testing.expectEqual(@as(u5, 15), sizeToPower(16385));

    // Exact powers
    try testing.expectEqual(@as(u5, 13), sizeToPower(8192));
    try testing.expectEqual(@as(u5, 14), sizeToPower(16384));
    try testing.expectEqual(@as(u5, 20), sizeToPower(1 << 20));

    // At MAX_POWER
    try testing.expectEqual(@as(u5, 24), sizeToPower(1 << 24));

    // Above MAX_POWER → clamped
    try testing.expectEqual(@as(u5, 24), sizeToPower((1 << 24) + 1));
    try testing.expectEqual(@as(u5, 24), sizeToPower(1 << 25));
}

test "BufferRecycler: LIFO reuse within same bin" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    const b1 = try recycler.acquire(4096);
    const b2 = try recycler.acquire(4096);
    const ptr1 = @intFromPtr(b1.ptr);
    const ptr2 = @intFromPtr(b2.ptr);

    recycler.release(b1);
    recycler.release(b2);

    // LIFO: last released first acquired
    const got1 = try recycler.acquire(4096);
    try testing.expectEqual(ptr2, @intFromPtr(got1.ptr));

    const got2 = try recycler.acquire(4096);
    try testing.expectEqual(ptr1, @intFromPtr(got2.ptr));

    recycler.release(got1);
    recycler.release(got2);
}

test "BufferRecycler: independent bins" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    const b4k = try recycler.acquire(4096);
    const b8k = try recycler.acquire(8192);
    const b16k = try recycler.acquire(16384);

    try testing.expectEqual(@as(usize, 4096), b4k.len);
    try testing.expectEqual(@as(usize, 8192), b8k.len);
    try testing.expectEqual(@as(usize, 16384), b16k.len);

    const ptr4k = @intFromPtr(b4k.ptr);
    const ptr8k = @intFromPtr(b8k.ptr);
    const ptr16k = @intFromPtr(b16k.ptr);

    recycler.release(b4k);
    recycler.release(b8k);
    recycler.release(b16k);

    // Acquire from each bin: should get back the same pointers
    const got4k = try recycler.acquire(4096);
    try testing.expectEqual(ptr4k, @intFromPtr(got4k.ptr));

    const got8k = try recycler.acquire(8192);
    try testing.expectEqual(ptr8k, @intFromPtr(got8k.ptr));

    const got16k = try recycler.acquire(16384);
    try testing.expectEqual(ptr16k, @intFromPtr(got16k.ptr));

    recycler.release(got4k);
    recycler.release(got8k);
    recycler.release(got16k);
}

test "BufferRecycler: deinit frees all (no leak)" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);

    // Acquire and release several buffers of various sizes
    const b1 = try recycler.acquire(4096);
    const b2 = try recycler.acquire(8192);
    const b3 = try recycler.acquire(4096);
    const b4 = try recycler.acquire(32768);

    recycler.release(b1);
    recycler.release(b2);
    recycler.release(b3);
    recycler.release(b4);

    try testing.expectEqual(@as(usize, 4), recycler.cachedCount());

    // deinit frees everything. Test allocator will catch leaks.
    recycler.deinit();
}

test "BufferRecycler: acquire rounds up non-power-of-2" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    // 5000 rounds up to 8192 (2^13)
    const buf = try recycler.acquire(5000);
    try testing.expectEqual(@as(usize, 8192), buf.len);

    recycler.release(buf);
}

test "BufferRecycler: acquire/release round-trip preserves data" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    const buf = try recycler.acquire(4096);

    // Write a pattern
    @memset(buf, 0xAB);

    // Release and re-acquire
    recycler.release(buf);
    const got = try recycler.acquire(4096);

    // Same pointer, same data (not zeroed)
    try testing.expectEqual(@intFromPtr(buf.ptr), @intFromPtr(got.ptr));
    try testing.expectEqual(@as(u8, 0xAB), got[0]);
    try testing.expectEqual(@as(u8, 0xAB), got[4095]);

    recycler.release(got);
}

test "BufferRecycler: total_allocated tracks malloc count" {
    const allocator = testing.allocator;
    var recycler = BufferRecycler.init(allocator);
    defer recycler.deinit();

    try testing.expectEqual(@as(usize, 0), recycler.total_allocated);

    const b1 = try recycler.acquire(4096);
    try testing.expectEqual(@as(usize, 1), recycler.total_allocated);

    const b2 = try recycler.acquire(4096);
    try testing.expectEqual(@as(usize, 2), recycler.total_allocated);

    // Release both
    recycler.release(b1);
    recycler.release(b2);

    // Re-acquire: should reuse, not malloc
    const b3 = try recycler.acquire(4096);
    const b4 = try recycler.acquire(4096);
    try testing.expectEqual(@as(usize, 2), recycler.total_allocated);

    recycler.release(b3);
    recycler.release(b4);
}
