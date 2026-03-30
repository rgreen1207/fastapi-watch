"""Probe router for organising probes across modules.

:class:`ProbeRouter` mirrors the FastAPI ``APIRouter`` pattern: declare probes
in submodules, then include the routers in :class:`~fastapi_watch.HealthRegistry`
at application startup.  Routers can also be nested inside other routers.
"""
from .probes.base import BaseProbe


class ProbeRouter:
    """Collect probes defined across modules and include them in a :class:`~fastapi_watch.HealthRegistry`.

    Mirrors the pattern of FastAPI's ``APIRouter``: declare probes anywhere in
    your codebase, then pass the router(s) to :class:`~fastapi_watch.HealthRegistry`
    at startup.

    Usage::

        # features/database/probes.py
        from fastapi_watch import ProbeRouter, PostgreSQLProbe, RedisProbe

        router = ProbeRouter()
        router.add(PostgreSQLProbe(url=settings.DATABASE_URL))
        router.add(RedisProbe(url=settings.REDIS_URL), critical=False)

        # main.py
        from fastapi_watch import HealthRegistry
        from features.database.probes import router as db_router

        registry = HealthRegistry(app, routers=[db_router])

    Routers can also be nested::

        # top-level probe aggregator
        from fastapi_watch import ProbeRouter
        from features.database.probes import router as db_router
        from features.users.probes import router as users_router

        router = ProbeRouter()
        router.include_router(db_router)
        router.include_router(users_router)
    """

    def __init__(self) -> None:
        self._probes: list[tuple[BaseProbe, bool]] = []

    def add(self, probe: BaseProbe, critical: bool = True) -> "ProbeRouter":
        """Add a single probe. Returns ``self`` for chaining.

        Args:
            probe: The probe to register.
            critical: When ``True`` (default) a failing probe marks the overall
                status as unhealthy.
        """
        if not any(p is probe for p, _ in self._probes):
            self._probes.append((probe, critical))
        return self

    def add_probes(self, probes: list[BaseProbe], critical: bool = True) -> "ProbeRouter":
        """Add multiple probes with the same criticality. Returns ``self`` for chaining."""
        for probe in probes:
            self.add(probe, critical=critical)
        return self

    def include_router(self, router: "ProbeRouter") -> "ProbeRouter":
        """Merge another router's probes into this one. Returns ``self`` for chaining.

        Preserves the criticality setting of each probe as declared in the
        included router.  Silently skips probes already registered (identity check).
        """
        for probe, critical in router._probes:
            self.add(probe, critical=critical)
        return self
