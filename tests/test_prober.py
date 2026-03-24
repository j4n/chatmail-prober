"""Tests for the prober (vendored direct-ping logic)."""

from unittest.mock import patch, MagicMock

import pytest

from chatmail_prober.prober import (
    run_probe, ProbeResult, RelayPool, PingError,
)


class FakePinger:
    """Minimal stand-in for Pinger returned by _perform_direct_ping()."""
    def __init__(self, sent=3, received=3, loss=0.0, results=None,
                 account_setup_time=0.5, message_time=2.0):
        self.sent = sent
        self.received = received
        self.loss = loss
        self.results = results or [(0, 400.0), (1, 500.0), (2, 600.0)]
        self.account_setup_time = account_setup_time
        self.message_time = message_time


class TestRunProbeSuccess:
    @patch("chatmail_prober.prober._perform_direct_ping")
    def test_returns_probe_result(self, mock_ping):
        mock_ping.return_value = FakePinger()
        contexts = {"a.test": MagicMock(), "b.test": MagicMock()}
        result = run_probe("a.test", "b.test", count=3, relay_contexts=contexts)

        assert isinstance(result, ProbeResult)
        assert result.source == "a.test"
        assert result.destination == "b.test"
        assert result.sent == 3
        assert result.received == 3
        assert result.loss == 0.0
        assert result.error is None

    @patch("chatmail_prober.prober._perform_direct_ping")
    def test_rtts_extracted_from_results(self, mock_ping):
        mock_ping.return_value = FakePinger(
            results=[(0, 123.4), (1, 567.8)]
        )
        contexts = {"a.test": MagicMock(), "b.test": MagicMock()}
        result = run_probe("a.test", "b.test", count=2, relay_contexts=contexts)
        assert result.rtts_ms == [123.4, 567.8]

    @patch("chatmail_prober.prober._perform_direct_ping")
    def test_timing_data_propagated(self, mock_ping):
        mock_ping.return_value = FakePinger(
            account_setup_time=1.1, message_time=3.3,
        )
        contexts = {"a.test": MagicMock(), "b.test": MagicMock()}
        result = run_probe("a.test", "b.test", count=3, relay_contexts=contexts)
        assert result.account_setup_time == pytest.approx(1.1)
        assert result.message_time == pytest.approx(3.3)

    @patch("chatmail_prober.prober._perform_direct_ping")
    def test_args_passed_to_perform(self, mock_ping):
        """Verify _perform_direct_ping receives correct arguments."""
        mock_ping.return_value = FakePinger()
        contexts = {"src.org": MagicMock(), "dst.org": MagicMock()}
        run_probe("src.org", "dst.org", count=7, interval=2.0, timeout=15.0,
                  relay_contexts=contexts)

        call_args = mock_ping.call_args
        assert call_args[0][0] is contexts
        assert call_args[0][1] == "src.org"
        assert call_args[0][2] == "dst.org"
        assert call_args[0][3] == 7
        assert call_args[0][4] == 2.0
        assert call_args[0][5] == 15.0


class TestRunProbeErrors:
    @patch("chatmail_prober.prober._perform_direct_ping")
    def test_ping_error_returns_error_result(self, mock_ping):
        mock_ping.side_effect = PingError("setup failed")
        contexts = {"a.test": MagicMock(), "b.test": MagicMock()}
        result = run_probe("a.test", "b.test", count=1, relay_contexts=contexts)

        assert result.error == "setup failed"
        assert result.sent == 0
        assert result.received == 0
        assert result.loss == 100.0
        assert result.rtts_ms == []

    @patch("chatmail_prober.prober._perform_direct_ping")
    def test_unexpected_exception_returns_error_result(self, mock_ping):
        mock_ping.side_effect = RuntimeError("something broke")
        contexts = {"a.test": MagicMock(), "b.test": MagicMock()}
        result = run_probe("a.test", "b.test", count=1, relay_contexts=contexts)

        assert result.error == "something broke"
        assert result.sent == 0

    @patch("chatmail_prober.prober._perform_direct_ping")
    def test_error_result_preserves_source_dest(self, mock_ping):
        mock_ping.side_effect = PingError("fail")
        contexts = {"src.example": MagicMock(), "dst.example": MagicMock()}
        result = run_probe("src.example", "dst.example", count=1,
                           relay_contexts=contexts)

        assert result.source == "src.example"
        assert result.destination == "dst.example"


class TestRunProbeWithContexts:
    @patch("chatmail_prober.prober._perform_direct_ping")
    def test_with_contexts(self, mock_ping):
        """With relay_contexts, uses _perform_direct_ping."""
        mock_ping.return_value = FakePinger()
        contexts = {"a.test": MagicMock(), "b.test": MagicMock()}
        result = run_probe("a.test", "b.test", count=3, relay_contexts=contexts)

        assert result.sent == 3
        assert result.received == 3
        mock_ping.assert_called_once()
        call_args = mock_ping.call_args
        assert call_args[0][0] is contexts

    @patch("chatmail_prober.prober._perform_direct_ping")
    def test_error_with_contexts(self, mock_ping):
        mock_ping.side_effect = PingError("rpc failed")
        contexts = {"a.test": MagicMock()}
        result = run_probe("a.test", "b.test", count=1, relay_contexts=contexts)

        assert result.error == "rpc failed"

    @patch("chatmail_prober.prober.RelayContext")
    @patch("chatmail_prober.prober._perform_direct_ping")
    def test_without_contexts_creates_temporary(self, mock_ping, MockCtx):
        """When relay_contexts is None, creates temporary RelayContexts."""
        mock_ping.return_value = FakePinger()
        result = run_probe("a.test", "b.test", count=3, accounts_dir="/tmp/c")

        mock_ping.assert_called_once()
        assert result.sent == 3


class TestRelayPool:
    @patch("chatmail_prober.prober.RelayContext")
    def test_open_all_creates_contexts(self, MockCtx, tmp_path):
        pool = RelayPool(tmp_path)
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
                                        tmp_path / "relay.example")

    @patch("chatmail_prober.prober.RelayContext")
    def test_reopen_replaces_context(self, MockCtx, tmp_path):
        ctx1 = MagicMock()
        ctx2 = MagicMock()
        MockCtx.side_effect = [ctx1, ctx2]

        pool = RelayPool(tmp_path)
        pool.open_all(["a.test"])
        assert pool.contexts()["a.test"] is ctx1

        pool.reopen("a.test")
        assert pool.contexts()["a.test"] is ctx2
        ctx1.close.assert_called_once()

    @patch("chatmail_prober.prober.RelayContext")
    def test_reopen_survives_close_error(self, MockCtx, tmp_path):
        """reopen works even if closing the old context raises."""
        old_ctx = MagicMock()
        old_ctx.close.side_effect = RuntimeError("dead")
        new_ctx = MagicMock()
        MockCtx.side_effect = [old_ctx, new_ctx]

        pool = RelayPool(tmp_path)
        pool.open_all(["a.test"])
        pool.reopen("a.test")  # should not raise
        assert pool.contexts()["a.test"] is new_ctx
