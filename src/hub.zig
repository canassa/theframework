const std = @import("std");
const posix = std.posix;
const linux = std.os.linux;
const Ring = @import("ring.zig").Ring;
const op_slot = @import("op_slot.zig");
const OpSlot = op_slot.OpSlot;
const OpSlotTable = op_slot.OpSlotTable;
const conn_mod = @import("connection.zig");
const Connection = conn_mod.Connection;
const ConnectionPool = conn_mod.ConnectionPool;
const ConnectionState = conn_mod.ConnectionState;
const http = @import("http.zig");
const http_response = @import("http_response.zig");

const py = @cImport({
    @cInclude("py_helpers.h");
});

const PyObject = py.PyObject;

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const MAX_FDS: usize = 4096;
const STOP_SENTINEL: u64 = std.math.maxInt(u64);
const ACCEPT_SENTINEL: u64 = std.math.maxInt(u64) - 1;
const IGNORE_SENTINEL: u64 = std.math.maxInt(u64) - 2;
const CQE_BATCH_SIZE: usize = 256;
const MAX_POLL_FDS: usize = 64;
const IOSQE_IO_LINK: u8 = 1 << 2;

// ---------------------------------------------------------------------------
// Hub struct
// ---------------------------------------------------------------------------

pub const Hub = struct {
    ring: Ring,
    pool: ConnectionPool,
    op_slots: OpSlotTable,
    fd_to_conn: [MAX_FDS]?u16, // fd → pool_index (for looking up connections by fd)
    // Accept state (listen fd is not in the connection pool)
    accept_greenlet: ?*PyObject,
    accept_result: i32,
    accept_pending: bool,
    ready: std.ArrayListUnmanaged(?*PyObject), // ready queue
    hub_greenlet: ?*PyObject, // the hub greenlet itself
    stop_pipe: [2]posix.fd_t, // pipe for thread-safe stop signaling
    stop_buf: [1]u8, // read buffer for stop pipe
    active_waits: usize, // count of pending I/O operations
    running: bool,
    allocator: std.mem.Allocator,

    pub fn init(allocator: std.mem.Allocator) !Hub {
        const ring = try Ring.init(256);
        errdefer {
            var r = ring;
            r.deinit();
        }

        var pool = try ConnectionPool.init(allocator);
        errdefer pool.deinit();

        var op_slots = try OpSlotTable.init(allocator);
        errdefer op_slots.deinit();

        const pipe_fds = try posix.pipe();

        return Hub{
            .ring = ring,
            .pool = pool,
            .op_slots = op_slots,
            .fd_to_conn = [_]?u16{null} ** MAX_FDS,
            .accept_greenlet = null,
            .accept_result = 0,
            .accept_pending = false,
            .ready = .{},
            .hub_greenlet = null,
            .stop_pipe = .{ pipe_fds[0], pipe_fds[1] },
            .stop_buf = .{0},
            .active_waits = 0,
            .running = false,
            .allocator = allocator,
        };
    }

    pub fn deinit(self: *Hub) void {
        // Decref accept greenlet
        if (self.accept_greenlet) |g| {
            py.py_helper_decref(g);
            self.accept_greenlet = null;
        }

        // Decref greenlets in active connections and free buffers
        for (&self.pool.connections) |*conn| {
            if (conn.greenlet) |g_opaque| {
                const g: *PyObject = @ptrCast(@alignCast(g_opaque));
                py.py_helper_decref(g);
                conn.greenlet = null;
            }
            if (conn.recv_buf) |buf| {
                self.allocator.free(buf);
                conn.recv_buf = null;
            }
        }

        // Decref greenlets in op slots
        for (&self.op_slots.slots) |*slot| {
            if (slot.greenlet) |g_opaque| {
                const g: *PyObject = @ptrCast(@alignCast(g_opaque));
                py.py_helper_decref(g);
                slot.greenlet = null;
            }
        }
        self.op_slots.deinit();

        // Decref ready queue entries
        for (self.ready.items) |maybe_g| {
            if (maybe_g) |g| {
                py.py_helper_decref(g);
            }
        }
        self.ready.deinit(self.allocator);

        // Close stop pipe
        posix.close(self.stop_pipe[0]);
        posix.close(self.stop_pipe[1]);

        // Decref hub greenlet
        if (self.hub_greenlet) |g| {
            py.py_helper_decref(g);
            self.hub_greenlet = null;
        }

        self.pool.deinit();
        self.ring.deinit();
    }

    // -----------------------------------------------------------------------
    // Hub loop
    // -----------------------------------------------------------------------

    pub fn hubLoop(self: *Hub) void {
        self.running = true;

        // Submit a read SQE on stop_pipe[0] to detect stop signals
        _ = self.ring.prepRead(self.stop_pipe[0], &self.stop_buf, STOP_SENTINEL) catch {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "hub: failed to submit stop pipe read",
            );
            return;
        };

        while (self.running) {
            // a. DRAIN READY QUEUE
            var idx: usize = 0;
            while (idx < self.ready.items.len) {
                const maybe_g = self.ready.items[idx];
                idx += 1;
                if (maybe_g) |g| {
                    const result = py.py_helper_greenlet_switch(g, null, null);
                    py.py_helper_decref(g);
                    if (result == null) {
                        if (py.py_helper_err_occurred() != null) {
                            while (idx < self.ready.items.len) {
                                if (self.ready.items[idx]) |remaining| {
                                    py.py_helper_decref(remaining);
                                }
                                idx += 1;
                            }
                            self.ready.clearRetainingCapacity();
                            return;
                        }
                        continue;
                    }
                    py.py_helper_decref(result);
                }
            }
            self.ready.clearRetainingCapacity();

            // b. EXIT CHECK
            if (self.active_waits == 0 and self.ready.items.len == 0) break;

            // c. SUBMIT + WAIT: release GIL, block until at least 1 CQE
            {
                const saved = py.py_helper_save_thread();
                const sw_result = self.ring.submitAndWait(1);
                py.py_helper_restore_thread(saved);
                _ = sw_result catch {
                    py.py_helper_err_set_string(
                        py.py_helper_exc_runtime_error(),
                        "hub: submitAndWait failed",
                    );
                    return;
                };
            }

            // d. BATCH DRAIN CQEs
            var cqe_buf: [CQE_BATCH_SIZE]linux.io_uring_cqe = undefined;
            const cqes = self.ring.copyCqes(&cqe_buf, 0) catch {
                py.py_helper_err_set_string(
                    py.py_helper_exc_runtime_error(),
                    "hub: copyCqes failed",
                );
                return;
            };

            // e. Process each CQE
            for (cqes) |cqe| {
                if (cqe.user_data == IGNORE_SENTINEL) continue;

                if (cqe.user_data == STOP_SENTINEL) {
                    self.running = false;
                    continue;
                }

                if (cqe.user_data == ACCEPT_SENTINEL) {
                    // Accept completion
                    self.accept_result = cqe.res;
                    self.accept_pending = false;
                    self.active_waits -= 1;
                    if (self.accept_greenlet) |g| {
                        self.accept_greenlet = null;
                        self.ready.append(self.allocator, g) catch {
                            py.py_helper_decref(g);
                        };
                    }
                    continue;
                }

                // Check for op slot CQE (bit 63 set)
                if (op_slot.isOpSlot(cqe.user_data)) {
                    const slot = self.op_slots.lookup(cqe.user_data) orelse continue;
                    slot.result = cqe.res;

                    if (slot.poll_group) |group| {
                        // Multi-poll slot: only the first completion resumes
                        if (!group.resumed) {
                            group.resumed = true;
                            if (group.greenlet) |g_opaque| {
                                const g: *PyObject = @ptrCast(@alignCast(g_opaque));
                                self.active_waits -= group.active_waits_n;
                                self.ready.append(self.allocator, g) catch {
                                    py.py_helper_decref(g);
                                };
                            }
                        }
                        // Don't decref greenlet here — PollGroup owns the single ref
                    } else if (slot.greenlet) |g_opaque| {
                        // Regular single-poll slot (existing path)
                        const g: *PyObject = @ptrCast(@alignCast(g_opaque));
                        slot.greenlet = null;
                        self.active_waits -= 1;
                        self.ready.append(self.allocator, g) catch {
                            py.py_helper_decref(g);
                        };
                    }
                    continue;
                }

                // Connection pool lookup with generation validation
                const conn = self.pool.lookup(cqe.user_data) orelse {
                    // Stale CQE — generation mismatch or invalid index, discard
                    continue;
                };

                conn.result = cqe.res;
                if (conn.pending_ops > 0) conn.pending_ops -= 1;

                // Handle cancel CQE (during cancel-before-close)
                if (conn.state == .cancelling) {
                    if (conn.pending_ops == 0) {
                        // All cancelled, now submit close
                        self.submitClose(conn);
                    }
                    continue;
                }

                // Handle close CQE
                if (conn.state == .closing) {
                    self.releaseConnection(conn);
                    continue;
                }

                // Normal I/O completion — wake the greenlet
                conn.state = .idle;
                if (conn.greenlet) |g_opaque| {
                    const g: *PyObject = @ptrCast(@alignCast(g_opaque));
                    conn.greenlet = null;
                    self.active_waits -= 1;
                    self.ready.append(self.allocator, g) catch {
                        py.py_helper_decref(g);
                    };
                }
            }
        }
    }

    fn submitClose(self: *Hub, conn: *Connection) void {
        conn.state = .closing;
        const user_data = conn_mod.encodeUserData(conn.pool_index, conn.generation);
        _ = self.ring.prepClose(conn.fd, user_data) catch {
            // If we can't submit close, just release directly
            self.releaseConnection(conn);
            return;
        };
        conn.pending_ops += 1;
    }

    fn releaseConnection(self: *Hub, conn: *Connection) void {
        const fd_usize: usize = @intCast(conn.fd);
        if (fd_usize < MAX_FDS) {
            self.fd_to_conn[fd_usize] = null;
        }
        // Free recv buffer if present
        if (conn.recv_buf) |buf| {
            self.allocator.free(buf);
            conn.recv_buf = null;
        }
        // Decref greenlet if present
        if (conn.greenlet) |g_opaque| {
            const g: *PyObject = @ptrCast(@alignCast(g_opaque));
            py.py_helper_decref(g);
            conn.greenlet = null;
        }
        self.pool.release(conn);
    }

    // -----------------------------------------------------------------------
    // green_accept: submit accept SQE, yield, return new fd
    // -----------------------------------------------------------------------

    pub fn greenAccept(self: *Hub, listen_fd: posix.fd_t) ?*PyObject {
        // Get current greenlet
        const current = py.py_helper_greenlet_getcurrent() orelse return null;

        // Submit accept SQE with ACCEPT_SENTINEL user_data
        _ = self.ring.prepAccept(listen_fd, ACCEPT_SENTINEL) catch {
            py.py_helper_decref(current);
            py.py_helper_err_set_string(
                py.py_helper_exc_oserror(),
                "green_accept: failed to submit accept SQE",
            );
            return null;
        };

        self.accept_greenlet = current;
        self.accept_pending = true;
        self.active_waits += 1;

        // Switch to hub (suspends here)
        const hub_g = self.hub_greenlet orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_accept: hub greenlet not set",
            );
            self.accept_greenlet = null;
            self.accept_pending = false;
            self.active_waits -= 1;
            py.py_helper_decref(current);
            return null;
        };
        const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
        if (switch_result == null) return null;
        py.py_helper_decref(switch_result);

        // On resume: read result
        const res = self.accept_result;
        if (res < 0) {
            py.py_helper_err_set_from_errno(-res);
            return null;
        }

        // Register the new fd in the connection pool
        const new_fd: posix.fd_t = @intCast(res);
        const new_fd_usize: usize = @intCast(new_fd);
        if (new_fd_usize < MAX_FDS) {
            if (self.pool.acquire()) |conn| {
                conn.fd = new_fd;
                conn.state = .idle;
                self.fd_to_conn[new_fd_usize] = conn.pool_index;
            }
            // If pool exhausted, the fd still works but won't be tracked
        }

        return py.PyLong_FromLong(@as(c_long, res));
    }

    // -----------------------------------------------------------------------
    // getOrCreateConn: look up connection by fd
    // -----------------------------------------------------------------------

    fn getConn(self: *Hub, fd: posix.fd_t) ?*Connection {
        const fd_usize: usize = @intCast(fd);
        if (fd_usize >= MAX_FDS) return null;
        const pool_idx = self.fd_to_conn[fd_usize] orelse return null;
        return &self.pool.connections[pool_idx];
    }

    // -----------------------------------------------------------------------
    // green_recv: submit recv SQE, yield, return bytes
    // -----------------------------------------------------------------------

    pub fn greenRecv(self: *Hub, fd: posix.fd_t, max_bytes: usize) ?*PyObject {
        const conn = self.getConn(fd) orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_oserror(),
                "green_recv: no connection for fd",
            );
            return null;
        };

        // Heap-allocate recv buffer (CRITICAL: greenlet copies C stacks)
        const buf = self.allocator.alloc(u8, max_bytes) catch {
            _ = py.py_helper_err_no_memory();
            return null;
        };
        conn.recv_buf = buf;

        // Get current greenlet
        const current = py.py_helper_greenlet_getcurrent() orelse {
            self.allocator.free(buf);
            conn.recv_buf = null;
            return null;
        };

        // Submit recv SQE with encoded user_data
        const user_data = conn_mod.encodeUserData(conn.pool_index, conn.generation);
        _ = self.ring.prepRecv(fd, buf, user_data) catch {
            py.py_helper_decref(current);
            self.allocator.free(buf);
            conn.recv_buf = null;
            py.py_helper_err_set_string(
                py.py_helper_exc_oserror(),
                "green_recv: failed to submit recv SQE",
            );
            return null;
        };

        conn.greenlet = current;
        conn.state = .reading;
        conn.pending_ops += 1;
        self.active_waits += 1;

        // Switch to hub
        const hub_g = self.hub_greenlet orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_recv: hub greenlet not set",
            );
            conn.greenlet = null;
            conn.state = .idle;
            conn.pending_ops -= 1;
            self.active_waits -= 1;
            py.py_helper_decref(current);
            self.allocator.free(buf);
            conn.recv_buf = null;
            return null;
        };
        const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
        if (switch_result == null) {
            if (conn.recv_buf) |remaining_buf| {
                self.allocator.free(remaining_buf);
                conn.recv_buf = null;
            }
            return null;
        }
        py.py_helper_decref(switch_result);

        // On resume: read result
        const res = conn.result;
        const recv_buf = conn.recv_buf;
        conn.recv_buf = null;

        if (res < 0) {
            if (recv_buf) |rbp| {
                self.allocator.free(rbp);
            }
            py.py_helper_err_set_from_errno(-res);
            return null;
        }

        const nbytes: usize = @intCast(res);

        // Create Python bytes from buffer
        const py_bytes = py.py_helper_bytes_from_string_and_size(
            if (recv_buf) |rbp| @ptrCast(rbp.ptr) else null,
            @intCast(nbytes),
        );

        // Free the heap buffer
        if (recv_buf) |rbp| {
            self.allocator.free(rbp);
        }

        return py_bytes;
    }

    // -----------------------------------------------------------------------
    // green_send: submit send SQE, yield, handle partial sends
    // -----------------------------------------------------------------------

    pub fn greenSend(self: *Hub, fd: posix.fd_t, py_bytes: *PyObject) ?*PyObject {
        const conn = self.getConn(fd) orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_oserror(),
                "green_send: no connection for fd",
            );
            return null;
        };

        // Get C buffer from Python bytes object
        const c_buf: [*]const u8 = @ptrCast(py.py_helper_bytes_as_string(py_bytes) orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_send: failed to get bytes buffer",
            );
            return null;
        });
        const buf_len: usize = @intCast(py.py_helper_bytes_get_size(py_bytes));

        // Incref the bytes object to prevent GC while SQE is in-flight
        py.py_helper_incref(py_bytes);

        // Get current greenlet
        const current = py.py_helper_greenlet_getcurrent() orelse {
            py.py_helper_decref(py_bytes);
            return null;
        };

        // Partial send loop
        var total_sent: usize = 0;
        while (total_sent < buf_len) {
            const remaining = c_buf[total_sent..buf_len];

            const user_data = conn_mod.encodeUserData(conn.pool_index, conn.generation);
            _ = self.ring.prepSend(fd, remaining, user_data) catch {
                if (total_sent == 0) py.py_helper_decref(current);
                py.py_helper_decref(py_bytes);
                py.py_helper_err_set_string(
                    py.py_helper_exc_oserror(),
                    "green_send: failed to submit send SQE",
                );
                return null;
            };

            conn.greenlet = current;
            conn.state = .writing;
            conn.pending_ops += 1;
            self.active_waits += 1;

            // Switch to hub
            const hub_g = self.hub_greenlet orelse {
                py.py_helper_err_set_string(
                    py.py_helper_exc_runtime_error(),
                    "green_send: hub greenlet not set",
                );
                conn.greenlet = null;
                conn.state = .idle;
                conn.pending_ops -= 1;
                self.active_waits -= 1;
                py.py_helper_decref(current);
                py.py_helper_decref(py_bytes);
                return null;
            };
            const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
            if (switch_result == null) {
                py.py_helper_decref(py_bytes);
                return null;
            }
            py.py_helper_decref(switch_result);

            const res = conn.result;
            if (res < 0) {
                py.py_helper_decref(py_bytes);
                py.py_helper_err_set_from_errno(-res);
                return null;
            }

            total_sent += @intCast(res);
        }

        py.py_helper_decref(py_bytes);
        return py.PyLong_FromLong(@as(c_long, @intCast(total_sent)));
    }

    // -----------------------------------------------------------------------
    // green_close: cancel pending ops, close fd, release connection
    // -----------------------------------------------------------------------

    pub fn greenClose(self: *Hub, fd: posix.fd_t) ?*PyObject {
        const conn = self.getConn(fd) orelse {
            // No connection tracked — just close the fd directly via io_uring
            _ = self.ring.prepClose(fd, IGNORE_SENTINEL) catch {};
            const none = py.py_helper_none();
            py.py_helper_incref(none);
            return none;
        };

        if (conn.pending_ops > 0) {
            // Cancel pending operations first
            conn.state = .cancelling;
            const user_data = conn_mod.encodeUserData(conn.pool_index, conn.generation);
            // Submit async cancel for this connection's user_data
            _ = self.ring.prepCancel(user_data) catch {
                // If cancel fails, try to close directly
                self.submitClose(conn);
                const none = py.py_helper_none();
                py.py_helper_incref(none);
                return none;
            };
        } else {
            // No pending ops, go straight to close
            self.submitClose(conn);
        }

        const none = py.py_helper_none();
        py.py_helper_incref(none);
        return none;
    }

    // -----------------------------------------------------------------------
    // green_sleep: submit IORING_OP_TIMEOUT, yield, resume after timeout
    // -----------------------------------------------------------------------

    pub fn greenSleep(self: *Hub, seconds: f64) ?*PyObject {
        // Acquire an op slot
        const slot = self.op_slots.acquire() orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_sleep: op slot table exhausted",
            );
            return null;
        };

        // Get current greenlet
        const current = py.py_helper_greenlet_getcurrent() orelse {
            self.op_slots.release(slot);
            return null;
        };

        // Convert seconds to kernel_timespec
        const whole_secs: i64 = @intFromFloat(seconds);
        const frac_nanos: i64 = @intFromFloat((seconds - @as(f64, @floatFromInt(whole_secs))) * 1_000_000_000.0);
        var ts = linux.kernel_timespec{ .sec = whole_secs, .nsec = frac_nanos };

        // Submit timeout SQE
        const user_data = op_slot.encodeUserData(slot.slot_index, slot.generation);
        _ = self.ring.prepTimeout(&ts, user_data) catch {
            py.py_helper_decref(current);
            self.op_slots.release(slot);
            py.py_helper_err_set_string(
                py.py_helper_exc_oserror(),
                "green_sleep: failed to submit timeout SQE",
            );
            return null;
        };

        slot.greenlet = current;
        self.active_waits += 1;

        // Switch to hub (suspend)
        const hub_g = self.hub_greenlet orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_sleep: hub greenlet not set",
            );
            slot.greenlet = null;
            self.active_waits -= 1;
            py.py_helper_decref(current);
            self.op_slots.release(slot);
            return null;
        };
        const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
        if (switch_result == null) {
            self.op_slots.release(slot);
            return null;
        }
        py.py_helper_decref(switch_result);

        // On resume: check result
        const res = slot.result;
        self.op_slots.release(slot);

        // -ETIME is the expected success case for timeout expiry
        const etime_neg = -@as(i32, @intFromEnum(linux.E.TIME));
        if (res != etime_neg and res < 0) {
            py.py_helper_err_set_from_errno(-res);
            return null;
        }

        const none = py.py_helper_none();
        py.py_helper_incref(none);
        return none;
    }

    // -----------------------------------------------------------------------
    // green_connect: create socket, submit CONNECT, yield, return fd
    // -----------------------------------------------------------------------

    pub fn greenConnect(self: *Hub, host: [*:0]const u8, port: u16) ?*PyObject {
        // Create TCP socket
        const fd = posix.socket(posix.AF.INET, posix.SOCK.STREAM, 0) catch {
            py.py_helper_err_set_string(
                py.py_helper_exc_oserror(),
                "green_connect: failed to create socket",
            );
            return null;
        };

        // Parse host address
        const addr_bytes = parseIpv4(host) orelse {
            posix.close(fd);
            py.py_helper_err_set_string(
                py.py_helper_exc_oserror(),
                "green_connect: invalid IPv4 address",
            );
            return null;
        };

        var sockaddr = std.net.Address.initIp4(addr_bytes, port);

        // Register fd in connection pool first
        const fd_usize: usize = @intCast(fd);
        if (fd_usize >= MAX_FDS) {
            posix.close(fd);
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_connect: fd too large",
            );
            return null;
        }

        const conn = self.pool.acquire() orelse {
            posix.close(fd);
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_connect: connection pool exhausted",
            );
            return null;
        };
        conn.fd = fd;
        conn.state = .idle;
        self.fd_to_conn[fd_usize] = conn.pool_index;

        // Acquire op slot for the connect operation
        const slot = self.op_slots.acquire() orelse {
            self.fd_to_conn[fd_usize] = null;
            self.pool.release(conn);
            posix.close(fd);
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_connect: op slot table exhausted",
            );
            return null;
        };

        // Get current greenlet
        const current = py.py_helper_greenlet_getcurrent() orelse {
            self.op_slots.release(slot);
            self.fd_to_conn[fd_usize] = null;
            self.pool.release(conn);
            posix.close(fd);
            return null;
        };

        // Submit connect SQE using op_slot user_data
        const user_data = op_slot.encodeUserData(slot.slot_index, slot.generation);
        _ = self.ring.prepConnect(fd, &sockaddr.any, sockaddr.getOsSockLen(), user_data) catch {
            py.py_helper_decref(current);
            self.op_slots.release(slot);
            self.fd_to_conn[fd_usize] = null;
            self.pool.release(conn);
            posix.close(fd);
            py.py_helper_err_set_string(
                py.py_helper_exc_oserror(),
                "green_connect: failed to submit connect SQE",
            );
            return null;
        };

        slot.greenlet = current;
        self.active_waits += 1;

        // Switch to hub
        const hub_g = self.hub_greenlet orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_connect: hub greenlet not set",
            );
            slot.greenlet = null;
            self.active_waits -= 1;
            py.py_helper_decref(current);
            self.op_slots.release(slot);
            self.fd_to_conn[fd_usize] = null;
            self.pool.release(conn);
            posix.close(fd);
            return null;
        };
        const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
        if (switch_result == null) {
            self.op_slots.release(slot);
            self.fd_to_conn[fd_usize] = null;
            self.pool.release(conn);
            posix.close(fd);
            return null;
        }
        py.py_helper_decref(switch_result);

        // On resume: check result
        const res = slot.result;
        self.op_slots.release(slot);

        if (res < 0) {
            self.fd_to_conn[fd_usize] = null;
            self.pool.release(conn);
            posix.close(fd);
            py.py_helper_err_set_from_errno(-res);
            return null;
        }

        return py.PyLong_FromLong(@as(c_long, @intCast(fd)));
    }

    // -----------------------------------------------------------------------
    // green_connect_fd: connect an already-registered fd cooperatively
    // -----------------------------------------------------------------------

    pub fn greenConnectFd(self: *Hub, fd: posix.fd_t, host: [*:0]const u8, port: u16) ?*PyObject {
        // Verify fd is registered in the pool
        _ = self.getConn(fd) orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_connect_fd: fd not registered",
            );
            return null;
        };

        // Parse host address
        const addr_bytes = parseIpv4(host) orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_oserror(),
                "green_connect_fd: invalid IPv4 address",
            );
            return null;
        };

        var sockaddr = std.net.Address.initIp4(addr_bytes, port);

        // Acquire op slot for the connect operation
        const slot = self.op_slots.acquire() orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_connect_fd: op slot table exhausted",
            );
            return null;
        };

        // Get current greenlet
        const current = py.py_helper_greenlet_getcurrent() orelse {
            self.op_slots.release(slot);
            return null;
        };

        // Submit connect SQE using op_slot user_data
        const user_data = op_slot.encodeUserData(slot.slot_index, slot.generation);
        _ = self.ring.prepConnect(fd, &sockaddr.any, sockaddr.getOsSockLen(), user_data) catch {
            py.py_helper_decref(current);
            self.op_slots.release(slot);
            py.py_helper_err_set_string(
                py.py_helper_exc_oserror(),
                "green_connect_fd: failed to submit connect SQE",
            );
            return null;
        };

        slot.greenlet = current;
        self.active_waits += 1;

        // Switch to hub
        const hub_g = self.hub_greenlet orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_connect_fd: hub greenlet not set",
            );
            slot.greenlet = null;
            self.active_waits -= 1;
            py.py_helper_decref(current);
            self.op_slots.release(slot);
            return null;
        };
        const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
        if (switch_result == null) {
            self.op_slots.release(slot);
            return null;
        }
        py.py_helper_decref(switch_result);

        // On resume: check result
        const res = slot.result;
        self.op_slots.release(slot);

        if (res < 0) {
            py.py_helper_err_set_from_errno(-res);
            return null;
        }

        const none = py.py_helper_none();
        py.py_helper_incref(none);
        return none;
    }

    // -----------------------------------------------------------------------
    // green_poll_fd: wait for fd readiness via IORING_OP_POLL_ADD
    // -----------------------------------------------------------------------

    pub fn greenPollFd(self: *Hub, fd: posix.fd_t, events: u32) ?*PyObject {
        // Acquire op slot
        const slot = self.op_slots.acquire() orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_poll_fd: op slot table exhausted",
            );
            return null;
        };

        // Get current greenlet
        const current = py.py_helper_greenlet_getcurrent() orelse {
            self.op_slots.release(slot);
            return null;
        };

        // Submit POLL_ADD SQE
        const user_data = op_slot.encodeUserData(slot.slot_index, slot.generation);
        _ = self.ring.prepPollAdd(fd, events, user_data) catch {
            py.py_helper_decref(current);
            self.op_slots.release(slot);
            py.py_helper_err_set_string(
                py.py_helper_exc_oserror(),
                "green_poll_fd: failed to submit poll SQE",
            );
            return null;
        };

        slot.greenlet = current;
        self.active_waits += 1;

        // Switch to hub
        const hub_g = self.hub_greenlet orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_poll_fd: hub greenlet not set",
            );
            slot.greenlet = null;
            self.active_waits -= 1;
            py.py_helper_decref(current);
            self.op_slots.release(slot);
            return null;
        };
        const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
        if (switch_result == null) {
            self.op_slots.release(slot);
            return null;
        }
        py.py_helper_decref(switch_result);

        // On resume: check result
        const res = slot.result;
        self.op_slots.release(slot);

        if (res < 0) {
            py.py_helper_err_set_from_errno(-res);
            return null;
        }

        // Return revents as int
        return py.PyLong_FromLong(@as(c_long, @intCast(res)));
    }

    // -----------------------------------------------------------------------
    // green_register_fd: register an external fd in the connection pool
    // -----------------------------------------------------------------------

    pub fn greenRegisterFd(self: *Hub, fd: posix.fd_t) ?*PyObject {
        const fd_usize: usize = @intCast(fd);
        if (fd_usize >= MAX_FDS) {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_register_fd: fd too large",
            );
            return null;
        }

        // Check if already registered
        if (self.fd_to_conn[fd_usize] != null) {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_register_fd: fd already registered",
            );
            return null;
        }

        const conn = self.pool.acquire() orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_register_fd: connection pool exhausted",
            );
            return null;
        };
        conn.fd = fd;
        conn.state = .idle;
        self.fd_to_conn[fd_usize] = conn.pool_index;

        const none = py.py_helper_none();
        py.py_helper_incref(none);
        return none;
    }

    // -----------------------------------------------------------------------
    // green_unregister_fd: remove fd from connection pool without closing it
    // -----------------------------------------------------------------------

    pub fn greenUnregisterFd(self: *Hub, fd: posix.fd_t) ?*PyObject {
        const fd_usize: usize = @intCast(fd);
        if (fd_usize >= MAX_FDS) {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_unregister_fd: fd too large",
            );
            return null;
        }

        const pool_idx = self.fd_to_conn[fd_usize] orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_unregister_fd: fd not registered",
            );
            return null;
        };

        const conn = &self.pool.connections[pool_idx];
        self.fd_to_conn[fd_usize] = null;
        // Do NOT close the fd — Python owns it
        // But we need to clean up the connection
        if (conn.greenlet) |g_opaque| {
            const g: *PyObject = @ptrCast(@alignCast(g_opaque));
            py.py_helper_decref(g);
            conn.greenlet = null;
        }
        if (conn.recv_buf) |buf| {
            self.allocator.free(buf);
            conn.recv_buf = null;
        }
        self.pool.release(conn);

        const none = py.py_helper_none();
        py.py_helper_incref(none);
        return none;
    }

    // -----------------------------------------------------------------------
    // green_poll_multi: wait for any of N fds to become ready, with timeout
    // -----------------------------------------------------------------------

    pub fn greenPollMulti(
        self: *Hub,
        fds: []const posix.fd_t,
        poll_events: []const u32,
        timeout_ms: i64,
    ) ?*PyObject {
        const n = fds.len;

        if (n == 0) {
            // No fds — just sleep for timeout or return empty
            if (timeout_ms > 0) {
                return self.greenSleep(@as(f64, @floatFromInt(timeout_ms)) / 1000.0);
            }
            return py.PyList_New(0);
        }

        const has_timeout = timeout_ms >= 0;
        const total_slots: usize = n + @as(usize, if (has_timeout) 1 else 0);

        // Acquire op slots
        var slots: [MAX_POLL_FDS + 1]*OpSlot = undefined;
        var acquired: usize = 0;
        for (0..total_slots) |i| {
            slots[i] = self.op_slots.acquire() orelse {
                // Release already-acquired
                for (0..acquired) |j| self.op_slots.release(slots[j]);
                py.py_helper_err_set_string(
                    py.py_helper_exc_runtime_error(),
                    "green_poll_multi: op slot table exhausted",
                );
                return null;
            };
            acquired = i + 1;
        }

        // Get current greenlet
        const current = py.py_helper_greenlet_getcurrent() orelse {
            for (0..total_slots) |j| self.op_slots.release(slots[j]);
            return null;
        };

        // Set up PollGroup on the stack (safe: greenlet stack preserved while suspended)
        var group = op_slot.PollGroup{
            .resumed = false,
            .greenlet = current,
            .active_waits_n = @intCast(total_slots),
        };

        // Set poll_group on all slots
        for (0..total_slots) |i| {
            slots[i].poll_group = &group;
        }

        // Submit POLL_ADD SQEs
        for (0..n) |i| {
            const user_data = op_slot.encodeUserData(slots[i].slot_index, slots[i].generation);
            _ = self.ring.prepPollAdd(fds[i], poll_events[i], user_data) catch {
                py.py_helper_decref(current);
                for (0..total_slots) |j| {
                    slots[j].poll_group = null;
                    self.op_slots.release(slots[j]);
                }
                py.py_helper_err_set_string(
                    py.py_helper_exc_oserror(),
                    "green_poll_multi: failed to submit poll SQE",
                );
                return null;
            };
        }

        // Submit TIMEOUT SQE if needed
        if (has_timeout) {
            var ts = linux.kernel_timespec{
                .sec = @divTrunc(timeout_ms, 1000),
                .nsec = @rem(timeout_ms, 1000) * 1_000_000,
            };
            const timeout_ud = op_slot.encodeUserData(slots[n].slot_index, slots[n].generation);
            _ = self.ring.prepTimeout(&ts, timeout_ud) catch {
                py.py_helper_decref(current);
                for (0..total_slots) |j| {
                    slots[j].poll_group = null;
                    self.op_slots.release(slots[j]);
                }
                py.py_helper_err_set_string(
                    py.py_helper_exc_oserror(),
                    "green_poll_multi: failed to submit timeout SQE",
                );
                return null;
            };
        }

        self.active_waits += total_slots;

        // Switch to hub
        const hub_g = self.hub_greenlet orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_poll_multi: hub greenlet not set",
            );
            self.active_waits -= total_slots;
            py.py_helper_decref(current);
            for (0..total_slots) |j| {
                slots[j].poll_group = null;
                self.op_slots.release(slots[j]);
            }
            return null;
        };
        const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
        if (switch_result == null) {
            self.cancelAndReleasePollSlots(slots[0..total_slots]);
            return null;
        }
        py.py_helper_decref(switch_result);

        // On resume: collect results from poll slots (not timeout slot)
        const result_list = py.PyList_New(0) orelse {
            self.cancelAndReleasePollSlots(slots[0..total_slots]);
            return null;
        };

        for (0..n) |i| {
            const res = slots[i].result;
            if (res > 0) {
                const tuple = py.py_helper_tuple_new(2) orelse {
                    py.py_helper_decref(result_list);
                    self.cancelAndReleasePollSlots(slots[0..total_slots]);
                    return null;
                };
                _ = py.py_helper_tuple_setitem(tuple, 0, py.PyLong_FromLong(@as(c_long, @intCast(fds[i]))));
                _ = py.py_helper_tuple_setitem(tuple, 1, py.PyLong_FromLong(@as(c_long, @intCast(res))));
                _ = py.PyList_Append(result_list, tuple);
                py.py_helper_decref(tuple);
            }
        }

        // Cancel remaining SQEs and release all slots
        self.cancelAndReleasePollSlots(slots[0..total_slots]);

        return result_list;
    }

    /// Cancel outstanding poll/timeout SQEs and release their op slots.
    fn cancelAndReleasePollSlots(self: *Hub, slots: []*OpSlot) void {
        for (slots) |slot| {
            const ud = op_slot.encodeUserData(slot.slot_index, slot.generation);
            _ = self.ring.prepCancelUserData(ud, IGNORE_SENTINEL) catch {};
        }
        _ = self.ring.submit() catch {};
        // Release after submitting cancels (generation bump invalidates stale CQEs)
        for (slots) |slot| {
            slot.poll_group = null;
            self.op_slots.release(slot);
        }
    }

    // -----------------------------------------------------------------------
    // green_poll_fd_timeout: single-fd poll with LINK_TIMEOUT
    // -----------------------------------------------------------------------

    pub fn greenPollFdTimeout(self: *Hub, fd: posix.fd_t, events: u32, timeout_ms: i64) ?*PyObject {
        // Acquire op slot
        const slot = self.op_slots.acquire() orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_poll_fd_timeout: op slot table exhausted",
            );
            return null;
        };

        // Get current greenlet
        const current = py.py_helper_greenlet_getcurrent() orelse {
            self.op_slots.release(slot);
            return null;
        };

        // Submit POLL_ADD SQE with IOSQE_IO_LINK flag
        const user_data = op_slot.encodeUserData(slot.slot_index, slot.generation);
        const sqe = self.ring.prepPollAdd(fd, events, user_data) catch {
            py.py_helper_decref(current);
            self.op_slots.release(slot);
            py.py_helper_err_set_string(
                py.py_helper_exc_oserror(),
                "green_poll_fd_timeout: failed to submit poll SQE",
            );
            return null;
        };
        sqe.flags |= IOSQE_IO_LINK;

        // Submit LINK_TIMEOUT SQE (kernel auto-cancels the poll if timeout fires)
        var ts = linux.kernel_timespec{
            .sec = @divTrunc(timeout_ms, 1000),
            .nsec = @rem(timeout_ms, 1000) * 1_000_000,
        };
        _ = self.ring.prepLinkTimeout(&ts, IGNORE_SENTINEL) catch {
            py.py_helper_decref(current);
            self.op_slots.release(slot);
            py.py_helper_err_set_string(
                py.py_helper_exc_oserror(),
                "green_poll_fd_timeout: failed to submit link_timeout SQE",
            );
            return null;
        };

        slot.greenlet = current;
        self.active_waits += 1;

        // Switch to hub
        const hub_g = self.hub_greenlet orelse {
            py.py_helper_err_set_string(
                py.py_helper_exc_runtime_error(),
                "green_poll_fd_timeout: hub greenlet not set",
            );
            slot.greenlet = null;
            self.active_waits -= 1;
            py.py_helper_decref(current);
            self.op_slots.release(slot);
            return null;
        };
        const switch_result = py.py_helper_greenlet_switch(hub_g, null, null);
        if (switch_result == null) {
            self.op_slots.release(slot);
            return null;
        }
        py.py_helper_decref(switch_result);

        // On resume: check result
        const res = slot.result;
        self.op_slots.release(slot);

        if (res > 0) {
            // Poll completed — return revents
            return py.PyLong_FromLong(@as(c_long, @intCast(res)));
        }

        const ecanceled_neg = -@as(i32, @intFromEnum(linux.E.CANCELED));
        if (res == ecanceled_neg) {
            // Timeout fired — poll was cancelled. Return 0.
            return py.PyLong_FromLong(0);
        }

        if (res < 0) {
            py.py_helper_err_set_from_errno(-res);
            return null;
        }

        // res == 0 — shouldn't happen for poll, return 0
        return py.PyLong_FromLong(0);
    }
};

