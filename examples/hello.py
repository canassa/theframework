import json

from theframework import Framework, Request, Response

import _framework_core

app = Framework()


@app.route("/hello")
def hello(request: Request, response: Response) -> None:
    response.write(b"hello world")


@app.route("/slow")
def slow(request: Request, response: Response) -> None:
    """Sleeps 1 second cooperatively — other requests keep flowing."""
    _framework_core.green_sleep(1.0)
    response.write(b"done sleeping")


@app.route("/proxy")
def proxy(request: Request, response: Response) -> None:
    """Makes an outbound TCP connection from inside a handler."""
    fd = _framework_core.green_connect("127.0.0.1", 9999)
    _framework_core.green_send(fd, b"ping")
    data = _framework_core.green_recv(fd, 4096)
    _framework_core.green_close(fd)
    response.set_header("Content-Type", "application/json")
    response.write(json.dumps({"echoed": data.decode()}).encode())


@app.route("/users/{id}")
def get_user(request: Request, response: Response) -> None:
    response.set_header("Content-Type", "application/json")
    response.write(json.dumps({"user_id": request.params["id"]}).encode())


app.run(host="127.0.0.1", port=8000, workers=0)  # 0 = auto-detect CPU count
