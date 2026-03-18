"""Tests for the cmping wrapper (prober.py)."""

import argparse
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from cmping import CMPingError
from chatmail_prober.prober import run_probe, ProbeResult, RelayPool, _cmping_verbose


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
        result = run_probe("a.test", "b.test", count=3, accounts_dir="/tmp/test-cache")

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
        result = run_probe("a.test", "b.test", count=2, accounts_dir="/tmp/test-cache")
        assert result.rtts_ms == [123.4, 567.8]

    @patch("chatmail_prober.prober.perform_ping")
    def test_timing_data_propagated(self, mock_ping):
        mock_ping.return_value = FakePinger(
            account_setup_time=1.1, group_join_time=2.2, message_time=3.3,
        )
        result = run_probe("a.test", "b.test", count=3, accounts_dir="/tmp/test-cache")
        assert result.account_setup_time == pytest.approx(1.1)
        assert result.group_join_time == pytest.approx(2.2)
        assert result.message_time == pytest.approx(3.3)

    @patch("chatmail_prober.prober.perform_ping")
    def test_accounts_dir_passed_directly(self, mock_ping):
        """Verify perform_ping receives accounts_dir as given (no source--dest subdir)."""
        mock_ping.return_value = FakePinger()
        run_probe("relay1.org", "relay2.org", count=1, accounts_dir="/tmp/cache")

        call_args = mock_ping.call_args
        accounts_dir = call_args.kwargs.get("accounts_dir") or call_args[1].get("accounts_dir")
        assert accounts_dir == Path("/tmp/cache")

    @patch("chatmail_prober.prober.perform_ping")
    def test_args_namespace_constructed(self, mock_ping):
        """Verify the argparse Namespace passed to perform_ping has correct fields."""
        mock_ping.return_value = FakePinger()
        run_probe("src.org", "dst.org", count=7, interval=2.0, accounts_dir="/tmp/c", timeout=15.0)

        call_args = mock_ping.call_args
        args = call_args[0][0]
        assert args.relay1 == "src.org"
        assert args.relay2 == "dst.org"
        assert args.count == 7
        assert args.interval == 2.0
        assert args.verbose == 0
        assert args.numrecipients == 1
        assert args.reset is False
        assert call_args.kwargs.get("timeout") == 15.0


class TestRunProbeErrors:
    @patch("chatmail_prober.prober.perform_ping")
    def test_cmping_error_returns_error_result(self, mock_ping):
        mock_ping.side_effect = CMPingError("setup failed")
        result = run_probe("a.test", "b.test", count=1, accounts_dir="/tmp/test-cache")

        assert result.error == "setup failed"
        assert result.sent == 0
        assert result.received == 0
        assert result.loss == 100.0
        assert result.rtts_ms == []

    @patch("chatmail_prober.prober.perform_ping")
    def test_unexpected_exception_returns_error_result(self, mock_ping):
        mock_ping.side_effect = RuntimeError("something broke")
        result = run_probe("a.test", "b.test", count=1, accounts_dir="/tmp/test-cache")

        assert result.error == "something broke"
        assert result.sent == 0

    @patch("chatmail_prober.prober.perform_ping")
    def test_error_result_preserves_source_dest(self, mock_ping):
        mock_ping.side_effect = CMPingError("fail")
        result = run_probe("src.example", "dst.example", count=1, accounts_dir="/tmp/c")

        assert result.source == "src.example"
        assert result.destination == "dst.example"


