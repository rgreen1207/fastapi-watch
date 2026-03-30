import time
from urllib.parse import urlparse

from ..models import ProbeResult, ProbeStatus
from .base import BaseProbe


class MySQLProbe(BaseProbe):
    """Health probe for MySQL / MariaDB using aiomysql.

    Returns server version and the current number of connected threads.

    Install with: ``pip install fastapi-watch[mysql]``

    Accepts either a URL or explicit keyword arguments:

    .. code-block:: python

        # URL form
        MySQLProbe(url="mysql://user:pass@localhost:3306/mydb")

        # Keyword form
        MySQLProbe(host="localhost", port=3306, user="root", password="s3cr3t", db="mydb")

    Args:
        url: MySQL DSN — ``mysql://user:pass@host:port/db``.
            If provided, overrides all other connection parameters.
        host: Database host (default ``localhost``).
        port: Database port (default ``3306``).
        user: Database user.
        password: Database password.
        db: Database name.
        name: Probe name shown in health reports.
        connect_timeout: Connection timeout in seconds (default 5).
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        host: str = "localhost",
        port: int = 3306,
        user: str = "root",
        password: str = "",
        db: str = "",
        name: str = "mysql",
        connect_timeout: int = 5,
        poll_interval_ms: int | None = None,
    ) -> None:
        self.name = name
        self.poll_interval_ms = poll_interval_ms
        self._timeout = connect_timeout

        if url is not None:
            parsed = urlparse(url)
            self._host = parsed.hostname or "localhost"
            self._port = parsed.port or 3306
            self._user = parsed.username or "root"
            self._password = parsed.password or ""
            self._db = parsed.path.lstrip("/") if parsed.path else ""
        else:
            self._host = host
            self._port = port
            self._user = user
            self._password = password
            self._db = db

    async def check(self) -> ProbeResult:
        try:
            import aiomysql
        except ImportError as exc:
            raise ImportError(
                "Install fastapi-watch[mysql] to use MySQLProbe."
            ) from exc

        start = time.perf_counter()
        conn = None
        try:
            conn = await aiomysql.connect(
                host=self._host,
                port=self._port,
                user=self._user,
                password=self._password,
                db=self._db,
                connect_timeout=self._timeout,
            )
            details: dict = {}
            async with conn.cursor() as cur:
                await cur.execute("SELECT VERSION()")
                row = await cur.fetchone()
                details["version"] = row[0] if row else None

                await cur.execute("SHOW STATUS LIKE 'Threads_connected'")
                row = await cur.fetchone()
                details["connected_threads"] = int(row[1]) if row else None

                await cur.execute("SHOW STATUS LIKE 'Uptime'")
                row = await cur.fetchone()
                details["uptime_seconds"] = int(row[1]) if row else None

                await cur.execute("SHOW STATUS LIKE 'Max_used_connections'")
                row = await cur.fetchone()
                details["max_used_connections"] = int(row[1]) if row else None

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
                error=str(exc),
            )
        finally:
            if conn is not None:
                conn.close()
