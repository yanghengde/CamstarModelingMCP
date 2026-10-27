"""Nonblocking process locks for attempts stored in one local SQLite database.

Lock files must remain in place: unlinking one can create two independently
locked files. OS locks are released when a process exits, including crashes.
"""
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path


class AttemptBusy(RuntimeError):
    pass


@contextmanager
def attempt_lock(database, plan_id):
    root = Path(database).resolve()
    directory = root.parent / (root.name + '.attempt-locks')
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(plan_id.encode('utf-8')).hexdigest()
    with (directory / key).open('a+b') as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'\0')
            handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            acquire = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as error:
            raise AttemptBusy('此计划仍有本地执行或恢复任务占用，不能恢复或重复提交。') from error
        try:
            yield
        finally:
            handle.seek(0)
            release()
