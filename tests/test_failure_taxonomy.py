"""Tests for ProbeResult.failure_category field (populated by _classify_error)
and for the orchestration._RPC_CRASH_KEYWORDS taxonomy used to gate
pool.reopen() in run_round / check_relays_alive.
"""
from __future__ import annotations

import pytest

from chatmail_prober.orchestration import _RPC_CRASH_KEYWORDS
from chatmail_prober.probe import ProbeResult


@pytest.mark.parametrize(("error", "category"), [
    (None, None),
    ("IMAP failed to connect: Could not find DNS resolutions for imap.a.example:993", "dns"),
    ("Cannot login as user@a.example: authentication failed", "auth"),
    ("Connection timeout: deadline has elapsed", "timeout"),
    ("SSL certificate verify failed", "tls"),
    ("Connection refused to imap.a.example:993", "connection_refused"),
    ("Something completely unexpected happened", "unknown"),
])
def test_failure_category(error, category):
    r = ProbeResult(source="a.example", destination="b.example", error=error)
    assert r.failure_category == category


# Application-level errors (DNS, auth, timeout) must NOT match the RPC-crash
# keywords -- otherwise run_round would tear down healthy pools on every
# transient host failure.
@pytest.mark.parametrize("error", [
    "Failed to setup sender profile on host.abc: JsonRpcError: "
    "{'code': -1, 'message': 'Could not find DNS resolutions'}",
    "AUTHENTICATIONFAILED: login failed",
    "Connection timeout: deadline has elapsed",
    "Failed to setup sender profile on relay.example: SomeError: details",
])
def test_app_error_not_rpc_crash(error):
    assert not any(kw in error.lower() for kw in _RPC_CRASH_KEYWORDS)


# Transport-level errors (RPC server gone, broken pipe, connection reset) MUST
# match so the pool is reopened.
@pytest.mark.parametrize("error", [
    "RPC server closed",
    "rpc process crashed",
    "BrokenPipeError writing to rpc stdin",
    "ConnectionResetError: [Errno 104] Connection reset by peer",
    "EOFError reading from rpc server",
])
def test_transport_error_is_rpc_crash(error):
    assert any(kw in error.lower() for kw in _RPC_CRASH_KEYWORDS)
