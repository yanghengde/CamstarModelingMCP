"""Finish a disk transaction before propagating cancellation to its caller."""
import asyncio
from functools import partial


async def durable_io(function, *args, **kwargs):
    return await finish_before_cancel(asyncio.to_thread(partial(function, *args, **kwargs)))


async def finish_before_cancel(awaitable):
    task = asyncio.create_task(awaitable)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                result = task.result()
                break
    if cancelled:
        raise asyncio.CancelledError()
    return result
