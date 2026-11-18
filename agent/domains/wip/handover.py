"""Shared shift notes for the current MES deployment."""
import sqlite3
from pathlib import Path


def handovers(path, container_id=None, name=None, note=None, author=None, request_id=None):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    try:
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS handovers (
                id INTEGER PRIMARY KEY, container_id TEXT NOT NULL, name TEXT NOT NULL,
                note TEXT NOT NULL, author TEXT NOT NULL,
                created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                request_id TEXT NOT NULL UNIQUE)""")
            if container_id is not None:
                db.execute("""INSERT INTO handovers(container_id,name,note,author,request_id)
                    VALUES (?,?,?,?,?) ON CONFLICT(request_id) DO NOTHING""",
                    (container_id, name, note, author, request_id))
                row = db.execute("SELECT id FROM handovers WHERE request_id=?", (request_id,)).fetchone()
                return f"交班记录 #{row[0]} · {name}\n\n{note}\n\n本记录可在本服务的「在制品 交班」中查看。"
            rows = db.execute("""SELECT id,name,note,author,created FROM handovers
                                ORDER BY id DESC LIMIT 20""").fetchall()
            return "### 最近交班记录（最多 20 条）\n\n" + ("\n\n".join(
                f"**#{i} · {name}**　{date} UTC · {author}\n\n{note}"
                for i, name, note, author, date in rows) or "当前没有交班记录。")
    finally:
        db.close()
