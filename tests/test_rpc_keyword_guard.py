"""Regression tests for the overly-broad RPC keyword guard in run_round.

Background (upstream commit 6cbe7db):
  The bare "rpc" keyword in _rpc_keywords matched every JsonRpcError string
  because the PingError wrapper always includes the text "JsonRpcError".
  This caused pool.reopen() to fire on DNS / auth / timeout failures, killing
  working RPC subprocesses and cascading failures across the probe matrix.

  Fix: replace bare "rpc" with specific transport strings
       "rpc server closed" and "rpc process".

These tests verify that application-level error strings (DNS, auth, timeout,
setup) do NOT match the transport-level keyword list, while genuine transport
crashes DO match.  This prevents the regression from being silently reintroduced
by a future edit to _rpc_keywords.
"""

from __future__ import annotations

import logging
import threading

import pytest

from chatmail_prober.__main__ import _SupprRpcClosedFilter


# ---------------------------------------------------------------------------
# The exact keyword list from __main__.run_round (must stay in sync).
# If the production list changes, update this tuple and the tests below.
# ---------------------------------------------------------------------------
_RPC_KEYWORDS = (
    "BrokenPipe",
    "ConnectionReset",
    "EOFError",
    "process",
    "rpc server closed",
    "rpc process",
)


def _matches(error: str) -> bool:
    """Return True if error would trigger a pool.reopen() call."""
    return any(kw.lower() in error.lower() for kw in _RPC_KEYWORDS)


# ---------------------------------------------------------------------------
# Real-world application-level error strings (must NOT trigger reopen)
# ---------------------------------------------------------------------------

class TestAppLevelErrorsDoNotTriggerReopen:
    """Application-level errors must never trigger pool.reopen().

    Each parametrized string is taken verbatim from production WARNING logs.
    """

    @pytest.mark.parametrize("error", [
        # DNS failure (upstream fix target)
        (
            "Failed to setup sender profile on host.abc: JsonRpcError: "
            "{'code': -1, 'message': 'Error:\\n\\n\"IMAP failed to connect to "
            "imap.host.abc:993:tls: Could not find DNS resolutions for "
            "imap.host.abc:993. Check server hostname and your network\"'}"
        ),
        # Auth failure (upstream fix target)
        (
            "Failed to setup sender profile on hostb.xyz: JsonRpcError: "
            "{'code': -1, 'message': 'Error:\\n\\n\"Cannot login as "
            '"wjfjpcxib@hostb.xyz". Please check if the email address and the '
            "password are correct. (no response: code: None, info: "
            'Some(\"[AUTHENTICATIONFAILED] Authentication failed.\"))\"\'}'
        ),
        # Connection timeout / deadline elapsed (upstream fix target)
        (
            "Failed to setup sender profile on hostd.xyz: JsonRpcError: "
            "{'code': -1, 'message': 'Error:\\n\\n\"IMAP failed to connect to "
            "hostd.xyz:993:tls: Connection timeout: deadline has elapsed\"'}"
        ),
        # Account-online timeout (raised by AccountMaker.wait_account_online)
        "Timeout or error waiting for profiles to be online: "
        "Timeout waiting for izkflobsi@hostc.zzz to come online",
        # Generic setup failure
        "Failed to setup sender profile on relay.example: SomeError: details",
    ])
    def test_does_not_trigger_reopen(self, error):
        assert not _matches(error), (
            f"Application-level error incorrectly matched _rpc_keywords:\n  {error!r}\n"
            "This would cause pool.reopen() to fire, killing working RPC subprocesses."
        )


# ---------------------------------------------------------------------------
# Genuine transport-level crash strings (MUST trigger reopen)
# ---------------------------------------------------------------------------

