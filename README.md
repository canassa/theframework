<img width="1536" height="1024" alt="Image" src="https://github.com/user-attachments/assets/ddbfa0d9-523c-4ece-bbb3-ca66b5efdd45" />

<h1 align="center">theframework</h1>

A fast Python HTTP framework powered by io_uring and greenlets.

All I/O is cooperative — handlers can sleep, make outbound connections, or wait
on slow clients without blocking other requests.

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
