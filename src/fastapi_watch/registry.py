import asyncio
import base64
import logging
import secrets
from datetime import datetime
from typing import AsyncGenerator, Awaitable, Callable
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from .alerts import BaseAlerter, WebhookAlerter
from .dashboard import render_dashboard
from .models import AlertRecord, HealthReport, ProbeResult, ProbeStatus
from .prometheus import render_prometheus
from .probe_router import ProbeRouter
from .probes.base import BaseProbe
from .storage import InMemoryProbeStorage, ProbeStorage

_MIN_POLL_INTERVAL_MS = 1000


def _normalize_interval(ms: int | None) -> int | None:
    """Validate and normalize a poll interval.

    - ``None`` or ``0``  → ``None``  (single-fetch mode)
    - ``1 – 999``        → ``1000``  (clamped to minimum)
    - ``>= 1000``        → used as-is
    """
    if ms is None or ms == 0:
        return None
    return max(ms, _MIN_POLL_INTERVAL_MS)


def _make_auth_checker(auth) -> Callable | None:
    """Return a FastAPI dependency that enforces authentication, or ``None`` for open access.

    *auth* may be:

    - ``None`` — no authentication (default).
    - ``{"type": "basic", "username": "x", "password": "y"}`` — HTTP Basic auth.
    - ``{"type": "apikey", "key": "x", "header": "X-API-Key"}`` — API key header.
    - A callable ``(request: Request) -> bool | Awaitable[bool]`` — custom check.
      Return ``True`` to allow the request, ``False`` to reject with 403.
    """
    if auth is None:
        return None

    if callable(auth):
        async def _custom(request: Request) -> None:
            result = auth(request)
            if asyncio.iscoroutine(result):
                result = await result
            if not result:
                raise HTTPException(status_code=403, detail="Forbidden")
        return _custom

    if not isinstance(auth, dict):
        raise ValueError(f"auth must be None, a callable, or a dict; got {type(auth)!r}")

    auth_type = auth.get("type")
    _realm = 'realm="health"'

    if auth_type == "basic":
        expected_user = auth["username"].encode()
        expected_pass = auth["password"].encode()
        _challenge = {"WWW-Authenticate": f"Basic {_realm}"}

        async def _basic(request: Request) -> None:
            header = request.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                raise HTTPException(status_code=401, headers=_challenge, detail="Unauthorized")
            try:
                decoded = base64.b64decode(header[6:]).decode()
                user, _, pwd = decoded.partition(":")
            except Exception:
                raise HTTPException(status_code=401, headers=_challenge)
            if not (
                secrets.compare_digest(user.encode(), expected_user)
                and secrets.compare_digest(pwd.encode(), expected_pass)
            ):
                raise HTTPException(status_code=401, headers=_challenge, detail="Unauthorized")
        return _basic

    if auth_type == "apikey":
        expected_key = auth["key"].encode()
        header_name = auth.get("header", "X-API-Key")

        async def _apikey(request: Request) -> None:
            provided = request.headers.get(header_name, "").encode()
            if not secrets.compare_digest(provided, expected_key):
                raise HTTPException(status_code=403, detail="Forbidden")
        return _apikey

    raise ValueError(f"Unknown auth type {auth_type!r}. Supported: 'basic', 'apikey'.")


