"""Greenlet + selectors echo server.

A single-threaded echo server using greenlets for concurrency and the
stdlib selectors module for I/O multiplexing. Each connection gets its
own greenlet. The hub greenlet runs the selector loop.

This is the exact same architecture as the final io_uring-based framework,
just with selectors instead of io_uring and pure Python instead of Zig.

Key insight: every greenlet that needs to wait for something registers
in _waiting and switches to the hub. Newly spawned greenlets go through
a _ready queue that the hub drains before blocking on I/O. The hub is
the ONLY greenlet that calls select().
"""

from __future__ import annotations

import selectors
import socket

import greenlet


class GreenletEchoServer:
    """A greenlet-based echo server using selectors for I/O."""

    def __init__(self) -> None:
        self._sel: selectors.DefaultSelector | None = None
        self._hub: greenlet.greenlet | None = None
        self._waiting: dict[int, greenlet.greenlet] = {}
        self._ready: list[greenlet.greenlet] = []
        self.running = False
        self.listen_sock: socket.socket | None = None

    def green_accept(self, sock: socket.socket) -> socket.socket:
        """Accept a connection, yielding to the hub until one is ready."""
        assert self._sel is not None and self._hub is not None
        fd = sock.fileno()
        self._sel.register(fd, selectors.EVENT_READ)
        self._waiting[fd] = greenlet.getcurrent()
        self._hub.switch()
        self._sel.unregister(fd)
        conn, _ = sock.accept()
        conn.setblocking(False)
        return conn

    def green_recv(self, sock: socket.socket, bufsize: int) -> bytes:
        """Read from a socket, yielding to the hub until data is available."""
        assert self._sel is not None and self._hub is not None
        fd = sock.fileno()
        self._sel.register(fd, selectors.EVENT_READ)
        self._waiting[fd] = greenlet.getcurrent()
        self._hub.switch()
        self._sel.unregister(fd)
        return sock.recv(bufsize)

    def green_send(self, sock: socket.socket, data: bytes) -> int:
        """Write to a socket, yielding to the hub until writable."""
        assert self._sel is not None and self._hub is not None
        fd = sock.fileno()
        self._sel.register(fd, selectors.EVENT_WRITE)
        self._waiting[fd] = greenlet.getcurrent()
        self._hub.switch()
        self._sel.unregister(fd)
        return sock.send(data)

    def spawn(self, fn: greenlet.greenlet) -> None:
        """Schedule a greenlet to be started on the next hub iteration."""
        self._ready.append(fn)

    def _handle_connection(self, conn: socket.socket) -> None:
        """Echo handler — runs in its own greenlet."""
        try:
            while True:
                data = self.green_recv(conn, 4096)
                if not data:
                    break
                total_sent = 0
                while total_sent < len(data):
                    sent = self.green_send(conn, data[total_sent:])
                    total_sent += sent
        except OSError:
            pass
        finally:
            conn.close()

    def _acceptor(self) -> None:
        """Accept loop — runs in its own greenlet, spawns handlers."""
        assert self._hub is not None and self.listen_sock is not None
        while self.running:
            try:
                conn = self.green_accept(self.listen_sock)
            except OSError:
                if not self.running:
                    break
                raise
            captured_conn = conn
            worker = greenlet.greenlet(
                lambda c=captured_conn: self._handle_connection(c),
                parent=self._hub,
            )
            self.spawn(worker)

    def run(self) -> None:
        """Run the hub loop. Blocks until self.running is set to False."""
        self._sel = selectors.DefaultSelector()
        self._hub = greenlet.getcurrent()
        self.running = True

        # Start the acceptor greenlet
        acceptor_g = greenlet.greenlet(self._acceptor, parent=self._hub)
        self._ready.append(acceptor_g)

        try:
            while self.running:
                # Drain ready queue — start/resume newly spawned greenlets
                while self._ready:
                    g = self._ready.pop(0)
                    if not g.dead:
                        g.switch()

                # Block on I/O — this is the ONLY blocking point
                events = self._sel.select(timeout=0.05)
                for key, _ in events:
                    fd = key.fd
                    worker = self._waiting.pop(fd, None)
                    if worker is not None and not worker.dead:
                        worker.switch()
        finally:
            if self.listen_sock is not None:
                self.listen_sock.close()
            self._sel.close()


def main() -> None:
    server = GreenletEchoServer()
    server.listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.listen_sock.setblocking(False)
    server.listen_sock.bind(("127.0.0.1", 9999))
    server.listen_sock.listen(128)
    print(f"Echo server listening on {server.listen_sock.getsockname()}")
    print("Press Ctrl+C to stop.")
    try:
        server.run()
    except KeyboardInterrupt:
        server.running = False


if __name__ == "__main__":
    main()
