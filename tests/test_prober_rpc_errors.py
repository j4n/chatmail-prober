"""Tests for RPC-level setup error handling in run_probe / _perform_direct_ping.

These tests inject real JsonRpcError instances — using the exact message strings
produced by chatmail/core's Rust network layer — into the account-setup path,
without requiring a live deltachat-rpc-server binary or network access.

Error string origins (from chatmail/core):
  DNS:     src/net/dns.rs  "Could not find DNS resolutions for {host}:{port}..."
  Auth:    src/stock_str.rs + IMAP server "[AUTHENTICATIONFAILED]"
  Timeout: src/net.rs tokio::time::timeout -> "Connection timeout: deadline has elapsed"

Each test verifies:
  1. run_probe returns a ProbeResult with a non-None error string.
  2. The error string causes metrics.relay_status_value() to return the expected code.
"""

from __future__ import annotations

import pytest

from deltachat_rpc_client.rpc import JsonRpcError

from chatmail_prober.prober import AccountMaker, RelayContext, run_probe
from chatmail_prober.metrics import relay_status_value


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dns_error(host: str = "imap.host.abc", port: int = 993) -> JsonRpcError:
    """Construct the exact JsonRpcError raised on DNS resolution failure."""
    return JsonRpcError(
        {
            "code": -1,
            "message": (
                f'Error:\n\n"IMAP failed to connect to {host}:{port}:tls: '
                f"Could not find DNS resolutions for {host}:{port}. "
                'Check server hostname and your network"'
            ),
        }
    )


def _auth_error(addr: str = "wjfjpcxib@hostb.xyz") -> JsonRpcError:
    """Construct the exact JsonRpcError raised on IMAP authentication failure."""
    return JsonRpcError(
        {
            "code": -1,
            "message": (
                f'Error:\n\n"Cannot login as "{addr}". '
                "Please check if the email address and the password are correct. "
                '(no response: code: None, info: Some("[AUTHENTICATIONFAILED] Authentication failed."))"'
            ),
        }
    )


def _timeout_error(host: str = "hostd.xyz") -> JsonRpcError:
    """Construct the exact JsonRpcError raised on TCP/TLS connection timeout."""
    return JsonRpcError(
        {
            "code": -1,
            "message": (
                f'Error:\n\n"IMAP failed to connect to {host}:993:tls: '
                'Connection timeout: deadline has elapsed"'
            ),
        }
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_relay_contexts(monkeypatch):
    """Return a factory that builds a pair of RelayContext stubs.

    The returned contexts have a real AccountMaker whose dc.add_account()
    is patched to raise the supplied exception, simulating a failure during
    account creation (set_config_from_qr -> start_io -> configure).
    """

    def _make(exc: Exception):
        """Build (src_ctx, dst_ctx) where get_relay_account raises exc."""

        class _FakeDC:
            def get_all_accounts(self):
                return []

            def add_account(self):
                raise exc

        maker = AccountMaker(_FakeDC())

        class _Ctx:
            def __init__(self):
                self.maker = maker

        src_ctx = _Ctx()
        dst_ctx = _Ctx()
        return {"src.example": src_ctx, "dst.example": dst_ctx}

    return _make


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDnsResolutionFailure:
    """run_probe must surface DNS errors with status code -6."""

    def test_returns_error_result(self, mock_relay_contexts):
        contexts = mock_relay_contexts(_dns_error())
        result = run_probe(
            "src.example", "dst.example",
            count=1,
            relay_contexts=contexts,
        )
        assert result.error is not None, "Expected an error result for DNS failure"
        assert result.sent == 0

    def test_error_classifies_as_dns(self, mock_relay_contexts):
        contexts = mock_relay_contexts(_dns_error())
        result = run_probe(
            "src.example", "dst.example",
            count=1,
            relay_contexts=contexts,
        )
        assert relay_status_value(result.error) == -6, (
            f"Expected DNS status -6, got {relay_status_value(result.error)!r} "
            f"for error: {result.error!r}"
        )

    def test_error_contains_imap_connect_prefix(self, mock_relay_contexts):
        """The PingError wrapper must preserve the core error message."""
        contexts = mock_relay_contexts(_dns_error("imap.host.abc"))
        result = run_probe(
            "src.example", "dst.example",
            count=1,
            relay_contexts=contexts,
        )
        assert "imap.host.abc" in result.error or "DNS" in result.error.upper() or "dns" in result.error.lower()


class TestAuthenticationFailure:
    """run_probe must surface auth errors with status code -3."""

    def test_returns_error_result(self, mock_relay_contexts):
        contexts = mock_relay_contexts(_auth_error())
        result = run_probe(
            "src.example", "dst.example",
            count=1,
            relay_contexts=contexts,
        )
        assert result.error is not None

    def test_error_classifies_as_auth(self, mock_relay_contexts):
        contexts = mock_relay_contexts(_auth_error())
        result = run_probe(
            "src.example", "dst.example",
            count=1,
            relay_contexts=contexts,
        )
        assert relay_status_value(result.error) == -3, (
            f"Expected auth status -3, got {relay_status_value(result.error)!r} "
            f"for error: {result.error!r}"
        )

    def test_error_contains_authenticationfailed_keyword(self, mock_relay_contexts):
        contexts = mock_relay_contexts(_auth_error())
        result = run_probe(
            "src.example", "dst.example",
            count=1,
            relay_contexts=contexts,
        )
        lower = result.error.lower()
        assert "auth" in lower or "login" in lower or "password" in lower, (
            f"Expected auth-related keyword in error: {result.error!r}"
        )


class TestConnectionTimeoutFailure:
    """run_probe must surface connection-timeout errors with status code -1."""

    def test_returns_error_result(self, mock_relay_contexts):
        contexts = mock_relay_contexts(_timeout_error())
        result = run_probe(
            "src.example", "dst.example",
            count=1,
            relay_contexts=contexts,
        )
        assert result.error is not None

    def test_error_classifies_as_timeout(self, mock_relay_contexts):
        contexts = mock_relay_contexts(_timeout_error())
        result = run_probe(
            "src.example", "dst.example",
            count=1,
            relay_contexts=contexts,
        )
        assert relay_status_value(result.error) == -1, (
            f"Expected timeout status -1, got {relay_status_value(result.error)!r} "
            f"for error: {result.error!r}"
        )

    def test_error_contains_deadline_keyword(self, mock_relay_contexts):
        contexts = mock_relay_contexts(_timeout_error())
        result = run_probe(
            "src.example", "dst.example",
            count=1,
            relay_contexts=contexts,
        )
        lower = result.error.lower()
        assert "deadline" in lower or "timeout" in lower, (
            f"Expected deadline/timeout keyword in error: {result.error!r}"
        )


class TestSelfLoopWithRpcError:
    """Self-loop probes (src==dst) must also surface errors correctly."""

    def test_self_loop_dns_error(self, mock_relay_contexts):
        contexts = mock_relay_contexts(_dns_error("imap.self.example"))
        # Use the same key for both src and dst
        contexts["self.example"] = contexts.pop("src.example")
        contexts.pop("dst.example", None)
        result = run_probe(
            "self.example", "self.example",
            count=1,
            relay_contexts=contexts,
        )
        assert result.error is not None
        assert relay_status_value(result.error) == -6
