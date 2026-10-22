import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone

# БД для тестов — во временной папке; переменная должна быть задана до импорта main
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

import pytest  # noqa: E402

import main  # noqa: E402


@pytest.fixture
def client():
    return main.app.test_client()


def iso(delta):
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def add(client, title, **extra):
    r = client.post("/api/tasks", json={"title": title, **extra})
    assert r.status_code == 201
    return r.get_json()["id"]


def find(client, task_id):
    return next(t for t in client.get("/api/tasks").get_json() if t["id"] == task_id)


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.get_json()["db"] is True


def test_add_toggle_delete(client):
    task_id = add(client, "сделать лабу")
    assert client.patch(f"/api/tasks/{task_id}", json={"done": True}).status_code == 200
    assert find(client, task_id)["done"] == 1
    assert client.delete(f"/api/tasks/{task_id}").status_code == 204
    assert client.delete(f"/api/tasks/{task_id}").status_code == 404


def test_empty_title_rejected(client):
    assert client.post("/api/tasks", json={"title": "   "}).status_code == 400


def test_invalid_fields_rejected(client):
    for bad in (
        {"title": "x", "priority": 5},
        {"title": "x", "color": "red"},
        {"title": "x", "deadline": "не дата"},
    ):
        assert client.post("/api/tasks", json=bad).status_code == 400


def test_sorting_puts_urgent_and_important_first(client):
    low = add(client, "низкий без срока", priority=3)
    high = add(client, "высокий без срока", priority=1)
    soon = add(client, "срок через 2 часа", priority=3, deadline=iso(timedelta(hours=2)))
    overdue = add(client, "просрочена", priority=2, deadline=iso(timedelta(hours=-3)))
    far = add(client, "срок через неделю", priority=2, deadline=iso(timedelta(days=7)))
    done = add(client, "сделано", priority=1)
    client.patch(f"/api/tasks/{done}", json={"done": True})

    mine = {low, high, soon, overdue, far, done}
    tasks = [t for t in client.get("/api/tasks").get_json() if t["id"] in mine]
    assert [t["id"] for t in tasks] == [overdue, soon, high, far, low, done]
    status = {t["id"]: t["status"] for t in tasks}
    assert status[overdue] == "overdue"
    assert status[soon] == "soon"
    assert status[high] == "normal"
    assert status[done] == "done"


def test_update_fields_and_reset_colors(client):
    task_id = add(client, "задача")
    r = client.patch(
        f"/api/tasks/{task_id}",
        json={"priority": 1, "color": "#336699", "deadline": iso(timedelta(days=3))},
    )
    assert r.status_code == 200
    task = find(client, task_id)
    assert task["priority"] == 1 and task["color"] == "#336699" and task["deadline"]

    assert client.post("/api/tasks/reset-colors").status_code == 200
    assert find(client, task_id)["color"] is None


def test_migrates_old_database(tmp_path, monkeypatch):
    old = str(tmp_path / "old.db")
    with closing(sqlite3.connect(old)) as conn:
        conn.execute(
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, "
            "done INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("INSERT INTO tasks (title) VALUES ('старая задача')")
        conn.commit()
    monkeypatch.setattr(main, "DB_PATH", old)
    main.init_db()
    row = main.fetch("SELECT title, priority, deadline, color FROM tasks")[0]
    assert row["title"] == "старая задача" and row["priority"] == 2 and row["deadline"] is None


def test_metrics_exposed(client):
    client.get("/")
    add(client, "подготовить отчет")
    body = client.get("/metrics").get_data(as_text=True)
    assert "todo_tasks_added_total" in body
    assert "todo_page_visits_total" in body
    assert "todo_db_up 1.0" in body
