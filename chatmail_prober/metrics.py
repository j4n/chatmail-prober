"""Prometheus metric definitions and update logic."""

import logging

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, REGISTRY

log = logging.getLogger(__name__)

# Histogram buckets tuned for email round-trip times (seconds), not ICMP.
RTT_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)

LABELS = ["source", "destination"]

# Separate registry without default process_* / gc_* collectors.
# Used for textfile output to avoid collisions with node_exporter's
# own process metrics. The default REGISTRY (with process collectors)
# is still used for the HTTP /metrics endpoint.
CMPING_REGISTRY = CollectorRegistry()

requests_total = Counter(
    "cmping_requests_total",
    "Total number of ping messages sent",
    LABELS,
    registry=CMPING_REGISTRY,
)

responses_total = Counter(
    "cmping_responses_total",
    "Total number of ping messages successfully received",
    LABELS,
    registry=CMPING_REGISTRY,
)

response_duration = Histogram(
    "cmping_response_duration_seconds",
    "Round-trip time for individual ping messages",
    LABELS,
    buckets=RTT_BUCKETS,
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

account_setup_seconds = Gauge(
    "cmping_account_setup_seconds",
    "Time spent on account setup in the last probe round",
    LABELS,
    registry=CMPING_REGISTRY,
)


def update_metrics(result):
    """Update Prometheus metrics from a ProbeResult."""
    labels = dict(source=result.source, destination=result.destination)

    if result.error:
        send_errors_total.labels(**labels).inc()
        probe_success.labels(**labels).set(0)
        return

    requests_total.labels(**labels).inc(result.sent)
    responses_total.labels(**labels).inc(result.received)
    probe_success.labels(**labels).set(1 if result.loss == 0 else 0)
    account_setup_seconds.labels(**labels).set(result.account_setup_time)

    for rtt_ms in result.rtts_ms:
        response_duration.labels(**labels).observe(rtt_ms / 1000.0)
