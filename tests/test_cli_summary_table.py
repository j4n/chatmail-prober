"""Tests for the table-format cli_summary renderer.

The render() function must produce a compact table where each probe pair
occupies one row:

  Route                                    Sent  Recv  Loss    p50    p90    p99   mdev  Setup   Msg
  nine.testrun.org -> nine.testrun.org        3     3   0.0%  2271   2316   2326    431   4.8s   3.2s
  ...

Failures are shown below the table grouped by category.
A one-line summary footer closes the output.
"""
from __future__ import annotations

import io

import pytest

from chatmail_prober.prober import ProbeResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(src: str = "a.example", dst: str = "b.example",
        rtts: list[float] | None = None,
        setup: float = 3.5, msg: float = 2.1) -> ProbeResult:
    return ProbeResult(
        source=src, destination=dst,
        sent=3, received=3, loss=0.0,
        rtts_ms=rtts or [1000.0, 1200.0, 900.0],
        account_setup_time=setup,
        message_time=msg,
    )


def _fail(src: str = "a.example", dst: str = "b.example",
          error: str = "Connection timeout: deadline has elapsed") -> ProbeResult:
    return ProbeResult(source=src, destination=dst, error=error)


def _render(*args, **kwargs) -> str:
    from chatmail_prober.cli_summary import render
    buf = io.StringIO()
    render(*args, out=buf, **kwargs)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Table header
# ---------------------------------------------------------------------------

class TestTableHeader:
    def test_header_contains_route_column(self):
        out = _render([_ok()], ["a.example", "b.example"], [], elapsed_s=5.0)
        assert "Route" in out

    def test_header_contains_sent_recv(self):
        out = _render([_ok()], ["a.example", "b.example"], [], elapsed_s=5.0)
        assert "Sent" in out
        assert "Recv" in out

    def test_header_contains_loss(self):
        out = _render([_ok()], ["a.example", "b.example"], [], elapsed_s=5.0)
        assert "Loss" in out

    def test_header_contains_rtt_columns(self):
        out = _render([_ok()], ["a.example", "b.example"], [], elapsed_s=5.0)
        assert "p50" in out
        assert "p90" in out
        assert "p99" in out
        assert "mdev" in out

    def test_header_contains_timing_columns(self):
        out = _render([_ok()], ["a.example", "b.example"], [], elapsed_s=5.0)
        assert "Setup" in out
        assert "Msg" in out


# ---------------------------------------------------------------------------
# Table rows — successful probes
# ---------------------------------------------------------------------------

class TestTableRows:
    def test_source_and_dest_in_row(self):
        out = _render([_ok("nine.testrun.org", "mailchat.pl")],
                      ["nine.testrun.org", "mailchat.pl"], [], elapsed_s=5.0)
        assert "nine.testrun.org" in out
        assert "mailchat.pl" in out

    def test_arrow_separator_in_route(self):
        out = _render([_ok()], ["a.example", "b.example"], [], elapsed_s=5.0)
        assert "->" in out

    def test_loss_zero_shown(self):
        out = _render([_ok()], ["a.example", "b.example"], [], elapsed_s=5.0)
        assert "0.0%" in out

    def test_partial_loss_shown(self):
        r = ProbeResult(source="a.example", destination="b.example",
                        sent=5, received=3, loss=40.0,
                        rtts_ms=[1000.0, 1100.0, 1200.0])
        out = _render([r], ["a.example", "b.example"], [], elapsed_s=5.0)
        assert "40.0%" in out

    def test_rtt_values_present(self):
        out = _render([_ok(rtts=[500.0, 600.0, 700.0])],
                      ["a.example", "b.example"], [], elapsed_s=5.0)
        # p50 of [500, 600, 700] = 600
        assert "600" in out

    def test_setup_time_present(self):
        out = _render([_ok(setup=6.66)], ["a.example", "b.example"], [], elapsed_s=5.0)
        assert "6.7" in out or "6.66" in out

    def test_msg_time_present(self):
        out = _render([_ok(msg=9.47)], ["a.example", "b.example"], [], elapsed_s=5.0)
        assert "9.5" in out or "9.47" in out

    def test_multiple_rows_all_present(self):
        results = [
            _ok("a.example", "b.example"),
            _ok("b.example", "a.example"),
        ]
        out = _render(results, ["a.example", "b.example"], [], elapsed_s=5.0)
        assert out.count("->") >= 2


# ---------------------------------------------------------------------------
# Table rows — failed probes
# ---------------------------------------------------------------------------

class TestFailedRows:
    def test_failed_row_shows_error_marker(self):
        out = _render([_fail()], [], ["a.example"], elapsed_s=5.0)
        # Failed rows should show some error indicator (category or FAIL/ERR)
        assert "timeout" in out.lower() or "FAIL" in out or "error" in out.lower()

    def test_failed_row_shows_route(self):
        out = _render([_fail("x.example", "y.example")], [], [], elapsed_s=5.0)
        assert "x.example" in out
        assert "y.example" in out

    def test_dns_failure_shown(self):
        out = _render(
            [_fail(error="Name or service not known: imap.a.example")],
            [], ["a.example"], elapsed_s=5.0,
        )
        assert "dns" in out.lower()

    def test_mixed_ok_and_failed(self):
        out = _render(
            [_ok("a.example", "b.example"),
             _fail("b.example", "a.example")],
            ["a.example", "b.example"], [], elapsed_s=5.0,
        )
        assert "a.example" in out
        assert "b.example" in out
        assert "0.0%" in out  # ok row
        assert "timeout" in out.lower()  # failed row


# ---------------------------------------------------------------------------
# Summary footer
# ---------------------------------------------------------------------------

class TestSummaryFooter:
    def test_elapsed_in_footer(self):
        out = _render([_ok()], ["a.example", "b.example"], [], elapsed_s=42.3)
        assert "42" in out

    def test_probes_ok_count_in_footer(self):
        results = [_ok(), _ok("b.example", "a.example")]
        out = _render(results, ["a.example", "b.example"], [], elapsed_s=5.0)
        assert "2/2" in out

    def test_alive_dead_in_footer(self):
        out = _render(
            [_ok(), _fail("c.example", "c.example")],
            ["a.example", "b.example"], ["c.example"],
            elapsed_s=5.0,
        )
        assert "2" in out   # alive
        assert "1" in out   # dead

    def test_footer_on_last_line(self):
        out = _render([_ok()], ["a.example", "b.example"], [], elapsed_s=5.0)
        last_line = out.rstrip("\n").split("\n")[-1]
        # Footer must contain elapsed
        assert "5.0s" in last_line or "elapsed" in last_line.lower()
