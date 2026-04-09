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
        "relay_status": metrics_mod.Gauge(
            "cmping_relay_status_test", "test", ["relay"], registry=registry,
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
        # statistics.quantiles(method="inclusive") interpolates within data range
        assert metrics_mod.rtt_p90.labels(**lbl)._value.get() == pytest.approx(1.3)
        assert metrics_mod.rtt_p10.labels(**lbl)._value.get() == pytest.approx(0.3)
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

    def test_error_clears_rtt_gauges_to_nan(self):
        """RTT gauges must be NaN on error so dashboards don't show stale values."""
        import math
        result = ProbeResult("a.example", "b.example", error="boom")
        metrics_mod.update_metrics(result)

        lbl = _labels()
        assert math.isnan(metrics_mod.rtt_median.labels(**lbl)._value.get())
        assert math.isnan(metrics_mod.rtt_p90.labels(**lbl)._value.get())
        assert math.isnan(metrics_mod.rtt_p10.labels(**lbl)._value.get())
        assert math.isnan(metrics_mod.rtt_stddev.labels(**lbl)._value.get())
        assert math.isnan(metrics_mod.account_setup_seconds.labels(**lbl)._value.get())


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


class TestClearStaleLabels:
    def test_removes_labels_for_removed_relay(self):
        r1 = ProbeResult("a.example", "b.example", sent=1, received=1, loss=0.0,
                         rtts_ms=[100.0])
        r2 = ProbeResult("a.example", "a.example", sent=1, received=1, loss=0.0,
                         rtts_ms=[50.0])
        metrics_mod.update_metrics(r1)
        metrics_mod.update_metrics(r2)

        # Both label sets exist
        assert ("a.example", "b.example", "cross") in metrics_mod.probe_success._metrics
        assert ("a.example", "a.example", "self") in metrics_mod.probe_success._metrics

        # Remove b.example from active set
        metrics_mod.clear_stale_labels(["a.example"])

        # b.example labels should be gone, a.example self-loop should remain
        assert ("a.example", "b.example", "cross") not in metrics_mod.probe_success._metrics
        assert ("a.example", "a.example", "self") in metrics_mod.probe_success._metrics

    def test_clears_all_metric_types(self):
        r = ProbeResult("a.example", "gone.example", error="dead")
        metrics_mod.update_metrics(r)

        lbl = ("a.example", "gone.example", "cross")
        assert lbl in metrics_mod.send_errors_total._metrics
        assert lbl in metrics_mod.rtt_median._metrics

        metrics_mod.clear_stale_labels(["a.example"])

        assert lbl not in metrics_mod.send_errors_total._metrics
        assert lbl not in metrics_mod.rtt_median._metrics


class TestClearStaleRelayLabels:
    def test_removes_labels_for_unconfigured_relay(self):
        metrics_mod.relay_status.labels(relay="a.example").set(1)
        metrics_mod.relay_status.labels(relay="b.example").set(1)

        metrics_mod.clear_stale_relay_labels(["a.example"])

        assert ("a.example",) in metrics_mod.relay_status._metrics
        assert ("b.example",) not in metrics_mod.relay_status._metrics

    def test_keeps_all_configured_relays(self):
        relays = ["a.example", "b.example", "c.example"]
        for r in relays:
            metrics_mod.relay_status.labels(relay=r).set(1)

        metrics_mod.clear_stale_relay_labels(relays)

        for r in relays:
            assert (r,) in metrics_mod.relay_status._metrics

    def test_noop_when_no_labels_exist(self):
        # Should not raise when metric has no label sets yet
        metrics_mod.clear_stale_relay_labels(["a.example"])


class TestRelayStatusMetric:
    def test_online_relay_set_to_one(self):
        metrics_mod.relay_status.labels(relay="a.example").set(1)
        assert metrics_mod.relay_status.labels(relay="a.example")._value.get() == 1.0

    def test_dead_relay_set_to_negative_value(self):
        metrics_mod.relay_status.labels(relay="a.example").set(-1)
        assert metrics_mod.relay_status.labels(relay="a.example")._value.get() == -1.0

    def test_relay_status_value_encoding(self):
        # Test integer encoding for different failure modes
        assert metrics_mod.relay_status_value(None) == 1  # ok
        assert metrics_mod.relay_status_value("timeout") == -1
        assert metrics_mod.relay_status_value("connection refused") == -5
        assert metrics_mod.relay_status_value("name or service not known") == -6  # DNS fail
        assert metrics_mod.relay_status_value("unknown error") == 0


class TestClassifyAliveCheckError:
    def test_none_returns_ok(self):
        assert metrics_mod.classify_alive_check_error(None) == "ok"

    def test_timeout_variants(self):
        assert metrics_mod.classify_alive_check_error("Timeout waiting for foo") == "timeout"
        assert metrics_mod.classify_alive_check_error("Connection timed out") == "timeout"
        assert metrics_mod.classify_alive_check_error("exceeded global deadline") == "timeout"
        assert metrics_mod.classify_alive_check_error("timeout") == "timeout"

    def test_connection_refused(self):
        assert metrics_mod.classify_alive_check_error("Connection refused") == "connection_refused"
        assert metrics_mod.classify_alive_check_error(
            "ConnectionRefusedError: [Errno 111]") == "connection_refused"

    def test_dns(self):
        assert metrics_mod.classify_alive_check_error(
            "Name or service not known") == "dns"
        assert metrics_mod.classify_alive_check_error(
            "getaddrinfo failed") == "dns"
        assert metrics_mod.classify_alive_check_error(
            "Could not find DNS resolutions for imap.foo:993") == "dns"
        assert metrics_mod.classify_alive_check_error(
            "dial tcp: lookup relay.example: no such host") == "dns"
        assert metrics_mod.classify_alive_check_error(
            "NXDOMAIN error for imap.example") == "dns"

    def test_dns_in_tls_connection_string(self):
        # Real-world: DNS error inside a TLS connection URI -- dns must win
        assert metrics_mod.classify_alive_check_error(
            'IMAP failed to connect to imap.chat.sus.fr:993:tls: '
            'Could not find DNS resolutions for imap.chat.sus.fr:993.'
        ) == "dns"

    def test_tls(self):
        assert metrics_mod.classify_alive_check_error("SSL: CERTIFICATE_VERIFY_FAILED") == "tls"
        assert metrics_mod.classify_alive_check_error("certificate has expired") == "tls"

    def test_auth(self):
        assert metrics_mod.classify_alive_check_error(
            "AUTHENTICATIONFAILED") == "auth"
        assert metrics_mod.classify_alive_check_error(
            "authentication failed") == "auth"

    def test_auth_inside_setup_message(self):
        # Real foobar.org error: auth failure wrapped in setup error
        assert metrics_mod.classify_alive_check_error(
            'Failed to setup sender profile on foobar.org: JsonRpcError: '
            "{'code': -1, 'message': 'Error:\\n\\n\"Cannot login as "
            '"gbegx86r9@foobar.org". Please check if the email address '
            "and the password are correct. (no response: code: None, "
            'info: Some("[AUTHENTICATIONFAILED] Authentication failed."))"\'}'
        ) == "auth"

    def test_setup(self):
        assert metrics_mod.classify_alive_check_error(
            "Failed to setup sender profile on foo: RPC process crashed") == "setup"

    def test_unknown_fallback(self):
        assert metrics_mod.classify_alive_check_error("something unexpected") == "unknown"
