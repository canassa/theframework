const std = @import("std");
const posix = std.posix;
const linux = std.os.linux;
const ring_mod = @import("ring.zig");
const Ring = ring_mod.Ring;

const BUF_SIZE = 4096;
const NUM_BUFFERS = 64;
const BUFFER_GROUP_ID = 0;

const Op = enum {
    accept,
    recv,
    send,
    close,
};

const Conn = struct {
    fd: posix.fd_t,
    op: Op,
    /// CQE saved from recv so we can return the buffer after send completes.
    recv_cqe: linux.io_uring_cqe,

    fn toUserData(self: *Conn) u64 {
        return @intFromPtr(self);
    }

    fn fromUserData(user_data: u64) *Conn {
        return @ptrFromInt(user_data);
    }
};

/// Sentinel Conn used to identify the multishot accept operation.
var accept_conn: Conn = .{
    .fd = -1,
    .op = .accept,
    .recv_cqe = undefined,
};

fn submitMultishotAccept(ring: *Ring, listen_fd: posix.fd_t) !void {
    accept_conn.fd = listen_fd;
    accept_conn.op = .accept;
    _ = try ring.prepAcceptMultishot(listen_fd, accept_conn.toUserData());
}

fn handleAccept(
    ring: *Ring,
    listen_fd: posix.fd_t,
    cqe: linux.io_uring_cqe,
    buf_grp: *linux.IoUring.BufferGroup,
    allocator: std.mem.Allocator,
) !void {
    if (cqe.res < 0) {
        std.log.err("accept failed: errno={d}", .{-cqe.res});
        // If IORING_CQE_F_MORE is not set, the multishot accept was terminated — resubmit.
        if (cqe.flags & linux.IORING_CQE_F_MORE == 0) {
            try submitMultishotAccept(ring, listen_fd);
        }
        return;
    }

    const client_fd: posix.fd_t = @intCast(cqe.res);

    // Allocate connection state
    const conn = allocator.create(Conn) catch {
        std.log.err("out of memory for new connection, closing fd={d}", .{client_fd});
        posix.close(client_fd);
        return;
    };
    conn.* = .{
        .fd = client_fd,
        .op = .recv,
        .recv_cqe = undefined,
    };

    // Submit recv using the provided buffer group — the kernel picks a buffer.
    _ = try buf_grp.recv(conn.toUserData(), conn.fd, 0);

    // If IORING_CQE_F_MORE is not set, the multishot accept was terminated — resubmit.
    if (cqe.flags & linux.IORING_CQE_F_MORE == 0) {
        std.log.warn("multishot accept terminated, resubmitting", .{});
        try submitMultishotAccept(ring, listen_fd);
    }
}

fn handleRecv(
    ring: *Ring,
    conn: *Conn,
    cqe: linux.io_uring_cqe,
    buf_grp: *linux.IoUring.BufferGroup,
    allocator: std.mem.Allocator,
) !void {
    if (cqe.res <= 0) {
        // 0 = client disconnected, negative = error
        if (cqe.res < 0) {
            std.log.err("recv error on fd={d}: errno={d}", .{ conn.fd, -cqe.res });
        }
        // No buffer was selected on error (no IORING_CQE_F_BUFFER), so nothing to put back.
        conn.op = .close;
        _ = ring.prepClose(conn.fd, conn.toUserData()) catch {
            posix.close(conn.fd);
            allocator.destroy(conn);
            return;
        };
        return;
    }

    // IORING_CQE_F_BUFFER should be set — extract the buffer from the group.
    const buf = buf_grp.get(cqe) catch {
        std.log.err("failed to get buffer from group for fd={d}", .{conn.fd});
        conn.op = .close;
        _ = ring.prepClose(conn.fd, conn.toUserData()) catch {
            posix.close(conn.fd);
            allocator.destroy(conn);
            return;
        };
        return;
    };

    // Save the CQE so we can return the buffer after the send completes.
    conn.recv_cqe = cqe;
    conn.op = .send;
    _ = try ring.prepSend(conn.fd, buf, conn.toUserData());
}

fn handleSend(
    ring: *Ring,
    conn: *Conn,
    cqe_res: i32,
    buf_grp: *linux.IoUring.BufferGroup,
    allocator: std.mem.Allocator,
) !void {
    // Return the recv buffer to the kernel now that the send is done.
    buf_grp.put(conn.recv_cqe) catch {};

    if (cqe_res < 0) {
        std.log.err("send error on fd={d}: errno={d}", .{ conn.fd, -cqe_res });
        conn.op = .close;
        _ = ring.prepClose(conn.fd, conn.toUserData()) catch {
            posix.close(conn.fd);
            allocator.destroy(conn);
            return;
        };
        return;
    }

    // Send succeeded, queue another recv using the buffer group.
    conn.op = .recv;
    _ = try buf_grp.recv(conn.toUserData(), conn.fd, 0);
}

fn handleClose(conn: *Conn, cqe_res: i32, allocator: std.mem.Allocator) void {
    if (cqe_res < 0) {
        std.log.err("close error on fd={d}: errno={d}", .{ conn.fd, -cqe_res });
    }
    allocator.destroy(conn);
}

fn eventLoop(
    ring: *Ring,
    listen_fd: posix.fd_t,
    buf_grp: *linux.IoUring.BufferGroup,
    allocator: std.mem.Allocator,
) !void {
    // Submit initial multishot accept
    try submitMultishotAccept(ring, listen_fd);

    while (true) {
        const cqe = try ring.waitCqe();
        const conn = Conn.fromUserData(cqe.user_data);

        switch (conn.op) {
            .accept => try handleAccept(ring, listen_fd, cqe, buf_grp, allocator),
            .recv => try handleRecv(ring, conn, cqe, buf_grp, allocator),
            .send => try handleSend(ring, conn, cqe.res, buf_grp, allocator),
            .close => handleClose(conn, cqe.res, allocator),
        }
    }
}

pub fn main() !void {
    var gpa = std.heap.GeneralPurposeAllocator(.{}){};
    defer _ = gpa.deinit();
    const allocator = gpa.allocator();

    // Parse port from args or use default
    var port: u16 = 9999;
    var args = try std.process.argsWithAllocator(allocator);
    defer args.deinit();
    _ = args.skip(); // skip program name
    if (args.next()) |port_str| {
        port = std.fmt.parseInt(u16, port_str, 10) catch {
            std.log.err("invalid port: {s}", .{port_str});
            return error.InvalidPort;
        };
    }

    // Create listen socket
    const listen_fd = try posix.socket(posix.AF.INET, posix.SOCK.STREAM, 0);
    defer posix.close(listen_fd);

    const one: [4]u8 = @bitCast(@as(i32, 1));
    try posix.setsockopt(listen_fd, posix.SOL.SOCKET, posix.SO.REUSEADDR, &one);

    const addr = std.net.Address.initIp4(.{ 127, 0, 0, 1 }, port);
    try posix.bind(listen_fd, &addr.any, addr.getOsSockLen());
    try posix.listen(listen_fd, 128);

    std.log.info("echo server v2 listening on 127.0.0.1:{d}", .{port});

    // Init io_uring ring
    var ring = try Ring.init(256);
    defer ring.deinit();

    // Init provided buffer ring
    var buf_grp = try ring.initBufferGroup(allocator, BUFFER_GROUP_ID, BUF_SIZE, NUM_BUFFERS);
    defer buf_grp.deinit(allocator);

    try eventLoop(&ring, listen_fd, &buf_grp, allocator);
}
