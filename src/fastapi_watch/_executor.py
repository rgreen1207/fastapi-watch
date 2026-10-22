"""A small, private thread pool for the blocking calls fastapi-watch's probes make.

Some checks can only be made with blocking calls (Celery's inspect API, the
TCP probe's DNS resolution).  They must not run in the event loop's
default executor: the application's own ``run_in_executor`` calls share it,
and a check that times out keeps its thread busy after the registry has given
up on it, so a hung broker or DNS server would slowly take the app's threads.

Instead they run here: at most :data:`_MAX_WORKERS` threads, created lazily and
shared by the whole process.  :class:`SingleFlight` additionally limits each
caller to one call in flight, so a dependency that hangs holds at most one
thread per probe rather than one per poll.
"""
import asyncio
import concurrent.futures
import queue
import threading
from typing import Any, Callable

_MAX_WORKERS = 4
_THREAD_NAME_PREFIX = "fastapi-watch"


class _DaemonThreadPool(concurrent.futures.Executor):
    """A bounded thread pool whose workers are daemon threads.

    It behaves like ``ThreadPoolExecutor(max_workers, thread_name_prefix)``,
    which cannot be used directly: its workers are joined at interpreter exit,
    so one call stuck on an unreachable host would keep the process from
    exiting.  Daemon workers are simply abandoned at exit instead.
    """

    def __init__(self, max_workers: int, thread_name_prefix: str) -> None:
        self._max_workers = max_workers
        self._thread_name_prefix = thread_name_prefix
        self._work: queue.SimpleQueue = queue.SimpleQueue()
        # Released by a worker each time it finishes an item and goes idle.
        self._idle = threading.Semaphore(0)
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._shutdown = False

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> concurrent.futures.Future:
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            future: concurrent.futures.Future = concurrent.futures.Future()
            self._work.put((future, fn, args, kwargs))
            # Hand the item to an idle worker if there is one; otherwise start
            # another worker, up to the cap (beyond it, the item waits its turn).
            if not self._idle.acquire(blocking=False) and len(self._threads) < self._max_workers:
                thread = threading.Thread(
                    target=self._worker,
                    name=f"{self._thread_name_prefix}_{len(self._threads)}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)
        return future

    def _worker(self) -> None:
        while True:
            item = self._work.get()
            if item is None:
                self._work.put(None)  # pass the shutdown signal on to the next worker
                return
            self._run(*item)
            del item  # do not keep the finished call's arguments alive while idle
            self._idle.release()

    @staticmethod
    def _run(future: concurrent.futures.Future, fn: Callable[..., Any], args: tuple, kwargs: dict) -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._lock:
            self._shutdown = True
            threads = list(self._threads)
        if cancel_futures:
            while True:
                try:
                    item = self._work.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    item[0].cancel()
        self._work.put(None)
        if wait:
            for thread in threads:
                thread.join()


_pool: _DaemonThreadPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> _DaemonThreadPool:
    """Return the process-wide pool, creating it on first use."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _DaemonThreadPool(_MAX_WORKERS, _THREAD_NAME_PREFIX)
    return _pool


_render_pool: _DaemonThreadPool | None = None


async def run_render(fn: Callable[..., Any], *args: Any) -> Any:
    """Run CPU-bound page rendering in its own single thread and await the result.

    Not in the probe pool: a DNS outage can hold every probe thread at once
    (one per TCP probe) for tens of seconds, and the dashboard would wait
    behind them.  One thread suffices, as renders are serialized anyway.
    """
    global _render_pool
    if _render_pool is None:
        with _pool_lock:
            if _render_pool is None:
                _render_pool = _DaemonThreadPool(1, f"{_THREAD_NAME_PREFIX}-render")
    return await asyncio.get_running_loop().run_in_executor(_render_pool, fn, *args)


async def run_blocking(fn: Callable[..., Any], *args: Any) -> Any:
    """Run ``fn(*args)`` in fastapi-watch's own thread pool and await the result.

    Cancelling the await does not stop a call that has already started; it
    runs to completion in its thread.  Callers that may be cancelled while a
    dependency hangs should go through :class:`SingleFlight`.
    """
    return await asyncio.get_running_loop().run_in_executor(_get_pool(), fn, *args)


class StillRunning(Exception):
    """Raised by :meth:`SingleFlight.run` while the previous call is still running."""


class SingleFlight:
    """Allow one blocking call at a time for its owner (typically one probe).

    The registry cancels a check that times out, but not the thread doing its
    blocking work.  If that thread is still busy when the next check comes
    round, :meth:`run` raises :class:`StillRunning` without submitting more
    work, so a hung dependency ties up one pool thread rather than one per poll.
    A call that was cancelled while still queued is dropped from the queue and
    does not count as running.
    """

    def __init__(self) -> None:
        self._future: concurrent.futures.Future | None = None

    @property
    def busy(self) -> bool:
        """``True`` while the previous call has not finished."""
        return self._future is not None and not self._future.done()

    async def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run ``fn(*args)`` in the shared pool, or raise :class:`StillRunning`."""
        if self.busy:
            raise StillRunning
        self._future = _get_pool().submit(fn, *args)
        return await asyncio.wrap_future(self._future)
