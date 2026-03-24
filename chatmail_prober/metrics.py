"""Prometheus metric definitions and update logic."""

import logging
import statistics

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

round_duration_seconds = Gauge(
    "cmping_round_duration_seconds",
    "Wall-clock duration of the last completed probe round",
    registry=CMPING_REGISTRY,
)


def clear_stale_labels(active_relays):
    """Remove label sets for relays no longer in the active set.

    Prevents label cardinality from growing unbounded when relays are
    removed from the relay list across process restarts or alive-check
    exclusions.
    """
    import sys
    mod = sys.modules[__name__]
    all_metrics = [
        mod.rtt_median, mod.rtt_stddev, mod.rtt_p90, mod.rtt_p10,
        mod.probe_success, mod.probe_loss_ratio, mod.account_setup_seconds,
        mod.send_errors_total,
    ]
    active = set(active_relays)
    for metric in all_metrics:
        for label_values in list(metric._metrics.keys()):
            src, dst, _ = label_values
            if src not in active or dst not in active:
                metric.remove(*label_values)


def update_metrics(result):
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
