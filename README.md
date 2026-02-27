<img width="1212" height="469" alt="Image" src="https://github.com/user-attachments/assets/2396a624-fe5f-4936-917e-14e0fb9d628a" />

<h1 align="center">theframework</h1>

A fast Python HTTP framework powered by io_uring and greenlets.

## Rationale

- **No async/await**: Write synchronous code. The runtime handles concurrency for you.
- **Ultimate performance**: io_uring completion-based I/O, pre-fork parallelism.
- **Custom HTTP server in Zig**: SIMD HTTP parser, zero-copy I/O, direct kernel integration.

## Quickstart

```python
from theframework import Framework, Request, Response

app = Framework()

@app.route("/hello")
def hello(request: Request, response: Response) -> None:
    response.write("hello world")

app.run(host="127.0.0.1", port=8000)
```

## Routing

Routes support path parameters:

```python
@app.route("/users/{id}")
def get_user(request: Request, response: Response) -> None:
    user_id = request.params["id"]
    response.write(f"user {user_id}")
```

Restrict HTTP methods:

```python
@app.route("/items", methods=["POST"])
def create_item(request: Request, response: Response) -> None:
    data = request.json()
    response.set_status(201)
    response.write(b"created")
```

## Request

- `request.method` — HTTP method (`"GET"`, `"POST"`, etc.)
- `request.path` — URL path without query string
- `request.query_params` — dict of query string parameters
- `request.headers` — dict of headers (lowercase keys)
- `request.body` — raw body as bytes
- `request.params` — dict of path parameters from route
- `request.json()` — parse body as JSON

## Response

- `response.write(data)` — append bytes or str to the response body
- `response.set_status(code)` — set HTTP status code (default 200)
- `response.set_header(name, value)` — set a response header

## Middleware

```python
def log_requests(request: Request, response: Response, next_fn) -> None:
    print(f"{request.method} {request.path}")
    next_fn(request, response)

app.use(log_requests)
```

Middleware runs in registration order. Call `next_fn` to continue the chain.

## Monkey patching

To make third-party libraries (requests, urllib3, etc.) cooperative:

```python
from theframework.monkey import patch_all
patch_all()  # call before importing third-party code
```

This patches `socket`, `ssl`, `select`, `selectors`, and `time.sleep` so that
blocking calls yield to other greenlets instead of blocking the thread.
