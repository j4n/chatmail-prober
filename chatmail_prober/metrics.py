"""Prometheus metric definitions and update logic."""

import logging

from prometheus_client import Counter, Gauge, Histogram

log = logging.getLogger(__name__)

# Histogram buckets tuned for email round-trip times (seconds), not ICMP.
RTT_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)

LABELS = ["source", "destination"]

requests_total = Counter(
    "cmping_requests_total",
    "Total number of ping messages sent",
    LABELS,
)

responses_total = Counter(
    "cmping_responses_total",
    "Total number of ping messages successfully received",
    LABELS,
)

response_duration = Histogram(
    "cmping_response_duration_seconds",
    "Round-trip time for individual ping messages",
    LABELS,
    buckets=RTT_BUCKETS,
)

send_errors_total = Counter(
    "cmping_send_errors_total",
    "Total number of failed probe rounds (timeout, crash, setup failure)",
    LABELS,
)

probe_success = Gauge(
    "cmping_probe_success",
    "Whether the last probe round succeeded (1) or failed (0)",
    LABELS,
)

account_setup_seconds = Gauge(
    "cmping_account_setup_seconds",
    "Time spent on account setup in the last probe round",
    LABELS,
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
