"""Example: monkey-patched framework with several routes and middleware.

Run::

    uv run python examples/monkey_patch.py

Then test with curl (see bottom of file for examples).
"""

from theframework.monkey import patch_all

patch_all()  # must be called before importing anything that caches stdlib I/O

import json
import time

from theframework import Framework, Request, Response

app = Framework()

# -- Middleware ----------------------------------------------------------------


def log_requests(request: Request, response: Response, next_fn) -> None:
    """Print every request to stdout."""
    print(f"  --> {request.method} {request.path}")
    next_fn(request, response)


app.use(log_requests)

# -- Routes --------------------------------------------------------------------


@app.route("/")
def index(request: Request, response: Response) -> None:
    response.set_header("Content-Type", "text/plain")
    response.write("Welcome to the-framework (monkey-patched)")


@app.route("/hello")
def hello(request: Request, response: Response) -> None:
    response.set_header("Content-Type", "text/plain")
    response.write("hello world")


@app.route("/sleep")
def sleep_route(request: Request, response: Response) -> None:
    """Uses time.sleep which is monkey-patched to yield cooperatively."""
    seconds = float(request.query_params.get("s", "1"))
    time.sleep(seconds)
    response.set_header("Content-Type", "application/json")
    response.write(json.dumps({"slept": seconds}))


@app.route("/users/{id}")
def get_user(request: Request, response: Response) -> None:
    user_id = request.params["id"]
    response.set_header("Content-Type", "application/json")
    response.write(json.dumps({"user_id": user_id, "name": f"User {user_id}"}))


@app.route("/echo", methods=["POST"])
def echo_post(request: Request, response: Response) -> None:
    """Echo back the JSON body with an extra 'echoed' field."""
    data = request.json()
    if isinstance(data, dict):
        data["echoed"] = True
    response.set_header("Content-Type", "application/json")
    response.write(json.dumps(data))


@app.route("/headers")
def show_headers(request: Request, response: Response) -> None:
    """Return the request headers as JSON."""
    response.set_header("Content-Type", "application/json")
    response.write(json.dumps(request.headers))


@app.route("/search")
def search(request: Request, response: Response) -> None:
    """Demonstrate query parameters: /search?q=hello&limit=10"""
    q = request.query_params.get("q", "")
    limit = int(request.query_params.get("limit", "5"))
    results = [f"result-{i} for '{q}'" for i in range(1, limit + 1)]
    response.set_header("Content-Type", "application/json")
    response.write(json.dumps({"query": q, "limit": limit, "results": results}))


@app.route("/status/{code}")
def custom_status(request: Request, response: Response) -> None:
    """Return an arbitrary HTTP status code."""
    code = int(request.params["code"])
    response.set_status(code)
    response.set_header("Content-Type", "application/json")
    response.write(json.dumps({"status": code}))


# -- Start server --------------------------------------------------------------

if __name__ == "__main__":
    print("Listening on http://127.0.0.1:8000")
    print()
    print("Try:")
    print("  curl http://127.0.0.1:8000/")
    print("  curl http://127.0.0.1:8000/hello")
    print("  curl http://127.0.0.1:8000/sleep?s=0.5")
    print("  curl http://127.0.0.1:8000/users/42")
    print('  curl -X POST -d \'{"msg":"hi"}\' http://127.0.0.1:8000/echo')
    print("  curl http://127.0.0.1:8000/headers")
    print("  curl 'http://127.0.0.1:8000/search?q=hello&limit=3'")
    print("  curl http://127.0.0.1:8000/status/201")
    app.run(host="127.0.0.1", port=8000)
