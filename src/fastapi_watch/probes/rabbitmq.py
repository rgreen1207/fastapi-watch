from __future__ import annotations

import time

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe


class RabbitMQProbe(BaseProbe):
    """Health probe for RabbitMQ using aio-pika.

    Install with: ``pip install fastapi-watch[rabbitmq]``
    """

    def __init__(
        self,
        url: str = "amqp://guest:guest@localhost/",
        name: str = "rabbitmq",
    ) -> None:
        self.url = url
        self.name = name

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
            if connection is not None:
                await connection.close()
