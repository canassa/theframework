from __future__ import annotations

import json
import urllib.parse


class Request:
    """HTTP request object parsed from raw bytes."""

    __slots__ = (
        "method",
        "path",
        "full_path",
        "query_params",
        "headers",
        "body",
        "params",
        "_raw_headers",
    )

    method: str
    path: str
    full_path: str
    query_params: dict[str, str]
    headers: dict[str, str]
    body: bytes
    params: dict[str, str]

    def __init__(
        self,
        *,
        method: str,
        path: str,
        full_path: str,
        query_params: dict[str, str],
        headers: dict[str, str],
        body: bytes,
    ) -> None:
        self.method = method
        self.path = path
        self.full_path = full_path
        self.query_params = query_params
        self.headers = headers
        self.body = body
        self.params = {}
        self._raw_headers: list[tuple[bytes, bytes]] = []

    @classmethod
    def _from_raw(
        cls,
        method: str,
        path: str,
        body: bytes,
        raw_bytes: bytes,
    ) -> Request:
        """Build a Request from the raw HTTP bytes and parsed fields."""
        # Parse headers from raw bytes
        headers: dict[str, str] = {}
        header_section, _, _ = raw_bytes.partition(b"\r\n\r\n")
        lines = header_section.split(b"\r\n")
        # Skip request line (first line)
        for line in lines[1:]:
            if b": " in line:
                name, _, value = line.partition(b": ")
                headers[name.decode("latin-1").lower()] = value.decode("latin-1")

        # Split path and query string
        full_path = path
        if "?" in path:
            path_part, _, query_string = path.partition("?")
            parsed_qs = urllib.parse.parse_qs(query_string)
            query_params = {k: v[0] for k, v in parsed_qs.items()}
        else:
            path_part = path
            query_params = {}

        return cls(
            method=method,
            path=path_part,
            full_path=full_path,
            query_params=query_params,
            headers=headers,
            body=body,
        )

    @classmethod
    def _from_parsed(
        cls,
        method: str,
        path: str,
        body: bytes,
        headers: list[tuple[bytes, bytes]],
    ) -> Request:
        """Build Request from Zig-parsed fields. No re-parsing."""
        # Convert headers list to dict (last value wins, backward compat)
        headers_dict: dict[str, str] = {}
        for key_bytes, val_bytes in headers:
            headers_dict[key_bytes.decode("latin-1").lower()] = val_bytes.decode("latin-1")

        # Split path and query string
        full_path = path
        if "?" in path:
            path_part, _, qs = path.partition("?")
            query_params = {k: v[0] for k, v in urllib.parse.parse_qs(qs).items()}
        else:
            path_part = path
            query_params = {}

        req = cls.__new__(cls)
        req.method = method
        req.path = path_part
        req.full_path = full_path
        req.query_params = query_params
        req.headers = headers_dict
        req.body = body
        req.params = {}
        req._raw_headers = headers  # preserve for duplicate access
        return req

    @property
    def raw_headers(self) -> list[tuple[bytes, bytes]]:
        """Headers as ordered list of (name, value) byte pairs.

        Preserves duplicates (e.g. multiple Set-Cookie).
        """
        return self._raw_headers

    def json(self) -> object:
        """Parse body as JSON."""
        return json.loads(self.body)
