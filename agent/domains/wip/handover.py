"""Shared shift notes for the current MES deployment."""
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


@contextmanager
def database(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS handovers (
                id INTEGER PRIMARY KEY, container_id TEXT NOT NULL, name TEXT NOT NULL,
                note TEXT NOT NULL, author TEXT NOT NULL,
                created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                request_id TEXT NOT NULL UNIQUE)""")
            db.execute("""CREATE TABLE IF NOT EXISTS wip_case_events (
                id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES handovers(id),
                action TEXT NOT NULL, status TEXT NOT NULL, assignee TEXT NOT NULL,
                actor TEXT NOT NULL, detail TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
                created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            db.execute("CREATE INDEX IF NOT EXISTS wip_case_history ON wip_case_events(case_id,id)")
            yield db


def handovers(path, container_id=None, name=None, note=None, author=None, request_id=None):
    with database(path) as db:
        if container_id is not None:
            db.execute("""INSERT INTO handovers(container_id,name,note,author,request_id)
                VALUES (?,?,?,?,?) ON CONFLICT(request_id) DO NOTHING""",
                (container_id, name, note, author, request_id))
            row = db.execute("SELECT id FROM handovers WHERE request_id=?", (request_id,)).fetchone()
            return f"交班记录 #{row[0]} · {name}\n\n{note}\n\n需要跟踪处理时发送 `在制品 立单 {row[0]}`。"
        rows = db.execute("""SELECT id,name,note,author,created FROM handovers
                            ORDER BY id DESC LIMIT 20""").fetchall()
        return "### 最近交班记录（最多 20 条）\n\n" + ("\n\n".join(
            f"**#{i} · {name}**　{date} UTC · {author}\n\n{note}"
            for i, name, note, author, date in rows) or "当前没有交班记录。")
