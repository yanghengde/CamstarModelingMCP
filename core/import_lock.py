"""Nonblocking OS locks shared by import workers on the same filesystem."""
from contextlib import contextmanager, ExitStack
import hashlib
import os
import unicodedata


@contextmanager
def workflow_locks(directory, workflows):
    directory.mkdir(parents=True, exist_ok=True)
    keys = sorted({hashlib.sha256((unicodedata.normalize("NFC", w["name"]).casefold() + "\0" +
                                  unicodedata.normalize("NFC", w["revision"]).casefold()).encode()).hexdigest()
                   for w in workflows})
    with ExitStack() as stack:
        for key in keys:
            handle = stack.enter_context(open(directory / (key + ".lock"), "a+b"))
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("该路线正在被另一导入任务处理；本次未领取计划，稍后重试或重新预览。") from exc
        yield
