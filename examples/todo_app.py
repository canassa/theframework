"""Todo JSON API backed by SQLite.

Run::

    uv run python examples/todo_app.py

Endpoints::

    GET    /todos       — list all todos
    POST   /todos       — create a todo  {"title": "..."}
    GET    /todos/{id}  — get one todo
    PATCH  /todos/{id}  — update a todo  {"title": "...", "done": true}
    DELETE /todos/{id}  — delete a todo
"""

from theframework.monkey import patch_all

patch_all()

import json
import sqlite3

from theframework import Framework, Request, Response

DB_PATH = "todos.db"

app = Framework()


# -- Database setup ------------------------------------------------------------


def get_db() -> sqlite3.Connection:
    """Return a module-level connection (single-threaded, single-process)."""
    return _db


def _init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS todos (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT    NOT NULL,
            done  INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    return conn


_db = _init_db()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "title": row["title"], "done": bool(row["done"])}


# -- Middleware ----------------------------------------------------------------


def json_content_type(request: Request, response: Response, next_fn) -> None:
    """Set JSON content type on every response."""
    response.set_header("Content-Type", "application/json")
    next_fn(request, response)


app.use(json_content_type)


# -- Routes --------------------------------------------------------------------


@app.route("/todos")
def list_todos(request: Request, response: Response) -> None:
    rows = get_db().execute("SELECT * FROM todos ORDER BY id").fetchall()
    response.write(json.dumps([_row_to_dict(r) for r in rows]))


@app.route("/todos", methods=["POST"])
def create_todo(request: Request, response: Response) -> None:
    body = request.json()
    if not isinstance(body, dict) or "title" not in body:
        response.set_status(400)
        response.write(json.dumps({"error": "missing 'title'"}))
        return
    db = get_db()
    cur = db.execute("INSERT INTO todos (title) VALUES (?)", (body["title"],))
    db.commit()
    row = db.execute("SELECT * FROM todos WHERE id = ?", (cur.lastrowid,)).fetchone()
    response.set_status(201)
    response.write(json.dumps(_row_to_dict(row)))


@app.route("/todos/{id}")
def get_todo(request: Request, response: Response) -> None:
    row = get_db().execute("SELECT * FROM todos WHERE id = ?", (request.params["id"],)).fetchone()
    if row is None:
        response.set_status(404)
        response.write(json.dumps({"error": "not found"}))
        return
    response.write(json.dumps(_row_to_dict(row)))


@app.route("/todos/{id}", methods=["PATCH"])
def update_todo(request: Request, response: Response) -> None:
    db = get_db()
    row = db.execute("SELECT * FROM todos WHERE id = ?", (request.params["id"],)).fetchone()
    if row is None:
        response.set_status(404)
        response.write(json.dumps({"error": "not found"}))
        return

    body = request.json()
    if not isinstance(body, dict):
        response.set_status(400)
        response.write(json.dumps({"error": "expected JSON object"}))
        return

    title = body.get("title", row["title"])
    done = int(body.get("done", row["done"]))
    db.execute(
        "UPDATE todos SET title = ?, done = ? WHERE id = ?",
        (title, done, request.params["id"]),
    )
    db.commit()
    updated = db.execute("SELECT * FROM todos WHERE id = ?", (request.params["id"],)).fetchone()
    response.write(json.dumps(_row_to_dict(updated)))


@app.route("/todos/{id}", methods=["DELETE"])
def delete_todo(request: Request, response: Response) -> None:
    db = get_db()
    row = db.execute("SELECT * FROM todos WHERE id = ?", (request.params["id"],)).fetchone()
    if row is None:
        response.set_status(404)
        response.write(json.dumps({"error": "not found"}))
        return
    db.execute("DELETE FROM todos WHERE id = ?", (request.params["id"],))
    db.commit()
    response.set_status(204)


# -- Start server --------------------------------------------------------------

if __name__ == "__main__":
    print("Todo API listening on http://127.0.0.1:8000")
    print()
    print("Try:")
    print('  curl -X POST -d \'{"title":"Buy milk"}\' http://127.0.0.1:8000/todos')
    print('  curl -X POST -d \'{"title":"Write tests"}\' http://127.0.0.1:8000/todos')
    print("  curl http://127.0.0.1:8000/todos")
    print("  curl http://127.0.0.1:8000/todos/1")
    print("  curl -X PATCH -d '{\"done\":true}' http://127.0.0.1:8000/todos/1")
    print("  curl -X DELETE http://127.0.0.1:8000/todos/1")
    app.run(host="127.0.0.1", port=8000)