// ---------------------------------------------------------------------------
// IPv4 address parser (helper for greenConnect)
// ---------------------------------------------------------------------------

fn parseIpv4(host: [*:0]const u8) ?[4]u8 {
    var result: [4]u8 = undefined;
    var octet_idx: usize = 0;
    var current_val: u16 = 0;
    var digits: u8 = 0;
    var i: usize = 0;
    while (host[i] != 0) : (i += 1) {
        const c = host[i];
        if (c == '.') {
            if (digits == 0 or octet_idx >= 3) return null;
            if (current_val > 255) return null;
            result[octet_idx] = @intCast(current_val);
            octet_idx += 1;
            current_val = 0;
            digits = 0;
        } else if (c >= '0' and c <= '9') {
            current_val = current_val * 10 + (c - '0');
            digits += 1;
            if (digits > 3) return null;
        } else {
            return null;
        }
    }
    if (digits == 0 or octet_idx != 3) return null;
    if (current_val > 255) return null;
    result[octet_idx] = @intCast(current_val);
    return result;
}

// ---------------------------------------------------------------------------
// Global hub instance (one per process)
// ---------------------------------------------------------------------------

var global_hub: ?*Hub = null;

fn getHub() ?*Hub {
    return global_hub;
}

// ---------------------------------------------------------------------------
// Python-facing API functions (called from extension.zig)
// ---------------------------------------------------------------------------

