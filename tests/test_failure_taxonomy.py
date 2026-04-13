"""RED tests for failure taxonomy feature.

ProbeResult must carry a failure_category field populated from the error
string, and run_probe must emit it as a structured field on probe_failed
log events.  The cli_summary renderer will use it to group failures.
"""
from __future__ import annotations

import json
import io
import sys
from unittest.mock import patch

import pytest

from chatmail_prober.log_config import configure_logging
from chatmail_prober.prober import ProbeResult, run_probe, PingError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _capture_events(fn) -> list[dict]:
    """Run fn() with JSON logging; return parsed log event dicts."""
    configure_logging(tty=False, level="DEBUG")
    buf = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = buf
    try:
        fn()
    finally:
        sys.stderr = old_stderr
        configure_logging(tty=False, level="DEBUG")
    events = []
    for line in buf.getvalue().splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


# ---------------------------------------------------------------------------
# ProbeResult.failure_category field
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# run_probe emits failure_category on probe_failed log event
# ---------------------------------------------------------------------------

class TestRunProbeFailureCategoryLog:
    def test_probe_failed_log_carries_category(self):
        """run_probe must emit a probe_failed event with failure_category field."""
        dns_error = (
            "IMAP failed to connect: Could not find DNS resolutions for "
            "imap.nonexistent.invalid:993"
        )

        def _raise(*args, **kwargs):
            raise PingError(dns_error)

        with patch("chatmail_prober.prober._perform_direct_ping", side_effect=_raise):
            events = _capture_events(
                lambda: run_probe("nonexistent.invalid", "nonexistent.invalid",
                                  relay_contexts={"nonexistent.invalid": object()})
            )

        failed = [e for e in events if e.get("event") == "probe_failed"]
        assert failed, f"probe_failed event missing; got events: {[e.get('event') for e in events]}"
        assert failed[0].get("failure_category") == "dns", (
            f"Expected failure_category=dns, got: {failed[0]}"
        )

    def test_probe_failed_log_carries_src_dst(self):
        """probe_failed event must carry src and dst fields."""
        with patch("chatmail_prober.prober._perform_direct_ping",
                   side_effect=PingError("timeout: deadline has elapsed")):
            events = _capture_events(
                lambda: run_probe("src.example", "dst.example",
                                  relay_contexts={"src.example": object(),
                                                  "dst.example": object()})
            )

        failed = [e for e in events if e.get("event") == "probe_failed"]
        assert failed
        ev = failed[0]
        assert ev.get("src") == "src.example"
        assert ev.get("dst") == "dst.example"
