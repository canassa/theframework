const std = @import("std");
const BufferRecycler = @import("buffer_recycler.zig").BufferRecycler;
const sizeToPower = @import("buffer_recycler.zig").sizeToPower;
const MIN_POWER = @import("buffer_recycler.zig").MIN_POWER;

/// Inline buffer size: 4 KB, embedded in each Connection struct.
/// Handles the common case (simple GET requests, small API calls).
pub const INLINE_BUF_SIZE: usize = 4096;

/// A contiguous, growable byte buffer for accumulating recv data per connection.
/// Modeled on h2o's `h2o_buffer_t`.
///
/// Memory layout:
/// ```
/// data --> [consumed waste][unconsumed data][free space]
///          0            read_pos        write_pos     capacity
/// ```
///
/// Key properties:
/// - Starts using the inline buffer embedded in Connection (zero allocation).
/// - Grows via the BufferRecycler (power-of-2 sizes, LIFO reuse).
/// - Compacts lazily when free space is insufficient but consumed waste is reclaimable.
/// - Preserves unconsumed data across keep-alive requests (pipelining).
/// - Remembers previous capacity across connection resets (capacity memory).
pub const InputBuffer = struct {
    /// Pointer to the backing allocation (from recycler or inline).
    data: [*]u8,

    /// Total allocated capacity of the backing buffer.
    capacity: usize,

    /// Offset into `data` where unconsumed data begins.
    /// Advances on consume(), reset to 0 on compact().
    read_pos: usize,

    /// Offset into `data` where data ends / free space begins.
    /// Advances when recv adds data.
    write_pos: usize,

    /// Pointer to the hub-local buffer recycler.
    recycler: *BufferRecycler,

    /// True if `data` points to the inline first chunk (embedded in Connection).
    /// Inline buffers are never returned to the recycler.
    is_inline: bool,

    /// Previous capacity, preserved across resetForNewConnection() for capacity memory.
    /// Prevents re-growth oscillation on repeated connections.
    prev_capacity: usize,

    /// Initialize an InputBuffer using an inline (embedded) buffer.
    pub fn init(inline_buf: [*]u8, inline_cap: usize, recycler: *BufferRecycler) InputBuffer {
        return .{
            .data = inline_buf,
            .capacity = inline_cap,
            .read_pos = 0,
            .write_pos = 0,
            .recycler = recycler,
            .is_inline = true,
            .prev_capacity = 0,
        };
    }

    /// Return a read-only slice of unconsumed data (for the parser).
    pub fn unconsumed(self: *const InputBuffer) []const u8 {
        return self.data[self.read_pos..self.write_pos];
    }

    /// Return how many unconsumed bytes are available.
    pub fn unconsumedLen(self: *const InputBuffer) usize {
        return self.write_pos - self.read_pos;
    }

    /// Return a writable slice for recv (free space at the end).
    /// Compacts or grows if free space < min_free.
    pub fn reserveFree(self: *InputBuffer, min_free: usize) ![]u8 {
        const free = self.capacity - self.write_pos;
        if (free >= min_free) {
            return self.data[self.write_pos..self.capacity];
        }

        // Try compaction first: if consumed waste is reclaimable and
        // compacting yields enough space, do a memmove instead of growing.
        const unconsumed_len = self.write_pos - self.read_pos;
        if (self.read_pos > 0 and unconsumed_len + min_free <= self.capacity) {
            self.compact();
            return self.data[self.write_pos..self.capacity];
        }

        // Must grow
        try self.grow(unconsumed_len + min_free);
        return self.data[self.write_pos..self.capacity];
    }

    /// Record that `n` bytes were written into the free space (after recv).
    pub fn commitWrite(self: *InputBuffer, n: usize) void {
        std.debug.assert(self.write_pos + n <= self.capacity);
        self.write_pos += n;
    }

    /// Consume `n` bytes from the front (after parsing a request).
    /// This is a pointer advance -- zero copy, O(1).
    pub fn consume(self: *InputBuffer, n: usize) void {
        std.debug.assert(self.read_pos + n <= self.write_pos);
        self.read_pos += n;
    }

    /// Compact: memmove unconsumed data to front of buffer.
    /// Called lazily by reserveFree() when free space is insufficient
    /// but consumed space is reclaimable.
    pub fn compact(self: *InputBuffer) void {
        const len = self.write_pos - self.read_pos;
        if (self.read_pos == 0) return;
        if (len > 0) {
            std.mem.copyForwards(u8, self.data[0..len], self.data[self.read_pos..self.write_pos]);
        }
        self.read_pos = 0;
        self.write_pos = len;
    }

    /// Grow the buffer to at least `min_capacity` bytes.
    /// Allocates a new buffer from the recycler, copies unconsumed data, returns old.
    fn grow(self: *InputBuffer, min_capacity: usize) !void {
        const new_buf = try self.recycler.acquire(min_capacity);
        const new_cap = new_buf.len; // recycler returns power-of-2 sized buffer

        // Copy unconsumed data to new buffer
        const len = self.write_pos - self.read_pos;
        if (len > 0) {
            @memcpy(new_buf[0..len], self.data[self.read_pos..self.write_pos]);
        }

        // Return old buffer to recycler (unless it's the inline buffer)
        if (!self.is_inline) {
            self.recycler.release(self.data[0..self.capacity]);
        }

        self.data = new_buf.ptr;
        self.capacity = new_cap;
        self.read_pos = 0;
        self.write_pos = len;
        self.is_inline = false;
    }

    /// Reset for new connection. Returns buffer to recycler, remembers capacity.
    /// After reset, buffer uses the inline first chunk again.
    pub fn resetForNewConnection(self: *InputBuffer, inline_buf: [*]u8, inline_cap: usize) void {
        self.prev_capacity = self.capacity;
        if (!self.is_inline) {
            self.recycler.release(self.data[0..self.capacity]);
        }
        self.data = inline_buf;
        self.capacity = inline_cap;
        self.read_pos = 0;
        self.write_pos = 0;
        self.is_inline = true;
    }

    /// Reset for new request on same connection (keep-alive).
    /// Preserves unconsumed data (pipelining). Compacts if beneficial.
    pub fn resetForNewRequest(self: *InputBuffer) void {
        if (self.read_pos == self.write_pos) {
            // No leftover data -- just reset positions
            self.read_pos = 0;
            self.write_pos = 0;
            return;
        }
        // Compact if more than half the buffer is consumed waste
        if (self.read_pos > self.capacity / 2) {
            self.compact();
        }
    }

    /// Return the amount of free space at the end of the buffer
    /// (without compaction or growth).
    pub fn freeSpace(self: *const InputBuffer) usize {
        return self.capacity - self.write_pos;
    }
};