/// hub_run(listen_fd, acceptor_fn) → None
pub fn pyHubRun(_: ?*PyObject, args: ?*PyObject) callconv(.c) ?*PyObject {
    var listen_fd_long: c_long = 0;
    var acceptor_fn: ?*PyObject = null;
    if (py.PyArg_ParseTuple(args, "lO", &listen_fd_long, &acceptor_fn) == 0)
        return null;

    const allocator = std.heap.c_allocator;

    // Allocate hub on the heap so it survives across greenlet switches
    const hub_ptr = allocator.create(Hub) catch {
        _ = py.py_helper_err_no_memory();
        return null;
    };
    hub_ptr.* = Hub.init(allocator) catch {
        allocator.destroy(hub_ptr);
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "hub_run: failed to initialize hub",
        );
        return null;
    };
    global_hub = hub_ptr;

    // The current greenlet IS the hub greenlet
    hub_ptr.hub_greenlet = py.py_helper_greenlet_getcurrent();
    if (hub_ptr.hub_greenlet == null) {
        hub_ptr.deinit();
        allocator.destroy(hub_ptr);
        global_hub = null;
        return null;
    }

    // Create the acceptor greenlet with hub as parent
    const acceptor_g = py.py_helper_greenlet_new(acceptor_fn, hub_ptr.hub_greenlet) orelse {
        hub_ptr.deinit();
        allocator.destroy(hub_ptr);
        global_hub = null;
        return null;
    };

    // Schedule the acceptor to run
    py.py_helper_incref(acceptor_g);
    hub_ptr.ready.append(hub_ptr.allocator, acceptor_g) catch {
        py.py_helper_decref(acceptor_g);
        py.py_helper_decref(acceptor_g);
        hub_ptr.deinit();
        allocator.destroy(hub_ptr);
        global_hub = null;
        _ = py.py_helper_err_no_memory();
        return null;
    };
    py.py_helper_decref(acceptor_g);

    // Run the hub loop
    hub_ptr.hubLoop();

    // Cleanup
    hub_ptr.deinit();
    allocator.destroy(hub_ptr);
    global_hub = null;

    if (py.py_helper_err_occurred() != null)
        return null;

    const none = py.py_helper_none();
    py.py_helper_incref(none);
    return none;
}

