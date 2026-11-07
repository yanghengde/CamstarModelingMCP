"""Retain authoritative conversations/plans; expire only configured auxiliary data."""
import asyncio
import logging
from pathlib import Path
import re
import time

from config import UPLOAD_RETENTION_DAYS, PERF_RETENTION_DAYS
from core.perf_logger import prune_perf_logs
from core.async_io import durable_io


def prune_uploads(runs, directory='data/uploads', days=UPLOAD_RETENTION_DAYS):
    if not days:
        return 0
    root = Path(directory).resolve()
    if not root.exists():
        return 0
    with runs.db() as db:
        protected = [row[0] for row in db.execute(
            "SELECT message FROM runs WHERE status NOT IN ('completed','cancelled','failed')")]
    removed = 0
    for path in root.iterdir():
        if (path.is_symlink() or not path.is_file() or path.resolve().parent != root
                or not re.match(r'^[0-9a-f]{32}_', path.name)
                or any(path.name in message for message in protected)):
            continue
        if path.stat().st_mtime < time.time() - days*86400:
            path.unlink()
            removed += 1
    return removed


def maintain(runs):
    from agent.experience import prune_events
    return {'perf_groups': prune_perf_logs(), 'chat_events': runs.prune_events(),
            'uploads': prune_uploads(runs), 'experience_events': prune_events(PERF_RETENTION_DAYS)}


async def maintenance_loop(runs):
    while True:
        try:
            await durable_io(maintain, runs)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).exception('Auxiliary data retention failed')
        await asyncio.sleep(86400)
