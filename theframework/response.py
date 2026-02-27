from __future__ import annotations

import _framework_core


_REASON_PHRASES: dict[int, str] = {
    200: "OK",
    201: "Created",
    204: "No Content",
    301: "Moved Permanently",
    302: "Found",
    304: "Not Modified",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Content Too Large",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


class Response:
    """HTTP response object that buffers data and sends on finalize."""

    __slots__ = ("status", "_headers", "_body_parts", "_fd", "_finalized")

    status: int
    _headers: dict[str, str]
    _body_parts: list[bytes]
    _fd: int
    _finalized: bool

    def __init__(self, fd: int) -> None:
        self.status = 200
        self._headers = {}
        self._body_parts = []
        self._fd = fd
        self._finalized = False

    def set_status(self, code: int) -> None:
        self.status = code

    def set_header(self, name: str, value: str) -> None:
        self._headers[name] = value

    def write(self, data: bytes | str) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._body_parts.append(data)

    def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True

        body = b"".join(self._body_parts)
        reason = _REASON_PHRASES.get(self.status, "Unknown")

        # Build response
        parts: list[str] = [f"HTTP/1.1 {self.status} {reason}\r\n"]

        # Set Content-Length if not already set
        if "Content-Length" not in self._headers:
            self._headers["Content-Length"] = str(len(body))

        for name, value in self._headers.items():
            parts.append(f"{name}: {value}\r\n")
        parts.append("\r\n")

        raw = "".join(parts).encode("latin-1") + body
        _framework_core.green_send(self._fd, raw)
