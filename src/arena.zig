const std = @import("std");
const chunk_pool = @import("chunk_pool.zig");
const config_mod = @import("config.zig");

const Chunk = chunk_pool.Chunk;
const ChunkRecycler = chunk_pool.ChunkRecycler;
const ArenaConfig = config_mod.ArenaConfig;

/// Direct allocation header. For single allocations >= direct_threshold.
/// Individually malloc'd and linked into the arena's directs list.
/// Payload bytes follow immediately after this header.
const DirectAlloc = struct {
    next: ?*DirectAlloc,
    size: usize, // payload size (for freeing)
};

/// Per-connection arena allocator with bump allocation and linked chunks.
///
/// The first_chunk is embedded in the Connection struct (always available,
/// zero allocation). Additional chunks come from the hub-local ChunkRecycler.
/// Large single allocations go through direct malloc.
/// The arena grows without bound until malloc fails.
///
/// All memory is reclaimed in one call to reset().
pub const RequestArena = struct {
    /// Head of linked list of chunks. Points to the current (most recently
    /// allocated) chunk. New chunks are prepended.
    chunks: ?*Chunk,

    /// Bump offset within the current chunk.
    chunk_offset: usize,

    /// Head of linked list of direct (large) allocations.
    directs: ?*DirectAlloc,

    /// The inline first chunk (embedded in Connection). Never returned
    /// to the recycler -- only reset.
    first_chunk: *Chunk,

    /// Hub-local chunk recycler.
    recycler: *ChunkRecycler,

    /// Direct-alloc threshold: allocations >= this size bypass bump allocation
    /// and go to direct malloc.
    direct_threshold: usize,

    /// Initialize arena with a given first chunk, recycler, and direct threshold.
    /// The first_chunk is typically embedded inline in the Connection struct.
    pub fn init(first_chunk: *Chunk, recycler: *ChunkRecycler, direct_threshold: usize) RequestArena {
        first_chunk.next = null;
        return .{
            .chunks = first_chunk,
            .chunk_offset = Chunk.DATA_START,
            .directs = null,
            .first_chunk = first_chunk,
            .recycler = recycler,
            .direct_threshold = direct_threshold,
        };
    }

    /// Allocate `sz` bytes. Returns null on OOM.
    /// The arena does NOT free individual allocations -- only reset() reclaims.
    /// All returned slices remain valid until reset().
    pub fn alloc(self: *RequestArena, sz: usize) ?[]u8 {
        const size = if (sz == 0) 1 else sz;

        // Large allocation: direct malloc
        if (size >= self.direct_threshold) return self.allocDirect(size);

        // Try bump in current chunk
        if (Chunk.CHUNK_SIZE - self.chunk_offset >= size) {
            const start = self.chunk_offset;
            self.chunk_offset += size;
            return self.chunks.?.bytes[start..][0..size];
        }

        // Current chunk full -- grab a new one from recycler
        return self.allocNewChunk(size);
    }

    /// Grab a new chunk from the recycler and allocate `size` bytes from it.
    fn allocNewChunk(self: *RequestArena, size: usize) ?[]u8 {
        const new_chunk = self.recycler.acquire() catch return null;
        new_chunk.next = self.chunks;
        self.chunks = new_chunk;
        self.chunk_offset = Chunk.DATA_START + size;
        return new_chunk.bytes[Chunk.DATA_START..][0..size];
    }

    /// Malloc a large allocation directly (bypasses chunk bump allocator).
    /// The DirectAlloc header is prepended so we can free it on reset().
    fn allocDirect(self: *RequestArena, size: usize) ?[]u8 {
        const header_size = @sizeOf(DirectAlloc);
        const total = header_size + size;
        const raw = std.heap.c_allocator.alloc(u8, total) catch return null;
        const header: *DirectAlloc = @ptrCast(@alignCast(raw.ptr));
        header.next = self.directs;
        header.size = size;
        self.directs = header;
        return raw[header_size..][0..size];
    }

    /// Get writable space in the current chunk for recv.
    /// If the current chunk is full, grabs a new one from the recycler.
    /// Returns null on OOM.
    pub fn currentFreeSlice(self: *RequestArena, max_len: usize) ?[]u8 {
        const remaining = Chunk.CHUNK_SIZE - self.chunk_offset;
        if (remaining == 0) {
            const new_chunk = self.recycler.acquire() catch return null;
            new_chunk.next = self.chunks;
            self.chunks = new_chunk;
            self.chunk_offset = Chunk.DATA_START;
            const available = Chunk.USABLE;
            return new_chunk.bytes[Chunk.DATA_START..][0..@min(max_len, available)];
        }
        return self.chunks.?.bytes[self.chunk_offset..][0..@min(max_len, remaining)];
    }

    /// Advance bump pointer after data has been written into the slice
    /// returned by currentFreeSlice().
    pub fn commitBytes(self: *RequestArena, nbytes: usize) void {
        std.debug.assert(self.chunk_offset + nbytes <= Chunk.CHUNK_SIZE);
        self.chunk_offset += nbytes;
    }

    /// Reset for the next request. Return linked chunks to recycler,
    /// free directs, reset first_chunk for reuse.
    /// After reset, the arena is ready for the next request with zero
    /// malloc/free calls in steady state.
    pub fn reset(self: *RequestArena) void {
        // Return linked chunks to recycler (skip first_chunk)
        var chunk = self.chunks;
        while (chunk) |c| {
            const next_chunk = c.next;
            if (c != self.first_chunk) {
                self.recycler.release(c);
            }
            chunk = next_chunk;
        }

        // Free all direct allocations
        var direct = self.directs;
        while (direct) |d| {
            const next_direct = d.next;
            const total = @sizeOf(DirectAlloc) + d.size;
            const raw: [*]u8 = @ptrCast(d);
            std.heap.c_allocator.free(raw[0..total]);
            direct = next_direct;
        }

        // Reset first chunk
        self.first_chunk.next = null;
        self.chunks = self.first_chunk;
        self.chunk_offset = Chunk.DATA_START;
        self.directs = null;
    }

    /// Return bytes remaining in the current chunk.
    pub fn currentChunkRemaining(self: *const RequestArena) usize {
        return Chunk.CHUNK_SIZE - self.chunk_offset;
    }

    /// Allocate a typed slice of `n` elements from the arena with proper alignment.
    /// Returns null on OOM.
    /// The returned slice remains valid until reset().
    pub fn allocSlice(self: *RequestArena, comptime T: type, n: usize) ?[]T {
        const byte_count = @sizeOf(T) * n;
        const bytes = self.alloc(byte_count) orelse return null;
        return @as([*]T, @ptrCast(@alignCast(bytes.ptr)))[0..n];
    }
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test "small alloc fits in first chunk (no recycler interaction)" {
    const allocator = std.testing.allocator;
    var recycler = ChunkRecycler.init(allocator);
    defer recycler.deinit();

    // Allocate a chunk to use as first_chunk
    const first_chunk = try allocator.create(Chunk);
    defer allocator.destroy(first_chunk);

    const config = ArenaConfig.defaults();
    var arena = RequestArena.init(first_chunk, &recycler, config.directThreshold());
    defer arena.reset();

    // Small alloc should fit in first chunk
    const buf = arena.alloc(64) orelse return error.TestUnexpectedResult;
    try std.testing.expectEqual(@as(usize, 64), buf.len);

    // Verify it's within the first chunk's memory
    const chunk_start = @intFromPtr(&first_chunk.bytes[0]);
    const chunk_end = chunk_start + Chunk.CHUNK_SIZE;
    const buf_start = @intFromPtr(buf.ptr);
    try std.testing.expect(buf_start >= chunk_start);
    try std.testing.expect(buf_start + buf.len <= chunk_end);

    // No chunks should have been acquired from recycler
    try std.testing.expectEqual(@as(usize, 0), recycler.total_allocated);
}

test "fill first chunk, next alloc grabs from recycler" {
    const allocator = std.testing.allocator;
    var recycler = ChunkRecycler.init(allocator);
    defer recycler.deinit();

    const first_chunk = try allocator.create(Chunk);
    defer allocator.destroy(first_chunk);

    const config = ArenaConfig.defaults();
    var arena = RequestArena.init(first_chunk, &recycler, config.directThreshold());
    defer arena.reset();

    // Fill up the first chunk. Usable = 4088 bytes. Use small allocs below threshold.
    // direct_threshold = 1022, so use 512-byte allocs
    const allocs_to_fill = Chunk.USABLE / 512; // 4088 / 512 = 7 (with 504 bytes remaining)
    for (0..allocs_to_fill) |_| {
        const buf = arena.alloc(512) orelse return error.TestUnexpectedResult;
        try std.testing.expectEqual(@as(usize, 512), buf.len);
    }

    // Recycler should not have been touched yet
    try std.testing.expectEqual(@as(usize, 0), recycler.total_allocated);

    // Now alloc something that won't fit in the remaining space.
    // Remaining: 4088 - (7 * 512) = 4088 - 3584 = 504 bytes
    // Alloc 600 bytes (< threshold 1022, but > remaining 504)
    const overflow_buf = arena.alloc(600) orelse return error.TestUnexpectedResult;
    try std.testing.expectEqual(@as(usize, 600), overflow_buf.len);

    // Recycler should have allocated one new chunk
    try std.testing.expectEqual(@as(usize, 1), recycler.total_allocated);
}

test "large alloc (>= direct_threshold) goes to direct malloc" {
    const allocator = std.testing.allocator;
    var recycler = ChunkRecycler.init(allocator);
    defer recycler.deinit();

    const first_chunk = try allocator.create(Chunk);
    defer allocator.destroy(first_chunk);

    const config = ArenaConfig.defaults();
    var arena = RequestArena.init(first_chunk, &recycler, config.directThreshold());
    defer arena.reset();

    // direct_threshold = 1022
    const threshold = config.directThreshold();
    try std.testing.expectEqual(@as(usize, 1022), threshold);

    // Alloc exactly at threshold -- should go to direct malloc
    const buf = arena.alloc(1022) orelse return error.TestUnexpectedResult;
    try std.testing.expectEqual(@as(usize, 1022), buf.len);

    // Should NOT have used recycler
    try std.testing.expectEqual(@as(usize, 0), recycler.total_allocated);

    // Direct list should be non-null
    try std.testing.expect(arena.directs != null);

    // Write to the buffer to verify it's usable
    @memset(buf, 0xAB);
    try std.testing.expectEqual(@as(u8, 0xAB), buf[0]);
    try std.testing.expectEqual(@as(u8, 0xAB), buf[1021]);
}

test "reset returns chunks to recycler (verify cachedCount)" {
    const allocator = std.testing.allocator;
    var recycler = ChunkRecycler.init(allocator);
    defer recycler.deinit();

    const first_chunk = try allocator.create(Chunk);
    defer allocator.destroy(first_chunk);

    const config = ArenaConfig.defaults();
    var arena = RequestArena.init(first_chunk, &recycler, config.directThreshold());

    // Force overflow to grab 2 additional chunks from recycler
    // Each chunk has USABLE=4088 bytes. Fill first, then 2 more.
    // Use 512-byte allocs (below threshold of 1022).
    // Fill first chunk: 8 * 512 = 4096 > 4088, so 7 fit, then one overflows
    for (0..8) |_| {
        _ = arena.alloc(512) orelse return error.TestUnexpectedResult;
    }
    // That filled first chunk and overflowed into one recycler chunk.
    try std.testing.expectEqual(@as(usize, 1), recycler.total_allocated);

    // Fill the second chunk and overflow into a third
    for (0..8) |_| {
        _ = arena.alloc(512) orelse return error.TestUnexpectedResult;
    }
    try std.testing.expectEqual(@as(usize, 2), recycler.total_allocated);

    // Before reset: recycler cache should be empty (chunks are in use)
    try std.testing.expectEqual(@as(usize, 0), recycler.cachedCount());

    // Reset should return the 2 extra chunks to recycler
    arena.reset();

    try std.testing.expectEqual(@as(usize, 2), recycler.cachedCount());
}

test "reset frees directs" {
    const allocator = std.testing.allocator;
    var recycler = ChunkRecycler.init(allocator);
    defer recycler.deinit();

    const first_chunk = try allocator.create(Chunk);
    defer allocator.destroy(first_chunk);

    const config = ArenaConfig.defaults();
    var arena = RequestArena.init(first_chunk, &recycler, config.directThreshold());

    // Allocate several direct (large) allocations
    const buf1 = arena.alloc(2000) orelse return error.TestUnexpectedResult;
    const buf2 = arena.alloc(5000) orelse return error.TestUnexpectedResult;

    // Write to them to verify they're usable
    @memset(buf1, 0x11);
    @memset(buf2, 0x22);

    try std.testing.expect(arena.directs != null);

    // Reset should free all directs
    arena.reset();

    try std.testing.expect(arena.directs == null);
}

test "reset + re-alloc reuses first chunk (chunk_offset back to DATA_START)" {
    const allocator = std.testing.allocator;
    var recycler = ChunkRecycler.init(allocator);
    defer recycler.deinit();

    const first_chunk = try allocator.create(Chunk);
    defer allocator.destroy(first_chunk);

    const config = ArenaConfig.defaults();
    var arena = RequestArena.init(first_chunk, &recycler, config.directThreshold());

    // Alloc some data
    const buf1 = arena.alloc(100) orelse return error.TestUnexpectedResult;
    @memset(buf1, 0xAA);

    try std.testing.expectEqual(Chunk.DATA_START + 100, arena.chunk_offset);

    // Reset
    arena.reset();

    // chunk_offset should be back to DATA_START
    try std.testing.expectEqual(Chunk.DATA_START, arena.chunk_offset);

    // Alloc again -- should reuse the same first chunk
    const buf2 = arena.alloc(100) orelse return error.TestUnexpectedResult;

    // The new alloc should be at the same position in the first chunk
    const chunk_base = @intFromPtr(&first_chunk.bytes[0]);
    const buf2_offset = @intFromPtr(buf2.ptr) - chunk_base;
    try std.testing.expectEqual(Chunk.DATA_START, buf2_offset);

    // No recycler interaction
    try std.testing.expectEqual(@as(usize, 0), recycler.total_allocated);

    arena.reset();
}

test "currentFreeSlice + commitBytes round-trip" {
    const allocator = std.testing.allocator;
    var recycler = ChunkRecycler.init(allocator);
    defer recycler.deinit();

    const first_chunk = try allocator.create(Chunk);
    defer allocator.destroy(first_chunk);

    const config = ArenaConfig.defaults();
    var arena = RequestArena.init(first_chunk, &recycler, config.directThreshold());
    defer arena.reset();

    // Get a free slice
    const slice = arena.currentFreeSlice(256) orelse return error.TestUnexpectedResult;
    try std.testing.expectEqual(@as(usize, 256), slice.len);

    // Write some data into it (simulating a recv)
    const message = "Hello, World!";
    @memcpy(slice[0..message.len], message);

    // Commit the bytes we wrote
    arena.commitBytes(message.len);

    // chunk_offset should have advanced by message.len
    try std.testing.expectEqual(Chunk.DATA_START + message.len, arena.chunk_offset);

    // Get another free slice -- should start where we left off
    const slice2 = arena.currentFreeSlice(100) orelse return error.TestUnexpectedResult;
    const expected_offset = Chunk.DATA_START + message.len;
    const slice2_offset = @intFromPtr(slice2.ptr) - @intFromPtr(&first_chunk.bytes[0]);
    try std.testing.expectEqual(expected_offset, slice2_offset);
}

test "multiple allocs across multiple chunks, verify all slices valid" {
    const allocator = std.testing.allocator;
    var recycler = ChunkRecycler.init(allocator);
    defer recycler.deinit();

    const first_chunk = try allocator.create(Chunk);
    defer allocator.destroy(first_chunk);

    const config = ArenaConfig.defaults();
    var arena = RequestArena.init(first_chunk, &recycler, config.directThreshold());
    defer arena.reset();

    // Allocate many slices across multiple chunks
    // Use 800-byte allocs (below threshold of 1022).
    // First chunk: 4088 / 800 = 5 fit (5 * 800 = 4000, remaining 88)
    // Then each subsequent chunk: 4088 / 800 = 5 fit
    const num_allocs = 20; // Should span ~4 chunks
    var slices: [num_allocs][]u8 = undefined;

    for (0..num_allocs) |i| {
        slices[i] = arena.alloc(800) orelse return error.TestUnexpectedResult;
        // Write a unique pattern to each slice
        @memset(slices[i], @as(u8, @intCast(i)));
    }

    // Verify all slices still contain their data (all valid until reset)
    for (0..num_allocs) |i| {
        const expected: u8 = @intCast(i);
        for (slices[i]) |byte| {
            try std.testing.expectEqual(expected, byte);
        }
    }

    // Should have allocated from recycler (20 * 800 = 16000, first chunk holds 5,
    // so we need 3 more chunks)
    try std.testing.expect(recycler.total_allocated >= 3);
}

test "zero-size alloc returns valid 1-byte slice" {
    const allocator = std.testing.allocator;
    var recycler = ChunkRecycler.init(allocator);
    defer recycler.deinit();

    const first_chunk = try allocator.create(Chunk);
    defer allocator.destroy(first_chunk);

    const config = ArenaConfig.defaults();
    var arena = RequestArena.init(first_chunk, &recycler, config.directThreshold());
    defer arena.reset();

    // Zero-size alloc should return a valid 1-byte slice
    const buf = arena.alloc(0) orelse return error.TestUnexpectedResult;
    try std.testing.expectEqual(@as(usize, 1), buf.len);

    // Should be writable
    buf[0] = 0xFF;
    try std.testing.expectEqual(@as(u8, 0xFF), buf[0]);
}

test "currentFreeSlice grabs new chunk when current is full" {
    const allocator = std.testing.allocator;
    var recycler = ChunkRecycler.init(allocator);
    defer recycler.deinit();

    const first_chunk = try allocator.create(Chunk);
    defer allocator.destroy(first_chunk);

    const config = ArenaConfig.defaults();
    var arena = RequestArena.init(first_chunk, &recycler, config.directThreshold());
    defer arena.reset();

    // Fill the first chunk completely using alloc
    // Usable = 4088. Alloc exactly 4088 bytes via small allocs.
    // Use allocs of size < threshold (1022). 8 * 511 = 4088 exactly.
    for (0..8) |_| {
        _ = arena.alloc(511) orelse return error.TestUnexpectedResult;
    }

    // chunk_offset should be at CHUNK_SIZE (fully used)
    try std.testing.expectEqual(Chunk.CHUNK_SIZE, arena.chunk_offset);
    try std.testing.expectEqual(@as(usize, 0), arena.currentChunkRemaining());

    // No recycler chunks used yet (all fit in first chunk)
    try std.testing.expectEqual(@as(usize, 0), recycler.total_allocated);

    // currentFreeSlice should grab a new chunk
    const slice = arena.currentFreeSlice(256) orelse return error.TestUnexpectedResult;
    try std.testing.expectEqual(@as(usize, 256), slice.len);

    // Recycler should have allocated one chunk
    try std.testing.expectEqual(@as(usize, 1), recycler.total_allocated);
}

test "mixed alloc and direct alloc, then reset cleans everything" {
    const allocator = std.testing.allocator;
    var recycler = ChunkRecycler.init(allocator);
    defer recycler.deinit();

    const first_chunk = try allocator.create(Chunk);
    defer allocator.destroy(first_chunk);

    const config = ArenaConfig.defaults();
    var arena = RequestArena.init(first_chunk, &recycler, config.directThreshold());

    // Small alloc in first chunk
    const small = arena.alloc(100) orelse return error.TestUnexpectedResult;
    @memset(small, 0x11);

    // Direct alloc (large)
    const large = arena.alloc(2000) orelse return error.TestUnexpectedResult;
    @memset(large, 0x22);

    // Another small alloc
    const small2 = arena.alloc(200) orelse return error.TestUnexpectedResult;
    @memset(small2, 0x33);

    // Force chunk overflow with small allocs (below threshold of 1022).
    // First chunk has 4088 usable, already used 300 (100 + 200), remaining = 3788.
    // Fill remaining with 512-byte allocs: 3788 / 512 = 7 fit (3584), leaving 204.
    // Then one more 512 alloc overflows to a new chunk.
    for (0..8) |_| {
        _ = arena.alloc(512) orelse return error.TestUnexpectedResult;
    }

    try std.testing.expect(arena.directs != null);
    try std.testing.expect(recycler.total_allocated >= 1);

    // Reset should clean everything
    arena.reset();

    try std.testing.expect(arena.directs == null);
    try std.testing.expectEqual(Chunk.DATA_START, arena.chunk_offset);
    try std.testing.expectEqual(arena.chunks, @as(?*Chunk, arena.first_chunk));
}