/// green_accept(listen_fd) → int
pub fn pyGreenAccept(_: ?*PyObject, args: ?*PyObject) callconv(.c) ?*PyObject {
    var listen_fd_long: c_long = 0;
    if (py.PyArg_ParseTuple(args, "l", &listen_fd_long) == 0)
        return null;

    const hub_ptr = getHub() orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "green_accept: hub not running",
        );
        return null;
    };

    const listen_fd: posix.fd_t = @intCast(listen_fd_long);
    return hub_ptr.greenAccept(listen_fd);
}

/// green_recv(fd, max_bytes) → bytes
pub fn pyGreenRecv(_: ?*PyObject, args: ?*PyObject) callconv(.c) ?*PyObject {
    var fd_long: c_long = 0;
    var max_bytes_long: c_long = 0;
    if (py.PyArg_ParseTuple(args, "ll", &fd_long, &max_bytes_long) == 0)
        return null;

    const hub_ptr = getHub() orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "green_recv: hub not running",
        );
        return null;
    };

    const fd: posix.fd_t = @intCast(fd_long);
    const max_bytes: usize = @intCast(max_bytes_long);
    return hub_ptr.greenRecv(fd, max_bytes);
}

/// green_send(fd, data) → int
pub fn pyGreenSend(_: ?*PyObject, args: ?*PyObject) callconv(.c) ?*PyObject {
    var fd_long: c_long = 0;
    var data: ?*PyObject = null;
    if (py.PyArg_ParseTuple(args, "lO", &fd_long, &data) == 0)
        return null;

    const hub_ptr = getHub() orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "green_send: hub not running",
        );
        return null;
    };

    const fd: posix.fd_t = @intCast(fd_long);
    return hub_ptr.greenSend(fd, data.?);
}

