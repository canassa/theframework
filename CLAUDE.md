# theframework

A fast Python HTTP framework powered by io_uring and greenlets. No async/await—write synchronous code with the runtime handling concurrency.

## Folder Structure

- `theframework/` — Python package with main framework code
  - `app.py` — Framework class and routing
  - `server.py` — Server and greenlet runtime
  - `request.py`, `response.py` — HTTP request/response objects
- `src/` — Zig source for io_uring integration
- `stubs/` — Type stubs for C extensions
- `tests/` — Pytest tests
- `examples/` — Example applications
- `plans/` — Design and implementation documentation
- `build.zig` — Zig build script

## Development

- **Python**: 3.13+ (via `uv`)
- **Zig**: 0.15.2 (via Nix flake)
- **Type checking**: mypy strict mode
- **Testing**: pytest

## Key Implementation Notes

- Custom HTTP server in Zig
- Greenlets handle concurrency (no async/await)
- io_uring for high-performance I/O
- Pre-fork parallelism for multi-core scaling
