import functools
import inspect
import time
from collections import deque
from typing import Any, Callable

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe, _resolved_signature, _update_ema, _validate_stats_params


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


_COUNTING_CLASSES: dict[type, type] = {}


def _counting_class(base: type) -> type:
    """Return (and cache) a subclass of *base* that counts application messages.

    Every Starlette ``receive_*`` / ``send_*`` / ``iter_*`` helper goes through
    ``receive()`` and ``send()``, so counting there — by message type, so the
    connect/accept/close handshake is not counted — covers them all exactly once.
    It must be a direct subclass with no extra bases: a mixin base changes the
    object layout, and ``__class__`` assignment refuses that.
    """
    counting = _COUNTING_CLASSES.get(base)
    if counting is not None:
        return counting

    async def receive(self) -> dict:
        message = await super(counting, self).receive()
        if message.get("type") == "websocket.receive":
            self._fastapi_watch_probe._messages_received += 1
        return message

    async def send(self, message: dict) -> None:
        await super(counting, self).send(message)
        if message.get("type") == "websocket.send":
            self._fastapi_watch_probe._messages_sent += 1

    counting = type(f"Counting{base.__name__}", (base,), {"receive": receive, "send": send, "_fastapi_watch_counting": True})
    _COUNTING_CLASSES[base] = counting
    return counting


def _attach_counter(ws: Any, probe: "FastAPIWebSocketProbe") -> None:
    """Make *ws* count its messages for *probe*, keeping it the same object.

    The instance's class is swapped for a cached counting subclass of its own
    class, rather than wrapping it in a proxy: the handler keeps the very
    socket FastAPI created (``isinstance(ws, WebSocket)`` holds, and state is
    shared with anything else holding it, such as dependencies).  Objects that
    are not real Starlette WebSockets are left alone — connection stats are
    still recorded, message counts are not.
    """
    try:
        from starlette.websockets import WebSocket
    except ImportError:
        return
    cls = type(ws)
    if not issubclass(cls, WebSocket) or getattr(cls, "_fastapi_watch_counting", False):
        return
    ws.__class__ = _counting_class(cls)
    ws._fastapi_watch_probe = probe


class FastAPIWebSocketProbe(BaseProbe):
    """Health probe that monitors a FastAPI WebSocket handler via the :meth:`watch` decorator.

    Collects per-endpoint connection stats from real traffic and reports them as a
    :class:`~fastapi_watch.models.ProbeResult`.  The probe is passive — it observes
    actual connections rather than making synthetic ones.

    The decorated handler receives the real WebSocket object (``isinstance``
    checks hold), instrumented to count every application message it sends or
    receives — including through ``iter_text()`` / ``iter_json()``.  Handshake
    messages (connect, accept, close) are not counted.

    Stats collected:

    * ``active_connections`` — sockets currently open
    * ``total_connections`` — all connections since the probe was created
    * ``messages_received`` — total messages received across all connections
    * ``messages_sent`` — total messages sent across all connections
    * ``error_count`` — connections that closed due to an unhandled exception
      (``WebSocketDisconnect`` is **not** counted as an error)
    * ``error_rate`` — fraction of the last ``window_size`` closed connections that errored
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
        window_size: Number of recently closed connections used for the error rate.
        ema_alpha: Smoothing factor for the exponential moving average (0–1).
            Higher values make ``avg_duration_ms`` react faster to changes.
        timeout: Passed to the registry; not used internally.
        circuit_breaker_enabled: Whether the registry's circuit breaker may
            suspend this probe (default ``False``: it only reads in-memory stats).
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
        circuit_breaker_enabled: bool = False,
    ) -> None:
        _validate_stats_params(window_size=window_size, ema_alpha=ema_alpha, max_error_rate=max_error_rate)
        self.name = name
        self.description = description
        self.tags = list(tags) if tags else []
        self.timeout = timeout
        self.poll_interval_ms = poll_interval_ms
        # Off by default: the probe reads in-memory counters, so suspending it
        # protects nothing and would show a stale result while traffic recovers.
        self.circuit_breaker_enabled = circuit_breaker_enabled
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
        # True = errored close; drives error_rate so health reflects recent connections only.
        self._outcome_window: deque[bool] = deque(maxlen=window_size)
        self._window_error_count: int = 0

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

        if len(self._outcome_window) == self._outcome_window.maxlen and self._outcome_window[0]:
            self._window_error_count -= 1
        self._outcome_window.append(errored)
        self._window_error_count += errored

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------

    @property
    def _error_rate(self) -> float:
        """Errored fraction of the last ``window_size`` closed connections.

        Open connections are excluded: they have not had the chance to error
        yet, so counting them would understate the rate.  ``error_count``
        still reports the lifetime total.
        """
        window = self._outcome_window
        return self._window_error_count / len(window) if window else 0.0

    # ------------------------------------------------------------------
    # Decorator
    # ------------------------------------------------------------------

    def watch(self, func: Callable) -> Callable:
        """Decorator that monitors a WebSocket handler.

        Instruments the ``WebSocket`` argument in place (see
        :func:`_attach_counter`) so message counts are collected without any
        change to the handler body or to the object the handler receives.

        ``WebSocketDisconnect`` is treated as a normal close and is always
        re-raised.  Any other exception increments ``error_count`` and is
        re-raised so FastAPI's exception handling is unaffected.

        The handler's signature is fully preserved via ``functools.wraps``.
        """
        ws_param = _find_ws_param(func)

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:  # type: ignore[return]
            ws_instance = kwargs.get(ws_param) if ws_param else None
            if ws_instance is None:
                # FastAPI passes parameters by keyword, and a string annotation
                # (``from __future__ import annotations``) hides the type from
                # _find_ws_param, so look for the socket among all arguments.
                try:
                    from fastapi import WebSocket
                    ws_instance = next(
                        (a for a in (*args, *kwargs.values()) if isinstance(a, WebSocket)), None
                    )
                except ImportError:
                    pass
            if ws_instance is not None:
                _attach_counter(ws_instance, self)

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
        return _resolved_signature(wrapper, func)

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
