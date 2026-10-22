"""Трекер студенческих задач: Flask + SQLite + метрики Prometheus."""
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, jsonify, render_template, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

DB_PATH = os.environ.get("DB_PATH", "/data/todo.db")
VERSION = os.environ.get("APP_VERSION", "dev")

SOON = timedelta(hours=24)                      # «горит»: до дедлайна меньше суток
FAR = datetime.max.replace(tzinfo=timezone.utc)  # для задач без дедлайна
ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"                   # единый формат дедлайна в БД (UTC)
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
URGENCY = {"overdue": 0, "soon": 1, "normal": 2}

app = Flask(__name__)

# --- Метрики использования (считаются с момента запуска, в Grafana берём increase(...[1h])) ---
TASKS_ADDED = Counter("todo_tasks_added_total", "Сколько задач добавлено")
TASKS_DONE = Counter("todo_tasks_completed_total", "Сколько задач отмечено выполненными")
PAGE_VISITS = Counter("todo_page_visits_total", "Сколько раз открыли главную страницу")


# --- Работа с SQLite ---
def execute(sql, args=()):
    """Выполнить запись. Возвращает (lastrowid, rowcount)."""
    with closing(sqlite3.connect(DB_PATH, timeout=5)) as conn:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid, cur.rowcount


def fetch(sql, args=()):
    with closing(sqlite3.connect(DB_PATH, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, args).fetchall()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    execute(
        "CREATE TABLE IF NOT EXISTS tasks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "title TEXT NOT NULL, "
        "done INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "priority INTEGER NOT NULL DEFAULT 2, "   # 1 высокий, 2 средний, 3 низкий
        "deadline TEXT, "                          # UTC, формат ISO_FMT
        "color TEXT)"                              # #rrggbb или NULL (цвет по умолчанию)
    )
    # Миграция: база из прошлой версии (volume с данными) получает новые колонки
    existing = {row["name"] for row in fetch("PRAGMA table_info(tasks)")}
    for name, ddl in (("priority", "INTEGER NOT NULL DEFAULT 2"), ("deadline", "TEXT"), ("color", "TEXT")):
        if name not in existing:
            execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")


def db_ok():
    """Health check: база читается и каталог с ней доступен для записи."""
    try:
        fetch("SELECT 1")
        return os.access(os.path.dirname(DB_PATH) or ".", os.W_OK)
    except Exception:
        return False


def open_tasks():
    try:
        return fetch("SELECT COUNT(*) AS n FROM tasks WHERE done = 0")[0]["n"]
    except Exception:
        return float("nan")


# Gauge-метрики вычисляются в момент скрейпа
Gauge("todo_db_up", "1, если SQLite доступна").set_function(lambda: 1 if db_ok() else 0)
Gauge("todo_tasks_open", "Количество невыполненных задач").set_function(open_tasks)

init_db()


# --- Проверка входных данных ---
def parse_priority(value):
    try:
        priority = int(value)
    except (TypeError, ValueError):
        priority = 0
    if priority not in (1, 2, 3):
        raise ValueError("Приоритет: 1 (высокий), 2 (средний) или 3 (низкий)")
    return priority


def parse_deadline(value):
    if value in (None, ""):
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Дедлайн должен быть датой и временем в формате ISO 8601")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(ISO_FMT)


def parse_color(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not COLOR_RE.match(value):
        raise ValueError("Цвет должен быть в формате #rrggbb")
    return value.lower()


# --- Сортировка ---
def ordered_tasks(rows, now):
    """Сначала просроченные, затем со сроком < 24 ч, затем остальные; внутри группы —
    по приоритету, затем по дедлайну. Выполненные уходят вниз."""
    active, done = [], []
    for r in rows:
        deadline = datetime.strptime(r["deadline"], ISO_FMT).replace(tzinfo=timezone.utc) if r["deadline"] else None
        if r["done"]:
            status = "done"
        elif deadline and deadline <= now:
            status = "overdue"
        elif deadline and deadline - now <= SOON:
            status = "soon"
        else:
            status = "normal"
        task = {
            "id": r["id"], "title": r["title"], "done": r["done"], "priority": r["priority"],
            "deadline": r["deadline"], "color": r["color"], "status": status,
        }
        if status == "done":
            done.append(task)
        else:
            active.append((URGENCY[status], r["priority"], deadline or FAR, r["id"], task))
    active.sort(key=lambda item: item[:4])
    done.sort(key=lambda t: -t["id"])
    return [item[4] for item in active] + done


# --- Страница и API ---
@app.get("/")
def index():
    PAGE_VISITS.inc()
    return render_template("index.html")


@app.get("/api/tasks")
def list_tasks():
    rows = fetch("SELECT id, title, done, priority, deadline, color FROM tasks")
    return jsonify(ordered_tasks(rows, datetime.now(timezone.utc)))


@app.post("/api/tasks")
def add_task():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title or len(title) > 200:
        return jsonify(error="Название должно быть от 1 до 200 символов"), 400
    try:
        priority = parse_priority(data.get("priority", 2))
        deadline = parse_deadline(data.get("deadline"))
        color = parse_color(data.get("color"))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    task_id, _ = execute(
        "INSERT INTO tasks (title, priority, deadline, color) VALUES (?, ?, ?, ?)",
        (title, priority, deadline, color),
    )
    TASKS_ADDED.inc()
    return jsonify(id=task_id, title=title, done=0, priority=priority, deadline=deadline, color=color), 201


@app.patch("/api/tasks/<int:task_id>")
def update_task(task_id):
    data = request.get_json(silent=True) or {}
    rows = fetch("SELECT done FROM tasks WHERE id = ?", (task_id,))
    if not rows:
        return jsonify(error="Задача не найдена"), 404
    changes = {}
    try:
        if "done" in data:
            changes["done"] = 1 if data["done"] else 0
        if "priority" in data:
            changes["priority"] = parse_priority(data["priority"])
        if "deadline" in data:
            changes["deadline"] = parse_deadline(data["deadline"])
        if "color" in data:
            changes["color"] = parse_color(data["color"])
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if changes:
        assignments = ", ".join(f"{column} = ?" for column in changes)  # имена колонок только из кода выше
        execute(f"UPDATE tasks SET {assignments} WHERE id = ?", (*changes.values(), task_id))
    if changes.get("done") and not rows[0]["done"]:
        TASKS_DONE.inc()
    return jsonify(id=task_id, **changes)


@app.post("/api/tasks/reset-colors")
def reset_colors():
    _, count = execute("UPDATE tasks SET color = NULL")
    return jsonify(reset=count)


@app.delete("/api/tasks/<int:task_id>")
def delete_task(task_id):
    _, count = execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    if count == 0:
        return jsonify(error="Задача не найдена"), 404
    return "", 204


# --- Health и метрики ---
@app.get("/health")
def health():
    ok = db_ok()
    return jsonify(status="ok" if ok else "error", db=ok, version=VERSION), (200 if ok else 503)


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)
