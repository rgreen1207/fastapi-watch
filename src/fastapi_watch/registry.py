import asyncio
import base64
import fnmatch
import functools
import hashlib
import hmac
import inspect
import logging
import re
import secrets
import signal as _signal
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncGenerator, Awaitable, Callable, Iterator, Literal
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from pydantic_core import to_json

from .alerts import BaseAlerter, WebhookAlerter
from .dashboard import _current_probe, render_dashboard
from .models import AlertRecord, HealthReport, ProbeResult, ProbeStatus
from .prometheus import render_prometheus, validate_metrics_prefix
from .probe_group import ProbeGroup
from .probes.base import BaseProbe, _failure_message
from .storage import InMemoryProbeStorage, ProbeStorage


class _MaintenanceRequest(BaseModel):
    """Body of ``POST /health/maintenance``: a duration or an end time (both optional)."""

    minutes: float | None = Field(None, gt=0, le=525600)  # max 1 year
    until: datetime | None = None

class _DefaultInt(int):
    """An ``int`` parameter default, distinguishable from the same value passed explicitly."""


class _DefaultFloat(float):
    """A ``float`` parameter default, distinguishable from the same value passed explicitly."""


_MIN_POLL_INTERVAL_MS = 1000
_AUTO_HTTP_METHODS = frozenset({"HEAD", "OPTIONS"})
# Used when the caller passed no logger= but an error must still be reported.
_LIB_LOGGER = logging.getLogger("fastapi_watch")
# SSE: comment-line interval between events, and the reconnect delay hinted to
# browsers in single-fetch mode.
_SSE_KEEPALIVE_S = 15.0
_SINGLE_FETCH_RETRY_MS = 30_000
# Storage: returned by _storage_call when an operation fails or times out; a
# failing operation is logged at most once per interval; stored data of
# probes this process wrote but no longer registers is forgotten at most this often.
_STORAGE_FAILED: Any = object()
_STORAGE_LOG_INTERVAL_S = 60.0
_FORGET_INTERVAL_S = 600.0
# Background storage writes: alert records held while the backend is behind,
# the writer key for alerts, and how long shutdown lets pending writes land.
_MAX_UNWRITTEN_ALERTS = 256
_ALERT_WRITER: Any = object()
_WRITE_FLUSH_TIMEOUT_S = 2.0
# Shutdown: how long each probe's aclose() may take.
_ACLOSE_TIMEOUT_S = 5.0
# Retry-After (seconds) sent when max_stream_connections streams are open.
_STREAM_RETRY_AFTER_S = 30


class _SlotStreamingResponse(StreamingResponse):
    """A StreamingResponse that calls *release* once it has finished, however it ends."""

    def __init__(self, content, *, release: Callable[[], None], **kwargs) -> None:
        super().__init__(content, **kwargs)
        self._release = release

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._release()


def _iter_app_routes(routes, prefix: str = "") -> Iterator[tuple[Any, str]]:
    """Yield ``(route, prefix)`` for every concrete route, recursing into router containers.

    Handles both the flat layout (FastAPI <0.137) where include_router copies routes
    with their full paths into app.routes, and the nested layout (FastAPI 0.137+) where
    _IncludedRouter wrapper objects are stored instead.  Routes inside _IncludedRouter
    retain their short path (without the prefix), so *prefix* is the accumulated
    include prefix; ``_effective_path(route, prefix)`` gives the path a client requests.
    """
    for item in routes:
        if hasattr(item, "path"):
            yield item, prefix
            continue

        # FastAPI 0.137+: _IncludedRouter stores prefix in include_context.prefix.
        # Older wrappers may have a plain .prefix attribute.
        include_ctx = getattr(item, "include_context", None)
        ctx_prefix = getattr(include_ctx, "prefix", None) or getattr(item, "prefix", "")
        item_prefix = prefix + ctx_prefix

        # Try all known container attributes to find sub-routes.
        sub = None
        for attr in ("routes", "_routes"):
            sub = getattr(item, attr, None)
            if sub:
                break

        if sub is None:
            # Covers _IncludedRouter.original_router (FastAPI 0.137+) as well as
            # .router.* and .app.* used by older wrappers.
            for container_attr in ("original_router", "router", "_router", "app", "_app"):
                container = getattr(item, container_attr, None)
                if container is not None:
                    for sub_attr in ("routes", "_routes"):
                        sub = getattr(container, sub_attr, None)
                        if sub:
                            break
                if sub:
                    break

        if sub:
            yield from _iter_app_routes(sub, item_prefix)


def _effective_path(route, prefix: str) -> str:
    """Return the full effective path for a route given an accumulated prefix."""
    return prefix + getattr(route, "path", "")


def _invalidate_included_router_caches(routes) -> None:
    """Reset lazy route caches on _IncludedRouter objects (FastAPI 0.137+).

    After patching route.endpoint for monitoring, this forces the _IncludedRouter to
    rebuild its _EffectiveRouteContext list on the next request so it picks up the
    wrapped endpoint.  Safe to call on older FastAPI where these attributes do not
    exist (the hasattr guards make it a no-op).
    """
    for item in routes:
        orig = getattr(item, "original_router", None)
        if orig is None:
            continue
        # Prefer the official API if available; fall back to direct attribute reset.
        mark_changed = getattr(orig, "_mark_routes_changed", None)
        if callable(mark_changed):
            mark_changed()
        elif hasattr(item, "_effective_candidates_version"):
            item._effective_candidates_version = None


def _route_description(route, path: str, is_websocket: bool) -> str:
    """Return a human-readable route label: ``'GET /items/{id}'`` or ``'WS /ws/chat'``.

    *path* is the full effective path, not ``route.path``, which lacks the
    include prefix on FastAPI 0.137+.
    """
    if is_websocket:
        return f"WS {path}"
    all_methods = set(getattr(route, "methods", None) or set())
    meaningful = sorted(all_methods - _AUTO_HTTP_METHODS) or sorted(all_methods)
    return f"{' '.join(meaningful)} {path}"


def _name_slug(text: str) -> str:
    """Collapse characters a probe name may not contain (and ``:``) into ``_``.

    ``':'`` is excluded because it separates a handler name from its route
    qualifier (``list_items:GET_users``).
    """
    return re.sub(r"[^A-Za-z0-9_\-.]+", "_", text).strip("_")


def _is_health_handler(endpoint) -> bool:
    """True for route handlers that a HealthRegistry mounted itself.

    Identifies health routes regardless of prefix — including another
    registry's, or a registry mounted at the root where no prefix can tell
    them apart from application routes.
    """
    endpoint = getattr(endpoint, "__wrapped__", endpoint)
    return getattr(endpoint, "__qualname__", "").startswith(f"{HealthRegistry.__name__}._register_routes.")


def _endpoint_keys(route) -> set:
    """The route's endpoint and, if fastapi-watch wrapped it, the original function."""
    endpoint = getattr(route, "endpoint", None)
    if endpoint is None:
        return set()
    return {endpoint, getattr(endpoint, "__wrapped__", endpoint)}


_STREAM_TOKEN_TTL_S = 300


_STREAM_TOKEN_SALT = b"fastapi-watch/stream-token"


@functools.lru_cache(maxsize=8)
def _derive_stream_token_key(material: bytes) -> bytes:
    """Stretch credentials into a token-signing key (cached: scrypt costs ~50 ms).

    Tokens appear in URLs and so in access logs; with a fast hash, one logged
    token would let an attacker test password guesses offline at billions per
    second.  scrypt makes each guess cost what it costs here.  PBKDF2 stands in
    on the rare Python builds whose OpenSSL lacks scrypt.
    """
    scrypt = getattr(hashlib, "scrypt", None)
    if scrypt is not None:
        return scrypt(material, salt=_STREAM_TOKEN_SALT, n=2**14, r=8, p=1, dklen=32)
    return hashlib.pbkdf2_hmac("sha256", material, _STREAM_TOKEN_SALT, 600_000)


def _stream_token_key(auth: dict | Callable, secret: str | bytes | None = None) -> bytes:
    """Return the key that signs dashboard stream tokens.

    An explicit *secret* (``stream_token_secret=``) is used as-is.  Otherwise
    the key is derived from the configured credentials, so every worker process
    and replica sharing them accepts each other's tokens (the stream request may
    land on a different worker than the page did).  A callable *auth* has no
    shared secret, so without *secret* it gets a per-process random key: tokens
    then only validate on the worker that issued them.
    """
    if secret is not None:
        return secret.encode() if isinstance(secret, str) else bytes(secret)
    if isinstance(auth, dict):
        if auth.get("type") == "apikey":
            material = f"apikey\x00{auth['key']}"
        else:
            material = f"basic\x00{auth['username']}\x00{auth['password']}"
        return _derive_stream_token_key(material.encode())
    return secrets.token_bytes(32)


def _mint_stream_token(key: bytes) -> str:
    """Return ``"<expiry>.<hmac>"``, valid for ``_STREAM_TOKEN_TTL_S`` seconds."""
    expiry = str(int(time.time()) + _STREAM_TOKEN_TTL_S)
    return f"{expiry}.{hmac.new(key, expiry.encode(), hashlib.sha256).hexdigest()}"


def _valid_stream_token(key: bytes, token: str) -> bool:
    """True if *token* was signed with *key* and has not expired."""
    return _stream_token_expiry(key, token) is not None


def _stream_token_expiry(key: bytes, token: str) -> int | None:
    """Return the expiry (Unix time) of a valid, unexpired *token*, else ``None``."""
    expiry, _, signature = token.partition(".")
    if not expiry.isdigit():
        return None
    expected = hmac.new(key, expiry.encode(), hashlib.sha256).hexdigest()
    # Bytes: compare_digest raises TypeError on a str with non-ASCII characters,
    # which turned "?token=1.é" into a 500 for any unauthenticated client.
    if hmac.compare_digest(signature.encode(), expected.encode()) and int(expiry) >= time.time():
        return int(expiry)
    return None


def _takes_two_positional(fn: Callable) -> bool:
    """True if *fn* can be called with two positional arguments.

    When the signature cannot be inspected (some builtins and C extensions),
    assume it can — that is how every renderer was called before 1.7.2.
    """
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return True
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params):
        return True
    positional = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    return sum(p.kind in positional for p in params) >= 2


def _parse_csv_filter(value: str | None) -> frozenset[str] | None:
    """Parse a comma-separated ``?tag=`` / ``?probe=`` query param into a frozenset.

    Returns None for no filter — empty strings, whitespace-only values, and
    None are all treated that way.  At most 50 entries are considered.
    """
    if not value:
        return None
    parts = value.split(",")[:50]
    return frozenset(p.strip() for p in parts if p.strip()) or None


def _normalize_interval(ms: int | None) -> int | None:
    """Validate and normalize a poll interval.

    - ``None`` or ``0``  → ``None``  (single-fetch mode)
    - ``1 – 999``        → ``1000``  (clamped to minimum)
    - ``>= 1000``        → used as-is
    """
    if ms is None or ms == 0:
        return None
    return max(ms, _MIN_POLL_INTERVAL_MS)


