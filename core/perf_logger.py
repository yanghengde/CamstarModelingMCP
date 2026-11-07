import os
import json
import time
from datetime import datetime
from config import ENABLE_PERFORMANCE_LOG, PERF_RETENTION_DAYS
from core.run_context import current_run_id
import sqlite3
import threading
import base64
import uuid
import hashlib
from contextlib import contextmanager

_LOCK = threading.RLock()

LOGS_DIR = os.path.join("data", "logs")
PERF_LOG_FILE = os.path.join(LOGS_DIR, "performance.jsonl")

# 确保目录存在
if ENABLE_PERFORMANCE_LOG:
    os.makedirs(LOGS_DIR, exist_ok=True)

def record_perf(action: str, duration_sec: float, username: str = "Unknown", session_id: str = "Unknown", details: dict = None):
    """
    记录一段程序的耗时到独立的纯文本行文件 (JSONL) 中
    """
    if not ENABLE_PERFORMANCE_LOG:
        return

    now = datetime.now()
    start_dt = datetime.fromtimestamp(now.timestamp() - duration_sec)
    
    end_time_str = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    start_time_str = start_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    record = {
        "timestamp": end_time_str,  # 保持原用以排序兼容的值
        "start_time": start_time_str,
        "end_time": end_time_str,
        "action": action,
        "duration_ms": round(duration_sec * 1000, 2),
        "username": username,
        "session_id": session_id,
        "details": {**(details or {}), **({"run_id": current_run_id.get()} if current_run_id.get() else {})}
    }
    
    # 追加到 JSONL 文件
    with _LOCK:
        os.makedirs(os.path.dirname(PERF_LOG_FILE) or ".", exist_ok=True)
        with open(PERF_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

@contextmanager
def _index():
    """Incrementally index appended lines; snapshots refer to indexed sequence IDs."""
    with _LOCK:
        os.makedirs(os.path.dirname(PERF_LOG_FILE) or '.', exist_ok=True)
        db = sqlite3.connect(str(PERF_LOG_FILE) + '.sqlite', timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY, offset INTEGER, mtime INTEGER, generation TEXT);
                CREATE TABLE IF NOT EXISTS logs (seq INTEGER PRIMARY KEY, group_key TEXT, timestamp TEXT, payload TEXT);
                CREATE INDEX IF NOT EXISTS logs_group ON logs(group_key, seq);
                CREATE TABLE IF NOT EXISTS versions (seq INTEGER PRIMARY KEY, group_key TEXT, timestamp TEXT, total REAL);
                CREATE INDEX IF NOT EXISTS versions_group ON versions(group_key, seq);
                CREATE INDEX IF NOT EXISTS versions_page ON versions(timestamp DESC, group_key DESC, seq DESC);
            """)
            meta = db.execute('SELECT * FROM meta WHERE id=1').fetchone()
            stat = os.stat(PERF_LOG_FILE) if os.path.exists(PERF_LOG_FILE) else None
            size, mtime = (stat.st_size, stat.st_mtime_ns) if stat else (0, 0)
            if meta is None or size < meta['offset'] or (size == meta['offset'] and mtime != meta['mtime']):
                db.execute('DELETE FROM logs')
                db.execute('DELETE FROM versions')
                db.execute('INSERT OR REPLACE INTO meta VALUES(1,0,?,?)', (mtime, uuid.uuid4().hex))
                meta = db.execute('SELECT * FROM meta WHERE id=1').fetchone()
            offset = meta['offset']
            if stat and size > offset:
                with open(PERF_LOG_FILE, 'rb') as stream:
                    stream.seek(offset)
                    while stream.tell() < size:
                        line = stream.readline()
                        if not line.endswith(b'\n'):
                            break  # Do not consume an in-flight append.
                        offset = stream.tell()
                        try:
                            log = json.loads(line)
                            details = log.get('details') or {}
                            key = json.dumps(['run', details['run_id']] if details.get('run_id') else
                                             ['legacy', log.get('username'), log.get('session_id'), details.get('prompt')], ensure_ascii=False)
                            key = hashlib.sha256(key.encode('utf-8')).hexdigest()
                            timestamp, duration = log['timestamp'], float(log['duration_ms'])
                            previous = db.execute('SELECT timestamp,total FROM versions WHERE group_key=? ORDER BY seq DESC LIMIT 1', (key,)).fetchone()
                            # Run_Total is an end-to-end span, not another additive step.
                            if log.get('action') == 'Run_Total':
                                total = duration
                            else:
                                total = (previous['total'] if previous else 0) + duration
                            group_time = max(previous['timestamp'], timestamp) if previous else timestamp
                            seq = db.execute('INSERT INTO logs(group_key,timestamp,payload) VALUES(?,?,?)',
                                             (key, timestamp, json.dumps(log, ensure_ascii=False))).lastrowid
                            db.execute('INSERT INTO versions VALUES(?,?,?,?)', (seq, key, group_time, total))
                        except (ValueError, KeyError, TypeError, AttributeError):
                            continue
                db.execute('UPDATE meta SET offset=?,mtime=? WHERE id=1', (offset, mtime))
            db.commit()
            yield db, meta['generation']
        finally:
            db.close()


def get_perf_logs(limit: int = 500) -> list:
    with _index() as (db, _):
        return [json.loads(row[0]) for row in db.execute('SELECT payload FROM logs ORDER BY seq DESC LIMIT ?', (max(0, limit),))]


def get_perf_log_groups(limit: int = 20, cursor: str | None = None) -> dict:
    """Indexed keyset pages of complete groups at a stable sequence snapshot."""
    decoded = None
    if cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor).decode('utf-8'))
            generation, snapshot, timestamp, key = decoded
            if not isinstance(generation, str) or type(snapshot) is not int or snapshot < 0 or not isinstance(timestamp, str) or not isinstance(key, str):
                raise ValueError()
        except Exception as exc:
            raise ValueError('无效的日志分页游标') from exc
    with _index() as (db, generation):
        if decoded and decoded[0] != generation:
            raise ValueError('日志分页游标已过期，请重新加载第一页')
        snapshot = decoded[1] if decoded else db.execute('SELECT COALESCE(MAX(seq),0) FROM logs').fetchone()[0]
        params = [snapshot, snapshot]
        predicate = ''
        if decoded:
            predicate = ' AND (v.timestamp,v.group_key) < (?,?)'
            params.extend(decoded[2:])
        params.append(limit + 1)
        rows = db.execute("""SELECT v.* FROM versions v WHERE v.seq<=?
            AND NOT EXISTS (SELECT 1 FROM versions newer WHERE newer.group_key=v.group_key AND newer.seq>v.seq AND newer.seq<=?)
            """ + predicate + ' ORDER BY v.timestamp DESC,v.group_key DESC LIMIT ?', params).fetchall()
        groups = []
        for row in rows[:limit]:
            logs = [json.loads(item[0]) for item in db.execute(
                'SELECT payload FROM logs WHERE group_key=? AND seq<=? ORDER BY timestamp DESC,seq DESC', (row['group_key'], snapshot))]
            first = logs[0]
            groups.append(dict(id=row['group_key'], prompt=first.get('details', {}).get('prompt') or '未知背景对话',
                               sessionId=first.get('session_id'), username=first.get('username'),
                               runId=first.get('details', {}).get('run_id'), timestamp=row['timestamp'],
                               totalTimeMs=row['total'], steps=logs))
        more = len(rows) > limit
        next_cursor = base64.urlsafe_b64encode(json.dumps([generation, snapshot, rows[limit-1]['timestamp'], rows[limit-1]['group_key']]).encode()).decode() if more else None
        return {'groups': groups, 'next_cursor': next_cursor, 'has_more': more}


def prune_perf_logs(days=PERF_RETENTION_DAYS):
    """Daily compaction of complete old groups; active traces are retained.

    Invalidates existing pagination cursors; never runs on the request loop.
    JSONL remains exportable and the rebuildable SQLite index is kept in sync.
    """
    if not days or not os.path.exists(PERF_LOG_FILE):
        return 0
    cutoff = datetime.fromtimestamp(time.time()-days*86400).strftime('%Y-%m-%d %H:%M:%S')
    with _LOCK:
        with _index() as (db, _):
            # Only terminal modern traces or historical legacy groups expire.
            old = {row[0] for row in db.execute("""SELECT v.group_key FROM versions v
                WHERE v.timestamp<? AND NOT EXISTS (SELECT 1 FROM versions n WHERE n.group_key=v.group_key AND n.seq>v.seq)
                AND (EXISTS (SELECT 1 FROM logs l WHERE l.group_key=v.group_key AND json_extract(l.payload,'$.details.run_id') IS NULL) OR EXISTS
                    (SELECT 1 FROM logs l WHERE l.group_key=v.group_key AND json_extract(l.payload,'$.action')='Run_Total'))""", (cutoff,))}
            if not old:
                return 0
            temp = str(PERF_LOG_FILE) + '.compact-' + uuid.uuid4().hex
            try:
                with open(temp, 'w', encoding='utf-8') as output:
                    for row in db.execute('SELECT group_key,payload FROM logs ORDER BY seq'):
                        if row['group_key'] not in old:
                            output.write(row['payload'] + '\n')
                os.replace(temp, PERF_LOG_FILE)
                db.execute('DELETE FROM meta')
                db.commit()
            finally:
                if os.path.exists(temp):
                    os.remove(temp)
        with _index():
            pass
        return len(old)
