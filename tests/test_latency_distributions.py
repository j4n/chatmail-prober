"""RED tests for latency distribution properties on ProbeResult.

ProbeResult must expose p50, p90, p95, p99, and mdev as computed
properties derived from rtts_ms, so the cli_summary renderer can use
them directly without reimplementing statistics.
"""
from __future__ import annotations

import statistics

import pytest

from chatmail_prober.probe import ProbeResult

SAMPLE_RTTS = [100.0, 200.0, 300.0, 400.0, 500.0,
               600.0, 700.0, 800.0, 900.0, 1000.0]


class TestLatencyProperties:
    def test_p50_is_median(self):
        r = ProbeResult(source="a", destination="b", rtts_ms=SAMPLE_RTTS)
        assert r.p50_ms == pytest.approx(statistics.median(SAMPLE_RTTS), rel=1e-3)

    def test_p90(self):
        r = ProbeResult(source="a", destination="b", rtts_ms=SAMPLE_RTTS)
        # statistics.quantiles with n=10 inclusive, index 8 = p90
        expected = statistics.quantiles(SAMPLE_RTTS, n=10, method="inclusive")[-1]
        assert r.p90_ms == pytest.approx(expected, rel=1e-3)

    def test_p95(self):
        r = ProbeResult(source="a", destination="b", rtts_ms=SAMPLE_RTTS)
        expected = statistics.quantiles(SAMPLE_RTTS, n=20, method="inclusive")[-1]
        assert r.p95_ms == pytest.approx(expected, rel=1e-3)

    def test_p99(self):
        r = ProbeResult(source="a", destination="b", rtts_ms=SAMPLE_RTTS)
        expected = statistics.quantiles(SAMPLE_RTTS, n=100, method="inclusive")[-1]
        assert r.p99_ms == pytest.approx(expected, rel=1e-3)

    def test_mdev(self):
        r = ProbeResult(source="a", destination="b", rtts_ms=SAMPLE_RTTS)
        assert r.mdev_ms == pytest.approx(statistics.stdev(SAMPLE_RTTS), rel=1e-3)

    def test_single_sample_no_crash(self):
        r = ProbeResult(source="a", destination="b", rtts_ms=[500.0])
        assert r.p50_ms == pytest.approx(500.0)
        assert r.p90_ms == pytest.approx(500.0)
        assert r.p95_ms == pytest.approx(500.0)
        assert r.p99_ms == pytest.approx(500.0)
        assert r.mdev_ms == pytest.approx(0.0)

    def test_empty_rtts_return_none(self):
        r = ProbeResult(source="a", destination="b")
        assert r.p50_ms is None
        assert r.p90_ms is None
        assert r.p95_ms is None
        assert r.p99_ms is None
        assert r.mdev_ms is None

    def test_avg_ms_property(self):
        r = ProbeResult(source="a", destination="b", rtts_ms=SAMPLE_RTTS)
        assert r.avg_ms == pytest.approx(statistics.mean(SAMPLE_RTTS), rel=1e-3)

    def test_avg_ms_none_when_empty(self):
        r = ProbeResult(source="a", destination="b")
        assert r.avg_ms is None
