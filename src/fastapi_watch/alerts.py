"""Alert webhook integrations for fastapi-watch.

Alerters dispatch probe state-change notifications to external services.
Register one or more alerters with :class:`~fastapi_watch.HealthRegistry`
to fan out notifications across multiple channels simultaneously.

Usage::

    from fastapi_watch import HealthRegistry
    from fastapi_watch.alerts import SlackAlerter, PagerDutyAlerter, TeamsAlerter

    registry = HealthRegistry(
        app,
        alerters=[
            SlackAlerter(
                webhook_url="https://hooks.slack.com/services/T.../B.../...",
                channel="#ops-alerts",
            ),
            TeamsAlerter(
                webhook_url="https://outlook.office.com/webhook/...",
            ),
            PagerDutyAlerter(routing_key="your-32-char-routing-key"),
        ],
    )

Custom alerters::

    from fastapi_watch.alerts import BaseAlerter
    from fastapi_watch.models import AlertRecord

    class SMSAlerter(BaseAlerter):
        def __init__(self, phone_number: str, api_key: str) -> None:
            self.phone_number = phone_number
            self.api_key = api_key

        async def notify(self, alert: AlertRecord) -> None:
            message = (
                f"[fastapi-watch] {alert.probe}: "
                f"{alert.old_status.value} -> {alert.new_status.value}"
            )
            # send SMS via your provider ...
"""
import ipaddress
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

from ._executor import run_blocking
from .models import AlertRecord, ProbeStatus

# Upper bound on one alerter's notify() call.  Built-in alerters cut their own
# request off after ``timeout`` (default 5 s); this only catches custom alerters
# that never return, so one of them cannot stall every later alert.
_NOTIFY_TIMEOUT_S = 30.0