/// hub_stop() → None
pub fn pyHubStop(_: ?*PyObject, _: ?*PyObject) callconv(.c) ?*PyObject {
    const hub_ptr = getHub() orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "hub_stop: hub not running",
        );
        return null;
    };

    _ = posix.write(hub_ptr.stop_pipe[1], &[_]u8{1}) catch {
        py.py_helper_err_set_string(
            py.py_helper_exc_oserror(),
            "hub_stop: failed to write to stop pipe",
        );
        return null;
    };

    const none = py.py_helper_none();
    py.py_helper_incref(none);
    return none;
}

/// hub_schedule(greenlet) → None
pub fn pyHubSchedule(_: ?*PyObject, args: ?*PyObject) callconv(.c) ?*PyObject {
    var greenlet: ?*PyObject = null;
    if (py.PyArg_ParseTuple(args, "O", &greenlet) == 0)
        return null;

    const hub_ptr = getHub() orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "hub_schedule: hub not running",
        );
        return null;
    };

    const g = greenlet.?;
    py.py_helper_incref(g);
    hub_ptr.ready.append(hub_ptr.allocator, g) catch {
        py.py_helper_decref(g);
        _ = py.py_helper_err_no_memory();
        return null;
    };

    const none = py.py_helper_none();
    py.py_helper_incref(none);
    return none;
}

