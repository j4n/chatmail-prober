"""Prometheus metric definitions and update logic."""

from __future__ import annotations

import logging
import socket
import statistics
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .prober import ProbeResult

from prometheus_client import (
    CollectorRegistry, Counter, Gauge, REGISTRY,
    disable_created_metrics,
)

# Suppress the _created timestamp lines added by prometheus_client for each
# counter and histogram series -- they double the textfile size and are not
# useful for node_exporter's textfile collector.
disable_created_metrics()

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
        for label_values in list(metric._metrics.keys()):
            src, dst, _ = label_values
            if src not in active or dst not in active:
                metric.remove(*label_values)


def relay_status_value(error_str: str | None) -> int:
    """Map alive-check error string to cmping_relay_status integer.

    Return values:
        1  = ok (no error)
        0  = unknown error
       -1  = timeout / deadline exceeded
       -2  = setup failure (account creation)
       -3  = auth failure
       -4  = TLS / certificate error
       -5  = connection refused
       -6  = DNS resolution failure
    """
    if error_str is None:
        return 1
    lower = error_str.lower()
    if "timeout" in lower or "timed out" in lower or "deadline" in lower:
        return -1
    if "connection refused" in lower or "connectionrefusederror" in lower:
        return -5
    if ("name or service not known" in lower or "getaddrinfo" in lower
            or "dns resolution" in lower or "no such host" in lower
            or "nxdomain" in lower):
        return -6
    if "ssl" in lower or "certificate" in lower:
        return -4
    if "auth" in lower or "authentication" in lower:
        return -3
    if "failed to setup" in lower:
        return -2
    return 0


def verify_relay_status(relay: str | None, error_str: str | None) -> int:
    """Get relay status value with DNS cross-check.

    Wraps relay_status_value() and corrects false DNS errors from the
    Delta Chat RPC.  When the RPC reports a DNS failure, the actual cause
    may be a missing imap.* subdomain because autoconfig was broken --
    the relay host itself still resolves.  In that case the relay is
    unreachable (timeout) rather than having a DNS problem.

    Args:
        relay: relay domain name (e.g. "chat.beeep.ir")
        error_str: error string from the probe, or None if ok
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


def classify_alive_check_error(error_str: str | None) -> str:
    """Deprecated: use relay_status_value() instead. Map error to string reason.

    Kept for backwards compatibility with dashboard metric queries that expect string reasons.
    """
    value = relay_status_value(error_str)
    mapping = {
        1: "ok", 0: "unknown", -1: "timeout", -2: "setup",
        -3: "auth", -4: "tls", -5: "connection_refused", -6: "dns"
    }
    return mapping.get(value, "unknown")


def clear_stale_relay_labels(configured_relays: list[str]) -> None:
    """Remove relay_status label sets for relays no longer in the configured list."""
    active = set(configured_relays)
    for (relay,) in list(relay_status._metrics.keys()):
        if relay not in active:
            relay_status.remove(relay)


def update_metrics(result: ProbeResult) -> None:
    """Update Prometheus metrics from a ProbeResult."""
    probe_type = "self" if result.source == result.destination else "cross"
    labels = dict(source=result.source, destination=result.destination,
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
        rtt_s = [r / 1000.0 for r in result.rtts_ms]
        rtt_median.labels(**labels).set(statistics.median(rtt_s))
        if len(rtt_s) >= 2:
            # quantiles(n=10, method="inclusive") returns 9 cut points;
            # index 0 = p10, index 8 = p90.  "inclusive" interpolates within
            # the data range (exclusive can extrapolate beyond min/max).
            deciles = statistics.quantiles(rtt_s, n=10, method="inclusive")
            rtt_p10.labels(**labels).set(deciles[0])
            rtt_p90.labels(**labels).set(deciles[-1])
            rtt_stddev.labels(**labels).set(statistics.stdev(rtt_s))
        else:
            rtt_p10.labels(**labels).set(rtt_s[0])
            rtt_p90.labels(**labels).set(rtt_s[0])
            rtt_stddev.labels(**labels).set(0.0)
