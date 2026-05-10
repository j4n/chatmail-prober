"""Prometheus metric definitions and update logic."""

from __future__ import annotations

import logging
import socket
import statistics
import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .probe import ProbeResult

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    disable_created_metrics,
)

from .probe import _classify_error

# Suppress the _created timestamp lines added by prometheus_client for each
# counter and histogram series -- they double the textfile size and are not
# useful for node_exporter's textfile collector.
# prometheus-client ships py.typed but disable_created_metrics() itself is
# unannotated upstream.  Single inline ignore is cleaner than scattering
# Any-casts; remove if/when prometheus-client tightens its annotations.
disable_created_metrics()  # type: ignore[no-untyped-call]

log = logging.getLogger(__name__)

LABELS = ["source", "destination", "probe_type"]

# Separate registry without default process_* / gc_* collectors.
# Used for textfile output to avoid collisions with node_exporter's
# own process metrics. The default REGISTRY (with process collectors)
# is still used for the HTTP /metrics endpoint.
CMPING_REGISTRY = CollectorRegistry()

rtt_median = Gauge(
    "cmping_rtt_median_seconds",
    "Median round-trip time for the last probe round",
    LABELS,
    registry=CMPING_REGISTRY,
)

rtt_stddev = Gauge(
    "cmping_rtt_stddev_seconds",
    "Standard deviation of round-trip times for the last probe round",
    LABELS,
    registry=CMPING_REGISTRY,
)

rtt_p90 = Gauge(
    "cmping_rtt_p90_seconds",
    "90th-percentile round-trip time for the last probe round",
    LABELS,
    registry=CMPING_REGISTRY,
)

rtt_p10 = Gauge(
    "cmping_rtt_p10_seconds",
    "10th-percentile round-trip time for the last probe round",
    LABELS,
    registry=CMPING_REGISTRY,
)

send_errors_total = Counter(
    "cmping_send_errors_total",
    "Total number of failed probe rounds (timeout, crash, setup failure)",
    LABELS,
    registry=CMPING_REGISTRY,
)

probe_success = Gauge(
    "cmping_probe_success",
    "Whether the last probe round succeeded (1) or failed (0)",
    LABELS,
    registry=CMPING_REGISTRY,
)

probe_loss_ratio = Gauge(
    "cmping_probe_loss_ratio",
    "Fraction of pings lost in the last probe round (0.0 = no loss, 1.0 = all lost)",
    LABELS,
    registry=CMPING_REGISTRY,
)

account_setup_seconds = Gauge(
    "cmping_account_setup_seconds",
    "Time spent on account setup in the last probe round",
    LABELS,
    registry=CMPING_REGISTRY,
)

last_round_timestamp = Gauge(
    "cmping_last_round_completion_timestamp",
    "Unix timestamp of the last completed probe round (for staleness alerting)",
    registry=CMPING_REGISTRY,
)
last_round_timestamp.set(float("nan"))  # NaN until first round completes

round_duration_seconds = Gauge(
    "cmping_round_duration_seconds",
    "Wall-clock duration of the last completed probe round",
    registry=CMPING_REGISTRY,
)
round_duration_seconds.set(float("nan"))  # NaN until first round completes

rounds_total = Counter(
    "cmping_rounds_total",
    "Total number of probe rounds completed since process start",
    registry=CMPING_REGISTRY,
)

account_creations_total = Counter(
    "cmping_account_creations_total",
    "Total chatmail accounts created since process start",
    ["relay"],
    registry=CMPING_REGISTRY,
)

relay_status = Gauge(
    "cmping_relay_status",
    (
        "Relay availability status from alive checks. Integer encodes state: "
        "1=online, 0=unknown, -1=timeout, -2=setup, -3=auth, "
        "-4=tls, -5=connection_refused, -6=dns. Label: relay."
    ),
    ["relay"],
    registry=CMPING_REGISTRY,
)

relay_connections = Gauge(
    "cmping_relay_connections",
    "Current network connection count to relay (established TCP connections)",
    ["relay"],
    registry=CMPING_REGISTRY,
)


def _drop_labels(
    metric: Any, keep: Callable[[tuple[str, ...]], bool]
) -> None:
    """Remove label sets where keep(label_tuple) is False."""
    for label_values in list(metric._metrics.keys()):
        if not keep(label_values):
            metric.remove(*label_values)


def clear_stale_labels(active_relays: list[str]) -> None:
    """Remove label sets for relays no longer in the active set.

    Prevents label cardinality from growing unbounded when relays are
    removed from the relay list across process restarts or alive-check
    exclusions.
    """
    all_metrics = [
        rtt_median, rtt_stddev, rtt_p90, rtt_p10,
        probe_success, probe_loss_ratio, account_setup_seconds,
        send_errors_total,
    ]
    active = set(active_relays)
    for metric in all_metrics:
        _drop_labels(metric, lambda lv: lv[0] in active and lv[1] in active)


_CATEGORY_TO_STATUS: dict[str | None, int] = {
    None: 1,
    "timeout": -1,
    "connection_refused": -5,
    "dns": -6,
    "tls": -4,
    "auth": -3,
    "setup": -2,
    "unknown": 0,
}


def relay_status_value(error_str: str | None) -> int:
    """Map alive-check error string to cmping_relay_status integer.

    Delegates to _classify_error() so all pattern matching lives in one place.
    Return values: 1=ok, 0=unknown, -1=timeout, -2=setup, -3=auth,
    -4=tls, -5=connection_refused, -6=dns.
    """
    return _CATEGORY_TO_STATUS.get(_classify_error(error_str), 0)


