from __future__ import annotations

import json
import urllib.parse


class Request:
    """HTTP request object parsed from raw bytes."""

    __slots__ = ("method", "path", "full_path", "query_params", "headers", "body", "params")

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

    def json(self) -> object:
        """Parse body as JSON."""
        return json.loads(self.body)
