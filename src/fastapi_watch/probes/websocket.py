import functools
import inspect
import time
from collections import deque
from typing import Any, Callable

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe, _update_ema


def _find_ws_param(func: Callable) -> str | None:
    """Return the name of the first WebSocket-typed parameter, or None."""
    try:
        from fastapi import WebSocket
    except ImportError:
        return None
    try:
        sig = inspect.signature(func)
    except (ValueError, TypeError):
        return None
    for param_name, param in sig.parameters.items():
        if param.annotation is WebSocket:
            return param_name
        if param_name in ("websocket", "ws"):
            return param_name
    return None


class _WebSocketWrapper:
    """Transparent proxy around a FastAPI WebSocket that counts messages.

    All attributes and methods not explicitly defined here are forwarded to
    the underlying WebSocket via ``__getattr__``, so the handler sees a
    fully-functional WebSocket object.

    The ``iter_text()``, ``iter_bytes()``, and ``iter_json()`` async
    generators are not intercepted; use the discrete ``receive_*`` methods
    if per-message counting matters.
    """

    def __init__(self, ws: Any, probe: "FastAPIWebSocketProbe") -> None:
        self._ws = ws
        self._probe = probe

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ws, name)

    # ------------------------------------------------------------------
    # Receive — increment messages_received on each call
    # ------------------------------------------------------------------

    async def receive(self) -> dict:
        data = await self._ws.receive()
        self._probe._messages_received += 1
        return data

    async def receive_text(self) -> str:
        data = await self._ws.receive_text()
        self._probe._messages_received += 1
        return data

    async def receive_bytes(self) -> bytes:
        data = await self._ws.receive_bytes()
        self._probe._messages_received += 1
        return data

    async def receive_json(self, mode: str = "text") -> Any:
        data = await self._ws.receive_json(mode=mode)
        self._probe._messages_received += 1
        return data

    # ------------------------------------------------------------------
    # Send — increment messages_sent on each call
    # ------------------------------------------------------------------

    async def send(self, data: dict) -> None:
        await self._ws.send(data)
        self._probe._messages_sent += 1

    async def send_text(self, data: str) -> None:
        await self._ws.send_text(data)
        self._probe._messages_sent += 1

    async def send_bytes(self, data: bytes) -> None:
        await self._ws.send_bytes(data)
        self._probe._messages_sent += 1

    async def send_json(self, data: Any, mode: str = "text") -> None:
        await self._ws.send_json(data, mode=mode)
        self._probe._messages_sent += 1


