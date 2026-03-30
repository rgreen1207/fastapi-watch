"""Hand-rolled Prometheus text format exporter.

No ``prometheus_client`` dependency required.  Implements the text exposition
format 0.0.4 which Prometheus and most compatible scrapers understand.
"""
from __future__ import annotations

from .models import ProbeResult, ProbeStatus


def _lbl(name: str, critical: bool) -> str:
    crit = "true" if critical else "false"
    return f'{{name="{name}",critical="{crit}"}}'


def render_prometheus(
    results: list[ProbeResult],
    trips: dict[str, int] | None = None,
) -> str:
    """Render *results* as a Prometheus text-format metrics page.

    Args:
        results: Current probe results.
        trips: Optional dict mapping probe name → lifetime circuit-breaker trip
            count.  Pass an empty dict or ``None`` if the circuit breaker is
            disabled.
    """
    trips = trips or {}
    lines: list[str] = []

    # ── probe_healthy ────────────────────────────────────────────────────────
    lines += [
        "# HELP probe_healthy 1 if probe status is healthy, 0 otherwise",
        "# TYPE probe_healthy gauge",
    ]
    for r in results:
        val = 1 if r.status == ProbeStatus.HEALTHY else 0
        lines.append(f"probe_healthy{_lbl(r.name, r.critical)} {val}")

    # ── probe_degraded ───────────────────────────────────────────────────────
    lines += [
        "# HELP probe_degraded 1 if probe status is degraded (warning), 0 otherwise",
        "# TYPE probe_degraded gauge",
    ]
    for r in results:
        val = 1 if r.status == ProbeStatus.DEGRADED else 0
        lines.append(f"probe_degraded{_lbl(r.name, r.critical)} {val}")

    # ── probe_latency_ms ─────────────────────────────────────────────────────
    lines += [
        "# HELP probe_latency_ms Last observed probe check latency in milliseconds",
        "# TYPE probe_latency_ms gauge",
    ]
    for r in results:
        lines.append(f"probe_latency_ms{_lbl(r.name, r.critical)} {r.latency_ms}")

    # ── probe_circuit_open ───────────────────────────────────────────────────
    lines += [
        "# HELP probe_circuit_open 1 if the circuit breaker is currently open for this probe",
        "# TYPE probe_circuit_open gauge",
    ]
    for r in results:
        cb = (r.details or {}).get("circuit_breaker", {})
        val = 1 if cb.get("open", False) else 0
        lines.append(f"probe_circuit_open{_lbl(r.name, r.critical)} {val}")

    # ── probe_circuit_consecutive_failures ───────────────────────────────────
    lines += [
        "# HELP probe_circuit_consecutive_failures Current consecutive failure count for this probe",
        "# TYPE probe_circuit_consecutive_failures gauge",
    ]
    for r in results:
        cb = (r.details or {}).get("circuit_breaker", {})
        val = cb.get("consecutive_failures", 0)
        lines.append(f"probe_circuit_consecutive_failures{_lbl(r.name, r.critical)} {val}")

    # ── probe_circuit_trips_total ────────────────────────────────────────────
    lines += [
        "# HELP probe_circuit_trips_total Total lifetime circuit breaker trips for this probe",
        "# TYPE probe_circuit_trips_total counter",
    ]
    for r in results:
        val = trips.get(r.name, 0)
        lines.append(f"probe_circuit_trips_total{_lbl(r.name, r.critical)} {val}")

    lines.append("")  # trailing newline required by spec
    return "\n".join(lines)
