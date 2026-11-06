"""Server-owned chat runs; browser subscriptions never own worker lifetime.

SQLite stores the event journal and request identity, not executable jobs.
After a server restart unfinished runs become interrupted and are never replayed.
The supported deployment is the existing single Uvicorn worker.
"""
import asyncio
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from config import CHAT_RUN_DB

ACTIVE = {"queued", "running", "cancelling"}


class RunConflict(ValueError):
    def __init__(self, run):
        self.run = run
        super().__init__("此对话已有后台任务，请查看进度或先停止任务。")


class ChatRuns:
    def __init__(self, path):
        self.path = Path(path)
        self.tasks = {}
        self.ready = False
        self.stopping = False

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        if self.ready:
            return
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript('''
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL, session_id TEXT NOT NULL,
                    request_id TEXT NOT NULL, message TEXT NOT NULL, history_start INTEGER NOT NULL,
                    status TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,
                    UNIQUE(username, request_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(run_id, seq)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_chat_run
                    ON runs(username, session_id) WHERE status IN ('queued','running','cancelling');
            ''')
            stale = db.execute("SELECT id FROM runs WHERE status IN ('queued','running','cancelling')").fetchall()
            for row in stale:
                db.execute("UPDATE runs SET status='interrupted',finished_at=? WHERE id=?", (time.time(), row['id']))
                self._append(db, row['id'], {"type": "run_state", "status": "interrupted",
                    "message": "服务已重启，上次执行已中断；请核实已提交操作，不会自动重放。"})
        self.ready = True

    @staticmethod
    def _append(db, run_id, payload):
        seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
        db.execute("INSERT INTO events VALUES(?,?,?)", (run_id, seq, json.dumps(payload, ensure_ascii=False)))

    def append(self, run_id, payload):
        self.append_many(run_id, [payload])

    def append_many(self, run_id, payloads):
        # Tool arguments are already kept by the execution/checkpoint layer.
        # The reconnect journal only needs the public step name.
        with self.db() as db:
            for payload in payloads:
                if payload.get('type') == 'step':
                    payload = {"type": "step", "func": payload.get('func', '')}
                self._append(db, run_id, payload)

    async def _record_stream(self, run_id, source):
        """Journal bursts off the event loop, with at most 50 ms batching delay.

        Keep a single pending read across flush timeouts: cancelling a read would
        cancel the underlying LLM/tool execution. Finish writes before terminal
        state, including explicit cancellation, so reconnect cursors stay ordered.
        """
        pending, buffer, error = [], '', False
        read = None
        last_flush = time.monotonic()

        async def flush():
            nonlocal last_flush
            if not pending:
                return
            batch = pending[:]
            pending.clear()
            write = asyncio.create_task(asyncio.to_thread(self.append_many, run_id, batch))
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                await write
                raise
            last_flush = time.monotonic()

        try:
            while True:
                if read is None:
                    read = asyncio.create_task(anext(source))
                timeout = max(0, .05 - (time.monotonic() - last_flush)) if pending else None
                done, _ = await asyncio.wait([read], timeout=timeout)
                if not done:
                    await flush()
                    continue
                completed, read = read, None
                try:
                    chunk = completed.result()
                except StopAsyncIteration:
                    break
                buffer += chunk.replace('\r\n', '\n')
                while '\n\n' in buffer:
                    frame, buffer = buffer.split('\n\n', 1)
                    raw = '\n'.join(line[5:].lstrip() for line in frame.splitlines() if line.startswith('data:'))
                    if raw:
                        payload = json.loads(raw)
                        error = error or payload.get('type') == 'error'
                        pending.append(payload)
                if len(pending) >= 100 or time.monotonic() - last_flush >= .05:
                    await flush()
            if buffer.strip():
                raise ValueError('聊天进度流未完整结束。')
            return error
        finally:
            if read is not None:
                read.cancel()
                await asyncio.gather(read, return_exceptions=True)
            try:
                await source.aclose()
            finally:
                await flush()

    def get(self, username, run_id):
        self.initialize()
        with self.db() as db:
            row = db.execute("SELECT * FROM runs WHERE username=? AND id=?", (username, run_id)).fetchone()
        return dict(row) if row else None

    def latest(self, username, session_id):
        self.initialize()
        with self.db() as db:
            row = db.execute("SELECT * FROM runs WHERE username=? AND session_id=? ORDER BY started_at DESC,rowid DESC LIMIT 1",
                             (username, session_id)).fetchone()
        return dict(row) if row else None

    def session_statuses(self, username):
        self.initialize()
        with self.db() as db:
            rows = db.execute("SELECT session_id,id,status FROM runs WHERE username=? ORDER BY started_at,rowid", (username,)).fetchall()
        return {row['session_id']: {"id": row['id'], "status": row['status']} for row in rows}

    def start(self, username, session_id, message, request_id, history_start, stream, workflow):
        self.initialize()
        if self.stopping:
            raise ValueError("服务正在关闭，暂不接受新任务。")
        with self.db() as db:
            previous = db.execute("SELECT * FROM runs WHERE username=? AND request_id=?", (username, request_id)).fetchone()
            if previous:
                if previous['session_id'] != session_id or previous['message'] != message:
                    raise ValueError("request_id 已用于另一请求；不能更换参数重用。")
                return dict(previous)
            active = db.execute("SELECT * FROM runs WHERE username=? AND session_id=? AND status IN ('queued','running','cancelling')",
                                (username, session_id)).fetchone()
            if active:
                raise RunConflict(dict(active))
            run_id = uuid.uuid4().hex
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,'queued',?,NULL)",
                       (run_id, username, session_id, request_id, message, history_start, time.time()))
            self._append(db, run_id, {"type": "run_state", "status": "queued"})
        self.tasks[run_id] = asyncio.create_task(self._work(run_id, username, session_id, message, stream, workflow))
        return self.get(username, run_id)

    def state(self, run_id, status, message=None):
        payload = {"type": "run_state", "status": status}
        if message:
            payload['message'] = message
        with self.db() as db:
            db.execute("UPDATE runs SET status=?,finished_at=? WHERE id=?",
                       (status, None if status in ACTIVE else time.time(), run_id))
            self._append(db, run_id, payload)

    async def _work(self, run_id, username, session_id, message, stream, workflow):
        try:
            self.state(run_id, 'running')
            error = await self._record_stream(run_id, stream(username, message, session_id))
            info = await workflow(username, session_id)
            status = {'approval_required': 'approval_required', 'limited': 'limited',
                      'cancelled': 'cancelled', 'incomplete': 'incomplete'}.get(info.get('status'), 'completed')
            self.state(run_id, 'failed' if error else status)
        except asyncio.CancelledError:
            status = 'interrupted' if self.stopping else 'cancelled'
            self.state(run_id, status, "执行已停止；已提交的操作不会撤销，请核实实际结果后继续。")
        except Exception as exc:
            self.append(run_id, {"type": "error", "message": f"后台任务异常：{type(exc).__name__}: {exc}"})
            self.state(run_id, 'failed')
        finally:
            self.tasks.pop(run_id, None)

    async def events(self, username, run_id, after=0):
        # Closing this generator only detaches a browser, never cancels _work.
        heartbeat = time.monotonic()
        while True:
            run = self.get(username, run_id)
            if not run:
                return
            with self.db() as db:
                rows = db.execute("SELECT seq,payload FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 200",
                                  (run_id, after)).fetchall()
            for row in rows:
                after = row['seq']
                yield f"id: {after}\ndata: {row['payload']}\n\n"
            if run['status'] not in ACTIVE and not rows:
                return
            if not rows:
                if time.monotonic() - heartbeat >= 15:
                    yield ': heartbeat\n\n'
                    heartbeat = time.monotonic()
                await asyncio.sleep(.25)

    def cancel(self, username, run_id):
        run = self.get(username, run_id)
        if not run:
            return None
        task = self.tasks.get(run_id)
        if run['status'] in ACTIVE and task:
            self.state(run_id, 'cancelling', '正在停止执行；已提交的操作不会撤销。')
            # Schedule cancellation after the worker has entered its try/finally,
            # including when cancel arrives immediately after task creation.
            asyncio.get_running_loop().call_soon(task.cancel)
        return self.get(username, run_id)

    async def shutdown(self):
        self.stopping = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # A queued task can be cancelled before executing its first instruction.
        with self.db() as db:
            rows = db.execute("SELECT id FROM runs WHERE status IN ('queued','running','cancelling')").fetchall()
            for row in rows:
                db.execute("UPDATE runs SET status='interrupted',finished_at=? WHERE id=?", (time.time(), row['id']))
                self._append(db, row['id'], {"type": "run_state", "status": "interrupted",
                                           "message": "服务已停止；请核实上次执行结果，不会自动重放。"})
        self.tasks.clear()
        self.ready = False
        self.stopping = False


RUNS = ChatRuns(CHAT_RUN_DB)
