"""Tests for ProbeResult.failure_category field (populated by _classify_error)."""
from __future__ import annotations

from chatmail_prober.prober import ProbeResult


class TestProbeResultFailureCategory:
    def test_no_error_has_none_category(self):
        r = ProbeResult(source="a.example", destination="b.example",
                        sent=3, received=3, loss=0.0, rtts_ms=[100.0])
        assert r.failure_category is None

    def test_dns_error_classified(self):
        r = ProbeResult(
            source="a.example", destination="b.example",
            error="IMAP failed to connect: Could not find DNS resolutions for imap.a.example:993",
        )
        assert r.failure_category == "dns"

    def test_auth_error_classified(self):
        r = ProbeResult(
            source="a.example", destination="b.example",
            error="Cannot login as user@a.example: authentication failed",
        )
        assert r.failure_category == "auth"

    def test_timeout_error_classified(self):
        r = ProbeResult(
            source="a.example", destination="b.example",
            error="Connection timeout: deadline has elapsed",
        )
        assert r.failure_category == "timeout"

    def test_tls_error_classified(self):
        r = ProbeResult(
            source="a.example", destination="b.example",
            error="SSL certificate verify failed",
        )
        assert r.failure_category == "tls"

    def test_connection_refused_classified(self):
        r = ProbeResult(
            source="a.example", destination="b.example",
            error="Connection refused to imap.a.example:993",
        )
        assert r.failure_category == "connection_refused"

    def test_unknown_error_classified(self):
        r = ProbeResult(
            source="a.example", destination="b.example",
            error="Something completely unexpected happened",
        )
        assert r.failure_category == "unknown"
