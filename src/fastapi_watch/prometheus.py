"""Hand-rolled Prometheus text format exporter.

No ``prometheus_client`` dependency required.  Implements the text exposition
format 0.0.4 which Prometheus and most compatible scrapers understand.

:class:`~fastapi_watch.HealthRegistry` serves the rendered output at
``GET <prefix>/metrics`` (default: ``GET /health/metrics``).  Point your
Prometheus scrape config at that URL::

    # prometheus.yml
    scrape_configs:
      - job_name: fastapi-watch
        static_configs:
          - targets: ["my-service:8000"]
        metrics_path: /health/metrics

Exposed metrics (all carry ``name`` and ``critical`` labels):

* ``probe_healthy`` — 1 if the probe is HEALTHY, 0 otherwise
* ``probe_degraded`` — 1 if the probe is DEGRADED, 0 otherwise
* ``probe_latency_ms`` — last observed check latency in milliseconds
* ``probe_circuit_open`` — 1 if the circuit breaker is currently open
* ``probe_circuit_consecutive_failures`` — current consecutive failure count
* ``probe_circuit_trips_total`` — lifetime circuit-breaker trip count

These names collide with blackbox_exporter's ``probe_*`` metrics when both are
scraped into one Prometheus.  Pass ``HealthRegistry(metrics_prefix=...)`` (for
example ``"fastapi_watch_"``) to namespace them; the default ``""`` keeps the
names above so existing dashboards and alerts continue to work.
"""
import math
import re
from typing import Callable

from .models import ProbeResult, ProbeStatus

# Valid start of a Prometheus metric name; the prefix is prepended to "probe_...".
_METRIC_PREFIX_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")


def validate_metrics_prefix(prefix: str) -> str:
    """Return *prefix* if it can begin a Prometheus metric name, else raise ``ValueError``."""
    if prefix and not _METRIC_PREFIX_RE.fullmatch(prefix):
        raise ValueError(
            f"metrics_prefix {prefix!r} is not a valid Prometheus metric name prefix "
            "(letters, digits, '_' and ':', not starting with a digit)"
        )
    return prefix


def _escape_label(value: str) -> str:
    """Escape a Prometheus label value per the text format spec."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _lbl(name: str, critical: bool) -> str:
    crit = "true" if critical else "false"
    return f'{{name="{_escape_label(name)}",critical="{crit}"}}'


def _circuit(result: ProbeResult) -> dict:
    # Without the registry's breaker, a custom probe's own "circuit_breaker"
    # detail passes through untouched and may be anything.
    info = (result.details or {}).get("circuit_breaker")
    return info if isinstance(info, dict) else {}


def _sample(value: object) -> str:
    """*value* as a sample value: a number, ``NaN`` or ``±Inf``.

    One value Prometheus cannot parse (``True``, ``nan``, a string) fails the
    whole scrape, so anything that is not a number is written as ``NaN``.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "+Inf" if value > 0 else "-Inf"
        return repr(value)
    if isinstance(value, int):
        return str(value)
    return "NaN"


def render_prometheus(
    results: list[ProbeResult],
    trips: dict[str, int] | None = None,
    prefix: str = "",
) -> str:
    """Render *results* as a Prometheus text-format metrics page.

    Args:
        results: Current probe results.
        trips: Optional dict mapping probe name → lifetime circuit-breaker trip
            count.  Pass an empty dict or ``None`` if the circuit breaker is
            disabled.
        prefix: Prepended to every metric name (default ``""``).
    """
    trips = trips or {}
    # A probe with nothing to report has no status: it gets no probe_healthy /
    # probe_degraded sample (a 0 would fire "probe down" alerts on idle
    # routes), and probe_no_data says why.
    metrics: list[tuple[str, str, str, Callable[[ProbeResult], object], bool]] = [
        ("probe_healthy", "gauge", "1 if probe status is healthy, 0 otherwise; absent while the probe has no data",
         lambda r: 1 if r.status == ProbeStatus.HEALTHY else 0, True),
        ("probe_degraded", "gauge", "1 if probe status is degraded (warning), 0 otherwise; absent while the probe has no data",
         lambda r: 1 if r.status == ProbeStatus.DEGRADED else 0, True),
        ("probe_no_data", "gauge", "1 while the probe has nothing to report yet (no status), 0 otherwise",
         lambda r: 0 if r.has_data else 1, False),
        ("probe_latency_ms", "gauge", "Last observed probe check latency in milliseconds",
         lambda r: r.latency_ms, False),
        ("probe_circuit_open", "gauge", "1 if the circuit breaker is currently open for this probe",
         lambda r: 1 if _circuit(r).get("open", False) else 0, False),
        ("probe_circuit_consecutive_failures", "gauge", "Current consecutive failure count for this probe",
         lambda r: _circuit(r).get("consecutive_failures", 0), False),
        ("probe_circuit_trips_total", "counter", "Total lifetime circuit breaker trips for this probe",
         lambda r: trips.get(r.name, 0), False),
    ]

    lines: list[str] = []
    for name, kind, help_text, value, needs_data in metrics:
        metric = f"{prefix}{name}"
        lines += [f"# HELP {metric} {help_text}", f"# TYPE {metric} {kind}"]
        lines += [
            f"{metric}{_lbl(r.name, r.critical)} {_sample(value(r))}"
            for r in results
            if r.has_data or not needs_data
        ]

    lines.append("")  # trailing newline required by spec
    return "\n".join(lines)
