"""Tests for the cmping wrapper (prober.py)."""

import argparse
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from cmping import CMPingError
from chatmail_prober.prober import run_probe, ProbeResult


class FakePinger:
    """Minimal stand-in for cmping.Pinger returned by perform_ping()."""
    def __init__(self, sent=3, received=3, loss=0.0, results=None,
                 account_setup_time=0.5, group_join_time=0.3, message_time=2.0):
        self.sent = sent
        self.received = received
        self.loss = loss
        self.results = results or [(0, 400.0, 0), (1, 500.0, 0), (2, 600.0, 0)]
        self.account_setup_time = account_setup_time
        self.group_join_time = group_join_time
        self.message_time = message_time


class TestRunProbeSuccess:
    @patch("chatmail_prober.prober.perform_ping")
    def test_returns_probe_result(self, mock_ping):
        mock_ping.return_value = FakePinger()
        result = run_probe("a.test", "b.test", count=3, cache_dir="/tmp/test-cache")

        assert isinstance(result, ProbeResult)
        assert result.source == "a.test"
        assert result.destination == "b.test"
        assert result.sent == 3
        assert result.received == 3
        assert result.loss == 0.0
        assert result.error is None

    @patch("chatmail_prober.prober.perform_ping")
    def test_rtts_extracted_from_results(self, mock_ping):
        mock_ping.return_value = FakePinger(
            results=[(0, 123.4, 0), (1, 567.8, 0)]
        )
        result = run_probe("a.test", "b.test", count=2, cache_dir="/tmp/test-cache")
        assert result.rtts_ms == [123.4, 567.8]

    @patch("chatmail_prober.prober.perform_ping")
    def test_timing_data_propagated(self, mock_ping):
        mock_ping.return_value = FakePinger(
            account_setup_time=1.1, group_join_time=2.2, message_time=3.3,
        )
        result = run_probe("a.test", "b.test", count=3, cache_dir="/tmp/test-cache")
        assert result.account_setup_time == pytest.approx(1.1)
        assert result.group_join_time == pytest.approx(2.2)
        assert result.message_time == pytest.approx(3.3)

    @patch("chatmail_prober.prober.perform_ping")
    def test_per_pair_accounts_dir(self, mock_ping):
        """Verify perform_ping is called with source--dest subdirectory."""
        mock_ping.return_value = FakePinger()
        run_probe("relay1.org", "relay2.org", count=1, cache_dir="/tmp/cache")

        call_args = mock_ping.call_args
        accounts_dir = call_args.kwargs.get("accounts_dir") or call_args[1].get("accounts_dir")
        assert accounts_dir == Path("/tmp/cache/relay1.org--relay2.org")

    @patch("chatmail_prober.prober.perform_ping")
    def test_args_namespace_constructed(self, mock_ping):
        """Verify the argparse Namespace passed to perform_ping has correct fields."""
        mock_ping.return_value = FakePinger()
        run_probe("src.org", "dst.org", count=7, interval=2.0, cache_dir="/tmp/c")

        call_args = mock_ping.call_args
        args = call_args[0][0]
        assert args.relay1 == "src.org"
        assert args.relay2 == "dst.org"
        assert args.count == 7
        assert args.interval == 2.0
        assert args.verbose == 0
        assert args.numrecipients == 1
        assert args.reset is False


class TestRunProbeErrors:
    @patch("chatmail_prober.prober.perform_ping")
    def test_cmping_error_returns_error_result(self, mock_ping):
        mock_ping.side_effect = CMPingError("setup failed")
        result = run_probe("a.test", "b.test", count=1, cache_dir="/tmp/test-cache")

        assert result.error == "setup failed"
        assert result.sent == 0
        assert result.received == 0
        assert result.loss == 100.0
        assert result.rtts_ms == []

    @patch("chatmail_prober.prober.perform_ping")
    def test_unexpected_exception_returns_error_result(self, mock_ping):
        mock_ping.side_effect = RuntimeError("something broke")
        result = run_probe("a.test", "b.test", count=1, cache_dir="/tmp/test-cache")

        assert result.error == "something broke"
        assert result.sent == 0

    @patch("chatmail_prober.prober.perform_ping")
    def test_error_result_preserves_source_dest(self, mock_ping):
        mock_ping.side_effect = CMPingError("fail")
        result = run_probe("src.example", "dst.example", count=1, cache_dir="/tmp/c")

        assert result.source == "src.example"
        assert result.destination == "dst.example"
