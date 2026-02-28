const std = @import("std");

// ---------------------------------------------------------------------------
// ArenaConfig
// ---------------------------------------------------------------------------

/// Configuration for the per-connection arena allocator and request limits.
/// Populated from a Python dict via configFromPyDict() in hub.zig.
/// Declared as extern struct for C-compatible layout (plans/arena.md Phase 1).
pub const ArenaConfig = extern struct {
    chunk_size: u32, // default 4096
    max_header_size: u32, // default 32768 (32 KB)
    max_body_size: u32, // default 1048576 (1 MB)
    max_connections: u16, // default 4096
    read_timeout_ms: u32, // default 30000 (future use)
    keepalive_timeout_ms: u32, // default 5000 (future use)
    handler_timeout_ms: u32, // default 30000 (future use)

    /// Return an ArenaConfig with sensible defaults matching the plan.
    pub fn defaults() ArenaConfig {
        return ArenaConfig{
            .chunk_size = 4096,
            .max_header_size = 32768,
            .max_body_size = 1_048_576,
            .max_connections = 4096,
            .read_timeout_ms = 30_000,
            .keepalive_timeout_ms = 5_000,
            .handler_timeout_ms = 30_000,
        };
    }

    /// Direct allocation threshold: (chunk_size - sizeof(next_ptr)) / 4
    /// Any single allocation >= this threshold goes to direct malloc
    /// instead of the bump allocator, preventing >25% waste of a chunk.
    pub fn directThreshold(self: ArenaConfig) usize {
        return (@as(usize, self.chunk_size) - @sizeOf(?*anyopaque)) / 4;
    }
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test "defaults returns expected values" {
    const config = ArenaConfig.defaults();
    try std.testing.expectEqual(@as(u32, 4096), config.chunk_size);
    try std.testing.expectEqual(@as(u32, 32768), config.max_header_size);
    try std.testing.expectEqual(@as(u32, 1_048_576), config.max_body_size);
    try std.testing.expectEqual(@as(u16, 4096), config.max_connections);
    try std.testing.expectEqual(@as(u32, 30_000), config.read_timeout_ms);
    try std.testing.expectEqual(@as(u32, 5_000), config.keepalive_timeout_ms);
    try std.testing.expectEqual(@as(u32, 30_000), config.handler_timeout_ms);
}

test "directThreshold with default chunk_size" {
    const config = ArenaConfig.defaults();
    // (4096 - 8) / 4 = 1022
    try std.testing.expectEqual(@as(usize, 1022), config.directThreshold());
}

test "directThreshold with custom chunk_size" {
    var config = ArenaConfig.defaults();
    config.chunk_size = 8192;
    // (8192 - 8) / 4 = 2046
    try std.testing.expectEqual(@as(usize, 2046), config.directThreshold());
}