def _validate_auth(auth) -> None:
    """Raise ``ValueError`` for an *auth* dict with missing or blank credentials.

    ``{"type": "apikey", "key": ""}`` would otherwise admit every request that
    simply omits the header (an absent header compares equal to ``""``).
    """
    if auth is None or callable(auth):
        return
    if not isinstance(auth, dict):
        raise ValueError(f"auth must be None, a callable, or a dict; got {type(auth)!r}")
    required = {"apikey": ("key",), "basic": ("username", "password")}.get(auth.get("type"))
    if required is None:
        raise ValueError(f"Unknown auth type {auth.get('type')!r}. Supported: 'basic', 'apikey'.")
    for field in required:
        value = auth.get(field)
        # The value itself is never echoed: it may be a real credential.
        if not isinstance(value, str):
            raise ValueError(f"auth {field!r} must be a non-empty string, got {type(value).__name__}")
        if not value.strip():
            raise ValueError(f"auth {field!r} must not be empty or whitespace")


def _origin_matches(origin: str, hosts: list[str]) -> bool:
    """True if *origin* (``scheme://host[:port]``) names one of *hosts* (``host[:port]``).

    A missing port means the scheme's default on both sides, so a TLS-terminating
    proxy that forwards ``Host: example.com`` still matches ``https://example.com``.
    """
    try:
        parts = urlsplit(origin)
        origin_port = parts.port
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False  # includes the opaque "null" origin
    default_port = 443 if parts.scheme == "https" else 80
    wanted = (parts.hostname.lower(), origin_port or default_port)
    for host in hosts:
        try:
            candidate = urlsplit(f"//{host.strip()}")
            candidate_port = candidate.port
        except ValueError:
            continue
        if candidate.hostname and (candidate.hostname.lower(), candidate_port or default_port) == wanted:
            return True
    return False


