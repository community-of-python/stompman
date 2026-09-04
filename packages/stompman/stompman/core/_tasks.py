import asyncio
from typing import TypeVar

Result = TypeVar("Result")


async def await_cleanup(task: asyncio.Task[Result]) -> Result:
    """Finish an owned cleanup task before propagating caller cancellation."""
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            # wait() leaves the owned task running when this waiter is cancelled.
            # Unlike shield() on Python 3.14, it does not also log a task failure
            # that we explicitly retrieve and propagate below.
            await asyncio.wait({task})
        except asyncio.CancelledError as error:
            if task.cancelled():
                raise
            cancellation = error
    if cancellation is not None:
        try:
            task.result()
        except BaseException as error:
            raise cancellation from error
        raise cancellation
    return task.result()
