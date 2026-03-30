import time
from urllib.parse import urlparse

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe

_SUPPORTED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


class HttpProbe(BaseProbe):
    """Health probe that performs an HTTP request against an upstream URL.

    Supports all standard HTTP methods so you can probe any REST endpoint,
    not just read-only health checks.

    Install with: ``pip install fastapi-watch[http]``

    Args:
        url: URL to request.
        method: HTTP method to use (default ``"GET"``). Accepts ``GET``,
            ``POST``, ``PUT``, ``PATCH``, ``DELETE``, ``HEAD``, or ``OPTIONS``
            (case-insensitive).
        json: Optional JSON-serialisable body sent with the request. Ignored
            for ``GET``, ``HEAD``, and ``OPTIONS``.
        headers: Optional dict of HTTP headers to include in the request.
        timeout: Request timeout in seconds (default 5.0).
        name: Probe name. Defaults to the URL host.
        expected_status: HTTP status code considered healthy (default 200).

    Example::

        # Simple GET health check (default)
        registry.add(HttpProbe(url="https://api.upstream.com/health"))

        # POST to a write endpoint, expect 201
        registry.add(HttpProbe(
            url="https://api.example.com/items",
            method="POST",
            json={"name": "probe-test"},
            expected_status=201,
            name="items-write",
        ))

        # DELETE with auth header, expect 204
        registry.add(HttpProbe(
            url="https://api.example.com/items/probe-test",
            method="DELETE",
            headers={"Authorization": "Bearer <token>"},
            expected_status=204,
            name="items-delete",
        ))
    """

    def __init__(
        self,
        url: str,
        method: str = "GET",
        json: dict | None = None,
        headers: dict | None = None,
        timeout: float = 5.0,
        name: str | None = None,
        expected_status: int = 200,
        poll_interval_ms: int | None = None,
    ) -> None:
        method = method.upper()
        if method not in _SUPPORTED_METHODS:
            raise ValueError(
                f"Unsupported method {method!r}. Must be one of: {', '.join(sorted(_SUPPORTED_METHODS))}"
            )
        self.url = url
        self.method = method
        self.json = json
        self.headers = headers
        self.timeout = timeout
        self.expected_status = expected_status
        self.name = name if name is not None else (urlparse(url).netloc or url)
        self.poll_interval_ms = poll_interval_ms

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
            async with aiohttp.ClientSession(timeout=timeout, headers=self.headers) as session:
                request = getattr(session, self.method.lower())
                kwargs = {}
                if self.json is not None and self.method not in {"GET", "HEAD", "OPTIONS"}:
                    kwargs["json"] = self.json
                async with request(self.url, **kwargs) as response:
                    latency = (time.perf_counter() - start) * 1000
                    cl = response.headers.get("Content-Length")
                    if cl is not None:
                        response_bytes = int(cl)
                    else:
                        response_bytes = len(await response.read())
                    details = {
                        "method": self.method,
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
