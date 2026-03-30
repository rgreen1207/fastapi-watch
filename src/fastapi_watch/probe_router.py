"""Probe router for organising probes across modules.

:class:`ProbeRouter` mirrors the FastAPI ``APIRouter`` pattern: declare probes
in submodules, then include the routers in :class:`~fastapi_watch.HealthRegistry`
at application startup.  Routers can also be nested inside other routers.
"""
from .probes.base import BaseProbe


class ProbeGroup:
    """Group probes defined across modules and register them with a :class:`~fastapi_watch.HealthRegistry`.

    Declare probes anywhere in your codebase, then pass the group(s) to
    :class:`~fastapi_watch.HealthRegistry` at startup — the same pattern as
    FastAPI's ``APIRouter``.

    Usage::

        # features/database/probes.py
        from fastapi_watch import ProbeGroup, PostgreSQLProbe, RedisProbe

        db_probes = ProbeGroup()
        db_probes.add(PostgreSQLProbe(url=settings.DATABASE_URL))
        db_probes.add(RedisProbe(url=settings.REDIS_URL), critical=False)

        # main.py
        from fastapi_watch import HealthRegistry
        from features.database.probes import db_probes

        registry = HealthRegistry(app, groups=[db_probes])

    Groups can also be nested::

        # top-level probe aggregator
        from fastapi_watch import ProbeGroup
        from features.database.probes import db_probes
        from features.users.probes import user_probes

        all_probes = ProbeGroup()
        all_probes.include(db_probes)
        all_probes.include(user_probes)
    """

    def __init__(self) -> None:
        self._probes: list[tuple[BaseProbe, bool]] = []

    def add(self, probe: BaseProbe, critical: bool = True) -> "ProbeGroup":
        """Add a single probe. Returns ``self`` for chaining.

        Args:
            probe: The probe to register.
            critical: When ``True`` (default) a failing probe marks the overall
                status as unhealthy.
        """
        if not any(p is probe for p, _ in self._probes):
            self._probes.append((probe, critical))
        return self

    def add_probes(self, probes: list[BaseProbe], critical: bool = True) -> "ProbeGroup":
        """Add multiple probes with the same criticality. Returns ``self`` for chaining."""
        for probe in probes:
            self.add(probe, critical=critical)
        return self

    def include(self, group: "ProbeGroup") -> "ProbeGroup":
        """Merge another group's probes into this one. Returns ``self`` for chaining.

        Preserves the criticality setting of each probe as declared in the
        included group.  Silently skips probes already registered (identity check).
        """
        for probe, critical in group._probes:
            self.add(probe, critical=critical)
        return self