async def _reject_cross_site(request: Request) -> None:
    """Dependency for state-changing routes: refuse browser cross-site requests.

    Basic credentials are cached by the browser and sent with any request to
    the site, so without this a page elsewhere could ``<form method=post>`` to
    ``/health/maintenance`` and switch on indefinite maintenance.  Refused with
    403: ``Sec-Fetch-Site: cross-site``, or an ``Origin`` that does not match
    the request's ``Host`` (or ``X-Forwarded-Host``).  A non-empty body that is
    not ``application/json`` is refused with 415, which also rules out every
    body a form or no-cors fetch can send.  Requests from non-browser clients
    (curl, scripts) carry neither header and are unaffected.
    """
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise HTTPException(status_code=403, detail="cross-site request refused")
    origin = request.headers.get("origin")
    if origin is not None:
        hosts = [request.headers.get("host", "")]
        forwarded = request.headers.get("x-forwarded-host")
        if forwarded:
            hosts.append(forwarded.split(",")[0])
        if not _origin_matches(origin, hosts):
            raise HTTPException(status_code=403, detail="cross-origin request refused")
    if await request.body():
        media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if media_type != "application/json":
            raise HTTPException(status_code=415, detail="request body must be application/json")


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
            if result is not True:
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
            # Compare both before combining: short-circuiting on the username
            # would let response timing reveal whether it was correct.
            user_ok = secrets.compare_digest(user.encode(), expected_user)
            pass_ok = secrets.compare_digest(pwd.encode(), expected_pass)
            if not (user_ok and pass_ok):
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

    - ``GET    /health/live``          — liveness; always 200
    - ``GET    /health/ready``         — readiness; 200 if all critical probes pass, 503 otherwise
    - ``GET    /health/status``        — full probe detail; 200 / 207 (Multi-Status)
    - ``GET    /health/history``       — rolling probe result history
    - ``GET    /health/startup``       — startup check; 503 until :meth:`set_started` is called
    - ``GET    /health/dashboard``     — HTML dashboard with live SSE (Server-Sent Events) updates
    - ``GET    /health/ready/stream``  — SSE (Server-Sent Events) stream of readiness
    - ``GET    /health/status/stream`` — SSE (Server-Sent Events) stream of full probe detail;
      ``?view=dashboard`` slims routes to what the built-in dashboard charts
    - ``GET    /health/maintenance``   — current maintenance mode status
    - ``POST   /health/maintenance``   — enable maintenance mode (optional body: ``minutes`` or ``until``);
      mounted only when *auth* is set
    - ``DELETE /health/maintenance``   — disable maintenance mode; mounted only when *auth* is set
    - ``GET    /health/probes``        — probe introspection; lists registered probes without running them

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
        grace_period_ms: Hold ``/health/ready`` at ``503 {"status": "starting"}``
            for this many ms, measured from when the registry is created
            (affects ``/health/ready`` only).
        history_size: Number of past results kept per probe (default 120).
            With a custom *storage*, the retention arguments (this,
            *result_ttl_seconds*, *alert_ttl_seconds*, *max_alerts*) reach the
            backend only when at least one is passed explicitly: then its
            ``configure()`` receives all four effective values (defaults for
            the ones not passed).  Otherwise the backend keeps the values it
            was constructed with.  A backend without ``configure()`` always
            keeps its own limits, and a warning names any ignored arguments.
        groups: :class:`~fastapi_watch.ProbeGroup` instances to merge at startup.
        dashboard: ``True`` (default) — built-in HTML dashboard.  ``False`` — omit the
            route.  Callable ``(report: HealthReport) -> str`` — custom renderer;
            a callable taking a second positional argument also receives the
            maintenance flag, ``(report, maintenance) -> str``.
            ``str`` or ``Path`` — path to a ``.html`` or ``.htm`` file served as-is.
        serve_routes: ``True`` (default) — register all health endpoints on the app.
            ``False`` — skip route registration entirely.  Use :meth:`get_report`
            to expose health status on your own endpoint with custom auth or path.
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
            or other implementation here for persistence across restarts.  If it
            defines ``forget(names)``, data of probes this process wrote and no
            longer registers is deleted at most every 10 minutes; names it never
            wrote are never touched, so apps and replicas can share a backend.
        webhook_url: HTTP(S) URL that receives a JSON ``POST`` whenever a probe
            changes state.  The call is fire-and-forget and never blocks health checks.
            Payload: ``{"probe", "old_status", "new_status", "timestamp"}``.
        auth: Authentication for all health endpoints.  ``None`` (default) = open.
            See :func:`_make_auth_checker` for accepted forms.
        startup_probes: Probes that must pass for ``/health/startup`` to return 200.
            These are separate from the main probe registry and are not shown in
            ``/health/status``.
        probe_timeout: Seconds a probe may run before it is reported UNHEALTHY
            (default ``30``).  Applies to every probe that does not set its own
            ``timeout``.  Every health endpoint waits on the probes it runs, so
            this bound is what keeps one hung dependency from hanging them all.
            ``None`` disables the default (probes without a ``timeout`` may then
            run indefinitely).
        metrics_prefix: Prepended to every metric name on ``/metrics``
            (default ``""``, keeping ``probe_healthy`` etc.).  Those names
            collide with blackbox_exporter's ``probe_*`` metrics; set e.g.
            ``"fastapi_watch_"`` to namespace them.
        storage_timeout: Seconds each storage operation may take (keyword-only,
            default ``2.0``; ``None`` disables).  A storage operation that
            fails or times out is logged (at most once a minute per operation)
            and never fails a health check: ``/ready``, ``/status``,
            ``/metrics`` and the streams are served from this process's last
            results, ``/history`` and ``/alerts`` return 503, and the
            dashboard renders without history.
        max_stream_connections: Most SSE stream connections open at once
            (keyword-only, default ``100``; ``None`` = unlimited).  Further
            stream requests get ``503`` with a ``Retry-After`` header.
        alert_min_interval_s: Flap limiting (keyword-only, default ``0`` = off).
            At most one alert per probe is sent to alerters per this many
            seconds.  The first change after a quiet interval alerts at once;
            later changes inside the interval are coalesced, and when it ends
            the probe's latest status is sent if it differs from the last one
            sent — so the final state is never lost, and a flap that ends
            where it started sends nothing.  Callbacks and stored alert
            records still see every transition.
        alert_dedupe_window_s: Cross-replica alert dedupe (keyword-only,
            default ``None`` = off).  With a shared storage backend that
            implements ``claim_alert(key, ttl_seconds)``, replicas claim each
            ``"<probe>:<new_status>"`` alert for this many seconds and only
            the winner sends it.  A failing claim sends anyway.  Without
            ``claim_alert`` a warning is logged at init and alerts are not
            deduplicated.
        stream_token_secret: Key that signs the dashboard's short-lived stream
            tokens (keyword-only).  By default the key is derived from the
            *auth* credentials with scrypt, or is random per process for a
            callable *auth*; pass the same secret to every worker and replica to
            make a callable *auth*'s tokens valid across all of them.

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
        history_size: int = _DefaultInt(120),
        timezone: str = "UTC",
        groups: list[ProbeGroup] | None = None,
        dashboard: bool | Callable[..., str] | str | Path = True,
        serve_routes: bool = True,
        circuit_breaker: bool = True,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_cooldown_ms: int = 600_000,
        webhook_url: str | None = None,
        alerters: list[BaseAlerter] | None = None,
        auth: dict | Callable | None = None,
        startup_probes: list[BaseProbe] | None = None,
        result_ttl_seconds: float = _DefaultFloat(7200.0),
        alert_ttl_seconds: float = _DefaultFloat(259200.0),
        max_alerts: int = _DefaultInt(120),
        storage: ProbeStorage | None = None,
        probe_timeout: float | None = 30.0,
        metrics_prefix: str = "",
        *,
        stream_token_secret: str | bytes | None = None,
        storage_timeout: float | None = 2.0,
        max_stream_connections: int | None = 100,
        alert_min_interval_s: float = 0.0,
        alert_dedupe_window_s: float | None = None,
    ) -> None:
        if probe_timeout is not None and probe_timeout <= 0:
            raise ValueError(f"probe_timeout must be positive or None, got {probe_timeout!r}")
        if storage_timeout is not None and storage_timeout <= 0:
            raise ValueError(f"storage_timeout must be positive or None, got {storage_timeout!r}")
        if max_stream_connections is not None and max_stream_connections < 1:
            raise ValueError(f"max_stream_connections must be at least 1 or None, got {max_stream_connections!r}")
        if alert_min_interval_s < 0:
            raise ValueError(f"alert_min_interval_s must be zero or positive, got {alert_min_interval_s!r}")
        if alert_dedupe_window_s is not None and alert_dedupe_window_s <= 0:
            raise ValueError(f"alert_dedupe_window_s must be positive or None, got {alert_dedupe_window_s!r}")
        _validate_auth(auth)
        if stream_token_secret is not None and not stream_token_secret:
            raise ValueError("stream_token_secret must not be empty")
        self.app = app
        self.prefix = prefix
        self._logger = logger
        self._probe_timeout = probe_timeout
        self._metrics_prefix = validate_metrics_prefix(metrics_prefix)
        self._grace_period_ms: int = max(0, grace_period_ms)
        self._timezone_name: str = timezone
        self._tzinfo: ZoneInfo = ZoneInfo(timezone)
        self._start_time: datetime = datetime.now(self._tzinfo)
        self._history_size: int = int(max(1, history_size))
        retention = {
            "max_results": self._history_size,
            "result_ttl_seconds": float(result_ttl_seconds),
            "alert_ttl_seconds": float(alert_ttl_seconds),
            "max_alerts": int(max(1, max_alerts)),
        }
        self._storage_timeout = storage_timeout
        # op name -> (monotonic time last logged, failures suppressed since)
        self._storage_failures: dict[str, tuple[float, int]] = {}
        if storage is None:
            self._storage: ProbeStorage = InMemoryProbeStorage(**retention)
        else:
            self._storage = storage
            self._configure_storage(retention, {
                "history_size": history_size,
                "result_ttl_seconds": result_ttl_seconds,
                "alert_ttl_seconds": alert_ttl_seconds,
                "max_alerts": max_alerts,
            })
        self._probes: list[tuple[BaseProbe, bool]] = []
        self._poll_interval_ms: int | None = self._set_interval(poll_interval_ms)

        # Per-probe scheduling
        self._probe_last_run: dict[str, float] = {}
        self._last_checked_at: datetime | None = None
        # Last result per probe, as checked by this process.  Health endpoints
        # are served from here, so they reflect this process's own checks and
        # keep working while storage is slow or down.  One entry per probe name.
        self._last_results: dict[str, ProbeResult] = {}
        # id(probe) -> its in-flight run, shared by concurrent callers
        self._inflight: dict[int, asyncio.Task] = {}
        # Writes to a backend other than the default in-memory one run in the
        # background (see _store_result): the newest result per probe and the
        # alert records not yet written, and one writer task per probe name
        # (plus _ALERT_WRITER).  Bounded by the number of probes.
        self._unwritten: dict[str, ProbeResult] = {}
        self._unwritten_alerts: deque[AlertRecord] = deque(maxlen=_MAX_UNWRITTEN_ALERTS)
        self._writers: dict[Any, asyncio.Task] = {}
        # Probe names this process has written to storage.  Bounded: only names
        # it registered.  Those it stops registering are forgotten in storage
        # (monotonic time of the last pass).
        self._written_names: set[str] = set()
        self._last_forget: float | None = None
        self._forget_task: asyncio.Task | None = None

        # Circuit breaker state
        self._circuit_breaker_enabled: bool = circuit_breaker
        self._circuit_threshold: int = circuit_breaker_threshold
        self._circuit_cooldown_ms: int = circuit_breaker_cooldown_ms
        self._circuit_open_until: dict[str, float] = {}
        self._circuit_err_count: dict[str, int] = {}
        self._circuit_trips: dict[str, int] = {}  # lifetime trip count per probe

        # Maintenance mode
        self._maintenance_active: bool = False
        self._maintenance_until: datetime | None = None

        # Alerters — webhook_url kept for backwards compat, wraps into WebhookAlerter
        self._alerters: list[BaseAlerter] = list(alerters or [])
        if webhook_url:
            self._alerters.append(WebhookAlerter(url=webhook_url))

        # Auth.  The stream-token key is derived once, here: scrypt is slow by design.
        self._auth = auth
        self._stream_token_key: bytes | None = (
            _stream_token_key(auth, stream_token_secret) if auth is not None and serve_routes else None
        )

        # Startup
        self._started: bool = False
        self._startup_probes: list[BaseProbe] = list(startup_probes or [])

        # State-change callbacks and streaming
        self._probe_states: dict[str, ProbeStatus] = {}
        # Last status handed to the alerters, per probe.  Differs from
        # _probe_states after maintenance, which suppresses alerters only.
        self._alerted_states: dict[str, ProbeStatus] = {}
        self._state_change_callbacks: list[
            Callable[[str, ProbeStatus, ProbeStatus], None | Awaitable[None]]
        ] = []
        self._poll_task: asyncio.Task | None = None
        self._active_connections: int = 0
        # Open SSE responses, counted from the request (not the generator start).
        self._max_stream_connections: int | None = max_stream_connections
        self._stream_slots: int = 0
        self._shutting_down: bool = False
        self._sse_tasks: set[asyncio.Task] = set()
        self._signal_handler_installed: bool = False

        # Bounded alert queue — avoids unbounded create_task() per state-change.
        # A None item only wakes the worker to re-check coalesced alerts.
        self._alert_queue: asyncio.Queue[AlertRecord | None] = asyncio.Queue(maxsize=256)
        self._alert_worker: asyncio.Task | None = None

        # Flap limiting: monotonic time of each probe's last alert, and the
        # time a coalesced alert falls due (one marker per probe at most).
        self._alert_min_interval_s: float = float(alert_min_interval_s)
        self._alert_last_sent_at: dict[str, float] = {}
        self._alert_pending: dict[str, float] = {}

        # Cross-replica dedupe through the storage's optional claim_alert().
        # InMemoryProbeStorage implements it, but its claims are process-local,
        # so it cannot stop other replicas from sending the same alert.
        self._alert_dedupe_window_s: float | None = alert_dedupe_window_s
        process_local = getattr(type(self._storage), "claim_alert", None) is InMemoryProbeStorage.claim_alert
        if alert_dedupe_window_s is not None and (
            process_local or not callable(getattr(self._storage, "claim_alert", None))
        ):
            (self._logger or _LIB_LOGGER).warning(
                "fastapi-watch: alert_dedupe_window_s needs a shared storage backend that implements "
                "claim_alert(); %s does not, so every replica sends its own alerts.",
                "in-process storage" if process_local else type(self._storage).__name__,
            )
            self._alert_dedupe_window_s = None

        if serve_routes:
            self._register_routes(tags or ["health"], dashboard=dashboard)
        self.app.router.on_shutdown.append(self._shutdown)
        for group in groups or []:
            self.include(group)

        if auth is None and serve_routes:
            # Falls back to the library logger: a security notice must not depend
            # on the caller having passed logger=.
            (self._logger or _LIB_LOGGER).warning(
                "fastapi-watch: health endpoints are publicly accessible (auth=None). "
                "POST/DELETE %s/maintenance are not mounted without auth; use "
                "set_maintenance()/clear_maintenance() or pass auth= to HealthRegistry.",
                prefix,
            )

    def _configure_storage(self, retention: dict, passed: dict) -> None:
        """Apply explicitly passed retention arguments to a caller-supplied *storage*.

        Without this, ``HealthRegistry(storage=s, history_size=5)`` silently
        kept the backend's own limits.  A backend with ``configure()`` gets all
        four effective values; one without it keeps its settings, and the
        ignored arguments are named in a warning.  Nothing happens when no
        retention argument was passed, so a backend configured in its own
        constructor is left alone.
        """
        explicit = [name for name, value in passed.items() if not isinstance(value, (_DefaultInt, _DefaultFloat))]
        if not explicit:
            return
        configure = getattr(self._storage, "configure", None)
        if callable(configure):
            configure(**retention)
            return
        (self._logger or _LIB_LOGGER).warning(
            "fastapi-watch: %s has no configure() method, so %s %s ignored; set retention "
            "on the storage backend itself.",
            type(self._storage).__name__,
            ", ".join(explicit),
            "is" if len(explicit) == 1 else "are",
        )

    # ------------------------------------------------------------------
    # Public API — probe registration
    # ------------------------------------------------------------------

    def include(self, group: ProbeGroup) -> "HealthRegistry":
        """Include all probes from a :class:`~fastapi_watch.ProbeGroup`. Returns ``self``.

        Group-level tags are merged into each probe's own tags (probe tags first,
        group tags appended, duplicates removed).
        """
        for probe, critical in group._probes:
            if group.tags and hasattr(probe, "tags"):
                existing = list(probe.tags or [])
                probe.tags = list(dict.fromkeys(existing + group.tags))
            self.add(probe, critical=critical)
        return self

    def add(self, probe: BaseProbe, critical: bool = True) -> "HealthRegistry":
        """Add a single probe. Returns ``self`` for chaining.

        Silently skips the probe if it is already registered (identity check).
        Logs a warning when a *different* probe already uses the same name:
        results, history, alerts, and circuit-breaker state are keyed by name,
        so the two will overwrite each other.  (A warning rather than an error,
        so existing apps keep starting; give each probe a unique name.)

        Raises:
            ValueError: if the name contains characters outside ``[A-Za-z0-9_\\-.:]``.
        """
        self._check_probe_name(probe.name)
        if any(p is probe for p, _ in self._probes):
            return self
        if any(p.name == probe.name for p, _ in self._probes):
            (self._logger or _LIB_LOGGER).warning(
                "fastapi-watch: a different probe is already registered as %r; their results "
                "will overwrite each other. Give each probe a unique name.",
                probe.name,
            )
        self._probes.append((probe, critical))
        return self

    @staticmethod
    def _check_probe_name(name: str) -> None:
        """Raise ``ValueError`` unless *name* uses only the allowed characters."""
        # fullmatch, not match with "$": "$" also matches before a trailing newline.
        if not re.fullmatch(r"[A-Za-z0-9_\-.:]+", name):
            raise ValueError(
                f"Probe name {name!r} contains invalid characters. "
                "Names must contain only letters, digits, hyphens, underscores, dots, and colons."
            )

    def add_probes(self, probes: list[BaseProbe], critical: bool = True) -> "HealthRegistry":
        """Add a list of probes. Returns ``self`` for chaining."""
        for probe in probes:
            self.add(probe, critical=critical)
        return self

    def reset_circuit(self, probe_name: str) -> "HealthRegistry":
        """Manually reset the circuit breaker for a probe, clearing its open state and failure count.

        Returns self for chaining.
        """
        self._circuit_open_until.pop(probe_name, None)
        self._circuit_err_count.pop(probe_name, None)
        return self

    def discover_routes(
        self,
        *,
        exclude_paths: list[str] | None = None,
        include_paths: list[str] | None = None,
        include_methods: list[str] | None = None,
        critical: bool = True,
        tags: list[str] | None = None,
        ws_probe_kwargs: dict | None = None,
        refresh: bool = False,
        name_fn: "Callable | None" = None,
        **probe_kwargs,
    ) -> "HealthRegistry":
        """Automatically monitor all registered FastAPI routes.

        Scans the app's route table and wraps each route handler with a
        :class:`~fastapi_watch.probes.route.FastAPIRouteProbe` (HTTP) or
        :class:`~fastapi_watch.probes.websocket.FastAPIWebSocketProbe` (WebSocket).
        Call this after all routes are registered (e.g. at the end of your app
        module or inside a lifespan startup hook).

        Routes mounted by any HealthRegistry are always excluded.  Path patterns
        match the full path a client requests, including ``include_router`` prefixes.
        FastAPI route tags (used for OpenAPI docs) are automatically merged into
        probe tags; *tags* adds extra labels on top of those.

        Args:
            exclude_paths: Route paths to skip; supports fnmatch glob patterns
                (e.g. ``["/internal/*", "/admin/*"]``).
            include_paths: Whitelist of route paths to monitor; supports fnmatch
                glob patterns (e.g. ``["/api/*"]``).  When provided, only routes
                matching at least one pattern are monitored.  ``exclude_paths``
                is applied after and takes precedence.
            include_methods: Only monitor routes whose HTTP method is in this
                list (e.g. ``["GET", "POST"]``).  WebSocket routes are always
                included.  Defaults to all methods.
            critical: Whether the auto-created probes are critical (default ``True``).
            tags: Extra tags attached to every auto-created probe, in addition to
                any FastAPI route tags already on each route.
            ws_probe_kwargs: Extra keyword arguments forwarded to each
                :class:`~fastapi_watch.probes.websocket.FastAPIWebSocketProbe`
                (e.g. ``{"min_active_connections": 1}``).
            refresh: Re-monitor routes that were previously auto-discovered.
                Useful when routes are added dynamically or when options change.
                ``@probe.watch`` and ``watch_router`` monitoring is never
                overridden even with ``refresh=True``.
            name_fn: Optional callable ``(route) -> str`` to customise probe names.
                Receives the FastAPI route object and returns the desired probe name.
                Defaults to ``route.name`` (the handler function name); when two
                routes share one, later ones are qualified with their methods and
                path (e.g. ``list_items:GET_orders``).
            **probe_kwargs: Extra keyword arguments forwarded to each
                :class:`~fastapi_watch.probes.route.FastAPIRouteProbe`
                (e.g. ``max_error_rate=0.05``).

        Returns ``self`` for chaining.
        """
        self._monitor_routes(
            list(_iter_app_routes(self.app.routes)),
            marker="discover",
            refresh=refresh,
            exclude_paths=exclude_paths,
            include_paths=include_paths,
            include_methods=include_methods,
            critical=critical,
            tags=tags,
            ws_probe_kwargs=ws_probe_kwargs,
            name_fn=name_fn,
            probe_kwargs=probe_kwargs,
        )
        return self

    def watch_router(
        self,
        router,
        *,
        tags: list[str] | None = None,
        critical: bool = True,
        exclude_paths: list[str] | None = None,
        include_paths: list[str] | None = None,
        include_methods: list[str] | None = None,
        ws_probe_kwargs: dict | None = None,
        name_fn: "Callable | None" = None,
        group: bool = False,
        **probe_kwargs,
    ) -> "HealthRegistry":
        """Monitor all routes belonging to a specific ``APIRouter``.

        Like :meth:`discover_routes` but scoped to a single router — useful
        when you want different tags, thresholds, or criticality per router.
        Call it after ``app.include_router(router)`` so that FastAPI has
        already baked the routes (with their final paths) into ``app.routes``.
        Both HTTP and WebSocket routes are monitored automatically.

        FastAPI route tags are merged into probe tags; *tags* adds extra labels
        on top of those.

        Priority: ``@probe.watch`` (highest) > ``watch_router`` > ``discover_routes``.
        Routes already monitored by ``@probe.watch`` or a previous
        ``watch_router`` call are silently skipped.  Routes previously
        monitored by ``discover_routes`` are replaced and the old probe
        removed from the registry.

        Args:
            router: The ``APIRouter`` whose routes should be monitored.
            tags: Extra tags attached to every probe, in addition to any FastAPI
                route tags already on each route.
            critical: Whether the auto-created probes are critical (default ``True``).
            exclude_paths: Route paths to skip; supports fnmatch glob patterns
                (e.g. ``["/admin/*"]``).
            include_paths: Whitelist of route paths to monitor; supports fnmatch
                glob patterns (e.g. ``["/users/*/profile"]``).  When provided,
                only matching routes are monitored.  ``exclude_paths`` takes
                precedence when both are set.
            include_methods: Only monitor routes whose HTTP method is in this
                list.  WebSocket routes are always included.
            ws_probe_kwargs: Extra keyword arguments forwarded to each
                :class:`~fastapi_watch.probes.websocket.FastAPIWebSocketProbe`.
            name_fn: Optional callable ``(route) -> str`` to customise probe names.
                Receives the FastAPI route object and returns the desired probe name.
                Defaults to ``route.name`` (the handler function name); when two
                routes share one, later ones are qualified with their methods and
                path (e.g. ``list_items:GET_orders``).
            group: When ``True``, all probes created for this router are collected
                into a :class:`~fastapi_watch.ProbeGroup` before being registered,
                so group-level tags propagate to every member probe.
                Defaults to ``False``.
            **probe_kwargs: Forwarded to each
                :class:`~fastapi_watch.probes.route.FastAPIRouteProbe`.

        Returns ``self`` for chaining.

        Example::

            app.include_router(users_router, prefix="/users")
            app.include_router(orders_router, prefix="/orders")

            registry.watch_router(users_router, tags=["users"], max_error_rate=0.05)
            registry.watch_router(orders_router, tags=["orders"], critical=False)

            # Monitor only write endpoints for the orders router
            registry.watch_router(orders_router, include_methods=["POST", "PUT", "DELETE"])
        """
        router_endpoints = set().union(*(_endpoint_keys(r) for r in getattr(router, "routes", [])))
        # Primary: the app's own route table, which carries the include prefix.
        # Fallback: router.routes for anything the primary pass could not reach
        # (they are the serving objects on FastAPI 0.137+, but carry no prefix).
        candidates = [
            (route, prefix) for route, prefix in _iter_app_routes(self.app.routes)
            if _endpoint_keys(route) & router_endpoints
        ]
        reached = set().union(*(_endpoint_keys(r) for r, _ in candidates))
        candidates += [
            (route, "") for route in getattr(router, "routes", [])
            if _endpoint_keys(route) and not (_endpoint_keys(route) & reached)
        ]
        probe_group = ProbeGroup(tags=list(tags) if tags else None) if group else None
        self._monitor_routes(
            candidates,
            marker="router",
            refresh=True,
            exclude_paths=exclude_paths,
            include_paths=include_paths,
            include_methods=include_methods,
            critical=critical,
            tags=tags,
            ws_probe_kwargs=ws_probe_kwargs,
            name_fn=name_fn,
            probe_kwargs=probe_kwargs,
            probe_group=probe_group,
        )
        if probe_group is not None:
            self.include(probe_group)
        return self

    def _monitor_routes(
        self,
        candidates: "list[tuple[Any, str]]",
        *,
        marker: str,
        refresh: bool,
        exclude_paths: list[str] | None,
        include_paths: list[str] | None,
        include_methods: list[str] | None,
        critical: bool,
        tags: list[str] | None,
        ws_probe_kwargs: dict | None,
        name_fn: "Callable | None",
        probe_kwargs: dict,
        probe_group: ProbeGroup | None = None,
    ) -> None:
        """Wrap each candidate ``(route, include_prefix)`` with a route probe.

        Shared by :meth:`discover_routes` (``marker="discover"``) and
        :meth:`watch_router` (``marker="router"``).  Paths are matched on the
        full effective path.  ``@probe.watch`` and ``watch_router`` monitoring
        is never replaced; a ``discover_routes`` probe is replaced when
        *refresh* is set.  Auto-generated names that collide are qualified with
        the route's methods and path so every route keeps its own result.
        """
        from fastapi.routing import APIRoute
        from .probes.route import FastAPIRouteProbe
        from .probes.websocket import FastAPIWebSocketProbe

        try:
            from fastapi.routing import APIWebSocketRoute
        except ImportError:
            APIWebSocketRoute = None

        methods_filter = {m.upper() for m in include_methods} if include_methods else None
        extra_tags = list(tags) if tags else []
        taken = {p.name for p, _ in self._probes}
        seen: set[int] = set()

        for route, prefix in candidates:
            is_ws = APIWebSocketRoute is not None and isinstance(route, APIWebSocketRoute)
            if (not isinstance(route, APIRoute) and not is_ws) or id(route) in seen:
                continue
            seen.add(id(route))
            if _is_health_handler(getattr(route, "endpoint", None)):
                continue
            path = _effective_path(route, prefix)
            if include_paths and not any(fnmatch.fnmatch(path, pat) for pat in include_paths):
                continue
            if any(fnmatch.fnmatch(path, pat) for pat in exclude_paths or []):
                continue
            if methods_filter is not None and not is_ws and not set(route.methods or ()) & methods_filter:
                continue

            current = getattr(route.dependant, "call", None)
            existing = getattr(current, "_fastapi_watch", None)
            replaced = None
            if existing in ("manual", "router"):
                continue
            if existing == "discover":
                if not refresh:
                    continue
                replaced = getattr(current, "_fastapi_watch_probe", None)

            description = _route_description(route, path, is_ws)
            if name_fn is not None:
                name = name_fn(route)
            else:
                name = route.name or _name_slug(path) or "root"
                if name in taken and (replaced is None or replaced.name != name):
                    name = f"{name}:{_name_slug(description)}"
            # Validate before touching the route, so a bad name leaves it unpatched.
            self._check_probe_name(name)

            probe_tags = list(dict.fromkeys(list(getattr(route, "tags", None) or []) + extra_tags))
            if is_ws:
                probe = FastAPIWebSocketProbe(name=name, description=description, tags=probe_tags, **(ws_probe_kwargs or {}))
            else:
                probe = FastAPIRouteProbe(name=name, description=description, tags=probe_tags, **probe_kwargs)

            # Watch the original function so re-monitoring never double-wraps.
            wrapped = probe.watch(getattr(route.endpoint, "__wrapped__", route.endpoint))
            try:
                wrapped._fastapi_watch = marker
                wrapped._fastapi_watch_probe = probe
                route.dependant.call = wrapped
                # FastAPI 0.137+ builds its request context from route.endpoint.
                route.endpoint = wrapped
            except AttributeError:
                continue

            if replaced is not None:
                self._probes = [(p, c) for p, c in self._probes if p is not replaced]
                taken.discard(replaced.name)
            taken.add(name)
            if probe_group is not None:
                probe_group.add(probe, critical=critical)
            else:
                self.add(probe, critical=critical)

        # FastAPI 0.137+: reset lazy _IncludedRouter caches so the next request
        # rebuilds its route context with the patched endpoints.
        _invalidate_included_router_caches(self.app.routes)

    def set_started(self) -> "HealthRegistry":
        """Mark the application as fully initialised.

        Call this from your lifespan startup after dependencies are ready.
        Until this is called ``GET /health/startup`` returns 503.

        This is also the correct place to install the early-shutdown signal hook.
        Lifespan startup runs after uvicorn's ``install_signal_handlers()``, so
        reading the existing SIGTERM handler here is race-free.  Calling it from
        ``_on_connect()`` instead risks a race where the browser reconnects before
        uvicorn re-registers its handler in the new worker, leaving our hook with
        no chain and uvicorn's subsequent ``add_signal_handler`` overwriting it.

        Returns ``self`` for chaining.
        """
        self._started = True
        if not self._signal_handler_installed:
            self._signal_handler_installed = True
            self._install_early_shutdown_hook()
        self._ensure_background_polling()
        return self

    def set_maintenance(self, until: datetime | None = None) -> "HealthRegistry":
        """Enter maintenance mode.

        While active, ``GET /health/ready`` returns ``200 {"status": "maintenance"}``
        and state-change webhooks are suppressed.  The dashboard shows a
        maintenance banner.

        Args:
            until: When maintenance ends.  ``None`` means indefinite —
                call :meth:`clear_maintenance` to exit.  A naive datetime is
                interpreted in the registry's *timezone*.

        Returns ``self`` for chaining.
        """
        self._maintenance_active = True
        self._maintenance_until = self._aware(until)
        return self

    def _aware(self, moment: datetime | None) -> datetime | None:
        """Attach the registry timezone to a naive datetime; pass others through.

        Naive values cannot be compared with the aware "now" used by
        :meth:`_in_maintenance` — left as-is they raise on every readiness check.
        """
        if moment is not None and moment.tzinfo is None:
            return moment.replace(tzinfo=self._tzinfo)
        return moment

    def clear_maintenance(self) -> "HealthRegistry":
        """Exit maintenance mode immediately. Returns ``self`` for chaining."""
        self._maintenance_active = False
        self._maintenance_until = None
        return self

    def _in_maintenance(self) -> bool:
        """Return True if the registry is currently in maintenance mode."""
        if not self._maintenance_active:
            return False
        if self._maintenance_until is None:
            return True  # indefinite
        if datetime.now(self._tzinfo) < self._maintenance_until:
            return True
        # Window has elapsed — auto-clear
        self._maintenance_active = False
        self._maintenance_until = None
        return False

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

        ``0`` / ``None`` switches to single-fetch mode and clears this process's
        cached results.  Values 1–999 are clamped to 1000 ms.
        """
        self._poll_interval_ms = self._set_interval(ms)
        if self._poll_interval_ms is None:
            # Requests are served from these in-process results, so clearing them
            # is enough.  Storage is left alone: storage.clear_latest() would also
            # delete the latest results of other apps and replicas sharing it.
            self._last_results.clear()
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

    async def get_report(self) -> HealthReport:
        """Return the current :class:`~fastapi_watch.models.HealthReport`.

        Intended for use with ``serve_routes=False`` — lets you expose health status
        on your own endpoint with custom auth, path, or response shaping::

            registry = HealthRegistry(app, serve_routes=False)

            @app.get("/internal/health", dependencies=[Depends(my_auth)])
            async def my_health():
                report = await registry.get_report()
                return report.model_dump()
        """
        results = await self._get_results()
        return HealthReport.from_results(
            results,
            checked_at=self._last_checked_at,
            timezone=self._timezone_name,
        )

    async def _execute_probes(self, pairs: list[tuple[BaseProbe, bool]]) -> list[ProbeResult]:
        """Run a subset of probes concurrently and return their results.

        A probe already being checked is not started again: callers join the
        run in flight.  Without this, concurrent requests (readiness checks,
        dashboards, the poll loop) each re-ran every due probe against the same
        dependency, and a slower, older run could land last and overwrite a
        newer result.  ``shield`` keeps one caller's cancellation (e.g. a client
        disconnecting) from cancelling a run other callers are waiting on.
        """
        if not pairs:
            return []
        return list(await asyncio.gather(*(asyncio.shield(self._shared_run(p, c)) for p, c in pairs)))

    def _shared_run(self, probe: BaseProbe, critical: bool) -> "asyncio.Task[ProbeResult]":
        """Return the in-flight run of *probe*, starting one if none exists."""
        key = id(probe)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._run_probe(probe, critical))
            self._inflight[key] = task

            def _forget(done: asyncio.Task) -> None:
                if self._inflight.get(key) is done:
                    del self._inflight[key]

            task.add_done_callback(_forget)
        return task

    async def _run_probe(self, probe: BaseProbe, critical: bool) -> ProbeResult:
        """Check one probe, then record its result, history, and state change exactly once.

        The result is kept in-process first, so it is reported even when the
        storage writes fail, and the writes never delay it (see
        :meth:`_store_result`).  A cached result served while the circuit is
        open is not written again: it is already the stored latest result, and
        re-appending it would fill history with copies of one stale entry.
        """
        result, from_cache = await self._check_or_cached(probe, critical)
        checked_at = datetime.now(self._tzinfo)
        # A cached result served while the circuit is open keeps its own
        # checked_at: stamping it "now" would make a stale result look fresh.
        updates: dict = {} if from_cache else {"checked_at": checked_at}
        if getattr(probe, "description", None) is not None and result.description is None:
            updates["description"] = probe.description
        probe_tags = getattr(probe, "tags", None) or []
        if probe_tags and not result.tags:
            updates["tags"] = probe_tags
        if updates:
            result = result.model_copy(update=updates)
        self._last_results[probe.name] = result
        self._probe_last_run[probe.name] = asyncio.get_running_loop().time()
        if from_cache:
            await self._fire_state_changes([result])
            return result
        self._last_checked_at = checked_at
        if result.name == probe.name:  # only ever forget a name both registered and written here
            self._written_names.add(result.name)
        await asyncio.gather(self._store_result(result), self._fire_state_changes([result]))
        return result

    def _writes_inline(self) -> bool:
        """True for the default in-memory backend, which cannot block and so is written inline."""
        return type(self._storage) is InMemoryProbeStorage

    async def _store_result(self, result: ProbeResult) -> None:
        """Write *result* as its probe's latest result and append it to history.

        Any backend other than the default in-memory one is written in the
        background: even bounded by ``storage_timeout``, a hung store would
        otherwise add that timeout to every health check that runs a probe,
        failing readiness probes with a shorter timeout (Kubernetes defaults
        to 1 s) because the *monitoring store* is down.  Each probe has at
        most one writer; while the store is behind, only the newest unwritten
        result per probe is kept.
        """
        if self._writes_inline():
            await asyncio.gather(
                self._storage_call("set_latest", result),
                self._storage_call("append_history", result),
            )
            return
        self._unwritten[result.name] = result
        self._start_writer(result.name, self._drain_results)

    async def _store_alert(self, alert: AlertRecord) -> None:
        """Append *alert* to storage: inline in memory, otherwise in the background."""
        if self._writes_inline():
            await self._storage_call("append_alert", alert)
            return
        self._unwritten_alerts.append(alert)  # the oldest is dropped if the store is far behind
        self._start_writer(_ALERT_WRITER, self._drain_alerts)

    def _start_writer(self, key: Any, drain: Callable[[Any], Awaitable[None]]) -> None:
        task = self._writers.get(key)
        if task is None or task.done():
            self._writers[key] = asyncio.get_running_loop().create_task(drain(key))

    async def _drain_results(self, name: str) -> None:
        try:
            while (result := self._unwritten.pop(name, None)) is not None:
                await asyncio.gather(
                    self._storage_call("set_latest", result),
                    self._storage_call("append_history", result),
                )
        finally:
            self._writers.pop(name, None)

    async def _drain_alerts(self, key: Any) -> None:
        try:
            while self._unwritten_alerts:
                await self._storage_call("append_alert", self._unwritten_alerts.popleft())
        finally:
            self._writers.pop(key, None)

    async def _flush_writes(self) -> None:
        """Wait until every background storage write has finished."""
        while self._writers:
            await asyncio.gather(*list(self._writers.values()), return_exceptions=True)

    async def _storage_call(self, op: str, *args: Any) -> Any:
        """Await ``storage.<op>(*args)``, bounded by ``storage_timeout``.

        Returns the result, or ``_STORAGE_FAILED`` if the call raised or timed
        out (after logging it, rate-limited).  Never raises an ``Exception``:
        the monitoring store being down must not take health checks down.
        The default in-memory backend cannot block, so it skips the timeout.
        """
        try:
            awaitable = getattr(self._storage, op)(*args)
            if self._storage_timeout is None or type(self._storage) is InMemoryProbeStorage:
                return await awaitable
            return await asyncio.wait_for(awaitable, self._storage_timeout)
        except Exception as exc:
            self._log_storage_failure(op, exc)
            return _STORAGE_FAILED

    def _log_storage_failure(self, op: str, exc: BaseException) -> None:
        """Log a failed storage *op*, at most once per ``_STORAGE_LOG_INTERVAL_S``."""
        now = time.monotonic()
        last, suppressed = self._storage_failures.get(op, (None, 0))
        if last is not None and now - last < _STORAGE_LOG_INTERVAL_S:
            self._storage_failures[op] = (last, suppressed + 1)
            return
        self._storage_failures[op] = (now, 0)
        timed_out = isinstance(exc, asyncio.TimeoutError) and self._storage_timeout is not None
        reason = f"timed out after {self._storage_timeout:g} s" if timed_out else f"failed ({type(exc).__name__})"
        (self._logger or _LIB_LOGGER).warning(
            "fastapi-watch: storage %s %s%s; health checks continue from in-process results "
            "(logged at most once per %g s per operation)",
            op,
            reason,
            f", {suppressed} more failure(s) since the last report" if suppressed else "",
            _STORAGE_LOG_INTERVAL_S,
            exc_info=None if timed_out else exc,
        )

    def _is_cb_active(self, probe: BaseProbe) -> bool:
        """Return True if the circuit breaker is active for this probe."""
        if not probe.circuit_breaker_enabled:
            return False
        return self._circuit_breaker_enabled

    async def _guarded_check(self, probe: BaseProbe, critical: bool) -> ProbeResult:
        """Run ``probe.check()`` bounded by its timeout, turning any failure into a result.

        Every probe is bounded: endpoints await the probes they run, so a single
        hung check would otherwise hang every one of them.
        """
        timeout = probe.timeout if probe.timeout is not None else self._probe_timeout
        try:
            coro = probe.check()
            result = await asyncio.wait_for(coro, timeout) if timeout is not None else await coro
        except asyncio.TimeoutError:
            if self._logger:
                self._logger.warning("Probe %r timed out after %g s", probe.name, timeout)
            return ProbeResult(
                name=probe.name,
                status=ProbeStatus.UNHEALTHY,
                critical=critical,
                error=f"probe timed out after {timeout:g} s",
            )
        except Exception as exc:
            if self._logger:
                self._logger.exception("Probe %r raised an unhandled %s", probe.name, type(exc).__name__)
            return ProbeResult(
                name=probe.name,
                status=ProbeStatus.UNHEALTHY,
                critical=critical,
                error=_failure_message(probe.name, exc),
            )
        return result if result.critical == critical else result.model_copy(update={"critical": critical})

    async def _safe_check(self, probe: BaseProbe, critical: bool) -> ProbeResult:
        """Run one probe with circuit-breaker and timeout handling."""
        return (await self._check_or_cached(probe, critical))[0]

    async def _check_or_cached(self, probe: BaseProbe, critical: bool) -> tuple[ProbeResult, bool]:
        """Run one probe with circuit-breaker and timeout handling.

        Returns ``(result, from_cache)``; *from_cache* is True when the circuit
        is open and the probe's last result is served in place of a check.
        """
        loop = asyncio.get_running_loop()
        cb_active = self._is_cb_active(probe)

        # Circuit breaker — skip probe while circuit is open
        if cb_active:
            open_until = self._circuit_open_until.get(probe.name, 0.0)
            if loop.time() < open_until:
                cb_info = self._cb_info(probe.name, True)
                cached = self._last_results.get(probe.name)
                if cached is None:
                    stored = await self._storage_call("get_latest", probe.name)
                    cached = None if stored is _STORAGE_FAILED else stored
                if cached is not None:
                    details = {**(cached.details or {}), "circuit_breaker": cb_info}
                    return cached.model_copy(update={"critical": critical, "details": details}), True
                return ProbeResult(
                    name=probe.name,
                    status=ProbeStatus.UNHEALTHY,
                    critical=critical,
                    error="circuit breaker open — probe temporarily suspended",
                    details={"circuit_breaker": cb_info},
                ), False

        result = await self._guarded_check(probe, critical)

        # Update circuit breaker state
        if cb_active:
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

        return result, False

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

    def _ensure_background_polling(self) -> None:
        """Start the poll loop for probes with their own ``poll_interval_ms``.

        Those probes poll in the background whether or not a stream client is
        connected.  This runs from ``set_started()`` and from every health
        request, so polling starts at the first of those that has a running
        loop — previously it waited for the first SSE client, so background
        polling depended on whether anyone had opened the dashboard.
        """
        if not self._has_custom_intervals() or self._shutting_down:
            return
        if self._poll_task is not None and not self._poll_task.done():
            return
        try:
            self._poll_task = asyncio.get_running_loop().create_task(self._poll_loop())
        except RuntimeError:
            pass  # no running loop yet (sync set_started); the first request starts it

    # ------------------------------------------------------------------
    # Caching / result retrieval
    # ------------------------------------------------------------------

    async def _get_results(self, reuse_within_s: float | None = None) -> list[ProbeResult]:
        """Return the current result of every registered probe, as checked by this process.

        Single-fetch probes run fresh, unless *reuse_within_s* is given: a
        stream passes its event interval, so a result checked within it is
        reused.  Otherwise every open stream would re-run each single-fetch
        probe once per event — N streams, N checks of that dependency per
        interval.  Polled probes run when their interval has elapsed, when
        they have not yet run in this process, or when no result is held
        for them.

        Results come from the in-process copy of each probe's last result, not
        from storage.  A shared backend (e.g. Redis) also holds other
        replicas' and other apps' results — under the same probe names, for
        replicas — and those must not decide this process's readiness.  It
        also means no storage read per request, so a slow or unavailable
        backend cannot slow or fail health checks.
        """
        self._ensure_background_polling()
        self._schedule_forget()
        now = asyncio.get_running_loop().time()

        def _needs_run(probe: BaseProbe) -> bool:
            last = self._probe_last_run.get(probe.name)
            if last is None or probe.name not in self._last_results:
                return True
            interval_s = self._effective_interval_s(probe)
            if interval_s is None:  # single-fetch
                return reuse_within_s is None or now - last >= reuse_within_s
            return now - last >= interval_s

        to_run = [(p, c) for p, c in self._probes if _needs_run(p)]
        if to_run:
            await self._execute_probes(to_run)
        results: dict[str, ProbeResult] = {}
        for p, _ in self._probes:
            result = self._last_results.get(p.name)
            if result is not None:
                results.setdefault(p.name, result)
        return list(results.values())

    def _forget_due(self) -> bool:
        return self._last_forget is None or time.monotonic() - self._last_forget >= _FORGET_INTERVAL_S

    def _schedule_forget(self) -> None:
        """Start a background :meth:`_forget_unregistered` when one is due; never awaited by a request."""
        if not self._forget_due() or self._shutting_down:
            return
        if self._forget_task is not None and not self._forget_task.done():
            return
        self._forget_task = asyncio.get_running_loop().create_task(self._forget_unregistered())

    async def _forget_unregistered(self) -> None:
        """Drop data of probes this process wrote but no longer registers, here and in storage.

        Runs at most every ``_FORGET_INTERVAL_S``: from the first health request
        and from the poll loop.  Without it, a renamed or removed probe (e.g.
        after ``discover_routes(refresh=True)``) would keep its results and
        history in storage until their TTL, or forever with TTL 0.

        Only names in ``_written_names`` are ever deleted, via the optional
        storage method ``forget(names)``.  Data written by other apps or other
        replicas under the same prefix -- including probes this process never
        registered, as during a rolling deploy -- is never touched.  A name
        stays tracked until ``forget()`` succeeds, so a failed call is retried,
        and until any background write of it has landed.
        """
        self._last_forget = time.monotonic()
        registered = {p.name for p, _ in self._probes}
        for state in (
            self._last_results, self._probe_last_run, self._probe_states,
            self._alerted_states, self._alert_last_sent_at, self._alert_pending,
            self._circuit_open_until, self._circuit_err_count, self._circuit_trips,
        ):
            for name in [n for n in state if n not in registered]:
                del state[name]
        stale = self._written_names - registered - self._writers.keys()
        if not stale:
            return
        if callable(getattr(self._storage, "forget", None)):
            if await self._storage_call("forget", set(stale)) is _STORAGE_FAILED:
                return
        # A name registered again meanwhile is re-added by its next write.
        self._written_names -= stale

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
        # Fallback for apps that never call set_started().
        # Note: this path has a race condition on reload (the browser may reconnect
        # before uvicorn re-registers its SIGTERM handler in the new worker).
        # Prefer calling set_started() from your lifespan startup instead.
        if not self._signal_handler_installed:
            self._signal_handler_installed = True
            self._install_early_shutdown_hook()
        self._active_connections += 1
        # done() covers a loop that exited unexpectedly, so it is restarted rather
        # than leaving every later client without background polling.
        if self._active_connections == 1 and (self._poll_task is None or self._poll_task.done()):
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

    def _install_early_shutdown_hook(self) -> None:
        """Chain SIGTERM/SIGINT handlers to stop SSE streams before uvicorn drains connections.

        uvicorn's shutdown order is:
          1. connection.shutdown() on all connections
          2. wait for connections to drain  ← SSE hangs here
          3. lifespan teardown

        By intercepting the signal before step 2, we set _shutting_down and cancel
        active SSE tasks so they close themselves within one poll tick (≤ 500 ms),
        allowing uvicorn to proceed without hanging.

        uvicorn registers its own handlers via signal.signal() (not
        loop.add_signal_handler()), so we chain at the Python signal level.

        A signal.signal() handler can run between any two bytecodes — including
        in the middle of an event-loop callback — so it only sets a flag and
        hands the task cancellation to the loop with call_soon_threadsafe().
        Without a running loop (hook installed from sync code) the flag alone
        still stops every stream within one 500 ms tick.
        """
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        for sig in (_signal.SIGTERM, _signal.SIGINT):
            existing_handler = _signal.getsignal(sig)
            if existing_handler is None:
                continue  # installed outside Python: it could not be restored or chained to

            def _make_handler(existing):
                def _chained(signum, frame):
                    self._shutting_down = True
                    if loop is not None and not loop.is_closed():
                        loop.call_soon_threadsafe(self._cancel_background_tasks)
                    # Delegate to uvicorn's handler so it still shuts down.
                    if callable(existing):
                        existing(signum, frame)
                    elif existing == _signal.SIG_DFL:
                        # The default action (terminate) has no function to call:
                        # restore it and re-raise, or the signal would be ignored.
                        _signal.signal(signum, _signal.SIG_DFL)
                        _signal.raise_signal(signum)
                return _chained

            try:
                _signal.signal(sig, _make_handler(existing_handler))
            except (OSError, ValueError):
                # Not the main thread, or the platform lacks the signal: streams
                # still stop via the lifespan teardown (_shutdown).
                (self._logger or _LIB_LOGGER).debug(
                    "fastapi-watch: could not install the early shutdown hook for %s", sig
                )

    def _cancel_background_tasks(self) -> None:
        """Cancel SSE streams, the poll loop, and the alert worker (on the loop thread)."""
        for task in list(self._sse_tasks):
            task.cancel()
        self._cancel_poll_task()
        if self._alert_worker is not None and not self._alert_worker.done():
            self._alert_worker.cancel()

    async def _shutdown(self) -> None:
        """Signal SSE streams to stop and cancel the poll task.

        Called from the lifespan teardown as a safety net.  On uvicorn the signal
        handler installed by _install_early_shutdown_hook fires first (before
        connection draining), so this is usually a no-op by the time it runs.
        It also handles non-signal shutdowns (e.g. programmatic server stop).
        """
        self._shutting_down = True
        for task in list(self._sse_tasks):
            task.cancel()
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        if self._alert_worker is not None and not self._alert_worker.done():
            self._alert_worker.cancel()
            try:
                await self._alert_worker
            except (asyncio.CancelledError, Exception):
                pass
            self._alert_worker = None
        if self._forget_task is not None and not self._forget_task.done():
            self._forget_task.cancel()
            try:
                await self._forget_task
            except (asyncio.CancelledError, Exception):
                pass
        self._forget_task = None
        # Give pending background writes a bounded chance to land; past that
        # they are cancelled (a hung store must not hold up shutdown).
        try:
            await asyncio.wait_for(self._flush_writes(), _WRITE_FLUSH_TIMEOUT_S)
        except asyncio.TimeoutError:
            pass
        await self._close_probes()

    async def _close_probes(self) -> None:
        """Await ``aclose()`` on every registered and startup probe that has one.

        Lets probes stop what they run in the background (e.g. the event-loop
        probe's sampler).  Closes run concurrently, each bounded by
        ``_ACLOSE_TIMEOUT_S``; a failure is logged and does not stop the rest.
        """
        unique = {id(p): p for p in [*(p for p, _ in self._probes), *self._startup_probes]}
        logger = self._logger or _LIB_LOGGER

        async def _close(probe: BaseProbe) -> None:
            try:
                ret = probe.aclose()
                if inspect.isawaitable(ret):
                    await asyncio.wait_for(ret, _ACLOSE_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.warning("fastapi-watch: probe %r aclose() timed out after %g s", probe.name, _ACLOSE_TIMEOUT_S)
            except Exception:
                logger.exception("fastapi-watch: probe %r aclose() failed", probe.name)

        closers = [_close(p) for p in unique.values() if callable(getattr(p, "aclose", None))]
        if closers:
            await asyncio.gather(*closers)

    async def _poll_loop(self) -> None:
        """Background loop: run each probe when its interval elapses (1 s granularity).

        A failing iteration (e.g. a storage backend error) is logged and the loop
        carries on; letting it escape would end background polling for good with
        nothing but an unretrieved-exception warning to show for it.
        """
        loop = asyncio.get_running_loop()
        while True:
            try:
                now = loop.time()
                # Probes on the registry-wide interval poll only while a stream
                # client is connected; the loop may be running for probes with
                # their own interval, which poll regardless.
                streaming = self._active_connections > 0
                due = [
                    (p, c) for p, c in self._probes
                    if (streaming or p.poll_interval_ms is not None) and self._is_probe_due(p, now)
                ]
                if due:
                    await self._execute_probes(due)
                if self._forget_due():
                    await self._forget_unregistered()
            except Exception:
                (self._logger or _LIB_LOGGER).exception("fastapi-watch: poll iteration failed")
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

    def _reserve_stream_slot(self) -> Callable[[], None] | None:
        """Take one of ``max_stream_connections`` stream slots.

        Returns a release function (safe to call more than once), or ``None``
        when every slot is taken.  Each open stream wakes every 0.5 s, so an
        uncapped count — reachable by anyone when *auth* is ``None`` — would
        let clients load the app's event loop at will.
        """
        if self._max_stream_connections is not None and self._stream_slots >= self._max_stream_connections:
            return None
        self._stream_slots += 1
        released = False

        def _release() -> None:
            nonlocal released
            if not released:
                released = True
                self._stream_slots -= 1

        return _release

    def _stream_interval_s(self) -> float | None:
        """Seconds between stream events: the shortest effective probe interval.

        Using only the global interval would leave a probe polled every 5 s
        shown at the global 60 s cadence.  ``None`` when every probe is
        single-fetch.
        """
        intervals = [s for p, _ in self._probes if (s := self._effective_interval_s(p)) is not None]
        if intervals:
            return min(intervals)
        return self._poll_interval_ms / 1000 if self._poll_interval_ms is not None else None

    async def _event_stream(
        self,
        request: Request,
        make_report: Callable[[list[ProbeResult]], str],
        filter_tags: frozenset[str] | None = None,
        filter_probes: frozenset[str] | None = None,
        expires_at: float | None = None,
    ) -> AsyncGenerator[str, None]:
        """Async generator that yields SSE (Server-Sent Events) events while the client is connected.

        Between events it sends a comment line every ``_SSE_KEEPALIVE_S`` so
        proxies with idle timeouts (commonly 60 s) do not cut the connection.
        In single-fetch mode it sends one event with a ``retry:`` hint, so the
        browser waits ``_SINGLE_FETCH_RETRY_MS`` before reconnecting instead of
        its ~3 s default — each reconnect re-runs every probe.

        *expires_at* (Unix time) ends the stream when the stream token that
        authorized it expires; a token checked only at connect time would
        otherwise grant access for as long as the connection stays open.
        """
        task = asyncio.current_task()
        if task is not None:
            self._sse_tasks.add(task)
        await self._on_connect()
        loop = asyncio.get_running_loop()

        def _expired() -> bool:
            return expires_at is not None and time.time() >= expires_at

        try:
            while not self._shutting_down and not _expired():
                interval_s = self._stream_interval_s()
                results = await self._get_results(reuse_within_s=interval_s)
                if filter_tags:
                    results = [r for r in results if filter_tags & set(r.tags)]
                if filter_probes:
                    results = [r for r in results if r.name in filter_probes]
                if interval_s is None:
                    yield f"retry: {_SINGLE_FETCH_RETRY_MS}\ndata: {make_report(results)}\n\n"
                    return
                yield f"data: {make_report(results)}\n\n"

                deadline = loop.time() + interval_s
                next_keepalive = loop.time() + _SSE_KEEPALIVE_S
                while (now := loop.time()) < deadline:
                    await asyncio.sleep(min(0.5, deadline - now))
                    if self._shutting_down or _expired() or await request.is_disconnected():
                        return
                    if loop.time() >= next_keepalive:
                        yield ": keepalive\n\n"
                        next_keepalive += _SSE_KEEPALIVE_S
        finally:
            # Cancellation (client gone, shutdown) propagates after cleanup;
            # swallowing it would leave the cancelling task believing it never ran.
            self._sse_tasks.discard(task)
            await self._on_disconnect()

    # ------------------------------------------------------------------
    # State-change callbacks and webhook
    # ------------------------------------------------------------------

    async def _fire_state_changes(self, results: list[ProbeResult]) -> None:
        """Record alerts and fire callbacks/webhook for any probe whose status changed.

        A probe's state before its first result is taken to be HEALTHY, so a
        probe that is already failing at startup alerts (``healthy -> unhealthy``)
        instead of staying silent until it happens to recover and fail again.
        A first HEALTHY result is not a change and fires nothing.

        Alerters are compared against the last status *they* were sent, not the
        previous result: maintenance suppresses alerters but not state tracking,
        so a dependency that failed during maintenance and stayed down would
        otherwise never alert.  The first check after maintenance sends any
        difference (``healthy -> unhealthy``); a failure that recovered within
        the window sends nothing.  Callbacks and stored alert records still
        follow real transitions only.
        """
        in_maintenance = self._in_maintenance()
        for result in results:
            old = self._probe_states.get(result.name, ProbeStatus.HEALTHY)
            self._probe_states[result.name] = result.status
            alert_record: AlertRecord | None = None
            if old != result.status:
                alert_record = AlertRecord(
                    probe=result.name,
                    old_status=old,
                    new_status=result.status,
                    timestamp=datetime.now(self._tzinfo),
                )
                await self._store_alert(alert_record)
                for cb in self._state_change_callbacks:
                    # User code: one broken callback must not fail the health check
                    # that triggered it, nor stop the callbacks and alerts after it.
                    try:
                        ret = cb(result.name, old, result.status)
                        if asyncio.iscoroutine(ret):
                            await ret
                    except Exception:
                        (self._logger or _LIB_LOGGER).exception(
                            "fastapi-watch: on_state_change callback %r failed for probe %r",
                            getattr(cb, "__name__", cb),
                            result.name,
                        )
            if self._alerters and not in_maintenance:
                self._send_to_alerters(result.name, result.status, alert_record)

    def _send_to_alerters(
        self, name: str, status: ProbeStatus, transition: AlertRecord | None, *, due: bool = False
    ) -> None:
        """Queue an alert if *status* differs from the status alerters last received.

        *transition* is the record of a real state change, reused when it
        starts from what alerters last saw; otherwise (catching up after
        maintenance, or a coalesced change) a record from the last-sent status
        is built.  The last-sent status advances only once the alert is
        queued, so an alert dropped on a full queue is retried at the next check.

        With ``alert_min_interval_s``, a change within that interval of the
        probe's last alert is not queued: a pending marker is set instead, and
        the alert worker sends the probe's then-current status when the
        interval ends — if it still differs from the last one sent.  The first
        alert after a quiet interval is never delayed.  *due* marks that send
        (the interval has ended), skipping the rate check.
        """
        sent = self._alerted_states.get(name, ProbeStatus.HEALTHY)
        if sent == status:
            # Back where alerters last saw it: a flap that needs no alert.
            self._alert_pending.pop(name, None)
            return
        now = time.monotonic()
        if self._alert_min_interval_s > 0 and not due:
            last = self._alert_last_sent_at.get(name)
            if last is not None and now - last < self._alert_min_interval_s:
                if name not in self._alert_pending:
                    self._alert_pending[name] = last + self._alert_min_interval_s
                    self._wake_alert_worker()
                return
        if transition is None or transition.old_status != sent:
            transition = AlertRecord(
                probe=name,
                old_status=sent,
                new_status=status,
                timestamp=datetime.now(self._tzinfo),
            )
        self._ensure_alert_worker()
        try:
            self._alert_queue.put_nowait(transition)
        except asyncio.QueueFull:
            if self._logger:
                self._logger.warning("Alert queue full, dropping alert for probe %r", transition.probe)
            return
        self._alerted_states[name] = status
        self._alert_last_sent_at[name] = now
        self._alert_pending.pop(name, None)

    def _wake_alert_worker(self) -> None:
        """Make the alert worker recompute when the next coalesced alert is due."""
        self._ensure_alert_worker()
        try:
            self._alert_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass  # the worker is busy and re-checks after each alert anyway

    def _next_pending_alert_delay(self) -> float | None:
        """Seconds until the next coalesced alert is due, or ``None`` if none is pending."""
        if not self._alert_pending:
            return None
        return max(0.0, min(self._alert_pending.values()) - time.monotonic())

    def _send_due_alerts(self) -> None:
        """Send the current status of every probe whose coalescing interval has ended.

        Pending markers that fall due during maintenance are dropped: the
        first check after maintenance catches alerters up anyway.
        """
        if not self._alert_pending:
            return
        now = time.monotonic()
        in_maintenance = self._in_maintenance()
        for name, due in list(self._alert_pending.items()):
            if due > now:
                continue
            del self._alert_pending[name]
            status = self._probe_states.get(name)
            if status is not None and self._alerters and not in_maintenance:
                self._send_to_alerters(name, status, None, due=True)

    async def _claim_alert(self, alert: AlertRecord) -> bool:
        """True if this replica should send *alert* (always, without dedupe).

        With ``alert_dedupe_window_s``, replicas sharing a storage backend race
        to claim ``"<probe>:<new_status>"`` for that window and only the winner
        sends.  A failing claim sends anyway: a duplicate alert beats none.
        """
        if self._alert_dedupe_window_s is None:
            return True
        key = f"{alert.probe}:{alert.new_status.value}"
        claimed = await self._storage_call("claim_alert", key, self._alert_dedupe_window_s)
        return claimed is _STORAGE_FAILED or bool(claimed)

    def _ensure_alert_worker(self) -> None:
        """Start the alert worker task if not already running (never after shutdown)."""
        if self._shutting_down:
            return  # a check finishing during shutdown must not leave a new worker pending
        if self._alert_worker is None or self._alert_worker.done():
            try:
                self._alert_worker = asyncio.get_running_loop().create_task(
                    self._alert_loop(), name="fastapi-watch-alert-worker"
                )
            except RuntimeError:
                pass  # no running loop yet

    async def _alert_loop(self) -> None:
        """Drain the alert queue, dispatching each alert to all registered alerters.

        Between alerts it also sends coalesced alerts (``alert_min_interval_s``)
        as they fall due, waking for the earliest one.
        """
        while True:
            self._send_due_alerts()
            delay = self._next_pending_alert_delay()
            try:
                if delay is None:
                    alert = await self._alert_queue.get()
                else:
                    alert = await asyncio.wait_for(self._alert_queue.get(), delay)
            except asyncio.TimeoutError:
                continue  # a coalesced alert is due
            except asyncio.CancelledError:
                return
            try:
                if alert is not None and await self._claim_alert(alert):
                    await self._dispatch_alert(alert)
            except asyncio.CancelledError:
                return
            except Exception:
                # The worker must outlive any one bad alert, but not silently.
                (self._logger or _LIB_LOGGER).exception(
                    "Dispatching alert for probe %s failed", getattr(alert, "probe", None)
                )
            finally:
                self._alert_queue.task_done()

    async def _dispatch_alert(self, alert: AlertRecord) -> None:
        """Call every registered alerter concurrently for a probe state change.

        Each alerter runs as its own task bounded by ``_NOTIFY_TIMEOUT_S``, so a
        slow or hung integration cannot delay the others or every later alert.
        Failures are logged with their traceback and never propagate.
        """
        from .alerts import _NOTIFY_TIMEOUT_S

        async def _notify(alerter: BaseAlerter) -> None:
            await alerter.notify(alert)

        tasks = {asyncio.ensure_future(_notify(alerter)): alerter for alerter in self._alerters}
        if not tasks:
            return
        try:
            _, pending = await asyncio.wait(tasks, timeout=_NOTIFY_TIMEOUT_S)
        finally:
            # Also reached when this dispatch is itself cancelled at shutdown.
            for task in tasks:
                task.cancel()
        logger = self._logger or _LIB_LOGGER
        for task, alerter in tasks.items():
            if task in pending:
                logger.warning(
                    "Alerter %s timed out after %g s for probe %s",
                    type(alerter).__name__, _NOTIFY_TIMEOUT_S, alert.probe,
                )
            elif not task.cancelled() and task.exception() is not None:
                logger.warning(
                    "Alerter %s failed for probe %s",
                    type(alerter).__name__, alert.probe,
                    exc_info=task.exception(),
                )

    # ------------------------------------------------------------------
    # Route registration
    # ------------------------------------------------------------------

    def _register_routes(
        self,
        tags: list[str],
        dashboard: bool | Callable[..., str] | str | Path = True,
    ) -> None:
        registry = self
        prefix = self.prefix

        auth_checker = _make_auth_checker(self._auth)
        auth_deps = [Depends(auth_checker)] if auth_checker else []
        stream_deps = auth_deps
        token_key: bytes | None = None
        if auth_checker is not None:
            # A browser EventSource cannot send headers, so the authenticated
            # dashboard page hands its stream a short-lived signed token instead.
            token_key = self._stream_token_key

            async def _stream_auth(request: Request) -> None:
                token = request.query_params.get("token")
                expiry = _stream_token_expiry(token_key, token) if token else None
                if expiry is not None:
                    # A token-only stream ends when its token does, so the page
                    # reconnects through a reload that mints a fresh one.
                    request.state.stream_token_expiry = expiry
                    return
                await auth_checker(request)

            stream_deps = [Depends(_stream_auth)]

        def _make_sse_report(results: list[ProbeResult], view: str | None = None) -> str:
            report = HealthReport.from_results(
                results,
                checked_at=registry._last_checked_at,
                timezone=registry._timezone_name,
            )
            if view != "dashboard":
                return report.model_dump_json()
            # The built-in dashboard's view: the same slimming as the page's
            # embedded probes (full details, routes cut to the charted top ones).
            data = report.model_dump(mode="json", exclude={"probes"})
            data["probes"] = [_current_probe(r) for r in report.probes]
            return to_json(data).decode()

        sse_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

        def _storage_unavailable() -> Response:
            return JSONResponse({"error": "storage unavailable"}, status_code=503)

        def _open_stream(request: Request, tag: str | None, probe: str | None, view: str | None) -> Response:
            """Start an SSE response, or answer 503 once ``max_stream_connections`` are open.

            The slot is taken here, before the generator starts, so a burst of
            connections cannot all pass the check; it is released when the
            response finishes, even if the generator never ran.
            """
            release = registry._reserve_stream_slot()
            if release is None:
                return JSONResponse(
                    {"error": "too many stream connections"},
                    status_code=503,
                    headers={"Retry-After": str(_STREAM_RETRY_AFTER_S)},
                )
            return _SlotStreamingResponse(
                registry._event_stream(
                    request,
                    functools.partial(_make_sse_report, view=view),
                    filter_tags=_parse_csv_filter(tag),
                    filter_probes=_parse_csv_filter(probe),
                    expires_at=getattr(request.state, "stream_token_expiry", None),
                ),
                release=release,
                media_type="text/event-stream",
                headers=sse_headers,
            )

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
        async def readiness(
            tag: str | None = Query(default=None),
            probe: str | None = Query(default=None),
        ) -> Response:
            if registry._in_maintenance():
                return JSONResponse({"status": "maintenance"}, status_code=200)
            if registry._in_grace_period():
                return JSONResponse({"status": "starting"}, status_code=503)
            results = await registry._get_results()
            _filter = _parse_csv_filter(tag)
            if _filter:
                results = [r for r in results if _filter & set(r.tags)]
            _probe_filter = _parse_csv_filter(probe)
            if _probe_filter:
                results = [r for r in results if r.name in _probe_filter]
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
        async def health_status(
            tag: str | None = Query(default=None),
            probe: str | None = Query(default=None),
        ) -> Response:
            results = await registry._get_results()
            _filter = _parse_csv_filter(tag)
            if _filter:
                results = [r for r in results if _filter & set(r.tags)]
            _probe_filter = _parse_csv_filter(probe)
            if _probe_filter:
                results = [r for r in results if r.name in _probe_filter]
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
            description=(
                "Returns the last N results for each probe (oldest-first), within the result TTL window. "
                "503 when storage is unavailable."
            ),
            dependencies=auth_deps,
        )
        async def probe_history() -> Response:
            history = await registry._storage_call("get_history")
            if history is _STORAGE_FAILED:
                return _storage_unavailable()
            # Serialized in one pass by pydantic-core: history can run to
            # megabytes, and dumping to dicts and then json.dumps blocked the
            # event loop about four times as long.
            return Response(content=to_json({"probes": history}), media_type="application/json")

        @self.app.get(
            f"{prefix}/alerts",
            tags=tags,
            summary="Alert history",
            description=(
                "Returns probe state-change alerts retained within the alert TTL window (default 72 hours). "
                "503 when storage is unavailable."
            ),
            dependencies=auth_deps,
        )
        async def alert_history() -> Response:
            alerts = await registry._storage_call("get_alerts")
            if alerts is _STORAGE_FAILED:
                return _storage_unavailable()
            return Response(content=to_json({"alerts": alerts}), media_type="application/json")

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
                # Same timeout and error handling as regular probes, so a hung or
                # raising startup probe answers 503 rather than hanging or 500ing.
                results = list(
                    await asyncio.gather(*(registry._guarded_check(p, True) for p in registry._startup_probes))
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
            f"{prefix}/probes",
            tags=tags,
            summary="Probe introspection",
            description="Lists all registered probes and their config without running them.",
            dependencies=auth_deps,
        )
        async def probe_list() -> Response:
            probes = []
            for p, critical in registry._probes:
                probes.append({
                    "name": p.name,
                    "critical": critical,
                    "tags": getattr(p, "tags", None) or [],
                    "description": getattr(p, "description", None),
                    "poll_interval_ms": p.poll_interval_ms,
                    "circuit_breaker_enabled": p.circuit_breaker_enabled,
                })
            return JSONResponse({"probes": probes})

        @self.app.get(
            f"{prefix}/ready/stream",
            tags=tags,
            summary="Readiness stream",
            description="SSE (Server-Sent Events) stream of readiness. Poll loop stops when last client disconnects.",
            dependencies=stream_deps,
        )
        async def readiness_stream(
            request: Request,
            tag: str | None = Query(default=None),
            probe: str | None = Query(default=None),
            view: Literal["dashboard"] | None = Query(
                default=None,
                description=(
                    "``dashboard``: each probe's ``details.routes`` cut to the 8 busiest routes "
                    "with only the charted fields, as the built-in dashboard needs."
                ),
            ),
        ) -> Response:
            return _open_stream(request, tag, probe, view)

        @self.app.get(
            f"{prefix}/status/stream",
            tags=tags,
            summary="Health status stream",
            description=(
                "SSE (Server-Sent Events) stream of full probe results. Poll loop stops when last client disconnects. "
                "``view=dashboard`` cuts each probe's ``details.routes`` to what the built-in dashboard charts."
            ),
            dependencies=stream_deps,
        )
        async def status_stream(
            request: Request,
            tag: str | None = Query(default=None),
            probe: str | None = Query(default=None),
            view: Literal["dashboard"] | None = Query(
                default=None,
                description=(
                    "``dashboard``: each probe's ``details.routes`` cut to the 8 busiest routes "
                    "with only the charted fields, as the built-in dashboard needs."
                ),
            ),
        ) -> Response:
            return _open_stream(request, tag, probe, view)

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
                content=render_prometheus(results, trips, prefix=registry._metrics_prefix),
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )

        @self.app.get(
            f"{prefix}/maintenance",
            tags=tags,
            summary="Maintenance mode status",
            description="Returns whether maintenance mode is currently active and when it expires.",
            dependencies=auth_deps,
        )
        async def maintenance_status() -> Response:
            active = registry._in_maintenance()
            until = registry._maintenance_until
            return JSONResponse({
                "active": active,
                "until": until.isoformat() if until is not None else None,
            })

        # State-changing routes are mounted only behind auth: an open POST would
        # let anyone pin /ready at 200 and silence alerts.  set_maintenance() and
        # clear_maintenance() remain available to the application either way.
        # The cross-site check runs first, before credentials are even looked at.
        if auth_checker is not None:
            change_deps = [Depends(_reject_cross_site), *auth_deps]

            @self.app.post(
                f"{prefix}/maintenance",
                tags=tags,
                summary="Enable maintenance mode",
                description=(
                    "Enables maintenance mode. Optional body: ``{\"minutes\": N}`` or "
                    "``{\"until\": \"<ISO datetime>\"}``."
                    " Omit body (or send ``{}``) for indefinite maintenance."
                    " A naive ``until`` is read in the registry timezone; a past one is rejected with 422."
                    " A body must be ``application/json`` (415 otherwise); browser cross-site requests get 403."
                ),
                dependencies=change_deps,
            )
            async def enable_maintenance(body: _MaintenanceRequest = Body(default=_MaintenanceRequest())) -> Response:
                now = datetime.now(registry._tzinfo)
                if body.until is not None:
                    until = registry._aware(body.until)
                    # Checked before any state changes: a past "until" would report
                    # active=True and then silently expire.
                    if until <= now:
                        raise HTTPException(status_code=422, detail="until must be in the future")
                elif body.minutes is not None:
                    until = now + timedelta(minutes=body.minutes)
                else:
                    until = None
                registry.set_maintenance(until=until)
                return JSONResponse({
                    "active": True,
                    "until": until.isoformat() if until is not None else None,
                })

            @self.app.delete(
                f"{prefix}/maintenance",
                tags=tags,
                summary="Disable maintenance mode",
                description="Clears maintenance mode immediately. Browser cross-site requests get 403.",
                dependencies=change_deps,
            )
            async def disable_maintenance() -> Response:
                registry.clear_maintenance()
                return JSONResponse({"active": False, "until": None})

        if dashboard is not False:
            # Only the built-in page charts history; don't make custom renderers
            # pay for a (possibly remote) history read on every page load.
            builtin_dashboard = dashboard is True
            if callable(dashboard):
                user_renderer = dashboard
                # The documented form is (report) -> str; 1.7.1 always passed
                # (report, maintenance), so keep honoring renderers written that way.
                pass_maintenance = _takes_two_positional(user_renderer)

                def _renderer(report: HealthReport, maintenance: bool, request: Request, history: dict | None) -> str:
                    return user_renderer(report, maintenance) if pass_maintenance else user_renderer(report)
            elif isinstance(dashboard, (str, Path)):
                dashboard_path = Path(dashboard).resolve()
                if dashboard_path.suffix in (".html", ".htm"):
                    _html_content = dashboard_path.read_text(encoding="utf-8")

                    def _renderer(report: HealthReport, maintenance: bool, request: Request, history: dict | None) -> str:
                        return _html_content
                else:
                    raise ValueError(
                        f"dashboard path must be an .html or .htm file — got {dashboard_path.suffix!r}. "
                        "To use a custom renderer, pass a callable instead of a file path."
                    )
            else:

                def _renderer(report: HealthReport, maintenance: bool, request: Request, history: dict | None) -> str:
                    # root_path carries any mount or proxy prefix the app is served
                    # under; without it the page would stream from the wrong URL.
                    root_path = request.scope.get("root_path", "").rstrip("/")
                    params = {"view": "dashboard"}  # slimmed events: only what the page reads
                    if token_key is not None:
                        params["token"] = _mint_stream_token(token_key)
                    stream_url = f"{root_path}{prefix}/status/stream?{urlencode(params)}"
                    return render_dashboard(report, stream_url=stream_url, maintenance_banner=maintenance, history=history)

            @self.app.get(
                f"{prefix}/dashboard",
                tags=tags,
                summary="Health dashboard",
                description="Server-rendered HTML dashboard with live SSE updates.",
                response_class=HTMLResponse,
                dependencies=auth_deps,
            )
            async def health_dashboard(request: Request) -> HTMLResponse:
                results = await registry._get_results()
                report = HealthReport.from_results(
                    results,
                    checked_at=registry._last_checked_at,
                    timezone=registry._timezone_name,
                )
                history = await registry._storage_call("get_history") if builtin_dashboard else None
                if history is _STORAGE_FAILED:
                    history = {}  # the page still renders, from live results only
                return HTMLResponse(content=_renderer(report, registry._in_maintenance(), request, history))
