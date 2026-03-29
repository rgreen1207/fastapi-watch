import time
from urllib.parse import urlparse

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe


class HttpProbe(BaseProbe):
    """Health probe that performs an HTTP GET against an upstream URL.

    Returns the HTTP status code, content type, and response body size.

    Install with: ``pip install fastapi-watch[http]``

    Args:
        url: URL to check.
        timeout: Request timeout in seconds (default 5.0).
        name: Probe name. Defaults to the URL host.
        expected_status: HTTP status code considered healthy (default 200).
    """

    def __init__(
        self,
        url: str,
        timeout: float = 5.0,
        name: str | None = None,
        expected_status: int = 200,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.expected_status = expected_status
        self.name = name if name is not None else (urlparse(url).netloc or url)

    async def check(self) -> ProbeResult:
        try:
            import aiohttp
        except ImportError as exc:
            raise ImportError(
                "Install fastapi-watch[http] to use HttpProbe."
            ) from exc

        start = time.perf_counter()
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self.url) as response:
                    latency = (time.perf_counter() - start) * 1000
                    # Prefer Content-Length to avoid reading the full body.
                    cl = response.headers.get("Content-Length")
                    if cl is not None:
                        response_bytes = int(cl)
                    else:
                        response_bytes = len(await response.read())
                    details = {
                        "status_code": response.status,
                        "content_type": response.headers.get("Content-Type"),
                        "response_bytes": response_bytes,
                    }
                    if response.status == self.expected_status:
                        return ProbeResult(
                            name=self.name,
                            status=ProbeStatus.HEALTHY,
                            latency_ms=round(latency, 2),
                            details=details,
                        )
                    return ProbeResult(
                        name=self.name,
                        status=ProbeStatus.UNHEALTHY,
                        latency_ms=round(latency, 2),
                        error=f"HTTP {response.status}",
                        details=details,
                    )
        except Exception as exc:
            latency = (time.perf_counter() - start) * 1000
            return ProbeResult(
                name=self.name,
                status=ProbeStatus.UNHEALTHY,
                latency_ms=round(latency, 2),
                error=str(exc),
            )