/// get_hub_greenlet() → greenlet
pub fn pyGetHubGreenlet(_: ?*PyObject, _: ?*PyObject) callconv(.c) ?*PyObject {
    const hub_ptr = getHub() orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "get_hub_greenlet: hub not running",
        );
        return null;
    };

    const g = hub_ptr.hub_greenlet orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "get_hub_greenlet: hub greenlet not set",
        );
        return null;
    };

    py.py_helper_incref(g);
    return g;
}

/// green_close(fd) → None
pub fn pyGreenClose(_: ?*PyObject, args: ?*PyObject) callconv(.c) ?*PyObject {
    var fd_long: c_long = 0;
    if (py.PyArg_ParseTuple(args, "l", &fd_long) == 0)
        return null;

    const hub_ptr = getHub() orelse {
        // No hub running — just close the fd directly
        posix.close(@intCast(fd_long));
        const none = py.py_helper_none();
        py.py_helper_incref(none);
        return none;
    };

    const fd: posix.fd_t = @intCast(fd_long);
    return hub_ptr.greenClose(fd);
}

/// green_sleep(seconds) → None
pub fn pyGreenSleep(_: ?*PyObject, args: ?*PyObject) callconv(.c) ?*PyObject {
    var seconds: f64 = 0;
    if (py.PyArg_ParseTuple(args, "d", &seconds) == 0)
        return null;

    const hub_ptr = getHub() orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "green_sleep: hub not running",
        );
        return null;
    };

    return hub_ptr.greenSleep(seconds);
}