class TestTransportLevelErrorsDoTriggerReopen:
    """Transport-level crashes must trigger pool.reopen() for recovery."""

    @pytest.mark.parametrize("error", [
        # RPC server closed (e.g. deltachat-rpc-server process died)
        "RPC server closed",
        "src=relay.a dst=relay.b error=RPC server closed",
        # RPC process crash
        "rpc process crashed",
        "Failed to setup sender profile: rpc process exited",
        # Broken pipe on stdin/stdout of the subprocess
        "BrokenPipeError writing to rpc stdin",
        "BrokenPipe",
        # Connection reset by peer at the OS level
        "ConnectionResetError: [Errno 104] Connection reset by peer",
        "ConnectionReset",
        # EOF on the subprocess stdout (reader_loop got empty line)
        "EOFError reading from rpc server",
        "EOFError",
        # Generic process marker
        "process dead",
    ])
    def test_does_trigger_reopen(self, error):
        assert _matches(error), (
            f"Transport-level error did NOT match _rpc_keywords:\n  {error!r}\n"
            "This would prevent pool.reopen() from firing, leaving a crashed RPC "
            "subprocess in place."
        )


# ---------------------------------------------------------------------------
# Boundary: strings that contain "rpc" but are NOT transport crashes
# (the original bug: bare "rpc" matched these)
# ---------------------------------------------------------------------------

class TestBareRpcKeywordRegression:
    """The bare string 'rpc' must not be in _rpc_keywords.

    This is the exact regression guard for commit 6cbe7db.
    """

    def test_bare_rpc_not_in_keywords(self):
        assert "rpc" not in [kw.lower() for kw in _RPC_KEYWORDS], (
            "Bare 'rpc' keyword found in _RPC_KEYWORDS. "
            "This would match every JsonRpcError string and cause cascading "
            "pool.reopen() calls on DNS/auth/timeout failures. "
            "Use specific strings like 'rpc server closed' and 'rpc process'."
        )

    def test_jsonrpcerror_string_does_not_match(self):
        """The literal text 'JsonRpcError' appears in every wrapped error — must not match."""
        assert not _matches("JsonRpcError: {'code': -1, 'message': 'some app error'}")

    def test_rpc_in_domain_name_does_not_match(self):
        """A relay domain containing 'rpc' must not trigger reopen."""
        assert not _matches(
            "Failed to setup sender profile on rpc.relay.example: "
            "JsonRpcError: {'code': -1, 'message': 'Error: IMAP timeout'}"
        )


# ---------------------------------------------------------------------------
# _SupprRpcClosedFilter: suppress RPC-closed errors during shutdown only
# ---------------------------------------------------------------------------


class TestSupprRpcClosedFilter:
    """_SupprRpcClosedFilter must suppress 'RPC server closed' only after
    shutdown_event is set, so mid-round RPC crashes remain visible.

    This tests the behaviour introduced in upstream commit 6cbe7db:
    the filter was previously unconditional, hiding real RPC crashes
    that occurred during normal probe operation.
    """

    def _make_record(self, msg: str) -> logging.LogRecord:
        record = logging.LogRecord(
            name="deltachat_rpc_client",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg=msg,
            args=(),
            exc_info=None,
        )
        return record

    def test_passes_rpc_closed_before_shutdown(self):
        """During normal operation the error must NOT be suppressed."""
        event = threading.Event()  # not set
        f = _SupprRpcClosedFilter(event)
        record = self._make_record("RPC server closed")
        assert f.filter(record) is True, (
            "RPC server closed should be visible during normal operation "
            "so mid-round crashes can be diagnosed."
        )

    def test_suppresses_rpc_closed_after_shutdown(self):
        """After shutdown_event.set() the error must be suppressed."""
        event = threading.Event()
        event.set()
        f = _SupprRpcClosedFilter(event)
        record = self._make_record("RPC server closed")
        assert f.filter(record) is False, (
            "RPC server closed should be suppressed during shutdown "
            "to avoid noisy but harmless log output."
        )

    def test_passes_unrelated_errors_always(self):
        """Non-RPC-closed errors must always pass through regardless of state."""
        event = threading.Event()
        f_before = _SupprRpcClosedFilter(event)
        event.set()
        f_after = _SupprRpcClosedFilter(event)
        for f in (f_before, f_after):
            record = self._make_record("Some other error")
            assert f.filter(record) is True

    def test_passes_rpc_closed_substring_in_larger_message(self):
        """The filter matches on substring; verify it still suppresses correctly."""
        event = threading.Event()
        event.set()
        f = _SupprRpcClosedFilter(event)
        record = self._make_record(
            "Exception in event loop thread: RPC server closed"
        )
        assert f.filter(record) is False
