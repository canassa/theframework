from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from theframework.request import Request
from theframework.response import Response
from theframework.server import HandlerFunc, serve

NextFn = Callable[[Request, Response], None]
MiddlewareFunc = Callable[[Request, Response, NextFn], None]


@dataclass
class _Route:
    pattern: str
    methods: set[str]
    handler: HandlerFunc
    regex: re.Pattern[str]
    param_names: list[str] = field(default_factory=list)


def _compile_route(pattern: str) -> tuple[re.Pattern[str], list[str]]:
    """Compile a route pattern like '/users/{id}' into a regex and param name list."""
    param_names: list[str] = []
    regex_parts: list[str] = []
    pos = 0
    for match in re.finditer(r"\{(\w+)\}", pattern):
        start, end = match.span()
        regex_parts.append(re.escape(pattern[pos:start]))
        regex_parts.append(f"(?P<{match.group(1)}>[^/]+)")
        param_names.append(match.group(1))
        pos = end
    regex_parts.append(re.escape(pattern[pos:]))
    return re.compile("^" + "".join(regex_parts) + "$"), param_names


class Framework:
    """Application framework with routing and middleware."""

    def __init__(self) -> None:
        self._routes: list[_Route] = []
        self._middleware: list[MiddlewareFunc] = []

    def route(
        self, path: str, methods: list[str] | None = None
    ) -> Callable[[HandlerFunc], HandlerFunc]:
        """Decorator: @app.route("/hello", methods=["GET"])"""
        method_set = set(methods) if methods else {"GET"}

        def decorator(fn: HandlerFunc) -> HandlerFunc:
            regex, param_names = _compile_route(path)
            self._routes.append(
                _Route(
                    pattern=path,
                    methods=method_set,
                    handler=fn,
                    regex=regex,
                    param_names=param_names,
                )
            )
            return fn

        return decorator

    def use(self, middleware: MiddlewareFunc) -> None:
        """Register middleware (executed in registration order)."""
        self._middleware.append(middleware)

    def dispatch(self, request: Request, response: Response) -> None:
        """Match route, populate request.params, run middleware chain + handler."""
        matched_route: _Route | None = None
        method_not_allowed = False

        for route in self._routes:
            m = route.regex.match(request.path)
            if m is not None:
                if request.method in route.methods:
                    matched_route = route
                    request.params = {name: m.group(name) for name in route.param_names}
                    break
                else:
                    method_not_allowed = True

        if matched_route is None:
            if method_not_allowed:
                response.set_status(405)
                response.write(b"Method Not Allowed")
            else:
                response.set_status(404)
                response.write(b"Not Found")
            return

        handler = _build_chain(self._middleware, matched_route.handler)
        handler(request, response)

    def _make_handler(self) -> HandlerFunc:
        """Create a handler function that dispatches through the router."""

        def handler(request: Request, response: Response) -> None:
            self.dispatch(request, response)

        return handler

    def run(
        self, host: str = "127.0.0.1", port: int = 8000, *, workers: int = 1
    ) -> None:
        """Start the server.

        Args:
            host: Address to bind to.
            port: Port to bind to.
            workers: Number of worker processes. 1 = single-process (default).
                     0 = auto-detect from CPU count.
        """
        serve(self._make_handler(), host, port, workers=workers)


def _build_chain(middlewares: list[MiddlewareFunc], handler: HandlerFunc) -> HandlerFunc:
    """Build middleware chain: first registered middleware runs first."""
    chain = handler
    for mw in reversed(middlewares):
        prev = chain

        def wrapper(
            req: Request,
            resp: Response,
            _mw: MiddlewareFunc = mw,
            _prev: HandlerFunc = prev,
        ) -> None:
            _mw(req, resp, _prev)

        chain = wrapper
    return chain