/// green_connect(host, port) → int (fd)
pub fn pyGreenConnect(_: ?*PyObject, args: ?*PyObject) callconv(.c) ?*PyObject {
    var host: [*c]const u8 = null;
    var port_long: c_long = 0;
    if (py.PyArg_ParseTuple(args, "sl", &host, &port_long) == 0)
        return null;

    const hub_ptr = getHub() orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "green_connect: hub not running",
        );
        return null;
    };

    // Convert [*c]const u8 to [*:0]const u8 (PyArg_ParseTuple "s" guarantees null-terminated)
    const host_sentinel: [*:0]const u8 = @ptrCast(host);
    return hub_ptr.greenConnect(host_sentinel, @intCast(port_long));
}

/// green_connect_fd(fd, host, port) → None
pub fn pyGreenConnectFd(_: ?*PyObject, args: ?*PyObject) callconv(.c) ?*PyObject {
    var fd_long: c_long = 0;
    var host: [*c]const u8 = null;
    var port_long: c_long = 0;
    if (py.PyArg_ParseTuple(args, "lsl", &fd_long, &host, &port_long) == 0)
        return null;

    const hub_ptr = getHub() orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "green_connect_fd: hub not running",
        );
        return null;
    };

    const host_sentinel: [*:0]const u8 = @ptrCast(host);
    return hub_ptr.greenConnectFd(@intCast(fd_long), host_sentinel, @intCast(port_long));
}

/// green_poll_fd(fd, events) → int (revents)
pub fn pyGreenPollFd(_: ?*PyObject, args: ?*PyObject) callconv(.c) ?*PyObject {
    var fd_long: c_long = 0;
    var events_long: c_long = 0;
    if (py.PyArg_ParseTuple(args, "ll", &fd_long, &events_long) == 0)
        return null;

    const hub_ptr = getHub() orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "green_poll_fd: hub not running",
        );
        return null;
    };

    return hub_ptr.greenPollFd(@intCast(fd_long), @intCast(events_long));
}

/// green_register_fd(fd) → None
pub fn pyGreenRegisterFd(_: ?*PyObject, args: ?*PyObject) callconv(.c) ?*PyObject {
    var fd_long: c_long = 0;
    if (py.PyArg_ParseTuple(args, "l", &fd_long) == 0)
        return null;

    const hub_ptr = getHub() orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "green_register_fd: hub not running",
        );
        return null;
    };

    return hub_ptr.greenRegisterFd(@intCast(fd_long));
}

/// green_unregister_fd(fd) → None
pub fn pyGreenUnregisterFd(_: ?*PyObject, args: ?*PyObject) callconv(.c) ?*PyObject {
    var fd_long: c_long = 0;
    if (py.PyArg_ParseTuple(args, "l", &fd_long) == 0)
        return null;

    const hub_ptr = getHub() orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "green_unregister_fd: hub not running",
        );
        return null;
    };

    return hub_ptr.greenUnregisterFd(@intCast(fd_long));
}

