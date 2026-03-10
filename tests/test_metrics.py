"""Tests for Prometheus metric updates from ProbeResults."""

import pytest
from prometheus_client import CollectorRegistry

from chatmail_prober.prober import ProbeResult
from chatmail_prober import metrics as metrics_mod


@pytest.fixture(autouse=True)
def _fresh_metrics(monkeypatch):
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
    }
    for name, metric in new.items():
        monkeypatch.setattr(metrics_mod, name, metric)
    return new


def _labels():
    return dict(source="a.example", destination="b.example", probe_type="cross")


class TestUpdateMetricsSuccess:
    def test_rtt_gauges_computed_from_rtts_ms(self):
        result = ProbeResult("a.example", "b.example", sent=3, received=3, loss=0.0,
                             rtts_ms=[500.0, 1500.0, 250.0])
        metrics_mod.update_metrics(result)

        lbl = _labels()
        assert metrics_mod.rtt_median.labels(**lbl)._value.get() == pytest.approx(0.5)
        assert metrics_mod.rtt_p90.labels(**lbl)._value.get() == pytest.approx(1.5)
        assert metrics_mod.rtt_p10.labels(**lbl)._value.get() == pytest.approx(0.25)
        assert metrics_mod.rtt_stddev.labels(**lbl)._value.get() == pytest.approx(0.6614, abs=0.001)

    def test_probe_success_set_to_one_on_zero_loss(self):
        result = ProbeResult("a.example", "b.example", sent=3, received=3, loss=0.0)
        metrics_mod.update_metrics(result)
        assert metrics_mod.probe_success.labels(**_labels())._value.get() == 1.0
        assert metrics_mod.probe_loss_ratio.labels(**_labels())._value.get() == pytest.approx(0.0)

    def test_probe_success_set_to_zero_on_partial_loss(self):
        result = ProbeResult("a.example", "b.example", sent=3, received=2, loss=33.3)
        metrics_mod.update_metrics(result)
        assert metrics_mod.probe_success.labels(**_labels())._value.get() == 0.0
        assert metrics_mod.probe_loss_ratio.labels(**_labels())._value.get() == pytest.approx(1/3)

    def test_account_setup_time_stored(self):
        result = ProbeResult("a.example", "b.example", sent=1, received=1, loss=0.0,
                             account_setup_time=1.234)
        metrics_mod.update_metrics(result)
        assert metrics_mod.account_setup_seconds.labels(**_labels())._value.get() == pytest.approx(1.234)

    def test_rtt_gauges_reflect_last_round(self):
        """Gauges should show the last round's values, not accumulate."""
        lbl = _labels()

        result1 = ProbeResult("a.example", "b.example", sent=2, received=2, loss=0.0,
                              rtts_ms=[1000.0, 2000.0])
        metrics_mod.update_metrics(result1)
        assert metrics_mod.rtt_median.labels(**lbl)._value.get() == pytest.approx(1.5)

        result2 = ProbeResult("a.example", "b.example", sent=2, received=2, loss=0.0,
                              rtts_ms=[100.0, 200.0])
        metrics_mod.update_metrics(result2)
        assert metrics_mod.rtt_median.labels(**lbl)._value.get() == pytest.approx(0.15)

    def test_rtt_stddev_zero_for_single_ping(self):
        result = ProbeResult("a.example", "b.example", sent=1, received=1, loss=0.0,
                             rtts_ms=[500.0])
        metrics_mod.update_metrics(result)
        assert metrics_mod.rtt_stddev.labels(**_labels())._value.get() == 0.0


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
        assert metrics_mod.probe_loss_ratio.labels(**_labels())._value.get() == 1.0

    def test_error_does_not_touch_rtt_gauges(self):
        result = ProbeResult("a.example", "b.example", error="boom")
        metrics_mod.update_metrics(result)

        lbl = _labels()
        assert metrics_mod.rtt_median.labels(**lbl)._value.get() == 0.0
        assert metrics_mod.rtt_p90.labels(**lbl)._value.get() == 0.0
        assert metrics_mod.rtt_p10.labels(**lbl)._value.get() == 0.0
        assert metrics_mod.rtt_stddev.labels(**lbl)._value.get() == 0.0


class TestProbeType:
    def test_probe_type_self_when_source_equals_destination(self):
        result = ProbeResult("a.example", "a.example", sent=1, received=1, loss=0.0,
                             rtts_ms=[100.0])
        metrics_mod.update_metrics(result)

        self_lbl = dict(source="a.example", destination="a.example", probe_type="self")
        assert metrics_mod.probe_success.labels(**self_lbl)._value.get() == 1.0
        assert metrics_mod.rtt_median.labels(**self_lbl)._value.get() == pytest.approx(0.1)

    def test_probe_type_cross_when_source_differs_from_destination(self):
        result = ProbeResult("a.example", "b.example", sent=1, received=1, loss=0.0,
                             rtts_ms=[100.0])
        metrics_mod.update_metrics(result)

        cross_lbl = dict(source="a.example", destination="b.example", probe_type="cross")
        assert metrics_mod.probe_success.labels(**cross_lbl)._value.get() == 1.0


class TestUpdateMetricsMultiplePairs:
    def test_different_pairs_are_independent(self):
        r1 = ProbeResult("a.example", "b.example", sent=5, received=5, loss=0.0)
        r2 = ProbeResult("b.example", "a.example", sent=5, received=3, loss=40.0)
        metrics_mod.update_metrics(r1)
        metrics_mod.update_metrics(r2)

        assert metrics_mod.probe_success.labels(
            source="a.example", destination="b.example", probe_type="cross")._value.get() == 1.0
        assert metrics_mod.probe_success.labels(
            source="b.example", destination="a.example", probe_type="cross")._value.get() == 0.0
