const std = @import("std");
const posix = std.posix;
const linux = std.os.linux;
const ring_mod = @import("ring.zig");
const Ring = ring_mod.Ring;

const BUF_SIZE = 4096;

const Op = enum {
    accept,
    recv,
    send,
    close,
};

const Conn = struct {
    fd: posix.fd_t,
    op: Op,
    buf: [BUF_SIZE]u8,
    len: usize, // bytes valid in buf (used for send)

    fn toUserData(self: *Conn) u64 {
        return @intFromPtr(self);
    }

    fn fromUserData(user_data: u64) *Conn {
        return @ptrFromInt(user_data);
    }
};

/// Sentinel user_data value for the listen-socket accept operation.
/// We use a dedicated Conn for accept so we can distinguish it from client connections.
var accept_conn: Conn = .{
    .fd = -1,
    .op = .accept,
    .buf = undefined,
    .len = 0,
};

fn submitAccept(ring: *Ring, listen_fd: posix.fd_t) !void {
    accept_conn.fd = listen_fd;
    accept_conn.op = .accept;
    _ = try ring.prepAccept(listen_fd, accept_conn.toUserData());
}

fn handleAccept(ring: *Ring, listen_fd: posix.fd_t, cqe_res: i32, allocator: std.mem.Allocator) !void {
    if (cqe_res < 0) {
        std.log.err("accept failed: errno={d}", .{-cqe_res});
        // Re-arm accept regardless
        try submitAccept(ring, listen_fd);
        return;
    }

    const client_fd: posix.fd_t = @intCast(cqe_res);

    // Allocate connection state
    const conn = allocator.create(Conn) catch {
        std.log.err("out of memory for new connection, closing fd={d}", .{client_fd});
        posix.close(client_fd);
        try submitAccept(ring, listen_fd);
        return;
    };
    conn.* = .{
        .fd = client_fd,
        .op = .recv,
        .buf = undefined,
        .len = 0,
    };

    // Submit recv on the new connection
    _ = try ring.prepRecv(conn.fd, &conn.buf, conn.toUserData());

    // Re-arm accept for the next connection
    try submitAccept(ring, listen_fd);
}

fn handleRecv(ring: *Ring, conn: *Conn, cqe_res: i32, allocator: std.mem.Allocator) !void {
    if (cqe_res <= 0) {
        // 0 = client disconnected, negative = error
        if (cqe_res < 0) {
            std.log.err("recv error on fd={d}: errno={d}", .{ conn.fd, -cqe_res });
        }
        // Close the connection
        conn.op = .close;
        _ = ring.prepClose(conn.fd, conn.toUserData()) catch {
            // If we can't even queue a close, just clean up directly
            posix.close(conn.fd);
            allocator.destroy(conn);
            return;
        };
        return;
    }

    // Got data, echo it back
    const n: usize = @intCast(cqe_res);
    conn.len = n;
    conn.op = .send;
    _ = try ring.prepSend(conn.fd, conn.buf[0..n], conn.toUserData());
}

fn handleSend(ring: *Ring, conn: *Conn, cqe_res: i32, allocator: std.mem.Allocator) !void {
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

    // Send succeeded, queue another recv
    conn.op = .recv;
    conn.len = 0;
    _ = try ring.prepRecv(conn.fd, &conn.buf, conn.toUserData());
}

fn handleClose(conn: *Conn, cqe_res: i32, allocator: std.mem.Allocator) void {
    if (cqe_res < 0) {
        std.log.err("close error on fd={d}: errno={d}", .{ conn.fd, -cqe_res });
    }
    allocator.destroy(conn);
}

fn eventLoop(ring: *Ring, listen_fd: posix.fd_t, allocator: std.mem.Allocator) !void {
    // Submit initial accept
    try submitAccept(ring, listen_fd);

    while (true) {
        const cqe = try ring.waitCqe();
        const conn = Conn.fromUserData(cqe.user_data);

        switch (conn.op) {
            .accept => try handleAccept(ring, listen_fd, cqe.res, allocator),
            .recv => try handleRecv(ring, conn, cqe.res, allocator),
            .send => try handleSend(ring, conn, cqe.res, allocator),
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

    std.log.info("echo server listening on 127.0.0.1:{d}", .{port});

    // Init io_uring ring
    var ring = try Ring.init(256);
    defer ring.deinit();

    try eventLoop(&ring, listen_fd, allocator);
}