class HealthRegistry:
    """Register health probes and mount health endpoints on a FastAPI app.

    Mounted routes (all customisable via *prefix*):

    - ``GET /health/live``          — liveness; always 200
    - ``GET /health/ready``         — readiness; 200 if all critical probes pass, 503 otherwise
    - ``GET /health/status``        — full probe detail; 200 / 207 (Multi-Status)
    - ``GET /health/history``       — rolling probe result history
    - ``GET /health/startup``       — startup check; 503 until :meth:`set_started` is called
    - ``GET /health/dashboard``     — HTML dashboard with live SSE (Server-Sent Events) updates
    - ``GET /health/ready/stream``  — SSE (Server-Sent Events) stream of readiness
    - ``GET /health/status/stream`` — SSE (Server-Sent Events) stream of full probe detail

    Args:
        app: FastAPI application.
        prefix: URL prefix for health routes (default ``/health``).
        tags: OpenAPI tags applied to all routes.
        poll_interval_ms: How often (ms) to re-run probes while a streaming client is
            connected (default ``60000``).  ``0`` / ``None`` = single-fetch mode.
            Values 1–999 are clamped to 1000.  Individual probes may override this
            with their own ``poll_interval_ms``.
        logger: Optional :class:`logging.Logger` for warnings and probe exceptions.
        timezone: IANA timezone name for all timestamps (default ``"UTC"``).
        grace_period_ms: Ignore probe failures for this many ms after startup
            (affects ``/health/ready`` only).
        history_size: Number of past results kept per probe (default 120).
        routers: :class:`~fastapi_watch.ProbeRouter` instances to merge at startup.
        dashboard: ``True`` (default) — built-in HTML dashboard.  ``False`` — omit the
            route.  Callable ``(report: HealthReport) -> str`` — custom renderer.
        circuit_breaker: Enable the circuit breaker (default ``True``).  When a probe
            fails *circuit_breaker_threshold* times consecutively it is suspended for
            *circuit_breaker_cooldown_ms* ms, avoiding repeated calls to a broken
            dependency.  Individual probes may override threshold / cooldown via their
            own ``circuit_breaker_threshold`` / ``circuit_breaker_cooldown_ms``
            attributes.
        circuit_breaker_threshold: Consecutive failures before opening the circuit
            (default 5).
        circuit_breaker_cooldown_ms: How long (ms) the circuit stays open before the
            probe is retried (default ``600_000`` = 10 minutes).
        result_ttl_seconds: How long (seconds) probe results are retained in storage
            (default ``7200`` = 2 hours).  Set to ``0`` to disable time-based expiry.
        alert_ttl_seconds: How long (seconds) alert records (state-change events) are
            retained (default ``259200`` = 72 hours).  Set to ``0`` to keep forever.
        max_alerts: Maximum number of alert records to retain (default ``120``).
            When the cap is reached the oldest alert is dropped (regardless of TTL).
        storage: Custom storage backend implementing :class:`~fastapi_watch.ProbeStorage`.
            Defaults to :class:`~fastapi_watch.InMemoryProbeStorage`.  Pass a Redis-backed
            or other implementation here for persistence across restarts.
        webhook_url: HTTP(S) URL that receives a JSON ``POST`` whenever a probe
            changes state.  The call is fire-and-forget and never blocks health checks.
            Payload: ``{"probe", "old_status", "new_status", "timestamp"}``.
        auth: Authentication for all health endpoints.  ``None`` (default) = open.
            See :func:`_make_auth_checker` for accepted forms.
        startup_probes: Probes that must pass for ``/health/startup`` to return 200.
            These are separate from the main probe registry and are not shown in
            ``/health/status``.

    Example::

        registry = HealthRegistry(
            app,
            circuit_breaker_threshold=3,
            webhook_url="https://hooks.example.com/health",
            auth={"type": "apikey", "key": "s3cr3t"},
        )
        registry.add(RedisProbe(url="redis://localhost", poll_interval_ms=5_000))
        registry.set_started()   # call once app init is complete
    """

    def __init__(
        self,
        app: FastAPI,
        prefix: str = "/health",
        tags: list[str] | None = None,
        poll_interval_ms: int | None = 60_000,
        logger: logging.Logger | None = None,
        grace_period_ms: int = 0,
        history_size: int = 120,
        timezone: str = "UTC",
        routers: list[ProbeRouter] | None = None,
        dashboard: bool | Callable[..., str] = True,
        circuit_breaker: bool = True,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_cooldown_ms: int = 600_000,
        webhook_url: str | None = None,
        alerters: list[BaseAlerter] | None = None,
        auth: dict | Callable | None = None,
        startup_probes: list[BaseProbe] | None = None,
        result_ttl_seconds: float = 7200.0,
        alert_ttl_seconds: float = 259200.0,
        max_alerts: int = 120,
        storage: ProbeStorage | None = None,
    ) -> None:
        self.app = app
        self.prefix = prefix
        self._logger = logger
        self._grace_period_ms: int = max(0, grace_period_ms)
        self._timezone_name: str = timezone
        self._tzinfo: ZoneInfo = ZoneInfo(timezone)
        self._start_time: datetime = datetime.now(self._tzinfo)
        self._history_size: int = max(1, history_size)
        self._storage: ProbeStorage = storage or InMemoryProbeStorage(
            max_results=self._history_size,
            result_ttl_seconds=result_ttl_seconds,
            alert_ttl_seconds=alert_ttl_seconds,
            max_alerts=max(1, max_alerts),
        )
        self._probes: list[tuple[BaseProbe, bool]] = []
        self._poll_interval_ms: int | None = self._set_interval(poll_interval_ms)

        # Per-probe scheduling
        self._probe_last_run: dict[str, float] = {}
        self._last_checked_at: datetime | None = None
        self._cache_lock = asyncio.Lock()

        # Circuit breaker state
        self._circuit_breaker_enabled: bool = circuit_breaker
        self._circuit_threshold: int = circuit_breaker_threshold
        self._circuit_cooldown_ms: int = circuit_breaker_cooldown_ms
        self._circuit_open_until: dict[str, float] = {}
        self._circuit_err_count: dict[str, int] = {}
        self._circuit_trips: dict[str, int] = {}  # lifetime trip count per probe

        # Maintenance mode
        self._maintenance_until: datetime | None = None

        # Alerters — webhook_url kept for backwards compat, wraps into WebhookAlerter
        self._alerters: list[BaseAlerter] = list(alerters or [])
        if webhook_url:
            self._alerters.append(WebhookAlerter(url=webhook_url))

        # Auth
        self._auth = auth

        # Startup
        self._started: bool = False
        self._startup_probes: list[BaseProbe] = list(startup_probes or [])

        # State-change callbacks and streaming
        self._probe_states: dict[str, ProbeStatus] = {}
        self._state_change_callbacks: list[
            Callable[[str, ProbeStatus, ProbeStatus], None | Awaitable[None]]
        ] = []
        self._poll_task: asyncio.Task | None = None
        self._active_connections: int = 0

        self._register_routes(tags or ["health"], dashboard=dashboard)
        for router in routers or []:
            self.include_router(router)

    # ------------------------------------------------------------------
    # Public API — probe registration
    # ------------------------------------------------------------------

    def include_router(self, router: ProbeRouter) -> "HealthRegistry":
        """Include all probes from a :class:`~fastapi_watch.ProbeRouter`. Returns ``self``."""
        for probe, critical in router._probes:
            self.add(probe, critical=critical)
        return self

    def add(self, probe: BaseProbe, critical: bool = True) -> "HealthRegistry":
        """Add a single probe. Returns ``self`` for chaining.

        Silently skips the probe if it is already registered (identity check).
        """
        if not any(p is probe for p, _ in self._probes):
            self._probes.append((probe, critical))
        return self

    def add_probes(self, probes: list[BaseProbe], critical: bool = True) -> "HealthRegistry":
        """Add a list of probes. Returns ``self`` for chaining."""
        for probe in probes:
            self.add(probe, critical=critical)
        return self

    def set_started(self) -> "HealthRegistry":
        """Mark the application as fully initialised.

        Until this is called ``GET /health/startup`` returns 503.
        Returns ``self`` for chaining.
        """
        self._started = True
        return self

    def set_maintenance(self, until: datetime | None = None) -> "HealthRegistry":
        """Enter maintenance mode.

        While active, ``GET /health/ready`` returns ``200 {"status": "maintenance"}``
        and state-change webhooks are suppressed.  The dashboard shows a
        maintenance banner.

        Args:
            until: When maintenance ends.  ``None`` means indefinite —
                call :meth:`clear_maintenance` to exit.

        Returns ``self`` for chaining.
        """
        self._maintenance_until = until
        return self

    def clear_maintenance(self) -> "HealthRegistry":
        """Exit maintenance mode immediately. Returns ``self`` for chaining."""
        self._maintenance_until = None
        return self

    def _in_maintenance(self) -> bool:
        """Return True if the registry is currently in maintenance mode."""
        if self._maintenance_until is None:
            return False
        return datetime.now(self._tzinfo) < self._maintenance_until

    def on_state_change(
        self,
        callback: Callable[[str, ProbeStatus, ProbeStatus], None | Awaitable[None]],
    ) -> "HealthRegistry":
        """Register a callback invoked when a probe's status changes.

        The callback receives ``(probe_name, old_status, new_status)`` and may
        be sync or async.  Returns ``self`` for chaining.
        """
        self._state_change_callbacks.append(callback)
        return self

    def set_poll_interval(self, ms: int | None) -> None:
        """Update the polling interval at runtime.

        ``0`` / ``None`` switches to single-fetch mode and clears the cache.
        Values 1–999 are clamped to 1000 ms.
        """
        self._poll_interval_ms = self._set_interval(ms)
        if self._poll_interval_ms is None:
            self._storage.clear_latest()
            if not self._has_custom_intervals():
                self._cancel_poll_task()
        elif self._active_connections > 0:
            self._cancel_poll_task()
            try:
                self._poll_task = asyncio.get_running_loop().create_task(self._poll_loop())
            except RuntimeError:
                pass

    # ------------------------------------------------------------------
    # Probe execution
    # ------------------------------------------------------------------

    async def run_all(self) -> list[ProbeResult]:
        """Run all probes concurrently and return their results."""
        return await self._execute_probes(self._probes)

    async def _execute_probes(self, pairs: list[tuple[BaseProbe, bool]]) -> list[ProbeResult]:
        """Run a subset of probes, update the cache, history, and fire callbacks."""
        if not pairs:
            return []
        results = list(await asyncio.gather(*(self._safe_check(p, c) for p, c in pairs)))
        now_dt = datetime.now(self._tzinfo)
        run_time = asyncio.get_running_loop().time()
        async with self._cache_lock:
            for r in results:
                await self._storage.set_latest(r)
                self._probe_last_run[r.name] = run_time
            self._last_checked_at = now_dt
        for r in results:
            await self._storage.append_history(r)
        await self._fire_state_changes(results)
        return results

    async def _safe_check(self, probe: BaseProbe, critical: bool) -> ProbeResult:
        """Run one probe with circuit-breaker and timeout handling."""
        loop = asyncio.get_running_loop()

        # Circuit breaker — skip probe while circuit is open
        if self._circuit_breaker_enabled:
            open_until = self._circuit_open_until.get(probe.name, 0.0)
            if loop.time() < open_until:
                cb_info = self._cb_info(probe.name, True)
                cached = await self._storage.get_latest(probe.name)
                if cached is not None:
                    details = {**(cached.details or {}), "circuit_breaker": cb_info}
                    return cached.model_copy(update={"critical": critical, "details": details})
                return ProbeResult(
                    name=probe.name,
                    status=ProbeStatus.UNHEALTHY,
                    critical=critical,
                    error="circuit breaker open — probe temporarily suspended",
                    details={"circuit_breaker": cb_info},
                )

        # Run the probe
        try:
            coro = probe.check()
            result = (
                await asyncio.wait_for(coro, timeout=probe.timeout)
                if probe.timeout is not None
                else await coro
            )
            result = (
                result
                if result.critical == critical
                else result.model_copy(update={"critical": critical})
            )
        except Exception as exc:
            if self._logger:
                self._logger.exception("Probe %r raised an exception", probe.name)
            result = ProbeResult(
                name=probe.name,
                status=ProbeStatus.UNHEALTHY,
                critical=critical,
                error=f"{type(exc).__name__}: {exc}",
            )

        # Update circuit breaker state
        if self._circuit_breaker_enabled:
            if result.is_passing:
                # HEALTHY or DEGRADED — probe is responding; reset failure count
                self._circuit_err_count.pop(probe.name, None)
                self._circuit_open_until.pop(probe.name, None)
            else:
                threshold = (
                    probe.circuit_breaker_threshold
                    if probe.circuit_breaker_threshold is not None
                    else self._circuit_threshold
                )
                cooldown_ms = (
                    probe.circuit_breaker_cooldown_ms
                    if probe.circuit_breaker_cooldown_ms is not None
                    else self._circuit_cooldown_ms
                )
                count = self._circuit_err_count.get(probe.name, 0) + 1
                self._circuit_err_count[probe.name] = count
                if count >= threshold:
                    self._circuit_open_until[probe.name] = loop.time() + cooldown_ms / 1000
                    self._circuit_trips[probe.name] = self._circuit_trips.get(probe.name, 0) + 1
                    if self._logger:
                        self._logger.warning(
                            "Probe %r circuit opened after %d failures; "
                            "suspended for %.0f s",
                            probe.name,
                            count,
                            cooldown_ms / 1000,
                        )

            # Inject circuit breaker stats into result details
            is_open = loop.time() < self._circuit_open_until.get(probe.name, 0.0)
            cb_info = self._cb_info(probe.name, is_open)
            result = result.model_copy(
                update={"details": {**(result.details or {}), "circuit_breaker": cb_info}}
            )

        return result

    # ------------------------------------------------------------------
    # Per-probe scheduling helpers
    # ------------------------------------------------------------------

    def _effective_interval_s(self, probe: BaseProbe) -> float | None:
        """Return effective poll interval for *probe* in seconds, or None (single-fetch)."""
        ms = probe.poll_interval_ms if probe.poll_interval_ms is not None else self._poll_interval_ms
        if ms is None or ms == 0:
            return None
        return max(ms, _MIN_POLL_INTERVAL_MS) / 1000

    def _is_probe_due(self, probe: BaseProbe, now: float) -> bool:
        """Return True if the probe should run now based on its schedule."""
        interval_s = self._effective_interval_s(probe)
        if interval_s is None:
            return False  # single-fetch mode — not managed by the poll loop
        last = self._probe_last_run.get(probe.name, -1.0)
        return last < 0 or (now - last) >= interval_s

    def _has_custom_intervals(self) -> bool:
        return any(p.poll_interval_ms is not None for p, _ in self._probes)

    # ------------------------------------------------------------------
    # Caching / result retrieval
    # ------------------------------------------------------------------

    async def _get_results(self) -> list[ProbeResult]:
        """Return current probe results.

        Single-fetch probes always run fresh.  Polled probes are served from
        the cache; uncached or expired probes run once to warm it.
        """
        cached = await self._storage.get_all_latest()
        to_run = [
            (p, c) for p, c in self._probes
            if self._effective_interval_s(p) is None or p.name not in cached
        ]
        if to_run:
            await self._execute_probes(to_run)
        return list((await self._storage.get_all_latest()).values())

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------

    def _set_interval(self, ms: int | None) -> int | None:
        normalized = _normalize_interval(ms)
        if self._logger and ms is not None and ms != 0 and ms < _MIN_POLL_INTERVAL_MS:
            self._logger.warning(
                "poll_interval_ms %d is below the minimum of %d ms; clamping to %d ms",
                ms,
                _MIN_POLL_INTERVAL_MS,
                _MIN_POLL_INTERVAL_MS,
            )
        return normalized

    async def _on_connect(self) -> None:
        self._active_connections += 1
        if self._active_connections == 1 and self._poll_task is None:
            if self._poll_interval_ms is not None or self._has_custom_intervals():
                self._poll_task = asyncio.create_task(self._poll_loop())

    async def _on_disconnect(self) -> None:
        self._active_connections -= 1
        if self._active_connections == 0 and not self._has_custom_intervals():
            self._cancel_poll_task()

    def _cancel_poll_task(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None

    async def _poll_loop(self) -> None:
        """Background loop: run each probe when its interval elapses (1 s granularity)."""
        loop = asyncio.get_running_loop()
        while True:
            now = loop.time()
            due = [(p, c) for p, c in self._probes if self._is_probe_due(p, now)]
            if due:
                await self._execute_probes(due)
            await asyncio.sleep(1.0)

    def _in_grace_period(self) -> bool:
        if not self._grace_period_ms:
            return False
        return (datetime.now(self._tzinfo) - self._start_time).total_seconds() * 1000 < self._grace_period_ms

    def _cb_info(self, probe_name: str, open: bool) -> dict:
        return {
            "open": open,
            "consecutive_failures": self._circuit_err_count.get(probe_name, 0),
            "trips_total": self._circuit_trips.get(probe_name, 0),
        }

    # ------------------------------------------------------------------
    # SSE (Server-Sent Events) streaming
    # ------------------------------------------------------------------

    async def _wait_for_next_poll(self, request: Request) -> bool:
        """Sleep for poll_interval_ms, checking client disconnect every 500 ms.

        Returns True if the client disconnected before the interval elapsed.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._poll_interval_ms / 1000
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.5, remaining))
            if await request.is_disconnected():
                return True

    async def _event_stream(
        self,
        request: Request,
        make_report: Callable[[list[ProbeResult]], str],
    ) -> AsyncGenerator[str, None]:
        """Async generator that yields SSE (Server-Sent Events) events while the client is connected."""
        await self._on_connect()
        try:
            while True:
                _cached = await self._storage.get_all_latest()
                results = (
                    list(_cached.values())
                    if _cached
                    else await self._execute_probes(self._probes)
                )
                yield f"data: {make_report(results)}\n\n"
                if self._poll_interval_ms is None:
                    return
                if await self._wait_for_next_poll(request):
                    return
        finally:
            await self._on_disconnect()

    # ------------------------------------------------------------------
    # State-change callbacks and webhook
    # ------------------------------------------------------------------

    async def _fire_state_changes(self, results: list[ProbeResult]) -> None:
        """Record alerts and fire callbacks/webhook for any probe whose status changed."""
        in_maintenance = self._in_maintenance()
        for result in results:
            old = self._probe_states.get(result.name)
            self._probe_states[result.name] = result.status
            if old is not None and old != result.status:
                alert_record = AlertRecord(
                    probe=result.name,
                    old_status=old,
                    new_status=result.status,
                    timestamp=datetime.now(self._tzinfo),
                )
                await self._storage.append_alert(alert_record)
                for cb in self._state_change_callbacks:
                    ret = cb(result.name, old, result.status)
                    if asyncio.iscoroutine(ret):
                        await ret
                if self._alerters and not in_maintenance:
                    asyncio.create_task(
                        self._dispatch_alert(alert_record)
                    )

    async def _dispatch_alert(self, alert: AlertRecord) -> None:
        """Call each registered alerter for a probe state change.

        Failures in individual alerters are caught and logged so one broken
        integration cannot silence the others.
        """
        for alerter in self._alerters:
            try:
                await alerter.notify(alert)
            except Exception:
                if self._logger:
                    self._logger.warning(
                        "Alerter %s failed for probe %s",
                        type(alerter).__name__,
                        alert.probe,
                    )

    # ------------------------------------------------------------------
    # Route registration
    # ------------------------------------------------------------------

    def _register_routes(
        self,
        tags: list[str],
        dashboard: bool | Callable[..., str] = True,
    ) -> None:
        registry = self
        prefix = self.prefix

        auth_checker = _make_auth_checker(self._auth)
        auth_deps = [Depends(auth_checker)] if auth_checker else []

        def _make_sse_report(results: list[ProbeResult]) -> str:
            return HealthReport.from_results(
                results,
                checked_at=registry._last_checked_at,
                timezone=registry._timezone_name,
            ).model_dump_json()

        sse_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

        @self.app.get(
            f"{prefix}/live",
            tags=tags,
            summary="Liveness check",
            description="Returns 200 while the process is running.",
            dependencies=auth_deps,
        )
        async def liveness() -> Response:
            return JSONResponse({"status": "ok"})

        @self.app.get(
            f"{prefix}/ready",
            tags=tags,
            summary="Readiness check",
            description="Returns 200 if all critical probes pass, 503 otherwise.",
            dependencies=auth_deps,
        )
        async def readiness() -> Response:
            if registry._in_maintenance():
                return JSONResponse({"status": "maintenance"}, status_code=200)
            if registry._in_grace_period():
                return JSONResponse({"status": "starting"}, status_code=503)
            results = await registry._get_results()
            report = HealthReport.from_results(
                results,
                checked_at=registry._last_checked_at,
                timezone=registry._timezone_name,
            )
            # HEALTHY and DEGRADED both return 200 — traffic still flows.
            # Only UNHEALTHY causes 503.
            code = 503 if report.status == ProbeStatus.UNHEALTHY else 200
            return Response(
                content=report.model_dump_json(),
                status_code=code,
                media_type="application/json",
            )

        @self.app.get(
            f"{prefix}/status",
            tags=tags,
            summary="Detailed health status",
            description="Returns full probe results. 200 when all healthy, 207 when any probe fails.",
            dependencies=auth_deps,
        )
        async def health_status() -> Response:
            results = await registry._get_results()
            report = HealthReport.from_results(
                results,
                checked_at=registry._last_checked_at,
                timezone=registry._timezone_name,
            )
            code = 200 if report.status == ProbeStatus.HEALTHY else 207
            return Response(
                content=report.model_dump_json(),
                status_code=code,
                media_type="application/json",
            )

        @self.app.get(
            f"{prefix}/history",
            tags=tags,
            summary="Probe result history",
            description="Returns the last N results for each probe (oldest-first), within the result TTL window.",
            dependencies=auth_deps,
        )
        async def probe_history() -> Response:
            history = await registry._storage.get_history()
            payload = {
                name: [r.model_dump(mode="json") for r in entries]
                for name, entries in history.items()
            }
            return JSONResponse({"probes": payload})

        @self.app.get(
            f"{prefix}/alerts",
            tags=tags,
            summary="Alert history",
            description="Returns probe state-change alerts retained within the alert TTL window (default 72 hours).",
            dependencies=auth_deps,
        )
        async def alert_history() -> Response:
            alerts = await registry._storage.get_alerts()
            return JSONResponse({"alerts": [a.model_dump(mode="json") for a in alerts]})

        @self.app.get(
            f"{prefix}/startup",
            tags=tags,
            summary="Startup check",
            description=(
                "Returns 503 until :meth:`set_started` is called (and any startup probes pass). "
                "Use this as a Kubernetes startupProbe target."
            ),
            dependencies=auth_deps,
        )
        async def startup_check() -> Response:
            if not registry._started:
                return JSONResponse({"status": "starting"}, status_code=503)
            if registry._startup_probes:
                results = list(
                    await asyncio.gather(*(p.check() for p in registry._startup_probes))
                )
                if not all(r.is_passing for r in results):
                    report = HealthReport.from_results(results)
                    return Response(
                        content=report.model_dump_json(),
                        status_code=503,
                        media_type="application/json",
                    )
            return JSONResponse({"status": "started"})

        @self.app.get(
            f"{prefix}/ready/stream",
            tags=tags,
            summary="Readiness stream",
            description="SSE (Server-Sent Events) stream of readiness. Poll loop stops when last client disconnects.",
            dependencies=auth_deps,
        )
        async def readiness_stream(request: Request) -> StreamingResponse:
            return StreamingResponse(
                registry._event_stream(request, _make_sse_report),
                media_type="text/event-stream",
                headers=sse_headers,
            )

        @self.app.get(
            f"{prefix}/status/stream",
            tags=tags,
            summary="Health status stream",
            description="SSE (Server-Sent Events) stream of full probe results. Poll loop stops when last client disconnects.",
            dependencies=auth_deps,
        )
        async def status_stream(request: Request) -> StreamingResponse:
            return StreamingResponse(
                registry._event_stream(request, _make_sse_report),
                media_type="text/event-stream",
                headers=sse_headers,
            )

        @self.app.get(
            f"{prefix}/metrics",
            tags=tags,
            summary="Prometheus metrics",
            description=(
                "Probe health exported in Prometheus text format 0.0.4.  "
                "Scraped by Prometheus or any compatible agent without extra deps."
            ),
            dependencies=auth_deps,
        )
        async def prometheus_metrics() -> Response:
            results = await registry._get_results()
            trips = registry._circuit_trips if registry._circuit_breaker_enabled else {}
            return Response(
                content=render_prometheus(results, trips),
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )

        if dashboard is not False:
            if callable(dashboard):
                _renderer: Callable[..., str] = dashboard
            else:
                _stream_url = f"{prefix}/status/stream"

                def _renderer(report: HealthReport, maintenance: bool = False) -> str:
                    return render_dashboard(
                        report,
                        stream_url=_stream_url,
                        maintenance_banner=maintenance,
                    )

            @self.app.get(
                f"{prefix}/dashboard",
                tags=tags,
                summary="Health dashboard",
                description="Server-rendered HTML dashboard with live SSE updates.",
                response_class=HTMLResponse,
                dependencies=auth_deps,
            )
            async def health_dashboard() -> HTMLResponse:
                results = await registry._get_results()
                report = HealthReport.from_results(
                    results,
                    checked_at=registry._last_checked_at,
                    timezone=registry._timezone_name,
                )
                return HTMLResponse(content=_renderer(report, registry._in_maintenance()))