class TestRunProbeWithContexts:
    @patch("chatmail_prober.prober.perform_direct_ping")
    def test_direct_mode_default(self, mock_direct):
        """With relay_contexts and direct=True (default), use perform_direct_ping."""
        mock_direct.return_value = FakePinger()
        contexts = {"a.test": MagicMock(), "b.test": MagicMock()}
        result = run_probe("a.test", "b.test", count=3, relay_contexts=contexts)

        assert result.sent == 3
        assert result.received == 3
        mock_direct.assert_called_once()
        call_args = mock_direct.call_args
        assert call_args[0][1] is contexts

    @patch("chatmail_prober.prober.perform_ping_with_contexts")
    def test_group_mode_with_contexts(self, mock_ctx_ping):
        """With relay_contexts and direct=False, use perform_ping_with_contexts."""
        mock_ctx_ping.return_value = FakePinger()
        contexts = {"a.test": MagicMock(), "b.test": MagicMock()}
        result = run_probe("a.test", "b.test", count=3, relay_contexts=contexts,
                           direct=False)

        assert result.sent == 3
        mock_ctx_ping.assert_called_once()
        call_args = mock_ctx_ping.call_args
        assert call_args[0][1] is contexts

    @patch("chatmail_prober.prober.perform_direct_ping")
    def test_error_with_contexts(self, mock_direct):
        mock_direct.side_effect = CMPingError("rpc failed")
        contexts = {"a.test": MagicMock()}
        result = run_probe("a.test", "b.test", count=1, relay_contexts=contexts)

        assert result.error == "rpc failed"

    @patch("chatmail_prober.prober.perform_ping")
    def test_without_contexts_uses_perform_ping(self, mock_ping):
        """When relay_contexts is None, fall back to perform_ping."""
        mock_ping.return_value = FakePinger()
        result = run_probe("a.test", "b.test", count=3, accounts_dir="/tmp/c")

        mock_ping.assert_called_once()
        assert result.sent == 3


class TestRelayPool:
    @patch("chatmail_prober.prober.RelayContext")
    def test_open_all_creates_contexts(self, MockCtx, tmp_path):
        pool = RelayPool(tmp_path, verbose=0)
        pool.open_all(["a.test", "b.test"])

        assert MockCtx.call_count == 2
        contexts = pool.contexts()
        assert set(contexts.keys()) == {"a.test", "b.test"}

    @patch("chatmail_prober.prober.RelayContext")
    def test_open_all_deduplicates(self, MockCtx, tmp_path):
        pool = RelayPool(tmp_path)
        pool.open_all(["a.test", "b.test"])
        pool.open_all(["a.test", "c.test"])

        assert MockCtx.call_count == 3  # a, b, then c (a not repeated)
        assert set(pool.contexts().keys()) == {"a.test", "b.test", "c.test"}

    @patch("chatmail_prober.prober.RelayContext")
    def test_close_clears_contexts(self, MockCtx, tmp_path):
        pool = RelayPool(tmp_path)
        pool.open_all(["a.test"])
        pool.close()

        assert pool.contexts() == {}
        MockCtx.return_value.close.assert_called_once()

    @patch("chatmail_prober.prober.RelayContext")
    def test_context_manager(self, MockCtx, tmp_path):
        with RelayPool(tmp_path) as pool:
            pool.open_all(["a.test"])
        MockCtx.return_value.close.assert_called_once()

    @patch("chatmail_prober.prober.RelayContext")
    def test_accounts_dir_per_relay(self, MockCtx, tmp_path):
        pool = RelayPool(tmp_path)
        pool.open_all(["relay.example"])

        MockCtx.assert_called_once_with("relay.example",
                                        tmp_path / "relay.example",
                                        verbose=0)


class TestCmpingVerbose:
    def test_default(self):
        assert _cmping_verbose(0) == 0

    def test_level_2(self):
        assert _cmping_verbose(2) == 1

    def test_level_3(self):
        assert _cmping_verbose(3) == 3


class TestCmpingLoggerLevel:
    """Verify the cmping logger is at INFO so structured messages are visible."""

    def test_cmping_logger_at_info(self):
        import logging
        cmping_logger = logging.getLogger("cmping")
        assert cmping_logger.level == logging.INFO

    def test_no_stdout_from_probe(self, capsys):
        """run_probe with mocked perform_ping produces no stdout output.

        cmping's statistics block is gated on log.isEnabledFor(INFO),
        and the cmping logger is at INFO. But perform_ping is mocked here
        so the statistics block never runs. This verifies the wrapper
        itself doesn't emit to stdout."""
        with patch("chatmail_prober.prober.perform_ping") as mock_ping:
            mock_ping.return_value = FakePinger()
            run_probe("a.test", "b.test", count=3, accounts_dir="/tmp/test-cache")
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_structured_log_visible(self, caplog):
        """Structured log.info messages from cmping are visible at default level."""
        import logging
        with caplog.at_level(logging.INFO, logger="cmping"):
            logging.getLogger("cmping").info("phase=test status=ok")
        assert "phase=test status=ok" in caplog.text
