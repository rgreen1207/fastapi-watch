import asyncio
import time
from urllib.parse import urlparse

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe


class RabbitMQProbe(BaseProbe):
    """Health probe for RabbitMQ using aio-pika.

    When ``management_url`` is provided, calls the RabbitMQ Management HTTP API
    to return rich details: server version, node info, per-queue message counts,
    consumer counts, publish/deliver/ack rates, and cluster-wide totals.

    Without ``management_url`` the probe only verifies TCP connectivity.

    Install with: ``pip install fastapi-watch[rabbitmq]``

    Args:
        url: AMQP(S) connection URL (default ``amqp://guest:guest@localhost/``).
        name: Probe name shown in health reports.
        management_url: Base URL of the Management API
            (e.g. ``http://localhost:15672``). Credentials are taken from *url*.
            If omitted, no queue-level details are collected.
    """

    def __init__(
        self,
        url: str = "amqp://guest:guest@localhost/",
        name: str = "rabbitmq",
        management_url: str | None = None,
    ) -> None:
        self.url = url
        self.name = name
        self.management_url = management_url

        parsed = urlparse(url)
        self._mgmt_user = parsed.username or "guest"
        self._mgmt_password = parsed.password or "guest"

    async def check(self) -> ProbeResult:
        try:
            import aio_pika
        except ImportError as exc:
            raise ImportError(
                "Install fastapi-watch[rabbitmq] to use RabbitMQProbe."
            ) from exc

        start = time.perf_counter()
        connection = None
        try:
            connection = await aio_pika.connect(self.url)
            latency = (time.perf_counter() - start) * 1000

            details: dict = {"connected": True}

            if self.management_url:
                await self._enrich_from_management_api(details)

            return ProbeResult(
                name=self.name,
                status=ProbeStatus.HEALTHY,
                latency_ms=round(latency, 2),
                details=details,
            )
        except Exception as exc:
            latency = (time.perf_counter() - start) * 1000
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.UNHEALTHY,
                latency_ms=round(latency, 2),
                error=str(exc),
                details={"connected": False},
            )
        finally:
            if connection is not None:
                await connection.close()

    async def _enrich_from_management_api(self, details: dict) -> None:
        """Populate *details* with data from the RabbitMQ Management HTTP API."""
        try:
            import aiohttp
        except ImportError:
            details["management_api_error"] = (
                "aiohttp not installed — install fastapi-watch[http] for rich RabbitMQ details"
            )
            return

        base = self.management_url.rstrip("/") + "/api"
        auth = aiohttp.BasicAuth(self._mgmt_user, self._mgmt_password)

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5.0)) as session:

                async def _get_json(path: str) -> dict | list:
                    async with session.get(f"{base}/{path}", auth=auth) as resp:
                        resp.raise_for_status()
                        return await resp.json()

                # Fetch overview and queues in parallel.
                overview, queues_data = await asyncio.gather(
                    _get_json("overview"),
                    _get_json("queues"),
                )

                totals = overview.get("queue_totals", {})
                stats = overview.get("message_stats", {})
                objects = overview.get("object_totals", {})

                details["server"] = {
                    "rabbitmq_version": overview.get("rabbitmq_version"),
                    "erlang_version": overview.get("erlang_version"),
                    "cluster_name": overview.get("cluster_name"),
                    "node": overview.get("node"),
                    "connections": objects.get("connections", 0),
                    "channels": objects.get("channels", 0),
                    "exchanges": objects.get("exchanges", 0),
                    "queues": objects.get("queues", 0),
                    "consumers": objects.get("consumers", 0),
                }
                details["totals"] = {
                    "messages": totals.get("messages", 0),
                    "messages_ready": totals.get("messages_ready", 0),
                    "messages_unacknowledged": totals.get("messages_unacknowledged", 0),
                    "publish_rate": stats.get("publish_details", {}).get("rate", 0),
                    "deliver_rate": stats.get("deliver_get_details", {}).get("rate", 0),
                    "ack_rate": stats.get("ack_details", {}).get("rate", 0),
                }

                queues = {}
                for q in queues_data:
                    qstats = q.get("message_stats", {})
                    queues[q["name"]] = {
                        "state": q.get("state"),
                        "messages": q.get("messages", 0),
                        "messages_ready": q.get("messages_ready", 0),
                        "messages_unacknowledged": q.get("messages_unacknowledged", 0),
                        "consumers": q.get("consumers", 0),
                        "memory_bytes": q.get("memory"),
                        "publish_rate": qstats.get("publish_details", {}).get("rate", 0),
                        "deliver_rate": qstats.get("deliver_get_details", {}).get("rate", 0),
                        "ack_rate": qstats.get("ack_details", {}).get("rate", 0),
                        "durable": q.get("durable"),
                        "auto_delete": q.get("auto_delete"),
                        "idle_since": q.get("idle_since"),
                    }
                details["queues"] = queues

        except Exception as exc:
            details["management_api_error"] = str(exc)