def _ip_literal(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return the address *hostname* denotes if it is an IP literal, else ``None``.

    Besides dotted-quad and IPv6, this recognises the legacy integer forms that
    C resolvers still accept as IPv4 — ``2130706433``, ``0x7f000001``,
    ``0177.0.0.1``, ``127.1`` — which ``ipaddress`` alone does not.
    """
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(hostname))
    except OSError:
        return None


def _is_internal(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for any address that is not publicly routable unicast."""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped  # ::ffff:127.0.0.1 reaches 127.0.0.1
    return addr.is_multicast or not addr.is_global


def _validate_webhook_url(url: str) -> None:
    """Raise ``ValueError`` if *url* is not a safe webhook URL.

    Rejects:
    - Non-HTTP(S) schemes (``file://``, ``ftp://``, etc.)
    - IP literals that are not publicly routable unicast — private, loopback,
      link-local (including cloud metadata), carrier-grade NAT, unspecified
      (``0.0.0.0``, ``::``), multicast, and reserved ranges — in any spelling
      a resolver accepts, including IPv4-mapped IPv6 (``::ffff:127.0.0.1``)
      and integer forms (``2130706433``, ``0x7f000001``)
    - ``localhost`` and ``*.localhost`` hostnames, with or without a trailing dot

    Both ``http://`` and ``https://`` are accepted. Use ``https://`` for
    production endpoints. Plain HTTP is only appropriate for local sidecars
    on a trusted network.

    This is a best-effort defence-in-depth check. It does **not** perform
    DNS resolution — a hostname that resolves to a private IP will pass.
    Alerters never follow redirects, so a validated URL cannot hand the
    request on to an internal one.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = (parsed.hostname or "").rstrip(".").lower()
    except ValueError as exc:
        raise ValueError(f"Invalid webhook URL: {exc}") from exc

    if parsed.scheme not in ("https", "http"):
        raise ValueError(
            f"Webhook URL must use https:// or http:// scheme, got {parsed.scheme!r}"
        )

    if not hostname or hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError(
            "Webhook URL must not target localhost. Use a public HTTPS endpoint."
        )

    addr = _ip_literal(hostname)
    if addr is not None and _is_internal(addr):
        raise ValueError(
            f"Webhook URL hostname {hostname!r} is a private, loopback, or otherwise "
            "non-public address. Use a public HTTPS endpoint."
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn every 3xx into an ``HTTPError`` instead of following it.

    Following redirects would let a validated public URL bounce the request
    (and its auth headers) to an internal address.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class _Deadline:
    """Cut a webhook request off once its whole time budget is spent.

    ``urllib``'s timeout bounds each socket operation separately, so an
    endpoint that trickles its response out a byte at a time can hold a
    request — and one of fastapi-watch's few pool threads — far longer than
    ``timeout``.  When the budget runs out this shuts the connection down,
    which makes the blocked call fail at once.  It holds a duplicate of each
    socket because TLS wrapping detaches the original socket object.

    Connecting is bounded too: each address the host resolves to is tried
    with only what is left of the budget (see :func:`_connect_within`).  DNS
    resolution of the webhook host happens before any socket exists and is
    bounded only by the system resolver's own timeouts.
    """

    def __init__(self, seconds: float) -> None:
        self.expired = False
        self._ends_at = time.monotonic() + seconds
        self._lock = threading.Lock()
        self._socks: list[socket.socket] = []
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def remaining(self) -> float:
        """Seconds left in the budget (zero or negative once it is spent)."""
        return self._ends_at - time.monotonic()

    def watch(self, sock: socket.socket) -> None:
        """Shut *sock* down when the deadline passes (at once if it already has)."""
        dup = sock.dup()
        with self._lock:
            self._socks.append(dup)
            expired = self.expired
        if expired:
            _shutdown(dup)

    def _expire(self) -> None:
        with self._lock:
            self.expired = True
            socks = list(self._socks)
        for sock in socks:
            _shutdown(sock)

    def close(self) -> None:
        """Stop the timer and release the duplicated sockets."""
        self._timer.cancel()
        with self._lock:
            socks, self._socks = self._socks, []
        for sock in socks:
            sock.close()


def _shutdown(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # already closed or never connected


def _connect_within(deadline: _Deadline, address: tuple, timeout: Any, source_address: Any = None) -> socket.socket:
    """``socket.create_connection``, with every attempt drawn from *deadline*'s budget.

    ``create_connection`` gives each resolved address the full *timeout*, so a
    host with N unreachable addresses takes N times as long; here the attempts
    share what is left of the budget.  The connected socket is handed to the
    deadline.
    """
    host, port = address
    error: OSError | None = None
    for family, type_, proto, _, sockaddr in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
        remaining = deadline.remaining()
        if remaining <= 0:
            break
        sock = socket.socket(family, type_, proto)
        try:
            sock.settimeout(min(timeout, remaining) if isinstance(timeout, (int, float)) else remaining)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
        except OSError as exc:
            sock.close()
            error = exc
            continue
        sock.settimeout(timeout if isinstance(timeout, (int, float)) else None)
        deadline.watch(sock)
        return sock
    if deadline.remaining() <= 0:
        raise TimeoutError("connect did not complete within the request deadline")
    raise error if error is not None else OSError(f"no addresses resolved for {host!r}")


class _DeadlineHandler:
    """Mixin for the HTTP(S) handlers: hand each new socket to the request's :class:`_Deadline`."""

    def do_open(self, http_class, req, **http_conn_args):  # noqa: D102
        deadline = getattr(req, "_fastapi_watch_deadline", None)
        if deadline is None:
            return super().do_open(http_class, req, **http_conn_args)

        def connection(host, **kwargs):
            conn = http_class(host, **kwargs)
            if not hasattr(conn, "_create_connection"):
                return conn  # unknown http.client internals: per-operation timeouts only
            conn._create_connection = lambda *a, **kw: _connect_within(deadline, *a, **kw)
            return conn

        return super().do_open(connection, req, **http_conn_args)


class _DeadlineHTTPHandler(_DeadlineHandler, urllib.request.HTTPHandler):
    pass


_HANDLERS: list[Any] = [_NoRedirect, _DeadlineHTTPHandler]
if hasattr(urllib.request, "HTTPSHandler"):  # absent when Python lacks ssl

    class _DeadlineHTTPSHandler(_DeadlineHandler, urllib.request.HTTPSHandler):
        pass

    _HANDLERS.append(_DeadlineHTTPSHandler)

_OPENER = urllib.request.build_opener(*_HANDLERS)


def _post_json(
    url: str, payload: bytes, timeout: float, headers: dict[str, str] | None = None
) -> None:
    """POST *payload* as JSON and close the response.  Raises on non-2xx.

    Blocking — call it through ``run_blocking``.  The whole request, not just
    each socket operation, is bounded by *timeout* (see :class:`_Deadline`);
    running out raises ``TimeoutError``.  Error responses are closed too, so a
    failing endpoint cannot leak a socket per alert.
    """
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    deadline = _Deadline(timeout)
    req._fastapi_watch_deadline = deadline
    try:
        with _OPENER.open(req, timeout=timeout):
            pass
    except urllib.error.HTTPError as exc:
        exc.close()
        raise
    except Exception as exc:
        if deadline.remaining() <= 0:
            raise TimeoutError(f"webhook request did not complete within {timeout:g} s") from exc
        raise
    finally:
        deadline.close()


class BaseAlerter(ABC):
    """Abstract base for all alerters.

    Subclass this to build a custom notification integration. Register
    instances with ``HealthRegistry(app, alerters=[...])``.

    :meth:`notify` is called once per probe state change. Exceptions are
    caught and logged by the registry so a broken alerter does not crash
    the health-check loop.

    Example::

        from fastapi_watch.alerts import BaseAlerter
        from fastapi_watch.models import AlertRecord

        class PrintAlerter(BaseAlerter):
            async def notify(self, alert: AlertRecord) -> None:
                print(
                    f"[alert] {alert.probe}: "
                    f"{alert.old_status.value} -> {alert.new_status.value}"
                )
    """

    @abstractmethod
    async def notify(self, alert: AlertRecord) -> None:
        """Send a notification for a probe state change.

        Args:
            alert: The :class:`~fastapi_watch.models.AlertRecord` describing
                the probe, its previous status, its new status, and the
                timestamp the change was detected.
        """


class WebhookAlerter(BaseAlerter):
    """Generic JSON webhook alerter.

    POSTs a JSON payload to any HTTP endpoint on every probe state change.
    Use this for custom integrations or services that accept arbitrary JSON.

    Args:
        url: The webhook URL to POST to.
        headers: Optional HTTP headers added to every request (e.g. auth tokens).
        timeout: Request timeout in seconds (default ``5``).

    Example::

        from fastapi_watch.alerts import WebhookAlerter

        alerter = WebhookAlerter(
            url="https://my-service.internal/hooks/health",
            headers={"Authorization": "Bearer my-secret-token"},
        )

    Payload sent::

        {
            "probe": "redis",
            "old_status": "healthy",
            "new_status": "unhealthy",
            "timestamp": "2026-03-30T12:00:00+00:00"
        }
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> None:
        _validate_webhook_url(url)
        self.url = url
        self.headers = headers or {}
        self.timeout = timeout

    def __repr__(self) -> str:
        return "WebhookAlerter(url='<redacted>')"

    def _build_payload(self, alert: AlertRecord) -> bytes:
        return json.dumps({
            "probe": alert.probe,
            "old_status": alert.old_status.value,
            "new_status": alert.new_status.value,
            "timestamp": alert.timestamp.isoformat(),
        }).encode()

    async def notify(self, alert: AlertRecord) -> None:
        await run_blocking(_post_json, self.url, self._build_payload(alert), self.timeout, self.headers)


class SlackAlerter(BaseAlerter):
    """Slack incoming webhook alerter.

    Sends a formatted Slack message when a probe changes status.
    Requires a `Slack Incoming Webhook URL
    <https://api.slack.com/messaging/webhooks>`_ from your Slack app.

    Args:
        webhook_url: Your Slack Incoming Webhook URL.
        channel: Optional channel override (e.g. ``"#ops-alerts"``). When
            omitted, the message goes to the channel configured in the Slack app.
        username: Bot display name shown in Slack (default ``"fastapi-watch"``).
        timeout: Request timeout in seconds (default ``5``).

    Example::

        from fastapi_watch.alerts import SlackAlerter

        # Single channel
        alerter = SlackAlerter(
            webhook_url="https://hooks.slack.com/services/T.../B.../...",
            channel="#ops-alerts",
        )

        # Multiple channels — create one alerter per webhook URL
        critical_alerter = SlackAlerter(
            webhook_url="https://hooks.slack.com/services/T.../B-crit/...",
            channel="#on-call",
        )
    """

    _STATUS_EMOJI = {
        ProbeStatus.HEALTHY: ":large_green_circle:",
        ProbeStatus.DEGRADED: ":large_yellow_circle:",
        ProbeStatus.UNHEALTHY: ":red_circle:",
    }
    _STATUS_COLOR = {
        ProbeStatus.HEALTHY: "#16a34a",
        ProbeStatus.DEGRADED: "#d97706",
        ProbeStatus.UNHEALTHY: "#dc2626",
    }

    def __init__(
        self,
        webhook_url: str,
        *,
        channel: str | None = None,
        username: str = "fastapi-watch",
        timeout: float = 5.0,
    ) -> None:
        _validate_webhook_url(webhook_url)
        self.webhook_url = webhook_url
        self.channel = channel
        self.username = username
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"SlackAlerter(url='<redacted>', channel={self.channel!r})"

    def _build_payload(self, alert: AlertRecord) -> bytes:
        emoji = self._STATUS_EMOJI.get(alert.new_status, ":white_circle:")
        color = self._STATUS_COLOR.get(alert.new_status, "#94a3b8")
        text = (
            f"{emoji} *{alert.probe}* changed: "
            f"`{alert.old_status.value}` → `{alert.new_status.value}`"
        )
        body: dict[str, Any] = {
            "username": self.username,
            "text": text,
            "attachments": [{
                "color": color,
                "fields": [
                    {"title": "Probe", "value": alert.probe, "short": True},
                    {"title": "New Status", "value": alert.new_status.value, "short": True},
                    {"title": "Previous", "value": alert.old_status.value, "short": True},
                    {"title": "Time", "value": alert.timestamp.isoformat(), "short": True},
                ],
            }],
        }
        if self.channel:
            body["channel"] = self.channel
        return json.dumps(body).encode()

    async def notify(self, alert: AlertRecord) -> None:
        await run_blocking(_post_json, self.webhook_url, self._build_payload(alert), self.timeout)


class TeamsAlerter(BaseAlerter):
    """Microsoft Teams incoming webhook alerter.

    Sends an Adaptive Card notification to a Teams channel when a probe
    changes status. Requires a `Teams Incoming Webhook connector URL
    <https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook>`_.

    Args:
        webhook_url: Your Teams Incoming Webhook URL.
        timeout: Request timeout in seconds (default ``5``).

    Example::

        from fastapi_watch.alerts import TeamsAlerter

        # One alerter per Teams channel/connector
        alerter = TeamsAlerter(
            webhook_url="https://outlook.office.com/webhook/..."
        )
    """

    _STATUS_COLOR = {
        ProbeStatus.HEALTHY: "16a34a",
        ProbeStatus.DEGRADED: "d97706",
        ProbeStatus.UNHEALTHY: "dc2626",
    }

    def __init__(self, webhook_url: str, *, timeout: float = 5.0) -> None:
        _validate_webhook_url(webhook_url)
        self.webhook_url = webhook_url
        self.timeout = timeout

    def __repr__(self) -> str:
        return "TeamsAlerter(url='<redacted>')"

    def _build_payload(self, alert: AlertRecord) -> bytes:
        color = self._STATUS_COLOR.get(alert.new_status, "94a3b8")
        body = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": color,
            "summary": (
                f"{alert.probe}: {alert.old_status.value} → {alert.new_status.value}"
            ),
            "sections": [{
                "activityTitle": f"**{alert.probe}** status changed",
                "activitySubtitle": alert.timestamp.isoformat(),
                "facts": [
                    {"name": "Probe", "value": alert.probe},
                    {"name": "Previous Status", "value": alert.old_status.value},
                    {"name": "New Status", "value": alert.new_status.value},
                    {"name": "Timestamp", "value": alert.timestamp.isoformat()},
                ],
            }],
        }
        return json.dumps(body).encode()

    async def notify(self, alert: AlertRecord) -> None:
        await run_blocking(_post_json, self.webhook_url, self._build_payload(alert), self.timeout)


class PagerDutyAlerter(BaseAlerter):
    """PagerDuty Events API v2 alerter.

    Triggers a PagerDuty incident when a probe goes UNHEALTHY and resolves
    it automatically when the probe recovers to HEALTHY. DEGRADED probes
    trigger with ``severity="warning"`` rather than ``"error"``.

    A stable ``dedup_key`` (``fastapi-watch:<probe-name>``) ensures that
    repeated UNHEALTHY states don't open duplicate incidents, and that a
    HEALTHY transition resolves the existing one.

    Args:
        routing_key: Your PagerDuty 32-character integration routing key.
        source: Event source label shown in PagerDuty (default
            ``"fastapi-watch"``).
        timeout: Request timeout in seconds (default ``5``).

    Example::

        from fastapi_watch.alerts import PagerDutyAlerter

        alerter = PagerDutyAlerter(routing_key="abc123def456abc123def456abc123de")

        # Separate routing keys for different services/escalation policies
        db_alerter = PagerDutyAlerter(
            routing_key="...",
            source="fastapi-watch/database",
        )
    """

    _EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"

    def __init__(
        self,
        routing_key: str,
        *,
        source: str = "fastapi-watch",
        timeout: float = 5.0,
    ) -> None:
        self.routing_key = routing_key
        self.source = source
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"PagerDutyAlerter(routing_key='<redacted>', source={self.source!r})"

    def _build_payload(self, alert: AlertRecord) -> bytes:
        if alert.new_status == ProbeStatus.HEALTHY:
            action = "resolve"
            severity = "info"
        elif alert.new_status == ProbeStatus.DEGRADED:
            action = "trigger"
            severity = "warning"
        else:
            action = "trigger"
            severity = "error"

        body = {
            "routing_key": self.routing_key,
            "event_action": action,
            "dedup_key": f"fastapi-watch:{alert.probe}",
            "payload": {
                "summary": (
                    f"{alert.probe}: {alert.old_status.value} → {alert.new_status.value}"
                ),
                "severity": severity,
                "source": self.source,
                "timestamp": alert.timestamp.isoformat(),
                "custom_details": {
                    "probe": alert.probe,
                    "old_status": alert.old_status.value,
                    "new_status": alert.new_status.value,
                },
            },
        }
        return json.dumps(body).encode()

    async def notify(self, alert: AlertRecord) -> None:
        await run_blocking(_post_json, self._EVENTS_URL, self._build_payload(alert), self.timeout)


class OpsGenieAlerter(BaseAlerter):
    """OpsGenie Alerts API v2 alerter.

    Triggers an OpsGenie alert when a probe goes UNHEALTHY (P1) or DEGRADED
    (P3) and closes it automatically when the probe recovers to HEALTHY.

    A stable alias (``fastapi-watch:<probe-name>``) deduplicates repeated
    UNHEALTHY transitions and ensures the corresponding HEALTHY transition
    closes the same alert.

    Args:
        api_key: Your OpsGenie API key (GenieKey).
        region: ``"us"`` (default) or ``"eu"`` — selects the correct API endpoint.
        source: Event source label shown in OpsGenie (default ``"fastapi-watch"``).
        timeout: Request timeout in seconds (default ``5``).

    Example::

        from fastapi_watch.alerts import OpsGenieAlerter

        alerter = OpsGenieAlerter(api_key="your-api-key-here")

        # EU region
        alerter = OpsGenieAlerter(api_key="your-api-key-here", region="eu")
    """

    _BASE_URL = {
        "us": "https://api.opsgenie.com",
        "eu": "https://api.eu.opsgenie.com",
    }
    _ALLOWED_HOSTS: frozenset[str] = frozenset({"api.opsgenie.com", "api.eu.opsgenie.com"})
    _PRIORITY = {
        ProbeStatus.UNHEALTHY: "P1",
        ProbeStatus.DEGRADED: "P3",
    }

    def __init__(
        self,
        api_key: str,
        *,
        region: str = "us",
        source: str = "fastapi-watch",
        timeout: float = 5.0,
    ) -> None:
        if region not in self._BASE_URL:
            raise ValueError(f"region must be 'us' or 'eu', got {region!r}")
        self.api_key = api_key
        self._base_url = self._BASE_URL[region]
        self.source = source
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"OpsGenieAlerter(source={self.source!r})"

    def _alias(self, probe_name: str) -> str:
        return f"fastapi-watch:{probe_name}"

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"GenieKey {self.api_key}", "Content-Type": "application/json"}

    def _assert_safe_url(self, url: str) -> None:
        """Guard: raise if the URL host is not in the OpsGenie allowlist.

        The API key travels in the ``Authorization`` header, so it must only
        ever be sent to OpsGenie itself.
        """
        host = urllib.parse.urlparse(url).hostname or ""
        if host not in self._ALLOWED_HOSTS:
            raise ValueError(f"OpsGenie URL host {host!r} not in allowed hosts")

    def _send(self, url: str, payload: bytes, action: str, subject: str) -> None:
        """POST to the OpsGenie API, raising ``RuntimeError`` with context on failure.

        Blocking — called through ``run_blocking``.
        """
        self._assert_safe_url(url)
        try:
            _post_json(url, payload, self.timeout, self._auth_headers())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"OpsGenie {action} returned HTTP {exc.code} for {subject}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpsGenie {action} network error for {subject}: {exc.reason}") from exc

    async def notify(self, alert: AlertRecord) -> None:
        alias = self._alias(alert.probe)

        if alert.new_status == ProbeStatus.HEALTHY:
            url = f"{self._base_url}/v2/alerts/{urllib.parse.quote(alias, safe='')}/close?identifierType=alias"
            await run_blocking(self._send, url, json.dumps({}).encode(), "close-alert", f"alias {alias!r}")
        else:
            priority = self._PRIORITY.get(alert.new_status, "P1")
            url = f"{self._base_url}/v2/alerts"
            payload = json.dumps({
                "message": f"{alert.probe}: {alert.old_status.value} → {alert.new_status.value}",
                "alias": alias,
                "description": (
                    f"Probe '{alert.probe}' changed from {alert.old_status.value} "
                    f"to {alert.new_status.value} at {alert.timestamp.isoformat()}"
                ),
                "source": self.source,
                "priority": priority,
                "details": {
                    "probe": alert.probe,
                    "old_status": alert.old_status.value,
                    "new_status": alert.new_status.value,
                },
            }).encode()
            await run_blocking(self._send, url, payload, "create-alert", f"probe {alert.probe!r}")
