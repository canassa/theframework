const std = @import("std");
const posix = std.posix;

// ---------------------------------------------------------------------------
// Connection
// ---------------------------------------------------------------------------

pub const ConnectionState = enum(u8) {
    idle,
    reading,
    writing,
    cancelling,
    closing,
};

pub const Connection = struct {
    fd: posix.fd_t,
    state: ConnectionState,
    generation: u32,
    recv_buf: ?[]u8,
    send_buf: ?[]const u8,
    send_offset: usize,
    send_total: usize,
    greenlet: ?*anyopaque, // opaque — caller (hub.zig) handles incref/decref
    pending_ops: u8,
    pool_index: u16,
    result: i32, // CQE result storage

    pub fn reset(self: *Connection) void {
        // Note: caller must decref greenlet before calling reset
        self.fd = -1;
        self.state = .idle;
        self.recv_buf = null;
        self.send_buf = null;
        self.send_offset = 0;
        self.send_total = 0;
        self.greenlet = null;
        self.pending_ops = 0;
        self.result = 0;
    }
};

// ---------------------------------------------------------------------------
// user_data encoding: [16 bits: pool_index][32 bits: generation][16 bits: reserved]
// ---------------------------------------------------------------------------

pub fn encodeUserData(pool_index: u16, generation: u32) u64 {
    return (@as(u64, pool_index) << 48) | (@as(u64, generation) << 16);
}

pub fn decodePoolIndex(user_data: u64) u16 {
    return @intCast(user_data >> 48);
}

pub fn decodeGeneration(user_data: u64) u32 {
    return @intCast((user_data >> 16) & 0xFFFFFFFF);
}

// ---------------------------------------------------------------------------
// ConnectionPool
// ---------------------------------------------------------------------------

pub const POOL_SIZE: u16 = 4096;

