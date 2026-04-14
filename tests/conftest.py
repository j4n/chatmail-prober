"""Shared test fixtures for chatmail-prober."""

import pytest
from prometheus_client import CollectorRegistry

from chatmail_prober import metrics as metrics_mod


@pytest.fixture()
def fresh_metrics(monkeypatch):
    """Replace all metrics with fresh instances per test to avoid cross-contamination."""
    registry = CollectorRegistry()
    labels = ["source", "destination", "probe_type"]
    new = {
        "rtt_median": metrics_mod.Gauge(
            "cmping_rtt_median_seconds_test", "test", labels, registry=registry,
        ),
        "rtt_stddev": metrics_mod.Gauge(
            "cmping_rtt_stddev_seconds_test", "test", labels, registry=registry,
        ),
        "rtt_p90": metrics_mod.Gauge(
            "cmping_rtt_p90_seconds_test", "test", labels, registry=registry,
        ),
        "rtt_p10": metrics_mod.Gauge(
            "cmping_rtt_p10_seconds_test", "test", labels, registry=registry,
        ),
        "send_errors_total": metrics_mod.Counter(
            "cmping_send_errors_total_test", "test", labels, registry=registry,
        ),
        "probe_success": metrics_mod.Gauge(
            "cmping_probe_success_test", "test", labels, registry=registry,
        ),
        "probe_loss_ratio": metrics_mod.Gauge(
            "cmping_probe_loss_ratio_test", "test", labels, registry=registry,
        ),
        "account_setup_seconds": metrics_mod.Gauge(
            "cmping_account_setup_seconds_test", "test", labels, registry=registry,
        ),
        "relay_status": metrics_mod.Gauge(
            "cmping_relay_status_test", "test", ["relay"], registry=registry,
        ),
    }
    for name, metric in new.items():
        monkeypatch.setattr(metrics_mod, name, metric)
    return new
