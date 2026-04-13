"""Tests for the cli_summary module.

cli_summary.render(results, alive_relays, dead_relays, elapsed_s)
must produce a compact table that includes:
  - One row per probe pair with Sent/Recv/Loss/p50/p90/p99/mdev/Setup/Msg
  - Failure block below the table (grouped by failure_category)
  - Overall summary footer (alive/dead/probes/elapsed)
"""
from __future__ import annotations

import io

import pytest

from chatmail_prober.prober import ProbeResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ok_result(src: str = "a.example", dst: str = "b.example",
                    rtts: list[float] | None = None,
                    setup_time: float = 3.5,
                    message_time: float = 2.1) -> ProbeResult:
    return ProbeResult(
        source=src, destination=dst,
        sent=5, received=5, loss=0.0,
        rtts_ms=rtts or [1000.0, 1200.0, 900.0, 1100.0, 1050.0],
        account_setup_time=setup_time,
        message_time=message_time,
    )


def _make_failed_result(src: str = "a.example", dst: str = "b.example",
                        error: str = "Connection timeout: deadline has elapsed") -> ProbeResult:
    return ProbeResult(source=src, destination=dst, error=error)


def _render(*args, **kwargs) -> str:
    from chatmail_prober.cli_summary import render
    buf = io.StringIO()
    render(*args, out=buf, **kwargs)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Module existence
# ---------------------------------------------------------------------------

class TestModuleExists:
    def test_import(self):
        from chatmail_prober import cli_summary  # noqa: F401

    def test_render_callable(self):
        from chatmail_prober.cli_summary import render
        assert callable(render)


# ---------------------------------------------------------------------------
# Packet statistics section
# ---------------------------------------------------------------------------

class TestPacketStatistics:
    def test_route_column_header_present(self):
        out = _render([_make_ok_result()], [], [], elapsed_s=10.0)
        assert "Route" in out

    def test_route_line_present(self):
        out = _render([_make_ok_result("nine.testrun.org", "mailchat.pl")], [], [], elapsed_s=10.0)
        assert "nine.testrun.org" in out
        assert "mailchat.pl" in out

    def test_sent_recv_loss_columns_present(self):
        out = _render([_make_ok_result()], [], [], elapsed_s=10.0)
        assert "Sent" in out
        assert "Recv" in out
        assert "Loss" in out
        assert "0.0%" in out

    def test_partial_loss_shown(self):
        r = ProbeResult(source="a.example", destination="b.example",
                        sent=5, received=3, loss=40.0,
                        rtts_ms=[1000.0, 1100.0, 1200.0])
        out = _render([r], [], [], elapsed_s=10.0)
        assert "40.0%" in out


# ---------------------------------------------------------------------------
# RTT statistics section
# ---------------------------------------------------------------------------

class TestRttStatistics:
    def test_rtt_column_headers_present(self):
        out = _render([_make_ok_result()], [], [], elapsed_s=10.0)
        assert "p50" in out
        assert "p90" in out
        assert "p99" in out
        assert "mdev" in out

    def test_p50_value_shown(self):
        # p50 of [500, 600, 700] = 600
        out = _render([_make_ok_result(rtts=[500.0, 600.0, 700.0])], [], [], elapsed_s=10.0)
        assert "600" in out

    def test_mdev_shown(self):
        out = _render([_make_ok_result()], [], [], elapsed_s=10.0)
        assert "mdev" in out.lower()

    def test_failed_probe_shows_dash_for_rtt(self):
        out = _render([_make_failed_result()], [], [], elapsed_s=10.0)
        # Failed rows show category in p50 column, dashes for p90/p99/mdev
        assert "-" in out


# ---------------------------------------------------------------------------
# Phase timing section
# ---------------------------------------------------------------------------

class TestPhaseTiming:
    def test_section_header_present(self):
        out = _render([_make_ok_result()], [], [], elapsed_s=10.0)
        assert "Timing" in out or "Phase" in out or "Setup" in out

    def test_account_setup_time_shown(self):
        out = _render([_make_ok_result(setup_time=6.66)], [], [], elapsed_s=10.0)
        assert "6.66" in out or "6.7" in out

    def test_message_time_shown(self):
        out = _render([_make_ok_result(message_time=9.47)], [], [], elapsed_s=10.0)
        assert "9.47" in out or "9.5" in out


# ---------------------------------------------------------------------------
# Failure summary section
# ---------------------------------------------------------------------------

class TestFailureSummary:
    def test_failure_section_present_when_failures_exist(self):
        out = _render([_make_failed_result()], [], ["a.example"], elapsed_s=10.0)
        assert "Failure" in out or "failed" in out.lower() or "dead" in out.lower()

    def test_failure_category_shown(self):
        out = _render(
            [_make_failed_result(error="Connection timeout: deadline has elapsed")],
            [], ["a.example"], elapsed_s=10.0
        )
        assert "timeout" in out.lower()

    def test_dns_failure_category_shown(self):
        out = _render(
            [_make_failed_result(
                error="Could not find DNS resolutions for imap.a.example:993"
            )],
            [], ["a.example"], elapsed_s=10.0
        )
        assert "dns" in out.lower()

    def test_no_failure_section_when_all_ok(self):
        out = _render([_make_ok_result()], ["a.example", "b.example"], [], elapsed_s=10.0)
        # Should not show a failure section when there are no failures
        assert "Failure" not in out or "0 failure" in out.lower()


# ---------------------------------------------------------------------------
# Overall summary line
# ---------------------------------------------------------------------------

class TestOverallSummary:
    def test_elapsed_shown(self):
        out = _render([_make_ok_result()], ["a.example", "b.example"], [], elapsed_s=42.3)
        assert "42" in out

    def test_success_count_shown(self):
        results = [_make_ok_result(), _make_ok_result("b.example", "a.example")]
        out = _render(results, ["a.example", "b.example"], [], elapsed_s=10.0)
        assert "2" in out

    def test_alive_dead_counts_shown(self):
        out = _render(
            [_make_ok_result(), _make_failed_result("c.example", "c.example")],
            ["a.example", "b.example"], ["c.example"],
            elapsed_s=10.0
        )
        assert "2" in out  # alive count
        assert "1" in out  # dead count