pub const ConnectionPool = struct {
    connections: [POOL_SIZE]Connection,
    free_list: std.ArrayListUnmanaged(u16),
    allocator: std.mem.Allocator,

    pub fn init(allocator: std.mem.Allocator) !ConnectionPool {
        var pool = ConnectionPool{
            .connections = undefined,
            .free_list = .{},
            .allocator = allocator,
        };

        // Initialize all connections
        for (0..POOL_SIZE) |i| {
            const idx: u16 = @intCast(i);
            pool.connections[i] = Connection{
                .fd = -1,
                .state = .idle,
                .generation = 0,
                .recv_buf = null,
                .send_buf = null,
                .send_offset = 0,
                .send_total = 0,
                .greenlet = null,
                .pending_ops = 0,
                .pool_index = idx,
                .result = 0,
            };
        }

        // Pre-populate free list (all slots are free)
        try pool.free_list.ensureTotalCapacity(allocator, POOL_SIZE);
        var i: u16 = POOL_SIZE;
        while (i > 0) {
            i -= 1;
            pool.free_list.appendAssumeCapacity(i);
        }

        return pool;
    }

    pub fn deinit(self: *ConnectionPool) void {
        // Note: caller must decref any remaining greenlets before deinit
        self.free_list.deinit(self.allocator);
    }

    /// Acquire a free connection slot. Returns null if pool is exhausted.
    pub fn acquire(self: *ConnectionPool) ?*Connection {
        const idx = self.free_list.pop() orelse return null;
        const conn = &self.connections[idx];
        conn.state = .idle;
        conn.fd = -1;
        conn.pending_ops = 0;
        conn.result = 0;
        return conn;
    }

    /// Release a connection back to the pool. Increments generation to
    /// invalidate any stale CQEs. Caller must decref greenlet first.
    pub fn release(self: *ConnectionPool, conn: *Connection) void {
        conn.generation +%= 1; // wrapping add
        conn.reset();
        self.free_list.append(self.allocator, conn.pool_index) catch {
            // If this fails, we leak a slot. Acceptable for now.
        };
    }

    /// Look up a connection by pool_index, validate generation.
    /// Returns null if the generation doesn't match (stale CQE).
    pub fn lookup(self: *ConnectionPool, user_data: u64) ?*Connection {
        const pool_index = decodePoolIndex(user_data);
        const generation = decodeGeneration(user_data);

        if (pool_index >= POOL_SIZE) return null;

        const conn = &self.connections[pool_index];
        if (conn.generation != generation) return null;

        return conn;
    }

    /// How many connections are currently in use.
    pub fn activeCount(self: *ConnectionPool) usize {
        return POOL_SIZE - self.free_list.items.len;
    }
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test "user_data encode/decode round-trip" {
    const pool_index: u16 = 42;
    const generation: u32 = 12345;
    const encoded = encodeUserData(pool_index, generation);
    try std.testing.expectEqual(pool_index, decodePoolIndex(encoded));
    try std.testing.expectEqual(generation, decodeGeneration(encoded));
}

test "user_data encode/decode max values" {
    const pool_index: u16 = std.math.maxInt(u16);
    const generation: u32 = std.math.maxInt(u32);
    const encoded = encodeUserData(pool_index, generation);
    try std.testing.expectEqual(pool_index, decodePoolIndex(encoded));
    try std.testing.expectEqual(generation, decodeGeneration(encoded));
}

test "ConnectionPool acquire and release" {
    const allocator = std.testing.allocator;
    var pool = try ConnectionPool.init(allocator);
    defer pool.deinit();

    // All slots should be free initially
    try std.testing.expectEqual(@as(usize, 0), pool.activeCount());

    // Acquire a connection
    const conn1 = pool.acquire() orelse return error.TestUnexpectedResult;
    try std.testing.expectEqual(@as(usize, 1), pool.activeCount());
    try std.testing.expectEqual(ConnectionState.idle, conn1.state);

    const idx1 = conn1.pool_index;
    const gen1 = conn1.generation;

    // Release it
    pool.release(conn1);
    try std.testing.expectEqual(@as(usize, 0), pool.activeCount());

    // Acquire again — should get the same slot but incremented generation
    const conn2 = pool.acquire() orelse return error.TestUnexpectedResult;
    try std.testing.expectEqual(idx1, conn2.pool_index);
    try std.testing.expectEqual(gen1 +% 1, conn2.generation);

    pool.release(conn2);
}

test "ConnectionPool acquire N, release all, acquire N again" {
    const allocator = std.testing.allocator;
    var pool = try ConnectionPool.init(allocator);
    defer pool.deinit();

    const N = 50;
    var conns: [N]*Connection = undefined;

    // Acquire N
    for (0..N) |i| {
        conns[i] = pool.acquire() orelse return error.TestUnexpectedResult;
        conns[i].fd = @intCast(100 + i);
    }
    try std.testing.expectEqual(@as(usize, N), pool.activeCount());

    // Store generations
    var gens: [N]u32 = undefined;
    for (0..N) |i| {
        gens[i] = conns[i].generation;
    }

    // Release all
    for (0..N) |i| {
        pool.release(conns[i]);
    }
    try std.testing.expectEqual(@as(usize, 0), pool.activeCount());

    // Acquire N again — generations should be incremented
    for (0..N) |i| {
        conns[i] = pool.acquire() orelse return error.TestUnexpectedResult;
    }
    try std.testing.expectEqual(@as(usize, N), pool.activeCount());

    // All acquired connections should have incremented generations
    for (0..N) |i| {
        const idx = conns[i].pool_index;
        try std.testing.expectEqual(gens[idx] +% 1, conns[i].generation);
    }

    // Release all
    for (0..N) |i| {
        pool.release(conns[i]);
    }
}

test "ConnectionPool lookup validates generation" {
    const allocator = std.testing.allocator;
    var pool = try ConnectionPool.init(allocator);
    defer pool.deinit();

    // Acquire a connection
    const conn = pool.acquire() orelse return error.TestUnexpectedResult;
    const user_data = encodeUserData(conn.pool_index, conn.generation);

    // Lookup should succeed
    const found = pool.lookup(user_data);
    try std.testing.expect(found != null);
    try std.testing.expectEqual(conn.pool_index, found.?.pool_index);

    // Release and re-acquire (bumps generation)
    pool.release(conn);
    _ = pool.acquire() orelse return error.TestUnexpectedResult;

    // Lookup with OLD user_data should fail (stale generation)
    const stale = pool.lookup(user_data);
    try std.testing.expect(stale == null);
}

test "ConnectionPool exhaustion" {
    const allocator = std.testing.allocator;
    var pool = try ConnectionPool.init(allocator);
    defer pool.deinit();

    // Exhaust all slots
    for (0..POOL_SIZE) |_| {
        _ = pool.acquire() orelse return error.TestUnexpectedResult;
    }

    // Next acquire should return null
    try std.testing.expect(pool.acquire() == null);
}