/// green_poll_multi(fds_list, events_list, timeout_ms) → list[tuple[int, int]]
pub fn pyGreenPollMulti(_: ?*PyObject, args: ?*PyObject) callconv(.c) ?*PyObject {
    var fds_list: ?*PyObject = null;
    var events_list: ?*PyObject = null;
    var timeout_ms_long: c_long = 0;
    if (py.PyArg_ParseTuple(args, "OOl", &fds_list, &events_list, &timeout_ms_long) == 0)
        return null;

    const hub_ptr = getHub() orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "green_poll_multi: hub not running",
        );
        return null;
    };

    const n_raw = py.PyList_Size(fds_list.?);
    if (n_raw < 0) return null;
    const n: usize = @intCast(n_raw);

    if (n > MAX_POLL_FDS) {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "green_poll_multi: too many fds (max 64)",
        );
        return null;
    }

    // Extract fds and events into stack arrays
    var fds: [MAX_POLL_FDS]posix.fd_t = undefined;
    var poll_events: [MAX_POLL_FDS]u32 = undefined;

    for (0..n) |i| {
        const fd_obj = py.PyList_GetItem(fds_list.?, @intCast(i)) orelse return null;
        const fd_val = py.PyLong_AsLong(fd_obj);
        if (fd_val == -1 and py.py_helper_err_occurred() != null) return null;
        fds[i] = @intCast(fd_val);

        const ev_obj = py.PyList_GetItem(events_list.?, @intCast(i)) orelse return null;
        const ev_val = py.PyLong_AsLong(ev_obj);
        if (ev_val == -1 and py.py_helper_err_occurred() != null) return null;
        poll_events[i] = @intCast(ev_val);
    }

    return hub_ptr.greenPollMulti(fds[0..n], poll_events[0..n], timeout_ms_long);
}

/// green_poll_fd_timeout(fd, events, timeout_ms) → int
pub fn pyGreenPollFdTimeout(_: ?*PyObject, args: ?*PyObject) callconv(.c) ?*PyObject {
    var fd_long: c_long = 0;
    var events_long: c_long = 0;
    var timeout_ms_long: c_long = 0;
    if (py.PyArg_ParseTuple(args, "lll", &fd_long, &events_long, &timeout_ms_long) == 0)
        return null;

    const hub_ptr = getHub() orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "green_poll_fd_timeout: hub not running",
        );
        return null;
    };

    return hub_ptr.greenPollFdTimeout(@intCast(fd_long), @intCast(events_long), timeout_ms_long);
}

// ---------------------------------------------------------------------------
// HTTP bridge functions
// ---------------------------------------------------------------------------

fn methodToStr(method: http.Method) []const u8 {
    return switch (method) {
        .get => "GET",
        .post => "POST",
        .head => "HEAD",
        .put => "PUT",
        .delete => "DELETE",
        .connect => "CONNECT",
        .options => "OPTIONS",
        .trace => "TRACE",
        .patch => "PATCH",
        .unknown => "UNKNOWN",
    };
}

fn statusFromInt(code: u16) ?http_response.StatusCode {
    return switch (code) {
        200 => .ok,
        201 => .created,
        204 => .no_content,
        301 => .moved_permanently,
        400 => .bad_request,
        404 => .not_found,
        405 => .method_not_allowed,
        431 => .request_header_fields_too_large,
        500 => .internal_server_error,
        503 => .service_unavailable,
        else => null,
    };
}

/// http_parse_request(data: bytes) → tuple | None
///
/// Returns (method, path, body, raw_bytes_consumed, keep_alive) or None if
/// the data is incomplete. Raises ValueError on malformed input.
pub fn pyHttpParseRequest(_: ?*PyObject, args: ?*PyObject) callconv(.c) ?*PyObject {
    var data: ?*PyObject = null;
    if (py.PyArg_ParseTuple(args, "S", &data) == 0) return null;

    const buf_ptr: [*]const u8 = @ptrCast(py.py_helper_bytes_as_string(data.?) orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "http_parse_request: invalid bytes",
        );
        return null;
    });
    const buf_len: usize = @intCast(py.py_helper_bytes_get_size(data.?));
    const buf = buf_ptr[0..buf_len];

    const req = http.parseRequestFull(buf) catch |err| switch (err) {
        error.Incomplete => {
            const none = py.py_helper_none();
            py.py_helper_incref(none);
            return none;
        },
        error.Invalid => {
            py.py_helper_err_set_string(
                py.py_helper_exc_value_error(),
                "Invalid HTTP request",
            );
            return null;
        },
    };

    // Build method string
    const method_str = methodToStr(req.method);
    const py_method = py.PyUnicode_FromStringAndSize(method_str.ptr, @intCast(method_str.len)) orelse return null;

    // Build path string
    const py_path = py.PyUnicode_FromStringAndSize(req.path.ptr, @intCast(req.path.len)) orelse {
        py.py_helper_decref(py_method);
        return null;
    };

    // Build body bytes
    const py_body = py.py_helper_bytes_from_string_and_size(
        @ptrCast(req.body.ptr),
        @intCast(req.body.len),
    ) orelse {
        py.py_helper_decref(py_method);
        py.py_helper_decref(py_path);
        return null;
    };

    // bytes_consumed
    const py_consumed = py.PyLong_FromLong(@intCast(req.raw_bytes_consumed)) orelse {
        py.py_helper_decref(py_method);
        py.py_helper_decref(py_path);
        py.py_helper_decref(py_body);
        return null;
    };

    // keep_alive
    const py_ka = if (req.keep_alive) py.py_helper_true() else py.py_helper_false();
    py.py_helper_incref(py_ka);

    // Build tuple (method, path, body, bytes_consumed, keep_alive)
    const tuple = py.py_helper_tuple_new(5) orelse {
        py.py_helper_decref(py_method);
        py.py_helper_decref(py_path);
        py.py_helper_decref(py_body);
        py.py_helper_decref(py_consumed);
        py.py_helper_decref(py_ka);
        return null;
    };

    // PyTuple_SetItem steals references
    _ = py.py_helper_tuple_setitem(tuple, 0, py_method);
    _ = py.py_helper_tuple_setitem(tuple, 1, py_path);
    _ = py.py_helper_tuple_setitem(tuple, 2, py_body);
    _ = py.py_helper_tuple_setitem(tuple, 3, py_consumed);
    _ = py.py_helper_tuple_setitem(tuple, 4, py_ka);

    return tuple;
}

/// http_format_response(status: int, body: bytes) → bytes
///
/// Formats a complete HTTP/1.1 response with Content-Length header.
pub fn pyHttpFormatResponse(_: ?*PyObject, args: ?*PyObject) callconv(.c) ?*PyObject {
    var status_long: c_long = 0;
    var body_obj: ?*PyObject = null;
    if (py.PyArg_ParseTuple(args, "lS", &status_long, &body_obj) == 0) return null;

    const status_code: u16 = @intCast(status_long);
    const status = statusFromInt(status_code) orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_value_error(),
            "Unsupported HTTP status code",
        );
        return null;
    };

    const body_ptr: [*]const u8 = @ptrCast(py.py_helper_bytes_as_string(body_obj.?) orelse {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "http_format_response: invalid body bytes",
        );
        return null;
    });
    const body_len: usize = @intCast(py.py_helper_bytes_get_size(body_obj.?));
    const body_slice = body_ptr[0..body_len];

    // Write response into a stack buffer
    var buf: [65536]u8 = undefined;
    const n = http_response.writeResponse(&buf, status, &.{}, body_slice) catch {
        py.py_helper_err_set_string(
            py.py_helper_exc_runtime_error(),
            "Response too large for buffer",
        );
        return null;
    };

    return py.py_helper_bytes_from_string_and_size(@ptrCast(&buf), @intCast(n));
}