def verify_relay_status(relay: str | None, error_str: str | None) -> int:
    """Get relay status value, cross-checking DNS errors against real resolution.

    When the RPC reports DNS failure but the base domain resolves, the
    error is reclassified as timeout (broken autoconfig, not missing DNS).
    """
    value = relay_status_value(error_str)
    if value != -6 or relay is None:
        return value
    # RPC says DNS failure -- verify by resolving the base domain.
    try:
        socket.getaddrinfo(relay, 993)
    except socket.gaierror:
        return -6  # genuine DNS failure: base domain does not resolve
    # Base domain resolves -- check autoconfig subdomains for diagnostics.
    missing = []
    for sub in (f"imap.{relay}", f"smtp.{relay}"):
        try:
            socket.getaddrinfo(sub, None)
        except socket.gaierror:
            missing.append(sub)

    if missing:
        log.info(
            "DNS cross-check: %s resolves but subdomain(s) %s missing "
            "(broken autoconfig); reclassifying as timeout",
            relay, ", ".join(missing),
        )
    else:
        log.info(
            "DNS cross-check: %s and subdomains all resolve but RPC "
            "reported DNS error (port filtered?); reclassifying as timeout",
            relay,
        )
    return -1


def is_transient_alive_error(relay: str | None, error_str: str | None) -> bool:
    """Check if an alive-check error is transient and worth retrying.

    Returns True for errors that might resolve on retry (timeouts,
    reclassified DNS).  Returns False for persistent errors (genuine DNS,
    auth, TLS, connection refused) that won't change with a retry.
    """
    status = verify_relay_status(relay, error_str)
    # -1 (timeout) and 0 (unknown) are potentially transient
    return status in (-1, 0)



def clear_stale_relay_labels(configured_relays: list[str]) -> None:
    """Remove per-relay label sets for relays no longer in the configured list."""
    active = set(configured_relays)
    for metric in (relay_status, account_creations_total, relay_connections):
        _drop_labels(metric, lambda lv: lv[0] in active)


def sample_relay_connections(relays: list[str]) -> None:
    """Sample network connection counts to each relay and update metrics.
    """
    for relay in relays:
        try:
            ip = socket.getaddrinfo(relay, 993)[0][4][0]
            result = subprocess.run(
                ["ss", "-tn", f"dst {ip}"],
                capture_output=True, text=True, timeout=5,
                check=False,
            )
            conn_count = sum(1 for l in result.stdout.splitlines() if l.strip())
            conn_count = max(0, conn_count - 1)  # subtract header
            relay_connections.labels(relay=relay).set(conn_count)
        except Exception as e:
            log.debug("sample_connections failed for %s: %s", relay, type(e).__name__)
            relay_connections.labels(relay=relay).set(0)


def _set_rtt_metrics(labels: dict[str, str], rtt_s: list[float]) -> None:
    """Set median/p10/p90/stddev gauges from a non-empty RTT sample (seconds)."""
    if len(rtt_s) >= 2:
        # quantiles(n=10, method="inclusive") returns 9 cut points;
        # index 0 = p10, index 8 = p90.  "inclusive" interpolates within
        # the data range (exclusive can extrapolate beyond min/max).
        deciles = statistics.quantiles(rtt_s, n=10, method="inclusive")
        p10, p90, stddev = deciles[0], deciles[-1], statistics.stdev(rtt_s)
    else:
        p10 = p90 = rtt_s[0]
        stddev = 0.0
    rtt_median.labels(**labels).set(statistics.median(rtt_s))
    rtt_p10.labels(**labels).set(p10)
    rtt_p90.labels(**labels).set(p90)
    rtt_stddev.labels(**labels).set(stddev)


def update_metrics(result: ProbeResult) -> None:
    """Update Prometheus metrics from a ProbeResult."""
    probe_type = "self" if result.source == result.destination else "cross"
    labels: dict[str, str] = dict(source=result.source, destination=result.destination,
                                  probe_type=probe_type)

    if result.error:
        send_errors_total.labels(**labels).inc()
        probe_success.labels(**labels).set(0)
        probe_loss_ratio.labels(**labels).set(1.0)
        # Clear RTT gauges so dashboards don't show stale values from the
        # last successful round while probe_success=0.
        rtt_median.labels(**labels).set(float("nan"))
        rtt_p90.labels(**labels).set(float("nan"))
        rtt_p10.labels(**labels).set(float("nan"))
        rtt_stddev.labels(**labels).set(float("nan"))
        account_setup_seconds.labels(**labels).set(float("nan"))
        return

    # Derive both success and loss from sent/received (single source of truth)
    # rather than mixing result.loss with our own computation.
    if result.sent > 0:
        loss_ratio = 1.0 - result.received / result.sent
        probe_success.labels(**labels).set(1 if loss_ratio == 0.0 else 0)
        probe_loss_ratio.labels(**labels).set(loss_ratio)
    else:
        probe_success.labels(**labels).set(0)
        probe_loss_ratio.labels(**labels).set(1.0)
    account_setup_seconds.labels(**labels).set(result.account_setup_time)

    if result.rtts_ms:
        _set_rtt_metrics(labels, [r / 1000.0 for r in result.rtts_ms])
