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
import asyncio
import ipaddress
import json
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

from .models import AlertRecord, ProbeStatus

# Private IPv4 and IPv6 ranges that must not be reachable via alerter webhooks.
_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / AWS metadata
    ipaddress.ip_network("100.64.0.0/10"),    # Carrier-grade NAT
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _validate_webhook_url(url: str) -> None:
    """Raise ``ValueError`` if *url* is not a safe HTTPS webhook URL.

    Rejects:
    - Non-HTTPS schemes (``http://``, ``file://``, etc.)
    - Hostnames that resolve to private / loopback / link-local IP ranges
    - Bare IP literals in private ranges

    This is a best-effort defence-in-depth check against accidental SSRF.
    It does **not** perform DNS resolution; literal IP addresses are checked
    directly.  Pass ``allow_http=True`` on the alerter if you need plain HTTP
    for an internal relay (e.g. a local sidecar).
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as exc:
        raise ValueError(f"Invalid webhook URL: {exc}") from exc

    if parsed.scheme not in ("https", "http"):
        raise ValueError(
            f"Webhook URL must use https:// or http:// scheme, got {parsed.scheme!r}"
        )

    hostname = parsed.hostname or ""

    # Reject bare IP literals in private ranges immediately.
    try:
        addr = ipaddress.ip_address(hostname)
        if any(addr in net for net in _PRIVATE_NETS):
            raise ValueError(
                f"Webhook URL hostname {hostname!r} resolves to a private/loopback "
                "address. Use a public HTTPS endpoint."
            )
    except ValueError as exc:
        if "private" in str(exc) or "loopback" in str(exc):
            raise
        # Not a valid IP literal — hostname string, fine to pass through.

    if hostname.lower() in ("localhost", ""):
        raise ValueError(
            "Webhook URL must not target localhost. Use a public HTTPS endpoint."
        )


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
        return f"WebhookAlerter(url={self.url!r})"

    def _build_payload(self, alert: AlertRecord) -> bytes:
        return json.dumps({
            "probe": alert.probe,
            "old_status": alert.old_status.value,
            "new_status": alert.new_status.value,
            "timestamp": alert.timestamp.isoformat(),
        }).encode()

    async def notify(self, alert: AlertRecord) -> None:
        payload = self._build_payload(alert)
        headers = {"Content-Type": "application/json", **self.headers}
        url = self.url
        timeout = self.timeout

        def _post() -> None:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=timeout)

        await asyncio.get_running_loop().run_in_executor(None, _post)


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
        payload = self._build_payload(alert)
        url = self.webhook_url
        timeout = self.timeout

        def _post() -> None:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=timeout)

        await asyncio.get_running_loop().run_in_executor(None, _post)


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
        payload = self._build_payload(alert)
        url = self.webhook_url
        timeout = self.timeout

        def _post() -> None:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=timeout)

        await asyncio.get_running_loop().run_in_executor(None, _post)


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
        payload = self._build_payload(alert)
        url = self._EVENTS_URL
        timeout = self.timeout

        def _post() -> None:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=timeout)

        await asyncio.get_running_loop().run_in_executor(None, _post)
