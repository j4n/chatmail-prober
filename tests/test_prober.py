"""Tests for the prober (vendored direct-ping logic)."""

from unittest.mock import patch, MagicMock

import pytest

import urllib.parse

from chatmail_prober.prober import (
    run_probe, ProbeResult, RelayPool, PingError,
    _classify_error, _FATAL_CATEGORIES,
    create_qr_url, is_ip_address,
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
    @patch("chatmail_prober.prober.Rpc")
    def test_open_all_starts_shared_rpc(self, MockRpc, tmp_path):
        pool = RelayPool(tmp_path)
        pool.open_all(["a.test", "b.test"])

        MockRpc.assert_called_once_with(accounts_dir=tmp_path)
        contexts = pool.contexts()
        assert set(contexts.keys()) == {"a.test", "b.test"}
        # All relays share the same pool as context
        assert all(ctx is pool for ctx in contexts.values())

    @patch("chatmail_prober.prober.Rpc")
    def test_open_all_deduplicates(self, MockRpc, tmp_path):
        pool = RelayPool(tmp_path)
        pool.open_all(["a.test", "b.test"])
        pool.open_all(["a.test", "c.test"])

        # Only one Rpc started, relays are just tracked
        MockRpc.assert_called_once()
        assert set(pool.contexts().keys()) == {"a.test", "b.test", "c.test"}

    @patch("chatmail_prober.prober.Rpc")
    def test_close_clears_pool(self, MockRpc, tmp_path):
        pool = RelayPool(tmp_path)
        pool.open_all(["a.test"])
        pool.close()

        assert pool.contexts() == {}
        assert pool.rpc is None
        assert pool.maker is None

    @patch("chatmail_prober.prober.Rpc")
    def test_context_manager(self, MockRpc, tmp_path):
        with RelayPool(tmp_path) as pool:
            pool.open_all(["a.test"])
            assert pool.maker is not None
        assert pool.rpc is None

    @patch("chatmail_prober.prober.Rpc")
    def test_shared_accounts_dir(self, MockRpc, tmp_path):
        """All relays share one accounts_dir (the pool's cache_dir)."""
        pool = RelayPool(tmp_path)
        pool.open_all(["relay.example"])

        MockRpc.assert_called_once_with(accounts_dir=tmp_path)

    @patch("chatmail_prober.prober.Rpc")
    def test_reopen_restarts_rpc(self, MockRpc, tmp_path):
        pool = RelayPool(tmp_path)
        pool.open_all(["a.test"])
        assert MockRpc.call_count == 1

        pool.reopen()
        assert MockRpc.call_count == 2
        # Pool still serves all relays after reopen
        assert "a.test" in pool.contexts()

    @patch("chatmail_prober.prober.Rpc")
    def test_prune_forgets_relays(self, MockRpc, tmp_path):
        pool = RelayPool(tmp_path)
        pool.open_all(["a.test", "b.test", "c.test"])
        pool.prune(["a.test", "c.test"])

        assert set(pool.contexts().keys()) == {"a.test", "c.test"}

    @patch("chatmail_prober.prober.Rpc")
    def test_pool_has_maker_attribute(self, MockRpc, tmp_path):
        """Pool duck-types as relay context for _perform_direct_ping."""
        pool = RelayPool(tmp_path)
        pool.open_all(["a.test"])

        assert pool.maker is not None
        ctx = pool.contexts()["a.test"]
        assert ctx.maker is pool.maker


class TestFatalCategories:
    """Verify that _FATAL_CATEGORIES correctly separates fatal from transient errors."""

    @pytest.mark.parametrize("error", [
        "Could not find DNS resolutions for imap.chat.beeep.ir:993",
        "Name or service not known",
        "Connection refused",
        "ConnectionRefusedError: [Errno 111]",
        "certificate has expired",
        "SSL handshake failed",
        "[AUTHENTICATIONFAILED] Authentication failed.",
    ])
    def test_fatal_errors(self, error):
        assert _classify_error(error) in _FATAL_CATEGORIES

    @pytest.mark.parametrize("error", [
        "Connection timed out",
        "something went wrong",
        "temporary failure in name resolution",
    ])
    def test_non_fatal_errors(self, error):
        assert _classify_error(error) not in _FATAL_CATEGORIES


# -- Tests merged from test_ip_relay.py --


class TestIsIpAddress:
    def test_ipv4_detected(self):
        assert is_ip_address("192.168.1.1") is True

    def test_ipv6_detected(self):
        assert is_ip_address("::1") is True
        assert is_ip_address("2001:db8::1") is True

    def test_domain_not_ip(self):
        assert is_ip_address("nine.testrun.org") is False

    def test_empty_string_not_ip(self):
        assert is_ip_address("") is False


class TestCreateQrUrl:
    def test_domain_produces_dcaccount_url(self):
        assert create_qr_url("nine.testrun.org") == "dcaccount:nine.testrun.org"

    def test_ip_produces_dclogin_url(self):
        url = create_qr_url("192.168.1.1")
        assert url.startswith("dclogin:")
        assert "192.168.1.1" in url

    def test_dclogin_url_has_required_params(self):
        url = create_qr_url("192.168.1.1")
        qs = urllib.parse.parse_qs(url.split("?")[1]) if "?" in url else {}
        assert "p" in qs
        assert "ip" in qs
        assert "sp" in qs