class FastAPIWebSocketProbe(BaseProbe):
    """Health probe that monitors a FastAPI WebSocket handler via the :meth:`watch` decorator.

    Collects per-endpoint connection stats from real traffic and reports them as a
    :class:`~fastapi_watch.models.ProbeResult`.  The probe is passive — it observes
    actual connections rather than making synthetic ones.

    The decorated handler receives a transparent proxy around the real WebSocket
    object.  The proxy counts every ``receive_*`` and ``send_*`` call; all other
    WebSocket behaviour (``accept``, ``close``, headers, state, etc.) is forwarded
    to the underlying socket unchanged.

    Stats collected:

    * ``active_connections`` — sockets currently open
    * ``total_connections`` — all connections since the probe was created
    * ``messages_received`` — total messages received across all connections
    * ``messages_sent`` — total messages sent across all connections
    * ``error_count`` — connections that closed due to an unhandled exception
      (``WebSocketDisconnect`` is **not** counted as an error)
    * ``error_rate`` — ``error_count / total_connections``
    * ``consecutive_errors`` — unbroken run of error closes; resets on any clean close
    * ``avg_duration_ms`` — exponential moving average of connection lifetimes
    * ``min_duration_ms`` / ``max_duration_ms`` — all-time bounds

    Health thresholds (probe reports ``UNHEALTHY`` when exceeded):

    * ``max_error_rate`` — default ``0.1`` (10 %)
    * ``min_active_connections`` — default ``0`` (disabled).  Set to a positive
      integer to require that many live sockets at check time.  Useful for services
      that maintain persistent client connections (live dashboards, data feeds, etc.).

    Usage::

        ws_probe = FastAPIWebSocketProbe(name="chat", max_error_rate=0.05)

        @app.websocket("/ws/chat")
        @ws_probe.watch
        async def chat(websocket: WebSocket):
            await websocket.accept()
            while True:
                msg = await websocket.receive_text()
                await websocket.send_text(msg)

        registry.add(ws_probe)

    Args:
        name: Probe name shown in health reports.
        max_error_rate: Error-rate threshold above which the probe is UNHEALTHY (0–1).
        min_active_connections: Minimum number of open sockets required for the probe
            to be HEALTHY.  ``0`` (default) disables this check.
        window_size: Number of recent connection durations kept for EMA calculations.
        ema_alpha: Smoothing factor for the exponential moving average (0–1).
            Higher values make ``avg_duration_ms`` react faster to changes.
        timeout: Passed to the registry; not used internally.
    """

    def __init__(
        self,
        name: str = "websocket",
        *,
        description: str | None = None,
        tags: list[str] | None = None,
        max_error_rate: float = 0.1,
        min_active_connections: int = 0,
        window_size: int = 100,
        ema_alpha: float = 0.1,
        timeout: float | None = None,
        poll_interval_ms: int | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.tags = list(tags) if tags else []
        self.timeout = timeout
        self.poll_interval_ms = poll_interval_ms
        self.max_error_rate = max_error_rate
        self.min_active_connections = min_active_connections
        self.ema_alpha = ema_alpha

        self._active_connections: int = 0
        self._total_connections: int = 0
        self._messages_received: int = 0
        self._messages_sent: int = 0
        self._error_count: int = 0
        self._consecutive_errors: int = 0
        self._avg_duration_ms: float | None = None
        self._min_duration_ms: float | None = None
        self._max_duration_ms: float | None = None
        self._duration_window: deque[float] = deque(maxlen=window_size)

    # ------------------------------------------------------------------
    # Internal recording
    # ------------------------------------------------------------------

    def _record_close(self, duration_ms: float, errored: bool) -> None:
        self._active_connections -= 1

        if errored:
            self._consecutive_errors += 1
        else:
            self._consecutive_errors = 0

        self._avg_duration_ms = _update_ema(self._avg_duration_ms, duration_ms, self.ema_alpha)
        self._min_duration_ms = duration_ms if self._min_duration_ms is None else min(self._min_duration_ms, duration_ms)
        self._max_duration_ms = duration_ms if self._max_duration_ms is None else max(self._max_duration_ms, duration_ms)

        self._duration_window.append(duration_ms)

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------

    @property
    def _error_rate(self) -> float:
        if self._total_connections == 0:
            return 0.0
        return self._error_count / self._total_connections

    # ------------------------------------------------------------------
    # Decorator
    # ------------------------------------------------------------------

    def watch(self, func: Callable) -> Callable:
        """Decorator that monitors a WebSocket handler.

        Injects a transparent :class:`_WebSocketWrapper` in place of the real
        ``WebSocket`` object so that message counts are collected without any
        changes to the handler body.

        ``WebSocketDisconnect`` is treated as a normal close and is always
        re-raised.  Any other exception increments ``error_count`` and is
        re-raised so FastAPI's exception handling is unaffected.

        The handler's signature is fully preserved via ``functools.wraps``.
        """
        ws_param = _find_ws_param(func)

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:  # type: ignore[return]
            # Locate and wrap the WebSocket argument.
            ws_instance = kwargs.get(ws_param) if ws_param else None
            if ws_instance is None:
                try:
                    from fastapi import WebSocket
                    ws_instance = next(
                        (a for a in args if isinstance(a, WebSocket)), None
                    )
                except ImportError:
                    pass

            if ws_instance is not None:
                wrapped_ws = _WebSocketWrapper(ws_instance, self)
                if ws_param and ws_param in kwargs:
                    kwargs = {**kwargs, ws_param: wrapped_ws}
                else:
                    args = tuple(wrapped_ws if a is ws_instance else a for a in args)

            start = time.perf_counter()
            self._active_connections += 1
            self._total_connections += 1
            errored = False

            try:
                await func(*args, **kwargs)
            except Exception as exc:
                try:
                    from fastapi import WebSocketDisconnect
                    if not isinstance(exc, WebSocketDisconnect):
                        self._error_count += 1
                        errored = True
                except ImportError:
                    self._error_count += 1
                    errored = True
                raise
            finally:
                duration_ms = round((time.perf_counter() - start) * 1000, 2)
                self._record_close(duration_ms, errored)

        wrapper._fastapi_watch = "manual"
        wrapper._fastapi_watch_probe = self
        return wrapper

    # ------------------------------------------------------------------
    # BaseProbe interface
    # ------------------------------------------------------------------

    async def check(self) -> ProbeResult:
        if self._total_connections == 0 and self._active_connections == 0:
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.HEALTHY,
                latency_ms=0.0,
                details={"message": "no connections observed yet"},
            )

        error_rate = self._error_rate
        avg_duration = self._avg_duration_ms or 0.0

        reasons: list[str] = []
        if error_rate > self.max_error_rate:
            reasons.append(
                f"error rate {error_rate:.1%} exceeds threshold {self.max_error_rate:.1%}"
            )
        if (
            self.min_active_connections > 0
            and self._active_connections < self.min_active_connections
        ):
            reasons.append(
                f"active connections {self._active_connections} below minimum "
                f"{self.min_active_connections}"
            )

        status = ProbeStatus.UNHEALTHY if reasons else ProbeStatus.HEALTHY

        details: dict[str, Any] = {
            "active_connections": self._active_connections,
            "total_connections": self._total_connections,
            "messages_received": self._messages_received,
            "messages_sent": self._messages_sent,
            "error_count": self._error_count,
            "error_rate": round(error_rate, 4),
            "consecutive_errors": self._consecutive_errors,
            "avg_duration_ms": round(avg_duration, 2) if self._avg_duration_ms is not None else None,
            "min_duration_ms": self._min_duration_ms,
            "max_duration_ms": self._max_duration_ms,
        }

        return ProbeResult(
            name=self.name,
            status=status,
            latency_ms=round(avg_duration, 2),
            error="; ".join(reasons) or None,
            details=details,
        )
