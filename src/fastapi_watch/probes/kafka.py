import asyncio
import time

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe


class KafkaProbe(BaseProbe):
    """Health probe for Apache Kafka using aiokafka.

    Actively connects an admin client on each poll, lists topics, and
    describes the cluster. No topics are created or consumed.

    Install with: ``pip install fastapi-watch[kafka]``

    Args:
        bootstrap_servers: Broker address(es) as a string
            (``"localhost:9092"``) or list (``["b1:9092", "b2:9092"]``).
        name: Probe name shown in health reports.
        request_timeout_ms: Timeout for the admin client metadata request
            in milliseconds (default 5000).

    Example::

        registry.add(KafkaProbe("broker1:9092,broker2:9092", name="kafka"))

        # Multiple brokers as a list
        registry.add(KafkaProbe(
            bootstrap_servers=["b1:9092", "b2:9092"],
            name="kafka",
            request_timeout_ms=3000,
        ))
    """

    def __init__(
        self,
        bootstrap_servers: str | list[str] = "localhost:9092",
        name: str = "kafka",
        request_timeout_ms: int = 5000,
        poll_interval_ms: int | None = None,
    ) -> None:
        if isinstance(bootstrap_servers, list):
            self.bootstrap_servers = ",".join(bootstrap_servers)
        else:
            self.bootstrap_servers = bootstrap_servers
        self.name = name
        self.poll_interval_ms = poll_interval_ms
        self._timeout_ms = request_timeout_ms

    async def check(self) -> ProbeResult:
        try:
            from aiokafka.admin import AIOKafkaAdminClient
        except ImportError as exc:
            raise ImportError(
                "Install fastapi-watch[kafka] to use KafkaProbe."
            ) from exc

        start = time.perf_counter()
        client = None
        try:
            client = AIOKafkaAdminClient(
                bootstrap_servers=self.bootstrap_servers,
                request_timeout_ms=self._timeout_ms,
            )
            await client.start()

            details: dict = {}
            try:
                topics, cluster = await asyncio.gather(
                    client.list_topics(),
                    client.describe_cluster(),
                )
                details["broker_count"] = len(cluster.brokers)
                details["controller_id"] = cluster.controller_id
                details["topics"] = sorted(t for t in topics if not t.startswith("__"))
                details["internal_topics"] = sorted(t for t in topics if t.startswith("__"))
            except Exception:
                pass  # details are best-effort

            latency = (time.perf_counter() - start) * 1000
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
                error="probe check failed",
            )
        finally:
            if client is not None:
                try:
                    await asyncio.wait_for(client.close(), timeout=5.0)
                except Exception:
                    pass
