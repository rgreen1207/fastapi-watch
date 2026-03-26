from __future__ import annotations

import time
from typing import Union

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe


class KafkaProbe(BaseProbe):
    """Health probe for Apache Kafka using aiokafka.

    Install with: ``pip install fastapi-watch[kafka]``

    Connects an ``AIOKafkaAdminClient`` to verify broker reachability.
    No topics are created or consumed.

    Args:
        bootstrap_servers: Broker address(es) as a string
            (``"localhost:9092"``) or list (``["b1:9092", "b2:9092"]``).
        name: Probe name shown in health reports.
        request_timeout_ms: Timeout for the admin client metadata request
            in milliseconds (default 5000).
    """

    def __init__(
        self,
        bootstrap_servers: Union[str, list[str]] = "localhost:9092",
        name: str = "kafka",
        request_timeout_ms: int = 5000,
    ) -> None:
        if isinstance(bootstrap_servers, list):
            self.bootstrap_servers = ",".join(bootstrap_servers)
        else:
            self.bootstrap_servers = bootstrap_servers
        self.name = name
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
            latency = (time.perf_counter() - start) * 1000
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.HEALTHY,
                latency_ms=round(latency, 2),
            )
        except Exception as exc:
            latency = (time.perf_counter() - start) * 1000
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.UNHEALTHY,
                latency_ms=round(latency, 2),
                error=str(exc),
            )
        finally:
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
