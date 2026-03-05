"""Tests for Prometheus metric updates from ProbeResults."""

import pytest
from prometheus_client import REGISTRY, CollectorRegistry

from chatmail_prober.prober import ProbeResult
from chatmail_prober import metrics as metrics_mod


@pytest.fixture(autouse=True)
def _fresh_metrics(monkeypatch):
    """Replace all metrics with fresh instances per test to avoid cross-contamination."""
    registry = CollectorRegistry()
    labels = ["source", "destination"]

    new = {
        "requests_total": metrics_mod.Counter(
            "cmping_requests_total_test", "test", labels, registry=registry,
        ),
        "responses_total": metrics_mod.Counter(
            "cmping_responses_total_test", "test", labels, registry=registry,
        ),
        "response_duration": metrics_mod.Histogram(
            "cmping_response_duration_seconds_test", "test", labels,
            buckets=metrics_mod.RTT_BUCKETS, registry=registry,
        ),
        "send_errors_total": metrics_mod.Counter(
            "cmping_send_errors_total_test", "test", labels, registry=registry,
        ),
        "probe_success": metrics_mod.Gauge(
            "cmping_probe_success_test", "test", labels, registry=registry,
        ),
        "account_setup_seconds": metrics_mod.Gauge(
            "cmping_account_setup_seconds_test", "test", labels, registry=registry,
        ),
    }
    for name, metric in new.items():
        monkeypatch.setattr(metrics_mod, name, metric)
    return new


def _labels():
    return dict(source="a.example", destination="b.example")


class TestUpdateMetricsSuccess:
    def test_counters_increment(self):
        result = ProbeResult("a.example", "b.example", sent=10, received=8, loss=20.0,
                             rtts_ms=[100, 200, 300])
        metrics_mod.update_metrics(result)

        lbl = _labels()
        assert metrics_mod.requests_total.labels(**lbl)._value.get() == 10
        assert metrics_mod.responses_total.labels(**lbl)._value.get() == 8

    def test_histogram_observations_converted_to_seconds(self):
        result = ProbeResult("a.example", "b.example", sent=3, received=3, loss=0.0,
                             rtts_ms=[500.0, 1500.0, 250.0])
        metrics_mod.update_metrics(result)

        lbl = _labels()
        h = metrics_mod.response_duration.labels(**lbl)
        # 3 observations
        assert h._sum.get() == pytest.approx(2.25, abs=0.001)  # (0.5 + 1.5 + 0.25)

    def test_probe_success_set_to_one_on_zero_loss(self):
        result = ProbeResult("a.example", "b.example", sent=3, received=3, loss=0.0)
        metrics_mod.update_metrics(result)
        assert metrics_mod.probe_success.labels(**_labels())._value.get() == 1.0

    def test_probe_success_set_to_zero_on_partial_loss(self):
        result = ProbeResult("a.example", "b.example", sent=3, received=2, loss=33.3)
        metrics_mod.update_metrics(result)
        assert metrics_mod.probe_success.labels(**_labels())._value.get() == 0.0

    def test_account_setup_time_stored(self):
        result = ProbeResult("a.example", "b.example", sent=1, received=1, loss=0.0,
                             account_setup_time=1.234)
        metrics_mod.update_metrics(result)
        assert metrics_mod.account_setup_seconds.labels(**_labels())._value.get() == pytest.approx(1.234)

    def test_counters_accumulate_across_updates(self):
        lbl = _labels()
        for _ in range(3):
            result = ProbeResult("a.example", "b.example", sent=5, received=4, loss=20.0)
            metrics_mod.update_metrics(result)

        assert metrics_mod.requests_total.labels(**lbl)._value.get() == 15
        assert metrics_mod.responses_total.labels(**lbl)._value.get() == 12


class TestUpdateMetricsError:
    def test_error_increments_send_errors(self):
        result = ProbeResult("a.example", "b.example", error="connection refused")
        metrics_mod.update_metrics(result)

        lbl = _labels()
        assert metrics_mod.send_errors_total.labels(**lbl)._value.get() == 1

    def test_error_sets_probe_success_to_zero(self):
        result = ProbeResult("a.example", "b.example", error="timeout")
        metrics_mod.update_metrics(result)
        assert metrics_mod.probe_success.labels(**_labels())._value.get() == 0.0

    def test_error_does_not_touch_request_counters(self):
        result = ProbeResult("a.example", "b.example", error="boom")
        metrics_mod.update_metrics(result)

        lbl = _labels()
        # Counter should not have been created for these labels
        # (accessing .labels() creates it, so check the underlying value)
        assert metrics_mod.requests_total.labels(**lbl)._value.get() == 0
        assert metrics_mod.responses_total.labels(**lbl)._value.get() == 0


class TestUpdateMetricsMultiplePairs:
    def test_different_pairs_are_independent(self):
        r1 = ProbeResult("a.example", "b.example", sent=5, received=5, loss=0.0)
        r2 = ProbeResult("b.example", "a.example", sent=5, received=3, loss=40.0)
        metrics_mod.update_metrics(r1)
        metrics_mod.update_metrics(r2)

        assert metrics_mod.probe_success.labels(source="a.example", destination="b.example")._value.get() == 1.0
        assert metrics_mod.probe_success.labels(source="b.example", destination="a.example")._value.get() == 0.0
