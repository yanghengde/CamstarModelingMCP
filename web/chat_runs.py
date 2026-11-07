"""Server-owned chat runs; browser subscriptions never own worker lifetime.

SQLite stores the event journal and request identity, not executable jobs.
After a server restart unfinished runs become interrupted and are never replayed.
The supported deployment is the existing single Uvicorn worker.
"""
import asyncio
import json
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from config import (CHAT_RUN_DB, CHAT_MAX_RUNNING, CHAT_MAX_RUNNING_PER_USER,
                    CHAT_MAX_QUEUED, CHAT_MAX_QUEUED_PER_USER, CHAT_QUEUE_TIMEOUT,
                    CHAT_EVENT_RETENTION_DAYS)
from core.async_io import durable_io, finish_before_cancel
from core.run_context import current_run_id
from core.perf_logger import record_perf
from collections import Counter, deque
import threading

ACTIVE = {"queued", "running", "cancelling"}


class RunConflict(ValueError):
    def __init__(self, run):
        self.run = run
        super().__init__("此对话已有后台任务，请查看进度或先停止任务。")


class QueueFull(ValueError):
    pass


class ChatRuns:
    def __init__(self, path, *, max_running=CHAT_MAX_RUNNING, per_user=CHAT_MAX_RUNNING_PER_USER,
                 max_queued=CHAT_MAX_QUEUED, queued_per_user=CHAT_MAX_QUEUED_PER_USER,
                 queue_timeout=CHAT_QUEUE_TIMEOUT):
        self.path = Path(path)
        self.tasks = {}
        self.ready = False
        self.stopping = False
        self.max_running, self.per_user = max_running, per_user
        self.max_queued, self.queued_per_user = max_queued, queued_per_user
        self.queue_timeout = queue_timeout
        self._admission = asyncio.Lock()
        self._init_lock = threading.RLock()
        self._waiting = {}
        self._running = Counter()
        self._slots = asyncio.Event()
        self._notifications = {}

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
        with self._init_lock:
            self._initialize()

    def _initialize(self):
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
                CREATE INDEX IF NOT EXISTS runs_by_session_started
                    ON runs(username, session_id, started_at DESC);
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
            self._notify(run_id)
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
            rows = db.execute("""SELECT r.session_id,r.id,r.status FROM runs r WHERE r.username=?
                AND NOT EXISTS (SELECT 1 FROM runs n WHERE n.username=r.username AND n.session_id=r.session_id
                    AND (n.started_at>r.started_at OR (n.started_at=r.started_at AND n.rowid>r.rowid)))""",
                              (username,)).fetchall()
        return {row['session_id']: {"id": row['id'], "status": row['status']} for row in rows}

    def _admit(self, username, session_id, message, request_id, history_start):
        self.initialize()
        if self.stopping:
            raise ValueError("服务正在关闭，暂不接受新任务。")
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute("SELECT * FROM runs WHERE username=? AND request_id=?", (username, request_id)).fetchone()
            if previous:
                if previous['session_id'] != session_id or previous['message'] != message:
                    raise ValueError("request_id 已用于另一请求；不能更换参数重用。")
                return dict(previous), False
            active = db.execute("SELECT * FROM runs WHERE username=? AND session_id=? AND status IN ('queued','running','cancelling')",
                                (username, session_id)).fetchone()
            if active:
                raise RunConflict(dict(active))
            counts = db.execute("SELECT username,COUNT(*) AS n FROM runs WHERE status IN ('queued','running','cancelling') GROUP BY username").fetchall()
            projected = {row['username']: row['n'] for row in counts}
            projected[username] = projected.get(username, 0) + 1
            runnable = min(self.max_running, sum(min(self.per_user, n) for n in projected.values()))
            if (sum(projected.values()) - runnable > self.max_queued
                    or projected[username] > self.per_user + self.queued_per_user):
                raise QueueFull('后台任务队列已满，请等待已有任务完成后重试。')
            run_id = uuid.uuid4().hex
            accepted_perf = time.perf_counter()
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,'queued',?,NULL)",
                       (run_id, username, session_id, request_id, message, history_start, time.time()))
            self._append(db, run_id, {"type": "run_state", "status": "queued"})
            row = dict(db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())
            row['_accepted_perf'] = accepted_perf
        return row, True

    def _launch(self, row, created, stream, workflow):
        if created:
            run_id, username = row['id'], row['username']
            self._waiting[run_id] = username
            self.tasks[run_id] = asyncio.create_task(self._work(
                run_id, username, row['session_id'], row['message'], stream, workflow,
                row.pop('_accepted_perf')))
        return row

    def start(self, username, session_id, message, request_id, history_start, stream, workflow):
        # Compatibility for in-process callers; HTTP uses astart().
        row, created = self._admit(username, session_id, message, request_id, history_start)
        return self._launch(row, created, stream, workflow)

    async def astart(self, username, session_id, message, request_id, history_start, stream, workflow):
        async def accept():
            async with self._admission:
                row, created = await asyncio.to_thread(self._admit, username, session_id, message, request_id, history_start)
                return self._launch(row, created, stream, workflow)
        # A disconnected POST cannot orphan a committed queued row.
        return await finish_before_cancel(accept())

    def _notify(self, run_id):
        event = self._notifications.pop(run_id, None)
        if event is not None:
            event.set()

    def _wake_slots(self):
        self._slots.set()
        self._slots = asyncio.Event()

    async def _acquire(self, run_id, username):
        async def wait():
            while True:
                event = self._slots
                first = next((rid for rid, user in self._waiting.items()
                              if self._running[user] < self.per_user), None)
                if sum(self._running.values()) < self.max_running and first == run_id:
                    self._waiting.pop(run_id, None)
                    self._running[username] += 1
                    self._wake_slots()
                    return
                await event.wait()
        async with asyncio.timeout(self.queue_timeout):
            await wait()

    async def astate(self, run_id, status, message=None):
        try:
            await durable_io(self.state, run_id, status, message)
        finally:
            self._notify(run_id)

    def state(self, run_id, status, message=None):
        payload = {"type": "run_state", "status": status}
        if message:
            payload['message'] = message
        with self.db() as db:
            db.execute("UPDATE runs SET status=?,finished_at=? WHERE id=?",
                       (status, None if status in ACTIVE else time.time(), run_id))
            self._append(db, run_id, payload)

    async def _work(self, run_id, username, session_id, message, stream, workflow, accepted_perf=None):
        token = current_run_id.set(run_id)
        started = accepted_perf if accepted_perf is not None else time.perf_counter()
        acquired = False
        queue_seconds = None
        status = 'failed'
        lag = deque(maxlen=1024)
        max_lag, samples = 0., 0
        async def monitor():
            nonlocal max_lag, samples
            while True:
                before = time.perf_counter()
                await asyncio.sleep(.1)
                delay = max(0., time.perf_counter() - before - .1)
                lag.append(delay)
                max_lag = max(max_lag, delay)
                samples += 1
        monitor_task = asyncio.create_task(monitor())
        try:
            await self._acquire(run_id, username)
            acquired = True
            queue_seconds = time.perf_counter() - started
            await self.astate(run_id, 'running')
            error = await self._record_stream(run_id, stream(username, message, session_id))
            info = await workflow(username, session_id)
            status = {'approval_required': 'approval_required', 'limited': 'limited',
                      'context_limited': 'context_limited',
                      'cancelled': 'cancelled', 'incomplete': 'incomplete'}.get(info.get('status'), 'completed')
            if error:
                status = 'failed'
            await self.astate(run_id, status)
        except asyncio.TimeoutError:
            status = 'failed'
            await self.astate(run_id, status, '执行超时，请核实已提交操作，不会自动重试。' if acquired
                              else '排队超时，未开始执行，请稍后重新提交。')
        except asyncio.CancelledError:
            status = 'interrupted' if self.stopping else 'cancelled'
            await self.astate(run_id, status, "执行已停止；已提交的操作不会撤销，请核实实际结果后继续。")
        except Exception as exc:
            status = 'failed'
            await durable_io(self.append, run_id, {"type": "error", "message": f"后台任务异常：{type(exc).__name__}: {exc}"})
            await self.astate(run_id, status)
        finally:
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)
            elapsed = time.perf_counter() - started
            self._waiting.pop(run_id, None)
            if acquired:
                self._running[username] -= 1
                if not self._running[username]:
                    del self._running[username]
            self._wake_slots()
            self._notify(run_id)
            ordered = sorted(lag)
            try:
                await durable_io(record_perf, 'Run_Total', elapsed, username, session_id, {
                    'prompt': message, 'status': status, 'queue_seconds': queue_seconds if acquired else elapsed,
                    'execution_seconds': max(0., elapsed - queue_seconds) if acquired else 0.,
                    'event_loop_lag_samples': len(ordered),
                    'event_loop_lag_total_samples': samples,
                    'event_loop_lag_p95_ms': ordered[min(len(ordered)-1, int(len(ordered)*.95))]*1000 if ordered else None,
                    'event_loop_lag_max_ms': max_lag*1000 if ordered else None})
            except Exception:
                logging.getLogger(__name__).exception('Could not record run metrics')
            finally:
                self.tasks.pop(run_id, None)
                current_run_id.reset(token)

    def _event_page(self, username, run_id, after):
        """Read a bounded journal page using one connection off the event loop."""
        self.initialize()
        with self.db() as db:
            run = db.execute("SELECT status FROM runs WHERE username=? AND id=?",
                             (username, run_id)).fetchone()
            if run is None:
                return None, []
            rows = db.execute("SELECT seq,payload FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 200",
                              (run_id, after)).fetchall()
        return run['status'], rows

    async def events(self, username, run_id, after=0):
        try:
            async for frame in self._events(username, run_id, after):
                yield frame
        finally:
            # Wake other subscribers before removing their shared event.
            self._notify(run_id)

    async def _events(self, username, run_id, after=0):
        # Closing this generator only detaches a browser, never cancels _work.
        heartbeat = time.monotonic()
        while True:
            notification = self._notifications.setdefault(run_id, asyncio.Event())
            status, rows = await asyncio.to_thread(self._event_page, username, run_id, after)
            if status is None:
                return
            for row in rows:
                after = row['seq']
                yield f"id: {after}\ndata: {row['payload']}\n\n"
            if status not in ACTIVE and not rows:
                return
            if not rows:
                if time.monotonic() - heartbeat >= 15:
                    yield ': heartbeat\n\n'
                    heartbeat = time.monotonic()
                try:
                    await asyncio.wait_for(notification.wait(), 15)
                except asyncio.TimeoutError:
                    pass

    def cancel(self, username, run_id):
        run = self.get(username, run_id)
        if not run:
            return None
        task = self.tasks.get(run_id)
        if run['status'] in ACTIVE and task and not task.cancelling():
            self.state(run_id, 'cancelling', '正在停止执行；已提交的操作不会撤销。')
            # Schedule cancellation after the worker has entered its try/finally,
            # including when cancel arrives immediately after task creation.
            asyncio.get_running_loop().call_soon(task.cancel)
        return self.get(username, run_id)

    async def acancel(self, username, run_id):
        run = await asyncio.to_thread(self.get, username, run_id)
        if not run:
            return None
        task = self.tasks.get(run_id)
        if run['status'] in ACTIVE and task and not task.done() and not task.cancelling():
            # Cancel first; the worker owns its final state, so a late cancel
            # cannot overwrite an already completed run with "cancelling".
            task.cancel()
        return await asyncio.to_thread(self.get, username, run_id)

    def prune_events(self, days=CHAT_EVENT_RETENTION_DAYS):
        if not days:
            return 0
        with self.db() as db:
            # Keep request identities forever (idempotency tombstones), and
            # keep pending approvals/interrupted or incomplete recovery evidence.
            cursor = db.execute("DELETE FROM events WHERE run_id IN (SELECT id FROM runs WHERE status IN ('completed','cancelled','failed') AND finished_at<?)",
                                (time.time()-days*86400,))
            return cursor.rowcount

    async def shutdown(self):
        self.stopping = True
        async with self._admission:
            tasks = list(self.tasks.values())
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await durable_io(self._interrupt_remaining)
        self.tasks.clear()
        self._waiting.clear()
        self._running.clear()
        self._wake_slots()
        for run_id in list(self._notifications):
            self._notify(run_id)
        self.ready = False
        self.stopping = False

    def _interrupt_remaining(self):
        with self.db() as db:
            rows = db.execute("SELECT id FROM runs WHERE status IN ('queued','running','cancelling')").fetchall()
            for row in rows:
                db.execute("UPDATE runs SET status='interrupted',finished_at=? WHERE id=?", (time.time(), row['id']))
                self._append(db, row['id'], {"type": "run_state", "status": "interrupted",
                                           "message": "服务已停止；请核实上次执行结果，不会自动重放。"})



RUNS = ChatRuns(CHAT_RUN_DB)
